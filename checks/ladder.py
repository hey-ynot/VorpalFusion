"""The ladder and the split panels, measured on a live endpoint through the sidecar's own submit and owned stream.

    uv run python checks/ladder.py --api … --comfy … --saddle … --rungs --out gv-ladder   # live → settled → final → 2K / 4K both ways
    uv run python checks/ladder.py --api … --comfy … --saddle … --batch --out gv-ladder   # 1 / 2 / 4 / 8 panels at live size

`--rungs`, per template: the live frame, the settle, Final (the settle half cached), then each hi-res rung both ways:
`refine` (Lanczos to the long side, tiled encode, the template's sampler on its schedule's tail, tiled decode; the Final
half cached) and `seedvr2` (Saddle's upscale tool on the Final's picture). Each rung: exec ms (ComfyUI's history),
submit to downloaded, drift from the rung below (16×16 mean difference, 0-255), and the pictures; a detail sheet per
template puts the same crops side by side at the hi-res rungs' own pixels (the Final upscaled with Lanczos beside them).

`--batch`, per template: panels 1 / 2 / 4 / 8 in one job at live size and steps (the words cached, a fresh seed per
job), exec ms and the process's peak VRAM (nvidia-smi every 100 ms over ssh, `--gpu-host`), and the live job
(one picture, stopping at the sampler) for the cost per panel against one picture.

The endpoint must be quiet: each family waits for 20 s without anyone else's job, and a job another client's job overlapped
is discarded and run again (`--quiet`).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WORDS = "a woman in a red coat walking through neon rain at night, holding a clear umbrella, a tram behind her, cinematic 35mm film"
EDIT_WORDS = "she wears a yellow raincoat, a snowy mountain behind her"
RUNG_TEMPLATES = ["image_flux3_klein_distilled_t2i", "image_z_image_turbo", "image_krea2_turbo_t2i"]
BATCH_TEMPLATES = RUNG_TEMPLATES + ["image_flux2_klein_image_edit_9b_distilled"]


class Watch:
    """Who else uses the lane: ComfyUI's queue every 200 ms; any item not ours marks the current job touched."""

    def __init__(self, live, comfy: str) -> None:
        self.live, self.comfy, self.touched, self.seen_at = live, comfy, False, 0.0

    async def run(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=3) as http:
            while True:
                try:
                    queue = (await http.get(self.comfy + "/queue")).json()
                    if any(item[1] not in self.live.mine for item in queue.get("queue_running", []) + queue.get("queue_pending", [])):
                        self.touched, self.seen_at = True, time.time()
                except Exception:  # noqa: BLE001  a missed poll is a missed poll
                    pass
                await asyncio.sleep(0.2)

    async def quiet(self, seconds: float) -> None:
        while time.time() - self.seen_at < seconds:
            await asyncio.sleep(1)


class Vram:
    """The live process's GPU memory, sampled by nvidia-smi every 100 ms over ssh."""

    def __init__(self, host: str | None, port: int = 8188) -> None:
        self.host, self.samples, self.proc = host, [], None
        if host:
            listening = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"ss -ltnpH 'sport = :{port}'"], capture_output=True, text=True, timeout=30).stdout
            self.pid = (listening.split("pid=")[1].split(",")[0]) if "pid=" in listening else ""
            self.proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", host, "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits -lms 100"],
                                         stdout=subprocess.PIPE, text=True)
            asyncio.get_event_loop().run_in_executor(None, self._read)

    def _read(self) -> None:
        for line in self.proc.stdout:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2 and parts[0] == self.pid:
                self.samples.append((time.time(), int(parts[1])))

    def peak(self, t0: float, t1: float) -> int | None:
        found = [mb for t, mb in self.samples if t0 <= t <= t1 + 0.2]
        return max(found) if found else None

    def close(self) -> None:
        if self.proc:
            self.proc.terminate()


async def job(live, state: dict, built, watch: Watch, vram: Vram, http, comfy: str, timeout: float = 600, every: bool = False) -> dict:
    """One job through the owned stream: exec ms from history, when it ended at the sidecar, when its pictures were
    downloaded, the outputs' bytes, peak VRAM, and whether another client's job overlapped it."""
    watch.touched = False
    t0, started = time.time(), time.perf_counter()
    j = await live.submit(state, settle=True, lane="ladder", built=built)
    try:
        async with asyncio.timeout(timeout):
            async with live._owned(j) as events:
                async for _event in events:
                    pass
    except TimeoutError:
        await j.cancel()
        return {"status": "timeout", "e2e_ms": round(1000 * timeout), "touched": watch.touched}
    ended = round(1000 * (time.perf_counter() - started))
    t1 = time.time()
    touched = watch.touched
    await j.refresh()
    kept = [o for o in j.outputs if (o.content_type or "").startswith("image")] if j.status == "succeeded" else []
    data = [await o.to_bytes() for o in (kept if every else kept[:1])]
    e2e = round(1000 * (time.perf_counter() - started))
    history = (await http.get(f"{comfy}/history/{j.id}")).json().get(j.id, {})
    messages = {m[0]: m[1] for m in history.get("status", {}).get("messages", [])}
    a = messages.get("execution_start", {}).get("timestamp")
    b = (messages.get("execution_success") or messages.get("execution_error") or {}).get("timestamp")
    if kept:
        live.outputs[j.id] = kept[0]  # what a tool rung climbs from
    return {"status": j.status, "exec_ms": (b - a) if a and b else None, "ended_ms": ended, "e2e_ms": e2e, "outputs": data, "id": j.id,
            "error": str(getattr(j.error, "message", "") or "")[:200], "peak_vram_mb": vram.peak(t0, t1), "touched": touched,
            "cached": len(messages.get("execution_cached", {}).get("nodes", []))}


async def clean(fn, *args, attempts: int = 4) -> dict:
    """A family's run, again from the top with fresh seeds and words whenever another client's job overlapped one of its jobs
    (a rerun of the same graph would be served from ComfyUI's cache)."""
    for attempt in range(attempts):
        result = await fn(*args, salt_offset=attempt * 97)
        touched = [label for label, rows in result["rows"].items() for row in (rows if isinstance(rows, list) else [rows]) if row.get("touched")]
        if not touched:
            return result
        print(f"    another client's job overlapped {touched}: the family runs again with fresh seeds", flush=True)
        await args[4].quiet(args[3].quiet)
    result["touched"] = True
    return result


def drift(a: bytes, b: bytes) -> float:
    from PIL import Image, ImageChops, ImageStat

    small = [Image.open(io.BytesIO(x)).convert("RGB").resize((16, 16), Image.Resampling.BOX) for x in (a, b)]
    return round(sum(ImageStat.Stat(ImageChops.difference(*small)).mean) / 3, 1)


def detail_sheet(name: str, final: bytes, rungs: dict[str, bytes], out: Path) -> Path:
    """The same two regions (a face-height crop at the centre, a corner of the background) at each hi-res rung's own
    pixels, the Final upscaled with Lanczos to 4K beside them: what a person judges detail by."""
    from PIL import Image, ImageDraw

    pictures = {"final, Lanczos": Image.open(io.BytesIO(final)).convert("RGB"), **{k: Image.open(io.BytesIO(v)).convert("RGB") for k, v in rungs.items()}}
    side = 4096
    cell, pad = 384, 28
    regions = [(0.5, 0.42), (0.18, 0.2)]  # centre (the subject), upper left (the background)
    sheet = Image.new("RGB", (cell * len(pictures), (cell + pad) * len(regions)), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    for c, (label, picture) in enumerate(pictures.items()):
        scale = side / max(picture.size)
        big = picture.resize((round(picture.width * scale), round(picture.height * scale)), Image.Resampling.LANCZOS) if scale != 1 else picture
        for r, (fx, fy) in enumerate(regions):
            # the same region in every picture: 1/8 of the 4K frame's side, shown at the cell size
            w = side // 8
            x, y = round(fx * big.width - w / 2), round(fy * big.height - w / 2)
            crop = big.crop((x, y, x + w, y + w)).resize((cell, cell), Image.Resampling.NEAREST if w < cell else Image.Resampling.LANCZOS)
            sheet.paste(crop, (c * cell, r * (cell + pad) + pad))
            draw.text((c * cell + 6, r * (cell + pad) + 6), f"{label} · {picture.width}x{picture.height}", fill=(240, 200, 120), font_size=14)
    path = out / f"{name}-detail.jpg"
    sheet.save(path, quality=92)
    return path


async def rungs(live, name: str, salt: int, args, watch: Watch, vram: Vram, http, salt_offset: int = 0) -> dict:
    from profile import HIRES

    salt += salt_offset
    await live.use(name)
    profile = live.profile
    await watch.quiet(args.quiet)
    state = {"prompt": f"{WORDS}, take {salt}", "seed": salt, "values": {}, "sources": [], "aspect": 1.0}
    folder = Path(args.out) / name
    folder.mkdir(parents=True, exist_ok=True)
    rows, pictures = {}, {}

    async def run(label: str, built, below: str | None) -> dict:
        row = await job(live, state, built, watch, vram, http, args.comfy)
        data = row.pop("outputs", [])
        if data:
            pictures[label] = data[0]
            (folder / f"{label}.png").write_bytes(data[0])
        if data and below in pictures:
            row["drift"] = drift(pictures[below], data[0])
        rows[label] = row
        print(f"  {label:14} {row['status']:9}{' touched' if row.get('touched') else ''} exec {row.get('exec_ms')!s:>7} ms  ended {row.get('ended_ms')!s:>7} ms  downloaded {row['e2e_ms']:>7} ms  cached {row.get('cached')!s:>3}  "
              f"drift {row.get('drift')!s:>5}  peak {row.get('peak_vram_mb')} MB {row.get('error') or ''}", flush=True)
        return row

    await run("warm", profile.build({**state, "prompt": f"warm {salt}", "seed": salt + 7, "live_steps": 1}, settle=False), None)
    await run("live", profile.build(state, settle=False), None)
    await run("settled", profile.build(state, settle=True), "live")
    await run("final", profile.build(state, settle=True, rung="final"), "settled")
    if "final" not in pictures:
        return {"template": name, "rows": rows}
    final_job = rows["final"]["id"]
    for rung in HIRES:
        await run(f"{rung}-refine", profile.build(state, settle=True, rung=rung), "final")
        built, _extra = await live._tool("seedvr2", rung, {"from": final_job, "state": state})
        await run(f"{rung}-seedvr2", built, "final")
    hires = {k: v for k, v in pictures.items() if "-" in k}
    sheet = detail_sheet(name, pictures["final"], hires, Path(args.out)) if hires else None
    return {"template": name, "rows": rows, "sheet": str(sheet) if sheet else None, "pictures": str(folder)}


async def batch(live, name: str, salt: int, args, watch: Watch, vram: Vram, http, source: str | None, salt_offset: int = 0) -> dict:
    salt += salt_offset
    await live.use(name)
    profile = live.profile
    await watch.quiet(args.quiet)
    words = EDIT_WORDS if profile.edits else WORDS
    sources = [{"slot": profile.media[0]["address"], "id": source}] if profile.edits and source else []
    base = {"prompt": f"{words}, take {salt}", "values": {}, "sources": sources, "aspect": 1.0}
    rows: dict[str, list] = {}

    async def run(label: str, state: dict, partial: bool) -> None:
        row = await job(live, state, profile.build(state, settle=False, partial=partial), watch, vram, http, args.comfy)
        row.pop("outputs", None)
        rows.setdefault(label, []).append(row)
        print(f"  {label:10} {row['status']:9}{' touched' if row.get('touched') else ''} exec {row.get('exec_ms')!s:>6} ms  ended {row.get('ended_ms')!s:>6} ms  peak {row.get('peak_vram_mb')} MB", flush=True)

    await run("warm", {**base, "seed": salt, "live_steps": 1, "prompt": f"warm {salt}"}, True)
    await run("words", {**base, "seed": salt + 1}, True)  # encodes the words once; every job after has them cached
    if args.pick and profile.refines():  # a picked panel settles that exact picture: drift from the panel to its settle
        state = {**base, "seed": salt + 900, "panels": 4}
        batch_row = await job(live, state, profile.build(state, settle=False), watch, vram, http, args.comfy, every=True)
        settle_row = await job(live, {**state, "panel": 2}, profile.build({**state, "panel": 2}, settle=True), watch, vram, http, args.comfy)
        single = {**base, "seed": salt + 901}
        single_live = await job(live, single, profile.build(single, settle=False), watch, vram, http, args.comfy)
        single_settle = await job(live, single, profile.build(single, settle=True), watch, vram, http, args.comfy)
        rows["pick"] = [{"touched": any(r.get("touched") for r in (batch_row, settle_row, single_live, single_settle)),
                         "panel_drift": drift(batch_row["outputs"][2], settle_row["outputs"][0]),
                         "single_drift": drift(single_live["outputs"][0], single_settle["outputs"][0]), "settle_exec_ms": settle_row["exec_ms"]}]
        print(f"  pick 3 of 4: settle drift {rows['pick'][0]['panel_drift']} (one picture: {rows['pick'][0]['single_drift']}), settle exec {settle_row['exec_ms']} ms", flush=True)
    k = 2
    for _ in range(args.repeat):
        await run("today", {**base, "seed": salt + k}, True)
        k += 1
        for n in (1, 2, 4, 8):
            await run(f"panels {n}", {**base, "seed": salt + k, "panels": n}, False)
            k += 1
    return {"template": name, "rows": rows}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--out", default="gv-ladder")
    parser.add_argument("--rungs", action="store_true")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--pick", action="store_true", help="with --batch: the drift of a picked panel's settle against one picture's")
    parser.add_argument("--templates", help="comma-separated, default the measured families")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--quiet", type=float, default=20, help="seconds without anyone else's job before a family runs")
    parser.add_argument("--gpu-host", default=None, help="ssh host for nvidia-smi peak VRAM (the live process's pid by its port)")
    parser.add_argument("--gpu-port", type=int, default=8188, help="the live ComfyUI's port on --gpu-host")
    parser.add_argument("--source", default="portrait.png", help="the reference picture for an edit template")
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    import httpx
    from comfy_sdk import AsyncComfy

    import live as sidecar

    live = sidecar.Live(AsyncComfy(client_info="saddle-gpu-vision-ladder"), args.comfy, args.saddle)
    watch = Watch(live, args.comfy)
    watcher = asyncio.create_task(watch.run())
    vram = Vram(args.gpu_host, args.gpu_port)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    salt = time.time_ns() % 2**31
    report = {"rungs": [], "batch": []}
    source = live.register(Path(args.source).read_bytes(), "image/png") if Path(args.source).exists() else None
    async with httpx.AsyncClient(timeout=30) as http:
        for name in (args.templates.split(",") if args.templates else RUNG_TEMPLATES if args.rungs else BATCH_TEMPLATES):
            print(f"{name}", flush=True)
            salt += 1000
            if args.rungs:
                report["rungs"].append(await clean(rungs, live, name, salt, args, watch, vram, http))
            if args.batch:
                report["batch"].append(await clean(batch, live, name, salt + 500, args, watch, vram, http, source))
            (Path(args.out) / "ladder.json").write_text(json.dumps(report, indent=1, default=str))
    watcher.cancel()
    vram.close()
    print(f"wrote {args.out}/ladder.json")


if __name__ == "__main__":
    asyncio.run(main())
