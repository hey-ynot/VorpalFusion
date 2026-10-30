"""A lighter model file against the template's own: time and likeness on the same seeds, on a live endpoint.

    uv run python checks/precision.py --api … --comfy … --saddle … --template image_z_image_turbo \
        --swap z_image_turbo_bf16.safetensors=z_image_turbo_nvfp4.safetensors [--swap qwen_3_4b.safetensors=qwen_3_4b_fp4_mixed.safetensors] \
        [--picture portrait.png]   # an edit or reference template's first picture

For each prompt and fresh seed it renders the live graph (live size and steps, its decode kept) with the template's
files and with the swaps, alternating which goes first, through the sidecar's owned stream (labelled `precision`).
Exec ms from ComfyUI's history (each arm warmed first); similarity is the correlation of 64x64 grayscale thumbnails
(1.0 the same picture), as in the step sweep. Writes the pictures and a contact sheet to --out.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPTS = ["a woman in a red wool coat on a rainy neon street at night, cinematic",
           "a ceramic teapot on a wooden table by a window, morning light, steam",
           "an old fisherman's weathered face in close-up, golden hour",
           "a hand-painted shop sign that reads OPEN in bold letters"]


def similarity(a: bytes, b: bytes) -> float:
    """Correlation of the two pictures' 64x64 grayscale thumbnails (1.0 the same picture)."""
    from PIL import Image

    x, y = ([float(v) for v in Image.open(io.BytesIO(d)).convert("L").resize((64, 64)).getdata()] for d in (a, b))
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sum((v - mx) ** 2 for v in x) ** 0.5
    sy = sum((v - my) ** 2 for v in y) ** 0.5
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / (sx * sy + 1e-6)


async def render(live, http, comfy: str, state: dict, swaps: dict) -> tuple[bytes, int]:
    saved = dict(live.profile.swaps)
    live.profile.swaps.update(swaps)
    try:
        job = await live.submit(state, settle=False, lane="precision")
    finally:
        live.profile.swaps = saved
    async with live._owned(job) as events:
        async for _ in events:
            pass
    await job.refresh()
    if job.status != "succeeded":
        raise RuntimeError(f"{job.status}: {getattr(job.error, 'message', '')}")
    messages = {m[0]: m[1] for m in (await http.get(f"{comfy}/history/{job.id}")).json()[job.id]["status"]["messages"]}
    return await job.outputs[0].to_bytes(), messages["execution_success"]["timestamp"] - messages["execution_start"]["timestamp"]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--template", required=True)
    parser.add_argument("--swap", action="append", default=[], help="file=replacement, any model file the graph names")
    parser.add_argument("--picture", help="the first picture slot's picture (an edit or reference template needs one)")
    parser.add_argument("--out", default="gv-precision")
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    import httpx
    from comfy_sdk import AsyncComfy
    from PIL import Image, ImageDraw

    from live import Live

    swaps = dict(item.split("=", 1) for item in args.swap)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    live = Live(AsyncComfy(client_info="saddle-gpu-vision-precision"), args.comfy, args.saddle)
    await live.use(args.template)
    salt = int(time.time()) % 100_000
    sources = []
    if args.picture:
        sid = live.register(Path(args.picture).read_bytes(), "image/png")
        sources = [{"slot": live.profile.media[0]["address"], "id": sid}]
    rows, tiles = [], []
    async with httpx.AsyncClient(timeout=30) as http:
        for arm in ({}, swaps):  # warm both arms so neither pays a load in the timed runs
            await render(live, http, args.comfy, {"prompt": f"warm {salt}", "seed": salt, "sources": sources}, arm)
        for i, prompt in enumerate(PROMPTS):
            state = {"prompt": prompt, "seed": salt + i, "sources": sources}
            arms = [("template", {}), ("swapped", swaps)][:: 1 if i % 2 == 0 else -1]
            got = {name: await render(live, http, args.comfy, state, arm) for name, arm in arms}
            sim = similarity(got["template"][0], got["swapped"][0])
            rows.append({"prompt": i, "seed": state["seed"], "template_ms": got["template"][1], "swapped_ms": got["swapped"][1], "similarity": round(sim, 3)})
            for name in ("template", "swapped"):
                (out / f"{i}-{name}.png").write_bytes(got[name][0])
            tiles.append((got["template"][0], got["swapped"][0], sim))
            print(json.dumps(rows[-1]), flush=True)
    cell = 384
    sheet = Image.new("RGB", (cell * 2, (cell + 24) * len(tiles)), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    for r, (a, b, sim) in enumerate(tiles):
        for c, data in enumerate((a, b)):
            tile = Image.open(io.BytesIO(data)).convert("RGB")
            tile.thumbnail((cell, cell))
            sheet.paste(tile, (c * cell, r * (cell + 24) + 24))
        draw.text((8, r * (cell + 24) + 4), f"template | {' '.join(swaps.values())}   similarity {sim:.3f}", fill=(240, 200, 120))
    sheet.save(out / "contact-sheet.jpg", quality=90)
    summary = {"template": args.template, "swaps": swaps, "template_ms": statistics.median(r["template_ms"] for r in rows),
               "swapped_ms": statistics.median(r["swapped_ms"] for r in rows), "similarity": [r["similarity"] for r in rows]}
    (out / "precision.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
