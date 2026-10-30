"""What stands between an act and its picture: one live frame's time outside the GPU, the GPU's idle gap between
frames, and the text encoder's cost by length.

    uv run python checks/realtime.py --api <v2 API> --comfy <ComfyUI> --template <template JSON file> [--frames 12]

Through the sidecar's own submit and owned stream (labelled `realtime`), one job at a time, each submitted only when
no other client's job is on the endpoint's queue; a job whose predecessor on the GPU was another client's is marked
`contended` and left out of the medians. Parts, per frame:

    submit  = the submit call (sidecar clock)            queue = proxy created_at -> execution_start (endpoint clock)
    exec    = execution_start -> execution_success        notify = e2e - submit - queue - exec (the end reaching the sidecar)
    gap     = this job's execution_start - the previous job's execution_success (the GPU idle between two frames: what
              a pipelined loop, the next frame already queued, gets back)

Frames: `seed` (the live graph at `--steps`, 1 by default, preview only, a new seed per frame: the cheapest frame
there is); with `--sink` the same again with the output a text preview of the latent's size instead of the latent
(profile `sized`); with `--encoder`, `words` with new words of 4 / 16 / 48 / 120 words (the encoder's cost by length:
words minus the same words on another seed). Writes `<out>/realtime-<time>.json` and prints the medians.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FILLER = ("a quiet harbour town at dusk with fishing boats lanterns wet cobblestones gulls a lighthouse low clouds "
          "a red bicycle against a blue door ivy on the walls a cat asleep on a crate nets drying on posts").split()


def words_of(n: int, salt: int) -> str:
    return " ".join(FILLER[i % len(FILLER)] for i in range(n - 1)) + f" take{salt}"


async def frame(live, state: dict, http, comfy: str, sink: str = "text") -> dict:
    """One live frame; `sink`: its output a text preview of the latent (`text`) or of the latent's size (`size`)."""
    from comfy_sdk import Preview, StatusChange

    live.profile.sized = sink == "size"
    started = time.perf_counter()
    job = await live.submit(state, settle=False, lane="realtime", partial=True)
    submitted = time.perf_counter()
    first = end = None
    async with live._owned(job) as events:
        async for event in events:
            at = 1000 * (time.perf_counter() - started)
            if isinstance(event, Preview) and event.data and first is None:
                first = at
            if isinstance(event, StatusChange) and event.status in ("succeeded", "failed", "canceled"):
                end = at
    end = end or 1000 * (time.perf_counter() - started)
    return {"job": job, "id": job.id, "submit": round(1000 * (submitted - started)), "e2e": round(end),
            "first": round(first) if first else None}


async def run(live, states: list[dict], http, comfy: str, sinks: list[str] | None = None) -> list[dict]:
    """The frames back to back, as a loop that is never short of new input runs them (the queue empty first, so the
    first is not behind anyone), then each one's times from the endpoint."""
    while await live._foreign():
        await asyncio.sleep(0.2)
    rows = [await frame(live, s, http, comfy, (sinks or ["text"] * len(states))[i]) for i, s in enumerate(states)]
    for row in rows:
        job = row.pop("job")
        await job.refresh()
        history = (await http.get(f"{comfy}/history/{job.id}")).json().get(job.id, {})
        messages = {m[0]: m[1] for m in history.get("status", {}).get("messages", [])}
        t0 = messages.get("execution_start", {}).get("timestamp")
        t1 = messages.get("execution_success", {}).get("timestamp")
        created = getattr(job._model, "created_at", None)
        row.update(status=job.status, start=t0, success=t1, exec=(t1 - t0) if t0 and t1 else None,
                   queue=round(t0 - created.timestamp() * 1000) if t0 and created else None)
        if row["exec"] is not None and row["queue"] is not None:
            row["notify"] = round(row["e2e"] - row["submit"] - row["queue"] - row["exec"])
    await gaps(rows, http, comfy)
    return rows


async def gaps(rows: list[dict], http, comfy: str) -> None:
    """Each row's GPU idle gap after the job before it, and whether that job was another client's."""
    history = (await http.get(f"{comfy}/history", params={"max_items": 200})).json()
    ends = sorted((({m[0]: m[1] for m in h["status"]["messages"]}.get("execution_success") or {}).get("timestamp") or 0, pid)
                  for pid, h in history.items())
    ours = {r["id"] for r in rows}
    for r in rows:
        if not r.get("start"):
            continue
        before = [(t, pid) for t, pid in ends if t and t <= r["start"] and pid != r["id"]]
        if before:
            t, pid = before[-1]
            r["gap"], r["contended"] = r["start"] - t, pid not in ours


def medians(rows: list[dict]) -> dict:
    clean = [r for r in rows if r["status"] == "succeeded" and not r.get("contended")]
    return {"n": len(clean), **{k: statistics.median(v) for k in ("submit", "queue", "exec", "notify", "e2e", "first", "gap")
                                if (v := [r[k] for r in clean if r.get(k) is not None])}}


EDITS = ["a cat on a windowsill, rain outside", "a cat on a windowsill, snow outside", "a black cat on a windowsill, snow outside",
         "a black cat on a windowsill, snow outside, warm lamp light inside",
         "a black cat on a wooden windowsill, snow outside, warm lamp light inside",
         "a fox on a wooden windowsill, snow outside, warm lamp light inside",
         "a fox on a wooden windowsill, snow outside, warm lamp light inside, a cup of tea beside it"]


async def flow(live, frames: int, http, comfy: str, seed: int) -> list[dict]:
    """Flow through the sidecar's own loop: an edit every third frame (EDITS, round), the same words between; each
    state is sent once the frame before it landed, marked typing so nothing settles. One job at a time."""
    events: asyncio.Queue = asyncio.Queue()
    live.listeners.add(events)
    loop = asyncio.create_task(live.run())
    rows, ids = [], set(live.mine)
    try:
        while await live._foreign():
            await asyncio.sleep(0.2)
        for i in range(frames):
            words = EDITS[(i // 3) % len(EDITS)]
            live.update({"template": live.profile.name, "prompt": words, "seed": seed, "flow": True, "typing": True, "v": i + 1})
            while True:
                e = await asyncio.wait_for(events.get(), 60)
                if os.environ.get("GV_DEBUG"):
                    print(i, {k: v for k, v in e.items() if k != "src"}, flush=True)
                if e.get("kind") == "error":
                    raise RuntimeError(e)
                if e.get("kind") == "final" and e.get("stage") == "live" and e.get("v") == i + 1:
                    rows.append({"frame": i, "kind": e.get("flow"), "e2e": e["ms"]})
                    break
    finally:
        loop.cancel()
    jobs = [j for j in live.mine if j not in ids]
    timed = []
    for job in jobs:
        h = (await http.get(f"{comfy}/history/{job}")).json().get(job, {})
        m = {k: v for k, v in h.get("status", {}).get("messages", [])}
        if m.get("execution_success") and "live" in str(h["prompt"][2].get("gv:label", {})):
            timed.append((m["execution_start"]["timestamp"], m["execution_success"]["timestamp"] - m["execution_start"]["timestamp"], len(h["prompt"][2])))
    for row, (_, exec_ms, nodes) in zip(rows, sorted(timed)[-len(rows):]):
        row.update(exec=exec_ms, nodes=nodes)
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--template", required=True, help="a template record as Saddle's /api/templates/<name> returns it")
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--mp", type=float, default=0.25, help="the live size in megapixels (a live bucket)")
    parser.add_argument("--encoder", action="store_true")
    parser.add_argument("--sink", action="store_true", help="also the seed frames with the latent previewed as its size")
    parser.add_argument("--flow", type=int, default=0, help="instead: this many Flow frames through the sidecar's loop")
    parser.add_argument("--out", default="gv-realtime")
    parser.add_argument("--salt", type=int, default=int(time.time()) % 1_000_000 * 100)
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    import httpx
    from comfy_sdk import AsyncComfy

    from live import Live
    from profile import Profile

    live = Live(AsyncComfy(client_info="saddle-gpu-vision-realtime"), args.comfy, "")
    live.profile = Profile(json.loads(Path(args.template).read_text()))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    words = words_of(12, args.salt)
    result = {"template": live.profile.name, "steps": args.steps, "mp": args.mp}
    async with httpx.AsyncClient(timeout=30) as http:
        if args.flow:
            live.pack, live.profile.sized = await live._has("GVLatentRecall"), await live._has("GetLatentSize")
            rows = await flow(live, args.flow, http, args.comfy, args.salt)
            by = {}
            for r in rows[1:]:
                by.setdefault(r["kind"], []).append(r)
            result["flow"] = {"pack": live.pack, "rows": rows, "median": {k: {"n": len(v), **{f: statistics.median(r[f] for r in v if r.get(f) is not None)
                                                                                         for f in ("exec", "e2e", "nodes")}} for k, v in by.items()}}
            print("flow", "pack" if live.pack else "stock chain", json.dumps(result["flow"]["median"]), flush=True)
            (out / f"realtime-flow-{datetime.now():%H%M%S}.json").write_text(json.dumps(result, indent=1))
            return
        await run(live, [{"prompt": words, "seed": args.salt, "live_steps": args.steps, "live_mp": args.mp}], http, args.comfy)  # warm: loads, encodes
        n = args.frames * (2 if args.sink else 1)
        sinks = ["text", "size"] * args.frames if args.sink else None  # interleaved, so drift on the lane hits both alike
        rows = await run(live, [{"prompt": words, "seed": args.salt + 1 + i, "live_steps": args.steps, "live_mp": args.mp} for i in range(n)], http, args.comfy, sinks)
        result["seed"] = {"rows": rows[::2] if args.sink else rows, "median": medians(rows[::2] if args.sink else rows)}
        print("seed", json.dumps(result["seed"]["median"]), flush=True)
        if args.sink:
            result["seed_size_sink"] = {"rows": rows[1::2], "median": medians(rows[1::2])}
            print("seed, size sink", json.dumps(result["seed_size_sink"]["median"]), flush=True)
        if args.encoder:
            for n in (4, 16, 48, 120):
                # New words, then the same words on another seed (the encoder cached, the rest the same job), four times.
                states = [{"prompt": words_of(n, args.salt + 1000 * n + k // 2), "seed": args.salt + k, "live_steps": args.steps, "live_mp": args.mp} for k in range(8)]
                both = await run(live, states, http, args.comfy)
                wrows = both[0::2]
                for r, twin in zip(wrows, both[1::2]):
                    r["seed_twin"] = twin["exec"] if not twin.get("contended") else None
                clean = [r for r in wrows if r["status"] == "succeeded" and not r.get("contended") and r["exec"] and r["seed_twin"]]
                result[f"words{n}"] = {"rows": both, "encoder": statistics.median([r["exec"] - r["seed_twin"] for r in clean]) if clean else None,
                                       "median": medians(wrows)}
                print(f"words {n}: encoder {result[f'words{n}']['encoder']} ms", json.dumps(result[f"words{n}"]["median"]), flush=True)
    (out / f"realtime-{datetime.now():%H%M%S}.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
