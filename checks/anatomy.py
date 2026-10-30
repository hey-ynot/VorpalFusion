"""Where a live job's time goes, per family, warm and cold: the history differential on a live endpoint.

    uv run python checks/anatomy.py --api <v2 API> --comfy <ComfyUI> --saddle <Saddle> [--log <gpu host>:<ComfyUI log>]

For each template, through the sidecar's own submit and owned stream (labelled `anatomy`), on fresh seeds so
ComfyUI's cache never voids a timing: the template's 1-step warm-up (cold: it pays the load), then its live job
with new words (A), the identical graph again (all cached: the transport alone), the seed changed (B), the seed
changed with one more step (C) and the words changed (D), `--repeat` times each. ComfyUI's history gives each job's exec time
and cached nodes; the stream gives each step's arrival and the node boundaries (`progress_state`); the proxy gives
its `created_at`, so the queue wait is read on the endpoint's own clock; the endpoint's log (`--verbose`) gives each
job's loads and how much of each model was resident (dynamic VRAM's `Model loaded: ... ram_mb vram_mb`). Parts:

    encoder  = D - B            (the text encoder re-runs only when the words change)
    step     = C - B            (one sampler step at live size)
    fixed    = B - steps * step (noise, sampler setup, the full decode and the output node)
    transport= the identical job end to end at the sidecar, minus its exec

`--variant partial` builds the live graph without its decode and output (profile `partial`), for the before/after.
Writes `<out>/anatomy.json` and prints one table row per family.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEMPLATES = ["image_z_image_turbo", "image_flux3_klein_distilled_t2i", "image_krea2_turbo_t2i",
             "image_flux2_klein_image_edit_9b_distilled"]
WORDS = ["a woman in a red coat walking through neon rain at night", "a lighthouse on a cliff at dawn, oil painting"]
EDIT_WORDS = ["she wears a yellow raincoat, a snowy mountain behind her", "she wears a green dress in a candle-lit library"]
LOADS = ("Requested to load", "loaded partially", "Unloading", "VRAM grow failed")


def log_size(where: str | None) -> int:
    if not where:
        return 0
    host, path = where.split(":", 1)
    return int(subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"stat -c %s {path}"], capture_output=True, text=True, timeout=30).stdout or 0)


def log_since(where: str | None, offset: int) -> list[str]:
    """The endpoint log's load and residency lines written after `offset` bytes."""
    if not where:
        return []
    host, path = where.split(":", 1)
    command = f"tail -c +{offset + 1} {path} | grep -aE 'Requested to load|loaded partially|Unloading|VRAM grow failed|Model loaded:'"
    lines = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command], capture_output=True, text=True, timeout=60).stdout.splitlines()
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def resident(lines: list[str]) -> dict[str, float]:
    """Per model, the share of its weights in VRAM when the job used it (dynamic VRAM's own count)."""
    found = {}
    for line in lines:
        if m := re.search(r"model=(\S+) ram_mb=([\d.]+) vram_mb=([\d.]+)", line):
            ram, vram = float(m[2]), float(m[3])
            found[m[1]] = round(vram / (ram + vram), 2) if ram + vram else 1.0
    return found


async def job(live, state: dict, http, comfy: str, partial: bool) -> dict:
    """One job: submit, stream it to the end (owned), then read its history and proxy record."""
    from comfy_sdk import OutputReady, Preview, Progress, StatusChange

    started = time.perf_counter()
    j = await live.submit(state, settle=False, lane="anatomy", partial=partial)
    submitted = time.perf_counter()
    steps, nodes, first_preview, output, end = [], {}, None, None, None
    async with live._owned(j) as events:
        async for event in events:
            at = round(1000 * (time.perf_counter() - started))
            if isinstance(event, Progress):
                if event.current_node and event.current_node not in nodes:
                    nodes[event.current_node] = at
                if (event.steps or 0) > 1 and event.step and (not steps or steps[-1][0] != event.step):
                    steps.append((event.step, at))
            elif isinstance(event, Preview) and event.data:
                first_preview = first_preview or at
            elif isinstance(event, OutputReady):
                output = output or at
            elif isinstance(event, StatusChange) and event.status in ("succeeded", "failed", "canceled"):
                end = at
    end = end or round(1000 * (time.perf_counter() - started))
    history = (await http.get(f"{comfy}/history/{j.id}")).json().get(j.id, {})
    messages = {m[0]: m[1] for m in history.get("status", {}).get("messages", [])}
    t0 = messages.get("execution_start", {}).get("timestamp")
    t1 = (messages.get("execution_success") or messages.get("execution_error") or {}).get("timestamp")
    await j.refresh()
    created = getattr(j._model, "created_at", None)
    created_ms = created.timestamp() * 1000 if created else None
    return {"id": j.id, "status": j.status, "exec_ms": (t1 - t0) if t0 and t1 else None,
            "queue_ms": round(t0 - created_ms) if t0 and created_ms else None,
            "cached": len(messages.get("execution_cached", {}).get("nodes", [])), "nodes": len(history.get("prompt", [0, 0, {}])[2]),
            "submit_ms": round(1000 * (submitted - started)), "e2e_ms": end, "first_preview_ms": first_preview,
            "output_ms": output, "steps_at": steps, "node_at": nodes}


async def family(live, name: str, args, http, salt: int, source: str | None) -> dict:
    await live.use(name)
    edit = live.profile.edits
    words = [f"{w}, take {salt}" for w in (EDIT_WORDS if edit else WORDS)]  # ComfyUI's cache outlives a run: words are fresh too
    sources = [{"slot": live.profile.media[0]["address"], "id": source}] if edit and source else []
    base = {"seed": salt, "sources": sources, "aspect": 1.0}
    n = live.profile.live_steps or 4
    partial = args.variant == "partial"
    rows = {}

    async def run(label: str, **state) -> dict:
        offset = log_size(args.log)
        stats = (await http.get(f"{args.comfy}/system_stats")).json()["devices"][0]
        row = await job(live, {**base, **state}, http, args.comfy, partial)
        lines = log_since(args.log, offset)
        row.update(label=label, vram_free_mb=stats["vram_free"] >> 20, resident=resident(lines),
                   loads=[line.split("]", 1)[-1].strip()[:120] for line in lines if any(k in line for k in LOADS)])
        rows.setdefault(label, []).append(row)
        print(f"  {label:5} {row['status']:9} exec {row['exec_ms']!s:>6} ms  e2e {row['e2e_ms']:>6} ms  queue {row['queue_ms']!s:>5}  "
              f"cached {row['cached']:>2}/{row['nodes']:<2} first preview {row['first_preview_ms']!s:>5}  steps at {[t for _, t in row['steps_at']]}  "
              f"free {row['vram_free_mb']} MB  resident {row['resident']}  loads {len(row['loads'])}", flush=True)
        return row

    await run("warm", prompt="warm", live_steps=1, seed=salt)
    await run("A", prompt=words[0], live_steps=n, seed=salt + 1)
    await run("same", prompt=words[0], live_steps=n, seed=salt + 1)
    for k in range(args.repeat):
        await run("B", prompt=words[0], live_steps=n, seed=salt + 10 + k)
        await run("C", prompt=words[0], live_steps=n + 1, seed=salt + 20 + k)
        await run("D", prompt=words[1 - k % 2] + (" " * k), live_steps=n, seed=salt + 30 + k)  # words differ from the job before
    return {"template": name, "live_steps": n, "rows": rows}


def parts(fam: dict) -> dict:
    def med(label: str, key: str = "exec_ms") -> float | None:
        values = [r[key] for r in fam["rows"][label] if r[key] is not None and r["status"] == "succeeded"]
        return statistics.median(values) if values else None

    if any(med(label) is None for label in ("B", "C", "D")):
        return {"template": fam["template"], "failed": sorted({r["status"] for rs in fam["rows"].values() for r in rs})}
    n, b = fam["live_steps"], med("B")
    step = med("C") - b
    deltas = [y - x for r in fam["rows"]["B"] for (_, x), (_, y) in zip(r["steps_at"], r["steps_at"][1:])]
    same = fam["rows"]["same"][0]
    return {"template": fam["template"], "steps": n, "cold_warm_ms": fam["rows"]["warm"][0]["exec_ms"],
            "resident": fam["rows"]["B"][-1]["resident"], "vram_free_mb": fam["rows"]["B"][-1]["vram_free_mb"],
            "cold_loads": len(fam["rows"]["warm"][0]["loads"]), "words_changed_ms": med("D"), "seed_changed_ms": b,
            "encoder_ms": med("D") - b, "step_ms": step, "step_ms_stream": statistics.median(deltas) if deltas else None,
            "fixed_ms": b - n * step, "cached_ms": same["exec_ms"], "transport_ms": same["e2e_ms"] - (same["exec_ms"] or 0),
            "queue_ms": med("B", "queue_ms"), "first_preview_ms": med("B", "first_preview_ms"), "e2e_seed_ms": med("B", "e2e_ms")}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--templates", default=",".join(TEMPLATES))
    parser.add_argument("--variant", default="full", choices=["full", "partial"], help="partial: the live graph without its decode and output")
    parser.add_argument("--log", help="host:path of the endpoint's log (started --verbose DEBUG), read over ssh for its load and residency lines")
    parser.add_argument("--picture", default="portrait.png", help="the reference for an edit template")
    parser.add_argument("--out", default="gv-anatomy")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--salt", type=int, default=int(time.time()) % 1_000_000 * 100)
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    import httpx
    from comfy_sdk import AsyncComfy

    from live import Live

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    live = Live(AsyncComfy(client_info="saddle-gpu-vision-anatomy"), args.comfy, args.saddle)
    source = live.register(Path(args.picture).read_bytes(), "image/png") if Path(args.picture).exists() else None
    async with httpx.AsyncClient(timeout=30) as http:
        rtt = []
        for _ in range(20):
            t = time.perf_counter()
            await http.get(f"{args.comfy}/queue")
            rtt.append(1000 * (time.perf_counter() - t))
        print(f"GET /queue RTT median {statistics.median(rtt):.0f} ms (sidecar to ComfyUI)", flush=True)
        families = []
        for i, name in enumerate(args.templates.split(",")):
            print(f"{name} ({args.variant})", flush=True)
            families.append(await family(live, name, args, http, args.salt + 1000 * i, source))
    table = [parts(f) for f in families]
    (out / f"anatomy-{args.variant}-{datetime.now():%H%M%S}.json").write_text(json.dumps({"rtt_ms": statistics.median(rtt), "families": families, "parts": table}, indent=1))
    for row in table:
        print(json.dumps(row))


if __name__ == "__main__":
    asyncio.run(main())
