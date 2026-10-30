"""The silent stall: a job ComfyUI serves from cache ends in ~7 ms, often before the endpoint's event
stream has connected, and the stream then waits for an end it already missed.

    uv run python checks/stall.py --api <v2 API> --comfy <ComfyUI> --saddle <Saddle> [--jobs 60]

Renders one live job, then submits the identical graph `--jobs` times through the sidecar's owned stream
(labelled `stall`), each served from ComfyUI's cache, and reports how long each took to end at the sidecar.
A pass has no job over 1 s.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--comfy", required=True)
    parser.add_argument("--saddle", help="a Saddle server's templates (default: the endpoint's own)")
    parser.add_argument("--template", default="image_z_image_turbo")
    parser.add_argument("--jobs", type=int, default=60)
    args = parser.parse_args()
    os.environ["COMFY_BASE_URL"] = args.api
    from comfy_sdk import AsyncComfy

    from live import Live

    live = Live(AsyncComfy(client_info="saddle-gpu-vision-stall"), args.comfy, args.saddle)
    await live.use(args.template)
    state = {"prompt": f"a paper lantern on a quiet river, take {time.time_ns()}", "seed": 7, "sources": []}
    times = []
    for i in range(args.jobs + 1):
        started = time.perf_counter()
        job = await live.submit(state, settle=False, lane="stall")
        async with live._owned(job) as events:
            async for _event in events:
                pass
        if i:  # the first one renders; the rest are served from cache
            times.append(round(1000 * (time.perf_counter() - started)))
    slow = [t for t in times if t > 1000]
    print(f"{len(times)} cached jobs: p50 {statistics.median(times)} ms, max {max(times)} ms, over 1 s: {len(slow)} {slow}")
    sys.exit(1 if slow else 0)


if __name__ == "__main__":
    asyncio.run(main())
