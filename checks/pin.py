"""Pin on a live endpoint: one live job with a region pinned and new words. Inside the pin the picture must not drift
(16x16 grayscale, mean absolute difference on 0-255), outside it must change.

    uv run python checks/pin.py --api … --comfy … --saddle … [--template image_flux3_klein_distilled_t2i]

Both jobs go through the sidecar's owned stream (labelled `pin`) with their decode kept; writes the pictures and a
side-by-side to --out.
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

RECT = [0, 0, 1000, 500]  # the left half, [ymin, xmin, ymax, xmax] on 0-1000


def drift(a: bytes, b: bytes, rect: list[int], inside: bool) -> float:
    """Mean absolute difference of the two pictures' 16x16 grayscale cells inside (or outside) `rect`, cells on its
    feathered edge left out."""
    from PIL import Image

    x, y = ([*Image.open(io.BytesIO(d)).convert("L").resize((16, 16), Image.BOX).getdata()] for d in (a, b))
    y0, x0, y1, x1 = (v * 16 / 1000 for v in rect)
    cells = [i for i in range(256) if (y0 + 1 <= i // 16 < y1 - 1 and x0 + 1 <= i % 16 < x1 - 1) == inside
             and (inside or not (y0 - 1 <= i // 16 < y1 + 1 and x0 - 1 <= i % 16 < x1 + 1))]
    return sum(abs(x[i] - y[i]) for i in cells) / len(cells)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--template", default="image_flux3_klein_distilled_t2i")
    parser.add_argument("--out", default="gv-pin")
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    import httpx
    from comfy_sdk import AsyncComfy
    from PIL import Image

    from live import Live

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    live = Live(AsyncComfy(client_info="saddle-gpu-vision-pin"), args.comfy, args.saddle)
    await live.use(args.template)

    async def render(state: dict) -> tuple[bytes, int]:
        job = await live.submit(state, settle=False, lane="pin")
        async with live._owned(job) as events:
            async for _ in events:
                pass
        await job.refresh()
        if job.status != "succeeded":
            raise RuntimeError(f"{job.status}: {getattr(job.error, 'message', '')}")
        async with httpx.AsyncClient(timeout=30) as http:
            messages = {m[0]: m[1] for m in (await http.get(f"{args.comfy}/history/{job.id}")).json()[job.id]["status"]["messages"]}
        return await job.outputs[0].to_bytes(), messages["execution_success"]["timestamp"] - messages["execution_start"]["timestamp"]

    seed = int(time.time()) % 100_000
    before, ms0 = await render({"prompt": "a red fox sitting on a mossy rock in a forest, photograph", "seed": seed})
    pid = live.register(before, "image/png")
    after, ms1 = await render({"prompt": "a snowy mountain village at night, warm windows, photograph", "seed": seed + 1,
                               "pin": {"id": pid, "rect": RECT}})
    result = {"template": args.template, "seed": seed, "rect": RECT, "inside": round(drift(before, after, RECT, True), 2),
              "outside": round(drift(before, after, RECT, False), 2), "before_ms": ms0, "pinned_ms": ms1}
    (out / "before.png").write_bytes(before)
    (out / "pinned.png").write_bytes(after)
    a, b = (Image.open(io.BytesIO(d)).convert("RGB") for d in (before, after))
    sheet = Image.new("RGB", (a.width * 2, a.height))
    sheet.paste(a, (0, 0))
    sheet.paste(b.resize(a.size), (a.width, 0))
    sheet.save(out / "pin.jpg", quality=90)
    print(json.dumps(result))
    if not (result["inside"] < 6 and result["outside"] > 3 * result["inside"]):
        sys.exit("FAIL: the pinned region drifted, or the rest did not change")


if __name__ == "__main__":
    asyncio.run(main())
