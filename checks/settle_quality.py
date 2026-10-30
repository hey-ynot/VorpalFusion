"""Settle quality: the pixel-space and the latent upscale, side by side and timed, on a live endpoint.

    uv run python checks/settle_quality.py --api <v2 API> --comfy <ComfyUI> --saddle <Saddle> --out gv-settle

For each template and prompt it renders the live frame, then settles it both ways once (same
seed, the live half cached; the order alternates by prompt, and a repeat would be served from
ComfyUI's cache), each timed from submit to the downloaded picture. Drift is the mean absolute difference (0-255) between the live frame and
the settle at 16x16: how far the settle moved from what the creator watched. It writes the
pictures and a contact sheet (rows: template x prompt; columns: live, latent, pixel). Every job
goes through the sidecar's owned stream, so none is ever left queued.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPTS = [("a woman in a red coat walking through neon rain at night, holding a clear umbrella, cinematic 35mm film", 520),
           ("a cat asleep on a red velvet chair beside a green door, a bicycle and potted plants", 7)]
TEMPLATES = ["image_flux3_klein_distilled_t2i", "image_z_image_turbo"]


async def run(live, state: dict, settle: bool, upscale: str) -> tuple[bytes, float]:
    """One job through the sidecar's owned stream: (the output picture, seconds to downloaded)."""
    from comfy_sdk import Preview

    started = time.perf_counter()
    graph, _takes = live.profile.build(state, settle, upscale)
    job = await live.client.submit(live.client.workflows.from_json(graph))
    live.mine.add(job.id)
    last = None
    async with live._owned(job) as events:
        async for event in events:
            if isinstance(event, Preview) and event.data:
                last = event.data
    await job.refresh()
    if job.status != "succeeded":
        raise RuntimeError(f"{live.profile.name} {'settle ' + upscale if settle else 'live'}: {job.status} {getattr(job.error, 'message', '')}")
    data = await job.outputs[0].to_bytes() if job.outputs else last
    return data, time.perf_counter() - started


def drift(a: bytes, b: bytes) -> float:
    from PIL import Image, ImageChops, ImageStat

    small = [Image.open(io.BytesIO(x)).convert("RGB").resize((16, 16), Image.Resampling.BOX) for x in (a, b)]
    return round(sum(ImageStat.Stat(ImageChops.difference(*small)).mean) / 3, 1)


def sheet(rows: list[dict], out: Path) -> Path:
    from PIL import Image, ImageDraw

    cell, pad = 512, 34
    image = Image.new("RGB", (cell * 3, (cell + pad) * len(rows)), (12, 12, 12))
    draw = ImageDraw.Draw(image)
    for r, row in enumerate(rows):
        for c, (name, caption) in enumerate([("live", f"live {row['live_s']} s"),
                                             ("latent", f"latent upscale {row['latent_s']} s · drift {row['latent_drift']}"),
                                             ("pixel", f"pixel upscale {row['pixel_s']} s · drift {row['pixel_drift']}")]):
            tile = Image.open(out / row[name]).convert("RGB")
            tile.thumbnail((cell, cell))
            y = r * (cell + pad)
            image.paste(tile, (c * cell, y + pad))
            draw.text((c * cell + 8, y + 8), f"{row['template'].replace('image_', '')} · {caption}", fill=(240, 200, 120), font_size=16)
    path = out / "contact-sheet.jpg"
    image.save(path, quality=90)
    return path


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--out", default="gv-settle")
    parser.add_argument("--templates", default=",".join(TEMPLATES))
    parser.add_argument("--salt", type=int, default=int(time.time()) % 100_000, help="added to the seeds, so no run is served from cache")
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    from comfy_sdk import AsyncComfy

    from live import Live

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    live = Live(AsyncComfy(client_info="saddle-gpu-vision-check"), args.comfy, args.saddle)
    rows = []
    for name in args.templates.split(","):
        await live.use(name)
        await run(live, {"prompt": "warm", "seed": 0, "live_steps": 1}, False, "pixel")  # load the models
        for p, (prompt, seed) in enumerate(PROMPTS):
            state = {"prompt": prompt, "seed": seed + args.salt}
            row = {"template": name, "prompt": prompt, "seed": state["seed"]}
            data, seconds = await run(live, state, False, "pixel")
            row["live"], row["live_s"] = f"{name}-{p}-live.png", round(seconds, 2)
            (out / row["live"]).write_bytes(data)
            for upscale in ("pixel", "latent")[::1 if p % 2 == 0 else -1]:
                data, seconds = await run(live, state, True, upscale)
                row[upscale], row[f"{upscale}_s"] = f"{name}-{p}-{upscale}.png", round(seconds, 2)
                row[f"{upscale}_drift"] = drift((out / row["live"]).read_bytes(), data)
                (out / row[upscale]).write_bytes(data)
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "prompt"}), flush=True)
    (out / "settle-quality.json").write_text(json.dumps(rows, indent=1))
    print(sheet(rows, out))


if __name__ == "__main__":
    asyncio.run(main())
