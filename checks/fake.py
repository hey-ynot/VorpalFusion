"""A fake endpoint for the page checks: VorpalFusion's own sidecar (live.py: its loop, lanes and
routes, unchanged) over stand-in jobs that stream fixture frames, a picture of the words, at
about a warm 5090's pace. Nothing reaches a GPU. Words containing `#busy` make a job report
someone else's queue; `#fail` makes it fail the way ComfyUI refuses a graph; `#slow` holds a
live job for 3 s, silent (for stale-frame and watchdog checks); `#long` makes each step take 0.5 s. A live graph run before is served
from cache: it ends at once with no preview. The enhancer of an Enrich settle
drops 'a' and 'the' and adds a phrase. The templates are `templates.json` beside this file (records as Saddle serves
them, model lists cut to the files each template uses), or a Saddle server's with `--saddle`; the Library drawer
shows fixture pictures. `POST /fake/standalone {"on": true}` lists them as an endpoint's own templates instead, checked
by the sidecar's detection, with three more only detection tells apart (`DETECTED`).

    uv run python checks/fake.py --port 8766

`GET /fake/last` returns the last submitted state, graph and picture takes, for the checks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live  # noqa: E402  (the sidecar under check)

GPU = asyncio.Lock()  # live jobs run one at a time, as ComfyUI runs prompts: one that went ahead waits its turn
LIVE_JOBS: list[dict[str, Any]] = []  # each live job's version and times (submitted, started, ended), for the checks


def loop_ms() -> float:
    return round(1000 * asyncio.get_running_loop().time(), 1)


def picture(words: str, seed: int, done: float, side: int) -> bytes:
    """A fixture frame: a colour from the words and seed, the words written on it, blurrier early."""
    from PIL import Image, ImageDraw, ImageFilter

    digest = hashlib.sha256(f"{words}|{seed}".encode()).digest()
    image = Image.new("RGB", (side, side), tuple(40 + b % 180 for b in digest[:3]))
    draw = ImageDraw.Draw(image)
    draw.ellipse([side * 0.2, side * 0.2, side * 0.8, side * 0.8], fill=tuple(255 - b % 200 for b in digest[3:6]))
    draw.text((side * 0.05, side * 0.05), f"{words[:40]}\nseed {seed}", fill=(255, 255, 255), font_size=side // 18)
    if done < 1:
        image = image.filter(ImageFilter.GaussianBlur(side * (1 - done) / 40))
    out = io.BytesIO()
    image.save(out, "PNG" if side > 512 else "JPEG")
    return out.getvalue()


def clip(words: str, seed: int) -> bytes:
    """A 1 s fixture clip of the words (ffmpeg), for the clip checks; with a tone when the words say `#audio`."""
    with tempfile.TemporaryDirectory() as folder:
        frame = Path(folder) / "f.png"
        frame.write_bytes(picture(words, seed, 1, 256))
        out = Path(folder) / "c.mp4"
        sound = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-shortest"] if "#audio" in words else []
        subprocess.run(["ffmpeg", "-loglevel", "error", "-loop", "1", "-i", str(frame), *sound, "-t", "1", "-r", "8", "-pix_fmt", "yuv420p",
                        "-vf", "scale=256:256", str(out)], check=True)
        return out.read_bytes()


class Asset:
    async def commit(self) -> str:
        await asyncio.sleep(0.01)
        return "asset"


class Output:
    def __init__(self, data: bytes, kind: str, node_id: str = "9") -> None:
        self.data, self.content_type, self.node_id, self.name = data, kind, node_id, f"fake_{uuid.uuid4().hex[:6]}.png"

    async def to_bytes(self) -> bytes:
        return self.data


class Job:
    def __init__(self, state: dict[str, Any], settle: bool, kind: str, cached: bool = False, lane: str = "live",
                 graph: dict[str, Any] | None = None, panels: int = 1) -> None:
        self.id, self.state, self.settle, self.kind, self.cached = str(uuid.uuid4()), state, settle, kind, cached
        self.status, self.error, self.outputs, self.canceled = "queued", None, [], False
        self.lane, self.panels, self.graph = lane, panels, graph or {}
        self.submitted = loop_ms()
        self.node = next((n for n, v in self.graph.items() if v["class_type"] in ("PreviewImage", "SaveImage", "SaveVideo")), "9")

    async def events(self):
        from comfy_sdk import OutputReady, Preview, Progress, StatusChange

        if self.lane == "live":
            async with GPU:
                record = {"v": self.state.get("v"), "submitted": self.submitted, "started": loop_ms()}
                LIVE_JOBS[:] = (LIVE_JOBS + [record])[-200:]
                try:
                    async for event in self._run():
                        yield event
                finally:
                    record["ended"] = loop_ms()
            return
        async for event in self._run():
            yield event

    async def _run(self):
        from comfy_sdk import OutputReady, Preview, Progress, StatusChange

        words, seed = self.state.get("prompt", ""), self.state.get("seed", 0)
        if "#busy" in words:
            yield StatusChange(status="queued", queue_position=3)
            await asyncio.sleep(30)
            return
        if "#fail" in words:
            self.status, self.error = "failed", SimpleNamespace(message="Prompt outputs failed validation: Value not in list")
            yield StatusChange(status="failed")
            return
        if self.cached:  # ComfyUI serves a repeated graph from cache: it ends at once, with no preview
            await asyncio.sleep(0.01)
            if self.panels > 1:  # a cached batch still lists its outputs
                self.outputs = [Output(picture(f"{words} · panel {i + 1}", seed, 1, 384), "image/jpeg", self.node) for i in range(self.panels)]
            self.status = "succeeded"
            yield StatusChange(status="succeeded")
            return
        warm = self.lane == "warm"
        steps = 1 if warm else 8 if self.settle else int(self.state.get("live_steps") or 4)
        # a step costs about 0.28 s per megapixel (the live size the graph asks for), a warm-up loads the models
        mp = next((n["inputs"]["width"] * n["inputs"]["height"] / 1e6 for n in self.graph.values()
                   if "Latent" in n["class_type"] and isinstance(n["inputs"].get("width"), int)), 0.25)
        await asyncio.sleep(0.6 if warm else 3 if "#slow" in words else 0.1)
        for step in range(1, steps + 1):
            if self.canceled:
                yield StatusChange(status="canceled")
                return
            await asyncio.sleep(0.5 if "#long" in words else 0.12 if self.settle else 0.28 * mp)
            yield Progress(value=step / steps, step=step, steps=steps)
            yield Preview(node_id="fake", content_type="image/jpeg", data=picture(words, seed, step / steps, 384))
        if self.settle:
            await asyncio.sleep(0.4)  # the decode and download tail
        rung = " · final" if "gv:final" in self.graph else ""
        rung = next((f" · {side}" for side in ("4096", "2048") if f'"largest_size": {side}' in json.dumps(self.graph)), rung)
        rung = " · tool" if any("Interpolat" in v["class_type"] or "SeedVR2" in v["class_type"] for v in self.graph.values()) else rung
        if self.panels > 1:  # a batch: one picture per panel, the panel's seed slice
            self.outputs = [Output(picture(f"{words} · panel {i + 1}", seed, 1, 384), "image/jpeg", self.node) for i in range(self.panels)]
        elif self.settle or self.kind == "video":
            data, kind = (clip(words, seed), "video/mp4") if self.kind == "video" else (picture(words + rung, seed, 1, 1024), "image/png")
            self.outputs = [Output(data, kind, self.node)]
            yield OutputReady(output=self.outputs[0])
        self.status = "succeeded"

    async def cancel(self) -> "Job":
        self.canceled, self.status = True, "canceled"
        return self

    async def refresh(self) -> "Job":
        return self


FIXTURES = json.loads((Path(__file__).parent / "templates.json").read_text())
# Records only detection tells apart: Z-Image under a name outside the tuned families, and it without a seed or a prompt slot.
_ZIMAGE = FIXTURES["records"]["image_z_image_turbo"]
DETECTED = {"image_fake_untuned_t2i": "Fake Untuned", "image_fake_noseed_t2i": "Fake No Seed", "image_fake_noprompt_t2i": "Fake No Prompt"}
for _name, _drop in (("image_fake_untuned_t2i", None), ("image_fake_noseed_t2i", "seed"), ("image_fake_noprompt_t2i", "text")):
    _record = json.loads(json.dumps(_ZIMAGE))
    _record.update(name=_name, label=DETECTED[_name])
    _record["inputs"]["slots"] = [slot for slot in _record["inputs"]["slots"] if slot.get("name") != _drop]
    FIXTURES["records"][_name] = _record
LIBRARY = [{"id": f"art_{n}", "rel": f"/api/artifacts/art_{n}/content", "title": words, "kind": "image"}
           for n, words in enumerate(("a lighthouse at dusk", "a red jacket on a chair", "a green door in the rain"), 1)]


live.Live.go_ahead = True  # the page checks cover the next frame going ahead (the loop reads it on Live itself)


class FakeLive(live.Live):
    calibration_file = Path(tempfile.gettempdir()) / "gv-fake-calibration.json"  # never the real one
    finetunes_file = Path(tempfile.gettempdir()) / "gv-fake-finetunes.json"  # never the user's: one offered on Krea 2, one nowhere
    FINETUNES = [{"name": "tune-krea.safetensors", "folder": "diffusion_models", "replaces": "krea2_turbo_fp8_scaled.safetensors", "label": "Tune"},
                 {"name": "absent.safetensors", "folder": "diffusion_models", "replaces": "krea2_turbo_fp8_scaled.safetensors", "label": "Absent"}]

    standalone = False  # POST /fake/standalone {"on": true}: the fixtures as the endpoint's own templates, detected

    async def _record(self, http: Any, name: str, info: dict[str, Any] | None = None) -> dict[str, Any]:
        return await super()._record(http, name, info) if self.saddle else json.loads(json.dumps(FIXTURES["records"][name]))

    async def catalog(self) -> list[dict[str, Any]]:
        return await super().catalog() if self.saddle or FakeLive.standalone else FIXTURES["templates"]

    async def _index(self, http: Any) -> list[dict[str, Any]]:  # an endpoint's index: the fixtures, the detected three, a partner one
        rows = [{"name": row["name"], "title": row["label"], "mediaType": row["kind"]} for row in FIXTURES["templates"]]
        rows += [{"name": name, "title": label, "mediaType": "image"} for name, label in DETECTED.items()]
        return [{"templates": rows + [{"name": "api_fake_partner_t2i", "title": "Partner", "mediaType": "image", "openSource": False}]}]

    async def _object_info(self, http: Any) -> dict[str, Any]:  # every node class the fixtures use; no choice lists
        return {node["class_type"]: {} for record in FIXTURES["records"].values() for node in record["wf"].values()}

    async def _check(self, http: Any, name: str, info: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.3)  # comfy-cli takes seconds; the page shows "checking…" meanwhile
        return await super()._check(http, name, info)

    async def library(self, rel: str) -> dict[str, Any]:  # a fixture library picture, kept like a dropped one
        item = next((item for item in LIBRARY if item["rel"] == rel), None)
        if self.saddle or item is None:
            return await super().library(rel)
        return {"id": self.register(picture(item["title"], 0, 1, 512), "image/jpeg", item["id"]), "type": "image/jpeg", "parent": item["id"]}

    async def _models(self, http: Any, folder: str) -> list[str]:  # the endpoint's files: the first fine-tune is installed, and H3's tiny decoder
        return {"diffusion_models": ["tune-krea.safetensors"], "vae_approx": ["taeh3.pth"]}.get(folder, [])
    last: dict[str, Any] = {}
    rungs: list = []  # every rung job submitted, for the checks
    idle_jobs: list = []  # the last idle-lane jobs (calibration, speculation, variations), for the checks
    rendered: set = set()  # graphs already run: a live job for one again is served from cache

    async def submit(self, state: dict[str, Any], settle: bool, lane: str = "live", partial: bool = False,
                     built: tuple[dict[str, Any], list] | None = None) -> Job:
        graph, takes = built or self.profile.build(state, settle, partial=partial)
        self.built = graph
        missing = [sid for _node, _key, sid in takes if sid not in self.sources]
        if missing:
            raise LookupError(f"unknown picture {missing[0]}")
        job_record = {"state": state, "settle": settle, "lane": lane, "graph": graph, "takes": takes}
        if lane == "rung":  # the ladder's own lane: /fake/rungs; /fake/last is the loop's last job
            self.rungs.append(job_record)
        elif lane in ("calib", "spec", "vary"):  # idle work: /fake/idle
            self.idle_jobs = (self.idle_jobs + [job_record])[-40:]
        else:
            self.last = job_record
        key = json.dumps([settle, {k: v for k, v in graph.items() if k != "gv:label"}], sort_keys=True)  # ComfyUI caches by graph
        kind = "video" if any(v["class_type"] == "SaveVideo" for v in graph.values()) else "image"
        panels = self.profile.panels(state) if lane in ("live", "warm") else int(state["vary"]["count"]) if lane == "vary" else 1
        job = Job(state, settle or lane == "rung", kind, cached=key in self.rendered and not settle and lane != "rung", lane=lane,
                  graph=graph, panels=panels)
        self.rendered.add(key)
        self.mine.add(job.id)
        self._graph(job.id, graph)
        return job

    async def _enhanced(self, job_id: str, state: dict[str, Any], tag: dict[str, Any]) -> None:  # an enhancer that adds and drops words
        wrote = state.get("prompt", "")
        read = " ".join(w for w in wrote.split() if w not in ("a", "the")) + ", cinematic lighting, shallow depth of field"
        self.emit({"kind": "enhanced", "wrote": wrote, "read": read, "v": state.get("v"), "id": tag.get("id")})

    pack_on = False  # the endpoint runs GPU Vision's node pack: POST /fake/pack {"on": true}, then choose a template

    async def _has(self, node: str) -> bool:
        return FakeLive.pack_on if node.startswith("GV") else True

    async def _foreign(self) -> list[str]:
        return [{"id": "someone", "app": None, "lane": None}] if "#busy" in self.state.get("prompt", "") else []


async def app(scope, receive, send) -> None:
    path = scope.get("path", "")
    if scope["type"] == "http" and not scope["app_state"].saddle and path == "/library":
        await live.reply(send, 200, json.dumps({"items": [{**item, "thumb": f"library/thumb/{item['id']}"} for item in LIBRARY],
                                                "boards": [], "next": None}).encode())
    elif scope["type"] == "http" and not scope["app_state"].saddle and path.startswith("/library/thumb/"):
        item = next((item for item in LIBRARY if item["id"] == path[15:]), None)
        await live.reply(send, 200, picture(item["title"], 0, 1, 256), b"image/jpeg") if item else await live.reply(send, 404)
    elif scope["type"] == "http" and path.startswith("/fake/picture/"):  # a fixture picture for the checks to drop
        await live.reply(send, 200, picture(path[14:], 0, 1, 1024), b"image/png")
    elif scope["type"] == "http" and scope["path"] == "/fake/last":
        await live.reply(send, 200, json.dumps(scope["app_state"].last).encode())
    elif scope["type"] == "http" and scope["path"] == "/fake/pack":
        FakeLive.pack_on = bool(json.loads(await live.read_body(receive) or b"{}").get("on"))
        await live.reply(send, 200, b"{}")
    elif scope["type"] == "http" and scope["path"] == "/fake/standalone":
        FakeLive.standalone = bool(json.loads(await live.read_body(receive) or b"{}").get("on"))
        await live.reply(send, 200, b"{}")
    elif scope["type"] == "http" and scope["path"] == "/fake/live":
        await live.reply(send, 200, json.dumps(LIVE_JOBS).encode())
    elif scope["type"] == "http" and scope["path"] == "/fake/idle":
        await live.reply(send, 200, json.dumps(scope["app_state"].idle_jobs).encode())
    elif scope["type"] == "http" and scope["path"] == "/fake/rungs":
        await live.reply(send, 200, json.dumps(scope["app_state"].rungs).encode())
    else:
        await live.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--saddle", help="a Saddle server whose imported templates the page reads (GET only); default templates.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--keep", default=str(Path(tempfile.gettempdir()) / "gv-checks" / "kept"), help="where Keep saves")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found: clip checks get no clip", file=sys.stderr)
    FakeLive.calibration_file.unlink(missing_ok=True)  # each run calibrates afresh
    FakeLive.finetunes_file.write_text(json.dumps(FakeLive.FINETUNES))
    client = SimpleNamespace(assets=SimpleNamespace(from_bytes=lambda *a, **k: Asset()))
    fake = FakeLive(client, comfy="", saddle=args.saddle, keep=args.keep)
    asyncio.run(live.serve(fake, args.port, app))


if __name__ == "__main__":
    main()
