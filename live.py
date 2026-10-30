"""VorpalFusion's sidecar: the picture develops while you type, on any template.

A latest-wins loop over comfy-sdk. Choosing a template loads it at once (a 1-step job). A live
job (the template at a quarter megapixel, a quarter of its steps, enhancers off, a clip short:
see profile.py) always runs out, then the next one takes the newest words, so frames keep
coming at the GPU's own pace while the creator types; each sampler step's preview streams to
the page as it lands, tagged with the state version it was rendered for. When the words rest,
the loop settles from the live frame. It never queues behind someone else's job: a live job
that reports a queue position checks the endpoint's queue, and if anything ahead is not its
own, it cancels, says so, and waits (polling the queue, submitting nothing) until it is free.

Pictures live in a source registry: `POST /source` keeps one by content hash and uploads it at
once; states refer to pictures by that id, one per image slot.

Run (needs the endpoint's v2 API with previews unthrottled; see README.md):

    uv run python live.py --api <v2 API> [--comfy <ComfyUI>] [--saddle <a Saddle server>]

then open http://127.0.0.1:8765 and choose a template. Templates come from a Saddle server with `--saddle`, else from
the endpoint's own official templates, built into records here with comfy-cli (vf_templates.py): any open-weights one
with a prompt, a seed and a sampler that the endpoint has the nodes and model files for.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

HERE = Path(__file__).parent
SEED_FIELD = 9  # the settled seed and eight neighbours
SEED_IDLE_MS = 1200  # how long the settled picture sits before the seed field fills
KEPT_OUTPUTS = 16  # settled outputs Keep may fetch; the page shows a JPEG
KEPT_FRAMES = 64  # recent live pictures, for a live job ComfyUI serves from cache
TERMINAL = ("succeeded", "failed", "canceled", "expired")
IDLE_LANES = ("seed", "calib", "spec", "vary")  # idle work: stops at any input, never says the GPU is shared, yields to anyone
# The speed dial: ms per live picture each setting aims for. Live-on calibrates a template on this GPU (1- and 2-step
# jobs at two sizes) to a fixed cost plus ms per step per megapixel, and the dial picks the steps and size that meet
# its target within the quality floors (the sweep's live steps; a tenth of a megapixel).
TARGETS = {"snappy": 250, "balanced": 400, "pretty": 700}
CALIBRATION = Path.home() / ".cache" / "gpu-vision" / "calibration.json"
OFFLOADED = 1.6  # a live job this much slower than its calibration: the model is partly offloaded (or the card is shared)
QUIET_POLL = 0.25  # a job whose stream says nothing this long is asked whether it ended
QUIET_SAY = 1.0  # a running job silent this long gets a phase saying what it waits on
FAILURES = [  # what the endpoint said -> what the page says (a sentence and what to do)
    ("out of memory", "The GPU ran out of memory. Close other GPU work or choose a lighter template."),
    ("vram grow failed", "The GPU ran out of memory: other GPU work holds it. Close that work or choose a lighter template."),
    ("no longer held", "The original of that picture is no longer held: let it settle (or graduate it) again, then download."),
    ("unknown picture", "A picture was lost when VorpalFusion restarted. Drop it again."),
    ("failed validation", "This template could not start: a picture or setting it needs is missing or wrong. Check its pictures and settings."),
    ("connect", "VorpalFusion cannot reach the GPU endpoint. Check that ComfyUI is running; it tries again on your next change."),
    ("timeout", "The GPU endpoint stopped answering. It tries again on your next change."),
    ("timed out", "The GPU endpoint stopped answering. It tries again on your next change."),
]
sys.path.insert(0, str(HERE))
# Lighter model files the manifest names for a template's own (precision variants measured against it).
MANIFEST = json.loads((HERE / "gpu-vision.app.json").read_text())
LIVE_MODELS = [asset for asset in MANIFEST["assets"] if asset.get("kind") == "live_model" and asset.get("replaces")]
DRAFT_DECODERS = [asset for asset in MANIFEST["assets"] if asset.get("drafts")]  # a tiny decoder and the VAE files a draft clip may swap for it
LADDER = MANIFEST["ladder"]  # which hi-res path each template climbs by, and the tools the ladder runs (data, measured)
LIBRARY_REL = re.compile(r"/api/artifacts/[A-Za-z0-9_.-]+/content")  # a Saddle library picture, the only URL the sidecar fetches for one

from profile import FLOW_LEVELS, FLOW_ROOT, HIRES, LIVE_BUCKETS, MODEL_FILES, Profile, title  # noqa: E402  (beside this file)
from vf_templates import TemplateError, gaps, import_template, media_slots  # noqa: E402

# The families the live profile is measured on: offered at once without Saddle. Any other template is offered once it
# is checked (`Live._check`) and runs on the generic profile, labelled untuned; a family joins here once measured.
TUNED = re.compile(r"(image|video)_(flux3_klein|z_image|krea2|flux2_klein|minimax_h3|fastvideo_fasth3|pika_h3_flex|ltx2_5)(?!.*controlnet)")
CHECKS_AT_ONCE = 2  # templates checked in parallel (comfy-cli builds a record in seconds, on the CPU)
RECORDS = Path.home() / ".cache" / "gpu-vision" / "templates"  # records built with comfy-cli, by template file and endpoint


def plain(raw: str) -> str:
    """A failure as a sentence a creator can act on, never empty, never raw."""
    text = " ".join(str(raw).split()).lower()
    return next((sentence for needle, sentence in FAILURES if needle in text),
                "The GPU job failed. Change the words or settings to try again.")


def jpeg(data: bytes) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def dimensions(data: bytes) -> list[int]:
    from PIL import Image

    return list(Image.open(io.BytesIO(data)).size)


def fit(points: list[tuple[int, float, float]]) -> dict[str, float]:
    """Least squares of `ms = fixed + per * steps * megapixels` over (steps, megapixels, ms) calibration jobs."""
    xs, ys = [steps * mp for steps, mp, _ms in points], [ms for _s, _m, ms in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    per = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sum((x - mx) ** 2 for x in xs) or 1)
    return {"fixed": round(max(0.0, my - per * mx), 1), "per_step_mp": round(max(0.0, per), 1)}


def pick(cal: dict[str, float], target: float, floor: int, ceiling: int) -> tuple[int, float]:
    """The steps and size a target allows: the largest live size that meets it at the step floor, then as many
    steps as still meet it there (at least the floor, at least the smallest size: quality floors before speed)."""
    cost = lambda steps, mp: cal["fixed"] + cal["per_step_mp"] * steps * mp  # noqa: E731
    mp = max((b for b in LIVE_BUCKETS if cost(floor, b) <= target), default=LIVE_BUCKETS[0])
    steps = max((n for n in range(floor, max(floor, ceiling) + 1) if cost(n, mp) <= target), default=floor)
    return steps, mp


def flow_kind(before: str | None, after: str) -> str:
    """How much the words changed between two Flow frames: `reset` (no frame yet, or a new sentence: no word kept),
    `same`, `word` (one word added, dropped or replaced; punctuation and case aside), else `phrase`."""
    import difflib

    a, b = (re.findall(r"[\w'-]+", (words or "").lower()) for words in (before, after))
    if not a or not b:
        return "reset"
    kept = sum(block.size for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks())
    if kept == 0:
        return "reset"
    changed = max(len(a), len(b)) - kept
    return "same" if a == b else "word" if changed <= 1 else "phrase"


def slug(words: str, limit: int = 48) -> str:
    """Words as a file name: lower case, letters and digits, dashes between, at most `limit` characters."""
    return re.sub(r"[^a-z0-9]+", "-", (words or "").lower()).strip("-")[:limit].rstrip("-") or "untitled"


def download_name(snapshot: dict[str, Any], ext: str) -> str:
    """`<slug of the words>_seed<seed>_<rung>.<ext>`; a panel adds `_p<n>` (1-based, as the stage numbers them)."""
    name = f"{slug(snapshot.get('prompt', ''))}_seed{snapshot.get('seed', 0)}_{snapshot.get('rung') or 'live'}"
    if (snapshot.get("panels") or 1) > 1:
        name += f"_p{int(snapshot.get('panel') or 0) + 1}"
    return f"{name}.{ext}"


def png_text(data: bytes, chunks: dict[str, str], keep: tuple[str, ...] = ()) -> bytes:
    """`data` (a PNG) with these text chunks (iTXt, UTF-8, uncompressed: what ComfyUI's loader reads) before IEND;
    an existing text chunk of the same keyword is replaced unless its keyword is in `keep` (then it stays, and the
    new one is not added)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    out, at, held = [data[:8]], 8, set()
    while at < len(data):
        length = int.from_bytes(data[at:at + 4], "big")
        kind, body = data[at + 4:at + 8], data[at + 8:at + 8 + length]
        whole = data[at:at + 12 + length]
        at += 12 + length
        if kind in (b"tEXt", b"iTXt", b"zTXt"):
            keyword = body.split(b"\0", 1)[0].decode("latin-1")
            if keyword in chunks and keyword not in keep:
                continue
            held.add(keyword)
        if kind == b"IEND":
            for keyword, text in chunks.items():
                if keyword in keep and keyword in held:
                    continue
                body = keyword.encode("latin-1") + b"\0\0\0\0\0" + text.encode("utf-8")
                out.append(len(body).to_bytes(4, "big") + b"iTXt" + body + zlib.crc32(b"iTXt" + body).to_bytes(4, "big"))
        out.append(whole)
    return b"".join(out)


def as_png(data: bytes) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "PNG")
    return out.getvalue()


def data_url(kind: str, data: bytes) -> str:
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


class Live:
    """The one live job: latest state wins, nothing ever waits in a queue."""

    calibration_file = CALIBRATION
    go_ahead = False  # with `--go-ahead`, the next live frame queues behind the running one (two jobs on the endpoint)
    finetunes_file = Path.home() / ".config" / "gpu-vision" / "finetunes.json"  # the user's own fine-tunes, beside the manifest's

    def __init__(self, client: Any, comfy: str, saddle: str | None, trace: str | None = None, keep: str | None = None) -> None:
        self.client, self.comfy, self.saddle = client, comfy, saddle
        self.keep = Path(keep or Path.home() / "Pictures" / "VorpalFusion")  # where Keep saves, standalone
        self.owner = uuid.uuid4().hex[:8]  # this sidecar, in every job's label
        self.tracer = csv.writer(open(trace, "a", newline="", buffering=1)) if trace else None
        self.profile: Profile | None = None
        self.state: dict[str, Any] = {}
        self.changed = asyncio.Event()
        self.listeners: set[asyncio.Queue] = set()
        self.version = 0
        self.epoch = 0  # the template or its pictures changed: live work for the old ones stops
        self.warmed: tuple | None = None  # the template and pictures the last warm-up loaded
        # The source registry: content hash -> {asset, type, data, upload}. It lives in memory: a restart
        # loses it and the page asks for the picture again (a disk cache would carry it across restarts).
        self.sources: dict[str, dict[str, Any]] = {}
        self.outputs: OrderedDict[str, Any] = OrderedDict()  # settled outputs Keep may fetch, by job
        self.mine: set[str] = set()  # every job this loop submitted (the v2 job id is ComfyUI's prompt id)
        self.built: dict[str, Any] = {}  # the graph last submitted, for what a silent node is doing
        self.models: dict[str, list[str]] = {}  # the endpoint's model files, by folder
        self.frames: OrderedDict[str, str] = OrderedDict()  # recent live pictures by condition, for cached jobs
        self.profiles: dict[str, Profile] = {}  # every template read, so a snapshot of another one can climb
        self.records: dict[str, dict[str, Any]] = {}  # without Saddle: the records built here, by template
        self.titles: dict[str, str] = {}  # without Saddle: the endpoint's own template titles
        self.checked: dict[str, dict[str, Any]] = {}  # without Saddle: each untuned template's verdict ({offer, why, kind, job, swaps})
        self.checking: asyncio.Task | None = None  # the background check of the templates not yet checked
        self.views: OrderedDict[str, tuple[bytes, str]] = OrderedDict()  # a rung's picture as shown (JPEG) or its clip, by job
        # The ladder: rungs asked for, run one at a time below live and settle. Live keeps working: any change
        # stops the rung's job, and it runs again from the top once the loop is idle.
        self.ladder: list[dict[str, Any]] = []
        self.idle, self.asked = asyncio.Event(), asyncio.Event()
        self.gpu: str | None = None  # the endpoint's GPU, by name, for the calibration key
        self.calibration: dict[str, Any] = json.loads(self.calibration_file.read_text()) if self.calibration_file.exists() else {}
        self.last_ms = self.live_ms = 0  # the last job's time, submit to end; the last live job's
        self.cal_key: str | None = None  # the chosen template's calibration key
        self.halted = -1  # the state version Cancel stopped: nothing renders for it again
        self.graphs: OrderedDict[str, dict[str, Any]] = OrderedDict()  # the API graph each kept output was made by, by job
        # Flow: the chain since its last reset or re-root ({key, frames, pics, anchor, roots, count}), and the chain each
        # settled condition came from, so its rungs climb from the same picture.
        self.stream: dict[str, Any] | None = None
        self.flows: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self.last_src: str | None = None  # the last live frame shown
        # The pipeline: the next live job, submitted while the one on the GPU still runs ({version, epoch, state, flowing,
        # built, job, inbox, reader}), and the task submitting it.
        self.ahead: dict[str, Any] | None = None
        self.pack = False  # the endpoint runs VorpalFusion's node pack (nodes/): Flow keeps its latents there
        self.presubmit: asyncio.Task | None = None

    @property
    def detecting(self) -> bool:
        """Whether the templates offered are the sidecar's own finds (no Saddle lists them)."""
        return not self.saddle

    async def use(self, name: str) -> dict[str, Any]:
        self.profile, self.epoch = await self._profile(name, fresh=True), self.epoch + 1
        self.pack = await self._has("GVLatentRecall")
        self.cal_key = await self._cal_key(self.profile)
        self.changed.set()
        # The family's edit template, where the manifest lists one: F ("pin as reference") switches to it from a template with no picture slot.
        return {**self.profile.describe(), "edit_template": MANIFEST.get("edit_templates", {}).get(name)}

    async def _profile(self, name: str, fresh: bool = False) -> Profile:
        """A template's profile, read from Saddle or built here (again when `fresh`: choosing a template re-reads it)."""
        if fresh or name not in self.profiles:
            async with httpx.AsyncClient(timeout=30) as http:
                profile = Profile(await self._record(http, name))
                profile.tuned = bool(TUNED.match(name))
                profile.swaps.update(self.checked.get(name, {}).get("swaps", {}))  # files the endpoint has in another precision
                for asset in LIVE_MODELS:  # a lighter file the manifest names, where the endpoint has it
                    if asset["replaces"] in json.dumps(profile.wf) and asset["name"] in await self._models(http, asset["folder"]):
                        profile.swaps[asset["replaces"]] = asset["name"]
                tiny = await self._models(http, "vae_approx")
                for asset in DRAFT_DECODERS:  # the endpoint's file of that name, whatever its extension
                    if found := next((f for f in tiny if Path(f).stem == Path(asset["name"]).stem), None):
                        profile.drafts.update(dict.fromkeys(asset["drafts"], found))
                files = {profile.swaps.get(v, v) for node in profile.wf.values() for v in node["inputs"].values() if isinstance(v, str)}
                profile.finetunes = [entry for entry in self._finetunes() if entry.get("replaces") in files and entry.get("label")
                                     and entry.get("name") in await self._models(http, entry.get("folder", "diffusion_models"))]
            profile.sized = await self._has("GetLatentSize")
            self.profiles[name] = profile
        return self.profiles[name]

    async def _record(self, http: Any, name: str, info: dict[str, Any] | None = None) -> dict[str, Any]:
        """A template's record: Saddle's, or built from the endpoint's own template with comfy-cli (once a run; `info`:
        the endpoint's object_info, when the caller holds it)."""
        if self.saddle:
            return (await http.get(f"{self.saddle}/api/templates/{quote(name)}")).json()
        if name not in self.records:
            self.records[name] = await asyncio.to_thread(import_template, self.comfy, name, RECORDS, info)
        return {**self.records[name], "label": self.titles.get(name, name)}

    async def _index(self, http: Any) -> list[dict[str, Any]]:
        return (await http.get(f"{self.comfy}/templates/index.json")).json()

    async def _object_info(self, http: Any) -> dict[str, Any]:
        return (await http.get(f"{self.comfy}/api/object_info")).json()

    async def catalog(self) -> list[dict[str, Any]]:
        """The template rows `listing` picks from: Saddle's, or the endpoint's official templates: the TUNED families at
        once, every other open-weights image or clip template once checked (`checking` until then; one that fails the
        check is not offered)."""
        async with httpx.AsyncClient(timeout=30) as http:
            if self.saddle:
                return (await http.get(f"{self.saddle}/api/templates")).json().get("templates", [])
            index = await self._index(http)
        rows, unchecked = [], []
        for template in (t for category in index for t in category.get("templates") or []):
            name = template.get("name", "")
            self.titles[name] = template.get("title") or name
            row = {"name": name, "label": self.titles[name], "role": "creation"}
            if TUNED.match(name):
                rows.append({**row, "kind": name.split("_")[0],
                             "job": "edit" if name.startswith("image_") and ("edit" in name or "reference" in name) else "make"})
            elif name.startswith("api_") or template.get("openSource") is False or template.get("mediaType") not in ("image", "video"):
                continue  # partner models, and what makes neither a picture nor a clip
            elif name not in self.checked:
                unchecked.append(name)
                rows.append({**row, "kind": template["mediaType"], "job": "make", "checking": True})
            elif self.checked[name]["offer"]:
                rows.append({**row, "kind": self.checked[name]["kind"], "job": self.checked[name]["job"]})
        if unchecked and (self.checking is None or self.checking.done()):
            self.checking = asyncio.create_task(self._check_all(unchecked))
        return rows

    async def _check_all(self, names: list[str]) -> None:
        """Check `names`, CHECKS_AT_ONCE at a time, against one read of the endpoint's object_info."""
        gate = asyncio.Semaphore(CHECKS_AT_ONCE)
        async with httpx.AsyncClient(timeout=60) as http:
            info = await self._object_info(http)

            async def one(name: str) -> None:
                async with gate:
                    self.checked[name] = await self._check(http, name, info)

            await asyncio.gather(*map(one, names))

    async def _check(self, http: Any, name: str, info: dict[str, Any]) -> dict[str, Any]:
        """Whether a template can run live here: it has a prompt slot, a seed slot and a sampler that starts from an
        empty latent or a picture (its record says), and the endpoint has every node class and model file its graph
        names (a file in another precision counts: `swaps`). Records are cached on disk by template file and object_info."""
        try:
            profile = Profile(await self._record(http, name, info))
        except Exception as error:  # noqa: BLE001  (a template that cannot be read is not offered, whatever the reason)
            return {"offer": False, "why": f"unreadable: {str(error)[:200]}"}
        missing, swaps = gaps(profile.wf, info)
        why = ("no prompt slot" if not profile.prompt else "no seed slot" if not profile.seed
               else "no sampler from an empty latent or a picture" if profile.start() is None
               else f"the endpoint lacks {', '.join(missing)}" if missing else None)
        return {"offer": why is None, "why": why, "kind": profile.kind, "job": "edit" if profile.edits else "make", "swaps": swaps}

    def _finetunes(self) -> list[dict[str, Any]]:
        """The fine-tunes a template's model file may be swapped for: the manifest's `finetunes` and the user's own
        file (`{name, folder, replaces, label}` each), read afresh so an edit shows on the next template choice."""
        local = []
        with contextlib.suppress(OSError, ValueError):
            local = json.loads(self.finetunes_file.read_text())
        return [entry for entry in [*MANIFEST.get("finetunes", []), *(local if isinstance(local, list) else [])] if isinstance(entry, dict)]

    async def _has(self, node: str) -> bool:
        """Whether the endpoint's ComfyUI has a node class."""
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=5) as http:
                return node in (await http.get(f"{self.comfy}/object_info/{node}")).json()
        return False

    async def _models(self, http: Any, folder: str) -> list[str]:
        if folder not in self.models:
            with contextlib.suppress(Exception):
                self.models[folder] = (await http.get(f"{self.comfy}/models/{folder}")).json()
        return self.models.get(folder, [])

    @staticmethod
    def _frame_key(state: dict[str, Any]) -> str:
        return json.dumps({k: v for k, v in state.items() if k not in ("v", "rest_ms", "typing", "enrich", "speed", "knob", "likely")
                           and v is not None}, sort_keys=True)

    @staticmethod
    def _key(state: dict[str, Any]) -> tuple:
        return state.get("template"), tuple(sorted((s["slot"], s["id"]) for s in state.get("sources") or []))

    def update(self, state: dict[str, Any]) -> None:
        if self._key(state) != self._key(self.state):
            self.epoch += 1
            self._cancel_ahead()  # a frame for the old template or pictures never starts
        self.state, self.version = state, self.version + 1
        self.idle.clear()  # a rung running now stops at once and runs again when the loop is idle
        self.changed.set()

    def halt(self) -> None:
        """Cancel (a clip seen early that is wrong): the job running for the current state stops, and nothing renders
        until the next change."""
        self.halted = self.version
        self._cancel_ahead()
        self.emit({"kind": "phase", "phase": "canceled", "v": self.state.get("v")})

    def graduate(self, ask: dict[str, Any]) -> None:
        """Ask for a snapshot's next rung: `{id, state, rung, from?, path?}` (`from`: the job whose output it climbs from)."""
        self.ladder = [a for a in self.ladder if (a["id"], a["rung"]) != (ask["id"], ask["rung"])] + [ask]
        self.emit({"kind": "rung", "phase": "queued", "id": ask["id"], "rung": ask["rung"], "ahead": len(self.ladder) - 1})
        self.asked.set()

    def register(self, data: bytes, kind: str, parent: str | None = None) -> str:
        """Keep a picture (or a clip) by its content hash and start its upload now; the id is what states carry.
        `parent`: the Saddle library result it came from, for lineage."""
        sid = hashlib.sha256(data).hexdigest()[:16]
        if sid not in self.sources:
            asset = self.client.assets.from_bytes(data, filename=f"{sid}.{kind.split('/')[-1]}", content_type=kind)
            self.sources[sid] = {"asset": asset, "type": kind, "data": data, "upload": asyncio.create_task(asset.commit())}
        if parent:
            self.sources[sid]["parent"] = parent
        return sid

    async def library(self, rel: str) -> dict[str, Any]:
        """A Saddle library picture by its rel, fetched from Saddle's API and kept like a dropped one."""
        if not self.saddle or not LIBRARY_REL.fullmatch(rel or ""):
            raise ValueError("not a library rel")
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(self.saddle + rel)
        kind = r.headers.get("content-type", "").split(";")[0]
        if r.status_code != 200 or not kind.startswith("image/"):
            raise LookupError(f"the library has no picture at {rel}")
        parent = rel.split("/")[3]
        return {"id": self.register(r.content, kind, parent), "type": kind, "parent": parent}

    def emit(self, frame: dict[str, Any]) -> None:
        for queue in self.listeners:
            if queue.qsize() > 4:  # a slow page drops stale frames, never lags behind
                queue.get_nowait()
            queue.put_nowait(frame)

    def trace(self, lane: str, job: str, started: float, event: str, **fields: Any) -> None:
        """One trace row (with --trace): wall ms, lane, template, job, ms since submit, the event."""
        if self.tracer:
            self.tracer.writerow([round(time.time() * 1000), lane, self.profile.name if self.profile else "", job,
                                  round(1000 * (time.perf_counter() - started)), event, json.dumps(fields, separators=(",", ":"))])

    async def residency(self, lane: str, job: str, started: float, when: str) -> None:
        """VRAM as ComfyUI reports it, traced before and after each job (with --trace)."""
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=2) as http:
                device = (await http.get(self.comfy + "/system_stats")).json()["devices"][0]
            self.trace(lane, job, started, "vram", when=when, free_mb=device["vram_free"] >> 20, torch_mb=device["torch_vram_total"] >> 20)

    async def submit(self, state: dict[str, Any], settle: bool, lane: str = "live", partial: bool = False,
                     built: tuple[dict[str, Any], list] | None = None) -> Any:
        graph, takes = built or self.profile.build(state, settle, partial=partial)
        self.built = graph
        # A label ComfyUI never executes (nothing reads it) but its queue shows: whose job this is, and which lane.
        graph["gv:label"] = {"class_type": "PrimitiveString", "inputs": {"value": f"gpu-vision {self.owner} {lane}"}}
        workflow = self.client.workflows.from_json(json.loads(json.dumps(graph)))  # a copy: set_input writes asset handles into it, the kept graph stays JSON
        for node, key, sid in takes:
            source = self.sources.get(sid)
            if source is None:
                raise LookupError(f"unknown picture {sid}")
            try:
                await source["upload"]
            except Exception:
                source["upload"] = asyncio.create_task(source["asset"].commit())  # the next try uploads again
                raise
            workflow.set_input(node, key, source["asset"])
        job = await self.client.submit(workflow)
        self.mine.add(job.id)
        self._graph(job.id, graph)
        return job

    def _graph(self, job_id: str, graph: dict[str, Any]) -> None:
        self.graphs[job_id] = graph
        while len(self.graphs) > 4 * KEPT_OUTPUTS:
            self.graphs.popitem(last=False)

    async def run(self) -> None:
        """Lanes, by priority. Choosing a template (or a picture) loads it with a 1-step job.
        Live always runs out, so frames keep coming while the creator types; the settle starts
        once the words rest and any change cancels it; the seed field fills alternative seeds
        once the settled picture has sat a while, and any change cancels it too. A failure is
        reported and the loop carries on. The ladder climbs in its own lane, below all of them."""
        climbing = asyncio.create_task(self._climbing())
        climbing.add_done_callback(lambda task: task.cancelled() or task.exception() is None
                                   or __import__("traceback").print_exception(task.exception()))
        while True:
            try:
                await self._turn()
            except Exception as error:  # the endpoint went away, a job was refused: say so, keep looping
                raw = f"{type(error).__name__}: {error}"
                self.emit({"kind": "error", "stage": "live", "message": plain(raw), "detail": raw[:240], "v": self.state.get("v")})

    async def _turn(self) -> None:
        while True:
            if not self.changed.is_set():
                self.idle.set()  # nothing to render: the ladder may climb
            await self.changed.wait()
            self.idle.clear()
            self.changed.clear()
            version, epoch, state = self.version, self.epoch, dict(self.state)
            if ahead := await self._taken(epoch):  # the next frame is already on the GPU's queue: stream it
                version, state, flowing = ahead["version"], ahead["state"], ahead["flowing"]
                if version != self.version:
                    self.changed.set()  # newer input came after it went ahead: the next turn takes that
            else:
                if self.profile is None or not state or state.get("template") not in (None, self.profile.name):
                    continue  # no template yet, or words meant for one the page is still switching to
                if label := self.profile.missing(state):
                    self.emit({"kind": "phase", "phase": "needs", "label": label, "v": state.get("v")})
                    continue
                if self.warmed != (warm := (self.profile.name, self._key(state)[1])):
                    self.emit({"kind": "phase", "phase": "loading", "template": self.profile.name, "title": self.profile.title, "v": state.get("v")})
                    started = time.perf_counter()
                    # Fresh words and seed: a warm-up ComfyUI could serve from cache would load nothing.
                    nonce = time.time_ns()
                    result = await self._render({**state, "prompt": f"{state.get('prompt') or ''} {nonce}", "seed": nonce % 2**31, "live_steps": 1}, "warm", version, epoch)
                    if result != "done":
                        await self._stopped(result)
                        continue
                    self.warmed = warm
                    self.emit({"kind": "loaded", "template": self.profile.name, "ms": round(1000 * (time.perf_counter() - started))})
                    if self.version != version:
                        continue  # the words moved on during the load: the loop is already woken for them
                if not state.get("prompt", "").strip():
                    self.emit({"kind": "phase", "phase": "idle", "v": state.get("v")})
                    await self._calibrate(state, version, epoch)
                    continue
                if state.get("typing") and self.profile.kind == "video":
                    continue  # a clip's draft takes seconds: words sent mid-sentence only stop what runs; the whole words render at rest
                state = self._dialed(state)
                flowing = bool(state.get("flow")) and self.profile.flows() and self.profile.panels(state) == 1
                if flowing:
                    state = self._flow(state)
            built = ahead["built"] if ahead else self.profile.build(state, settle=False, partial=True) if flowing else None
            result = await self._render(state, "live", version, epoch, tag={"flow": state["flow"]["kind"]} if flowing else None,
                                         built=built, ahead=ahead)
            if flowing:
                self._flowed(state, result)
            if result != "done" or self.version != version:
                await self._stopped(result)
                continue  # a live frame landed for older words: go again with the newest
            self.live_ms = self.last_ms
            self._meter(state)
            if state.get("typing"):
                continue  # words sent mid-sentence: the page sends them whole once the keys rest
            if self.profile.panels(state) > 1 and state.get("panel") is None:
                continue  # split panels: only the one the creator picks settles
            if not await self._rests(state.get("rest_ms", 450)):
                continue
            if (result := await self._render(state, "settle", version, epoch)) != "done":
                await self._stopped(result)
                continue
            if flowing:
                self.flows[(state.get("template"), state.get("prompt"), state.get("seed"))] = state["flow"]
                while len(self.flows) > KEPT_OUTPUTS:
                    self.flows.popitem(last=False)
                state = {**state, "flow": None}  # the seed field, speculation and calibration render from words, not the chain
            await self._calibrate(state, version, epoch)
            if self.profile.kind != "image" or not state.get("seeds", True) or self.profile.panels(state) > 1 \
                    or self.ladder or not await self._rests(SEED_IDLE_MS):
                continue  # the panels are the alternatives; a rung asked for climbs before the seed field
            if await self._field(state, version, epoch):
                await self._speculate(state, version, epoch)

    def _flow(self, state: dict[str, Any]) -> dict[str, Any]:
        """The next Flow frame for `state`: a reset (the live job from noise) for new words or any change but the
        words; else the previous frame's latent re-noised by how much the words changed (FLOW_LEVELS). Every
        FLOW_ROOT frames the chain restarts from its last picture, the anchor's picture beside it, both kept as
        sources and fixed until the next re-root (changing them per job would re-run the chain). On an endpoint with
        VorpalFusion's node pack the chain is a session there instead: each frame names the frame it starts from and its
        anchor by number, ComfyUI keeps their latents, and nothing re-roots."""
        state = {**state, "fast": None}  # one size for the whole chain
        key = (self.epoch, json.dumps({k: v for k, v in state.items() if k not in (
            "v", "prompt", "rest_ms", "typing", "speed", "knob", "likely", "field", "vary_strength", "flow")}, sort_keys=True))
        s = self.stream
        if not s or s["key"] != key:
            s = self.stream = {"key": key, "frames": [], "pics": [], "anchor": None, "roots": None, "count": 0,
                               "session": f"{self.owner}-{uuid.uuid4().hex[:8]}" if self.pack else None, "anchor_n": None}
        words = state.get("prompt") or ""
        kind = flow_kind(s["frames"][-1]["words"] if s["frames"] else None, words)
        seed = state["seed"] + s["count"]  # a fresh seed per frame
        if kind == "reset":
            s.update(frames=[{"words": words, "seed": seed, "n": s["count"]}], pics=[], roots=None, anchor_n=s["count"])
        else:
            if self._reroots():
                s["roots"] = {"frame": self._keep_src(s["pics"][-1]), "anchor": self._keep_src(s["anchor"])}
                s["frames"], s["pics"] = [], []
            steps, denoise = FLOW_LEVELS[kind]
            frame = {"words": words, "seed": seed, "steps": steps, "denoise": denoise, "n": s["count"]}
            if s["session"]:  # the pack: the anchor by number; the graph needs only the frame before
                frame["anchor"] = s["anchor_n"]
                s["anchor_n"] = s["count"] if kind != "same" else s["anchor_n"]
                s["frames"] = s["frames"][-FLOW_ROOT:]
            s["frames"].append(frame)
        s["count"] += 1
        return {**state, "flow": {"frames": [dict(f) for f in s["frames"]], "roots": s["roots"], "kind": kind, "session": s["session"]}}

    def _reroots(self) -> bool:
        """Whether the chain's next frame (unless it resets) restarts from the last picture."""
        s = self.stream
        return bool(s and not s["session"] and len(s["frames"]) >= FLOW_ROOT and s["pics"] and s["anchor"])

    def _flowed(self, state: dict[str, Any], result: str) -> None:
        """After a Flow frame: its picture joins the chain (the anchor's, when its words changed); a frame that did
        not run leaves it, unless a frame already went ahead on it (that frame's graph runs it again)."""
        s, n = self.stream, state["flow"]["frames"][-1]["n"]
        at = next((i for i, f in enumerate(s["frames"]) if f["n"] == n), None) if s else None
        if at is None:
            return  # the chain reset or re-rooted since
        if s["session"]:
            if result != "done":
                self.stream = None  # its latent was never stored, and a frame may already name it: start again
            return
        if result != "done" or not self.last_src:
            if at == len(s["frames"]) - 1:
                s["frames"].pop()
                if not s["frames"]:
                    self.stream = None
            return
        s["pics"].append(self.last_src)
        if state["flow"]["kind"] != "same" and not (s["roots"] and at == 0):
            s["anchor"] = self.last_src  # a re-root frame that is itself an edit keeps the earlier anchor

    def _keep_src(self, src: str) -> str:
        """A frame (a data URL) as a source the next graphs can load."""
        head, body = src.split(",", 1)
        return self.register(base64.b64decode(body), head[5:].split(";")[0])

    async def _presubmit(self) -> None:
        """The next live frame, submitted while the one on the GPU still runs, for input that came during it. ComfyUI
        starts a queued prompt 1-2 ms after the one before ends; a loop that submits only after the end leaves the GPU
        idle while the end travels back and the next graph travels out (~100 ms a frame through the tunnel, measured:
        `checks/realtime.py`). Its stream opens at once, so none of its step previews is missed. Only a plain live
        image frame goes ahead (one at a time, never replaced: it is at most one frame older than the newest input,
        and its version says so); a warm-up, panels, a variation, a Flow re-root and anything else wait for the end."""
        version, epoch, state, p = self.version, self.epoch, dict(self.state), self.profile
        if p is None or not state or state.get("template") not in (None, p.name) or p.missing(state) or self.halted == version \
                or self.warmed != (p.name, self._key(state)[1]) or not (state.get("prompt") or "").strip() \
                or p.kind != "image" or p.panels(state) != 1 or state.get("variation"):
            return
        state = self._dialed(state)
        flowing = bool(state.get("flow")) and p.flows()
        if flowing and self.stream and not self.stream["session"] and len(self.stream["frames"]) >= FLOW_ROOT:
            return  # the next frame re-roots on the picture of the one still running
        if flowing:
            state = self._flow(state)
        built = p.build(state, settle=False, partial=True) if flowing else None
        job = await self.submit(state, settle=False, lane="live", partial=True, built=built)
        inbox: asyncio.Queue = asyncio.Queue()
        self.ahead = {"version": version, "epoch": epoch, "state": state, "flowing": flowing, "built": built, "job": job,
                      "inbox": inbox, "reader": asyncio.create_task(self._pump(job, inbox))}

    async def _taken(self, epoch: int) -> dict[str, Any] | None:
        """The frame that went ahead, when it is still this epoch's and not cancelled; else it is dropped."""
        if self.presubmit:
            with contextlib.suppress(Exception):
                await self.presubmit
            self.presubmit = None
        ahead, self.ahead = self.ahead, None
        if ahead and (ahead["epoch"] != epoch or self.halted == ahead["version"] or ahead.get("canceled")):
            ahead["reader"].cancel()
            with contextlib.suppress(Exception):
                await ahead["job"].cancel()
            if ahead["flowing"]:
                self._flowed(ahead["state"], "stopped")
            return None
        return ahead

    def _cancel_ahead(self) -> None:
        if self.ahead and not self.ahead.get("canceled"):
            self.ahead["canceled"] = True
            asyncio.get_running_loop().create_task(self.ahead["job"].cancel())

    @staticmethod
    async def _pump(job: Any, inbox: asyncio.Queue) -> None:
        """A job's events into `inbox`, then its end (None); a refused stream as its error."""
        try:
            async with contextlib.aclosing(job.events()) as events:
                async for event in events:
                    await inbox.put(event)
        except Exception as error:
            await inbox.put(error)
        await inbox.put(None)

    async def _field(self, state: dict[str, Any], version: int, epoch: int) -> bool:
        """The seed field, while the settled picture sits: eight seed neighbours one tile at a time (Seeds), or eight
        variations of the live latent in one batched job (Variations: `field`, `vary_strength`). Whether it finished."""
        if state.get("field") == "variations" and self.profile.refines():
            vary = {"seed": state["seed"] + 1, "strength": float(state.get("vary_strength") or 0.4), "count": SEED_FIELD - 1}
            if await self._foreign():
                return False
            if (result := await self._render({**state, "vary": vary}, "vary", version, epoch)) != "done":
                await self._stopped(result)
                return False
            return True
        for offset in range(1, SEED_FIELD):
            if await self._foreign() or self.ladder:
                return False  # idle work yields: someone else is waiting for the GPU, or a rung was asked for
            if (result := await self._render({**state, "seed": state["seed"] + offset}, "seed", version, epoch)) != "done":
                await self._stopped(result)
                return False
        return True

    async def _speculate(self, state: dict[str, Any], version: int, epoch: int) -> None:
        """Render ahead while the loop rests, after the seed field: the next R tile (in Variations, where the field holds no
        seeds), the active knob's next position either way, and the next rung's first step when Graduate is likely (the
        Final's graph with its last sampler cut to one step, so its enhancer's words and the settle half are cached).
        Each lands in the frame cache, so the click finds a cached job and its picture at once. Any input stops it;
        it never runs while someone else waits."""
        jobs: list[tuple[dict[str, Any], Any]] = []
        if state.get("field") == "variations":
            jobs.append(({**state, "seed": state["seed"] + 1}, None))
        knob = next((k for k in self.profile.knobs() if k["address"] == state.get("knob")), None)
        if knob:
            now = float((state.get("values") or {}).get(knob["address"], knob["value"]))
            for by in (1, -1):
                at = round(now + by * knob["step"], 6)
                if knob["min"] <= at <= knob["max"]:
                    jobs.append(({**state, "values": {**(state.get("values") or {}), knob["address"]: at}}, None))
        if state.get("likely") and self.profile.refines():
            graph, takes = self.profile.build(state, settle=True, rung="final")
            if graph.get("gv:final", {}).get("class_type") == "KSampler":
                graph["gv:final"]["inputs"]["steps"] = 1
                jobs.append((state, (graph, takes)))
        for job, built in jobs:
            if await self._foreign() or self.ladder or await self._render(job, "spec", version, epoch, built=built) != "done":
                return

    async def _cal_key(self, profile: Profile) -> str:
        """What a calibration holds for: the template, its capability (a hash of its graph), the GPU, its model files."""
        if self.gpu is None:
            with contextlib.suppress(Exception):
                async with httpx.AsyncClient(timeout=5) as http:
                    self.gpu = (await http.get(self.comfy + "/system_stats")).json()["devices"][0]["name"]
        files = sorted({profile.swaps.get(v, v) for node in profile.wf.values() for v in node["inputs"].values()
                        if isinstance(v, str) and v.endswith(MODEL_FILES)})
        capability = hashlib.sha256(json.dumps(profile.wf, sort_keys=True).encode()).hexdigest()[:12]
        return f"{profile.name}|{capability}|{self.gpu}|{','.join(files)}"

    async def _calibrate(self, state: dict[str, Any], version: int, epoch: int) -> None:
        """Once per template, GPU and model files, while the loop is idle: 1- and 2-step live jobs at two sizes, fresh
        words and seed each (nothing cached), fitted to a fixed cost plus ms per step per megapixel. Any input stops it;
        it runs again at the next rest."""
        if self.profile.kind != "image" or self.profile.live_steps is None or not self.profile.tuned \
                or (key := await self._cal_key(self.profile)) in self.calibration:
            return
        points = []
        for steps, mp in ((1, 0.16), (2, 0.16), (1, 0.25), (2, 0.25)):
            nonce = time.time_ns()
            started = time.perf_counter()
            job = {**state, "prompt": f"{state.get('prompt') or 'a photograph'} {nonce}", "seed": nonce % 2**31,
                   "live_steps": steps, "live_mp": mp, "fast": None, "panels": None}
            if await self._render(job, "calib", version, epoch) != "done":
                return
            points.append((steps, mp, round(1000 * (time.perf_counter() - started))))
        self.calibration[key] = {**fit(points), "points": points, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with contextlib.suppress(OSError):
            self.calibration_file.parent.mkdir(parents=True, exist_ok=True)
            self.calibration_file.write_text(json.dumps(self.calibration, indent=1))
        self.emit({"kind": "calibrated", "template": self.profile.name, **self.calibration[key]})
        if self.live_ms:
            self._meter(self._dialed(state))

    def _dialed(self, state: dict[str, Any]) -> dict[str, Any]:
        """The live steps and size the speed dial's target allows on this GPU (the creator's own live steps win)."""
        cal = self.calibration.get(self.cal_key or "")
        if not cal or state.get("live_steps") or state.get("speed") not in TARGETS or self.profile.kind != "image" \
                or self.profile.live_steps is None:
            return state
        floor = self.profile.live_steps
        steps, mp = pick(cal, TARGETS[state["speed"]], floor, min(int(self.profile.size["steps"][0]), 2 * floor))
        return {**state, "live_steps": steps, "live_mp": mp}

    def _meter(self, state: dict[str, Any]) -> None:
        """The GPU meter after each live job: its time, what the calibration predicts for its steps and size, ms per
        step, and whether the model ran resident (within OFFLOADED of the prediction) or partly offloaded."""
        cal = self.calibration.get(self.cal_key or "")
        steps, mp = state.get("live_steps") or self.profile.live_steps, self.profile.live_size(state)
        frame = {"kind": "meter", "ms": self.live_ms, "steps": steps, "mp": mp, "speed": state.get("speed"), "v": state.get("v")}
        if cal and steps:
            predicted = cal["fixed"] + cal["per_step_mp"] * steps * mp
            frame.update(predicted=round(predicted), ms_per_step=round(cal["per_step_mp"] * mp), resident=self.live_ms <= OFFLOADED * predicted)
        self.emit(frame)

    async def _stopped(self, result: str) -> None:
        """After someone else's job took the GPU: submit nothing until ComfyUI's queue holds only
        our own work, then go again with the newest state."""
        if result != "busy":
            return
        while (ahead := await self._foreign()) and not self._yields(ahead):
            self._busy(ahead)
            await asyncio.sleep(0.25)
        self.changed.set()

    async def _rests(self, ms: int) -> bool:
        """Whether the creator leaves everything alone for `ms`."""
        try:
            await asyncio.wait_for(self.changed.wait(), timeout=ms / 1000)
            return False
        except asyncio.TimeoutError:
            return True

    async def _render(self, state: dict[str, Any], stage: str, version: int, epoch: int,
                      built: tuple[dict[str, Any], list] | None = None, tag: dict[str, Any] | None = None,
                      profile: Profile | None = None, ahead: dict[str, Any] | None = None) -> str:
        """Run one job of a lane (`ahead`: a live job already submitted, with its stream) and stream it: `done`; `stopped` when newer input made it moot
        (any change stops a settle or seed tile at once; a new template or picture stops the
        live and warm jobs; any input at all stops a rung, which runs again once the loop is idle);
        `busy` when someone else's job holds the GPU. A live image's last step preview is its frame
        (sharp with a tiny decoder), so live never waits on the output's download. Split panels
        (`state.panels`) land together with the job's output."""
        from comfy_sdk import OutputReady, Preview, Progress, StatusChange

        profile = profile or self.profile  # a rung streams as its snapshot's template
        seen = [time.perf_counter(), "gpu", False]  # the last event's time, what a silence there means, said
        panels = profile.panels(state) if stage in ("live", "warm") else 1

        def moot() -> bool:
            if stage == "rung":
                return not self.idle.is_set()
            # A clip's draft takes seconds: one for older words stops, as a settle does, so the newest words go next.
            stale = stage in ("settle", *IDLE_LANES) or (stage == "live" and profile.kind == "video")
            return self.epoch != epoch or (stale and self.version != version) or self.halted == version

        async def watch() -> None:  # cancels at once, even mid-step or mid-load when no event comes
            while not moot():
                await asyncio.sleep(0.05)
                if stage not in (*IDLE_LANES, "rung") and not seen[2] and time.perf_counter() - seen[0] > QUIET_SAY:
                    seen[2] = True  # a running job gone silent: say what it waits on, once per silence
                    self.emit({"kind": "phase", "phase": "waiting", "why": seen[1], "title": profile.title, "stage": stage, "v": state.get("v")})
                if stage == "live" and preview_only and Live.go_ahead and self.version != version and self.ahead is None \
                        and (self.presubmit is None or self.presubmit.done()):
                    self.presubmit = asyncio.create_task(self._presubmit())  # newer input: its frame queues behind this one
            with contextlib.suppress(Exception):
                await job.cancel()

        started = time.perf_counter()
        preview_only = stage in ("live", "warm", *IDLE_LANES) and stage != "vary" and profile.kind == "image" and panels == 1 \
            and not state.get("variation")
        job = ahead["job"] if ahead else await self.submit(state, settle=stage == "settle", lane=stage, partial=preview_only, built=built)
        tag = {"stage": stage, "v": state.get("v"), **(tag or {})}
        if self.tracer:
            self.trace(stage, job.id, started, "submit", v=state.get("v"))
            asyncio.create_task(self.residency(stage, job.id, started, "before"))
        first, last, landed = None, None, False
        watcher = asyncio.create_task(watch())
        try:
            async with self._owned(job, ahead) as events:
                async for event in events:
                    if self.tracer:
                        self.trace(stage, job.id, started, type(event).__name__, **{k: v for k, v in vars(event).items()
                                   if k in ("status", "queue_position", "step", "steps", "current_node", "nodes_done", "nodes_total", "node_id")})
                    if isinstance(event, Progress):
                        node = (self.built.get(event.current_node or "") or {}).get("class_type", "")
                        loads = "Load" in node or ("Sampler" in node and not event.step)  # models load inside the sampler
                        seen[1] = "loading" if loads else "gpu"
                    seen[0], seen[2] = time.perf_counter(), False
                    if moot():
                        return "stopped"
                    ms = round(1000 * (time.perf_counter() - started))
                    if isinstance(event, StatusChange) and event.status == "failed":
                        await job.refresh()
                        raw = " ".join(str(getattr(job.error, "message", "") or "").split())
                        self.emit({"kind": "error", **tag, "message": plain(raw), "detail": raw[:240]})
                        return "stopped"
                    if isinstance(event, StatusChange) and event.status == "queued" and event.queue_position:
                        ahead = await self._foreign()
                        if not ahead or (stage not in IDLE_LANES and self._yields(ahead)):
                            continue  # only our own cancelled job, or another VorpalFusion's idle work, is ahead
                        await job.cancel()
                        if stage not in (*IDLE_LANES, "rung"):  # our own idle work just stops; it never says the GPU is shared
                            self._busy(ahead)
                        return "busy"
                    if stage in ("warm", *IDLE_LANES) or (stage == "live" and state.get("variation")):
                        if isinstance(event, Preview) and event.data:
                            last = data_url(event.content_type, event.data)
                        continue
                    if isinstance(event, Progress) and (event.steps or 0) > 1:  # a sampler's steps, not a loader's 0/1
                        self.emit({"kind": "step", **tag, "step": event.step, "steps": event.steps, "ms": ms})
                    elif isinstance(event, Preview) and event.data:
                        first = first or ms
                        last = data_url(event.content_type, event.data)
                        self.emit({"kind": "frame", **tag, "ms": ms, "first": first, "src": last})
                    elif isinstance(event, OutputReady) and not preview_only and panels == 1 and not landed:
                        landed = True
                        await self._final(job.id, event.output, tag, started, first)
            if moot():
                return "stopped"
            ms = self.last_ms = round(1000 * (time.perf_counter() - started))
            if preview_only and stage in ("live", "seed", "spec") and (built is None or stage == "live"):
                # Every live-graph picture is kept by its condition: a later live job ComfyUI serves from cache
                # streams no preview, and its picture is the one this loop already made (a seed tile, a knob position
                # rendered ahead, an earlier frame). A Flow frame is its chain's, never served by condition.
                key = self._frame_key(state)
                if last and built is None:
                    self.frames[key] = last
                    self.frames.move_to_end(key)
                    while len(self.frames) > KEPT_FRAMES:
                        self.frames.popitem(last=False)
                last = last or (self.frames.get(key) if built is None else None)
                if stage == "live":
                    self.last_src = last
                if stage == "seed" and last:
                    self.emit({"kind": "seed", "seed": state["seed"], "ms": ms, "src": last, "v": state.get("v")})
                elif stage == "live" and last:
                    self.emit({"kind": "final", **tag, "ms": ms, "first": first, "media": "image", "src": last})
            elif stage == "vary":
                await self._panels(job, state, tag, started, first, field=True)
            elif stage in IDLE_LANES:
                pass
            elif panels > 1:
                await self._panels(job, state, tag, started, first)
            elif not landed:  # the stream can end before an output event; the finished job holds the result
                await job.refresh()
                for output in job.outputs[:1]:
                    await self._final(job.id, output, tag, started, first)
            if tag.get("enhanced"):
                await self._enhanced(job.id, state, tag)
            return "done"
        finally:
            watcher.cancel()
            if self.tracer:
                self.trace(stage, job.id, started, "end")
                asyncio.create_task(self.residency(stage, job.id, started, "after"))

    @contextlib.asynccontextmanager
    async def _owned(self, job: Any, ahead: dict[str, Any] | None = None):
        """A job's event stream, owned: the stream is always closed (the proxy allows only a few
        open streams), a refused stream falls back to polling the job, and a job left any way but
        finished is cancelled, so nothing this loop submits is ever left queued. A job that went
        ahead brings the stream it opened when it was submitted."""
        from comfy_sdk import StatusChange

        finished = False
        inbox: asyncio.Queue = ahead["inbox"] if ahead else asyncio.Queue()

        async def stream():
            """The stream's events; while it says nothing, the job itself is asked whether it ended. A job
            ComfyUI serves from cache can end before the proxy's stream connects, and the stream then
            waits 15 s for an end it already missed (checks/stall.py)."""
            nonlocal finished
            while True:
                try:
                    item = await asyncio.wait_for(inbox.get(), QUIET_POLL)
                except asyncio.TimeoutError:
                    await job.refresh()
                    if job.status in TERMINAL:
                        yield StatusChange(status=job.status)
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, Exception):
                    if getattr(item, "code", None) != "too_many_streams":  # the SDK's typed error for a refused stream
                        raise item
                    while job.status not in TERMINAL:  # a refused stream: poll instead
                        await asyncio.sleep(0.05)
                        await job.refresh()
                    break
                yield item
                if isinstance(item, StatusChange) and item.status in TERMINAL:
                    break
            finished = True

        reader = ahead["reader"] if ahead else asyncio.create_task(self._pump(job, inbox))
        events = stream()
        try:
            yield events
        finally:
            await events.aclose()
            reader.cancel()  # closes the endpoint's stream now, not when collected
            with contextlib.suppress(BaseException):
                await reader
            if not finished:
                with contextlib.suppress(Exception):
                    await job.cancel()

    async def _final(self, job_id: str, output: Any, tag: dict[str, Any], started: float, first: int | None) -> None:
        """The finished output. A settled picture shows as a JPEG (quality 92) ComfyUI makes beside the
        GPU from its own PNG (`/view?preview=`), a seventh of the bytes over the wire; the original is
        fetched only when Keep asks for it (`original`, `GET /output/<job>`)."""
        kind = output.content_type or "image/png"
        frame = {"kind": "final", **tag, "first": first, "media": "video" if kind.startswith("video") else "image"}
        data = None
        if tag["stage"] == "rung":  # shown from the sidecar by URL (a 4K picture is megabytes), kept for Keep
            self.outputs[job_id] = output
            while len(self.outputs) > KEPT_OUTPUTS:
                self.outputs.popitem(last=False)
            view = await self._jpeg(output) if kind.startswith("image") else None
            view = view or ((await asyncio.to_thread(jpeg, await output.to_bytes()), "image/jpeg") if kind.startswith("image") else (await output.to_bytes(), kind))
            self.views[job_id] = view
            while len(self.views) > KEPT_OUTPUTS:
                self.views.popitem(last=False)
            size = await asyncio.to_thread(dimensions, view[0]) if kind.startswith("image") else None
            self.emit({**frame, "ms": round(1000 * (time.perf_counter() - started)), "src": f"view/{job_id}",
                       "original": f"/output/{job_id}", "job": job_id, "size": size})
            return
        if tag["stage"] == "settle" and kind.startswith("image"):
            self.outputs[job_id] = output
            while len(self.outputs) > KEPT_OUTPUTS:
                self.outputs.popitem(last=False)
            frame["original"], frame["job"] = f"/output/{job_id}", job_id
            if found := await self._jpeg(output):
                data, kind = found
            if data is None:  # an endpoint without the preview: download the original and make the JPEG here
                data, kind = await asyncio.to_thread(jpeg, await output.to_bytes()), "image/jpeg"
        else:
            data = await output.to_bytes()
            if tag["stage"] == "settle":  # a settled clip, kept for Keep and for the ladder's next rung
                self.outputs[job_id], frame["original"], frame["job"] = output, f"/output/{job_id}", job_id
                while len(self.outputs) > KEPT_OUTPUTS:
                    self.outputs.popitem(last=False)
        self.emit({**frame, "ms": round(1000 * (time.perf_counter() - started)), "src": data_url(kind, data)})

    async def _jpeg(self, output: Any) -> tuple[bytes, str] | None:
        """The output as a JPEG (quality 92) ComfyUI makes beside the GPU from its own PNG, or None."""
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=60) as http:
                r = await http.get(f"{self.comfy}/view", params={"filename": output.name, "type": "temp", "preview": "jpeg;92"})
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return r.content, "image/jpeg"
        return None

    async def _panels(self, job: Any, state: dict[str, Any], tag: dict[str, Any], started: float, first: int | None,
                      field: bool = False) -> None:
        """A batch's pictures, panel by panel, as small JPEGs ComfyUI makes beside the GPU; panel 1 is also the frame."""
        await job.refresh()
        srcs = []
        async with httpx.AsyncClient(timeout=10) as http:
            for output in [o for o in job.outputs if (o.content_type or "").startswith("image")]:
                data = kind = None
                with contextlib.suppress(httpx.HTTPError):
                    r = await http.get(f"{self.comfy}/view", params={"filename": output.name, "type": "temp", "preview": "jpeg;85"})
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                        data, kind = r.content, "image/jpeg"
                if data is None:
                    data, kind = await output.to_bytes(), output.content_type
                srcs.append(data_url(kind, data))
        if srcs and field:  # the seed field's other mode: tiles (seed, index) of one batch
            self.emit({"kind": "variations", "ms": round(1000 * (time.perf_counter() - started)), "seed": state["vary"]["seed"],
                       "strength": state["vary"]["strength"], "srcs": srcs, "v": state.get("v")})
        elif srcs:
            ms = round(1000 * (time.perf_counter() - started))
            self.emit({"kind": "final", **tag, "ms": ms, "first": first, "media": "image", "src": srcs[0], "panels": len(srcs)})
            self.emit({"kind": "panels", **tag, "ms": ms, "seed": state.get("seed"), "srcs": srcs})

    async def _enhanced(self, job_id: str, state: dict[str, Any], tag: dict[str, Any]) -> None:
        """What the model read when an enhancer rewrote the words: the longest text a node of the job
        showed (ComfyUI's history keeps each text preview) that is not the words as written."""
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=5) as http:
                outputs = (await http.get(f"{self.comfy}/history/{job_id}")).json()[job_id]["outputs"]
            wrote = state.get("prompt", "")
            # not the words as written, nor the words with a layout table appended, nor the table alone
            texts = [t for out in outputs.values() for t in out.get("text", []) if isinstance(t, str) and t.strip()
                     and not t.strip().startswith((wrote.strip() or "\0", "[", "{"))]
            if texts:
                self.emit({"kind": "enhanced", "wrote": wrote, "read": max(texts, key=len), "v": state.get("v"), "id": tag.get("id")})

    async def _climbing(self) -> None:
        """The ladder's lane: the rungs asked for, one at a time, each only while the loop is idle. Any input
        stops a running rung (live keeps working); it runs again from the top once the loop is idle."""
        while True:
            await self.asked.wait()
            self.asked.clear()
            while self.ladder:
                await self.idle.wait()
                ask = self.ladder[0]
                try:
                    result = await self._rung(ask)
                except Exception as error:  # a rung that cannot run says so and leaves the ladder
                    raw = f"{type(error).__name__}: {error}"
                    self.emit({"kind": "error", "stage": "rung", "id": ask["id"], "rung": ask["rung"], "message": plain(raw), "detail": raw[:240]})
                    result = "failed"
                if result == "busy":
                    await asyncio.sleep(1)
                elif result != "stopped" and self.ladder and self.ladder[0] is ask:
                    self.ladder.pop(0)

    def path(self, profile: Profile, rung: str) -> str:
        """How a rung climbs on this template: hi-res by its measured path (`refine`, tiled with core nodes, or a
        tool such as `seedvr2`), a clip's `smooth` by the interpolation tool."""
        if rung == "smooth":
            return "interpolate"
        if rung in HIRES:
            chosen = LADDER["hires"].get(profile.name, LADDER["hires"]["default"])
            return chosen if chosen != "refine" or profile.refines() else "seedvr2"
        return "template"

    async def _rung(self, ask: dict[str, Any]) -> str:
        """One rung of a snapshot: the same words, seed, pictures and settings, more fidelity. `final` and the tiled
        hi-res path build on the template's own graph; a tool rung (`seedvr2` hi-res, `interpolate` for a clip) runs
        Saddle's tool template on the output the snapshot already has (`from`)."""
        state, rung = ask["state"], ask["rung"]
        if (flow := self.flows.get((state.get("template"), state.get("prompt"), state.get("seed")))) and not isinstance(state.get("flow"), dict):
            state = {**state, "flow": flow}  # a Flow's settled picture climbs from its own chain
        profile = await self._profile(state["template"])
        path = ask.get("path") or self.path(profile, rung)
        tag = {"rung": rung, "id": ask["id"], "path": path, "enhanced": rung == "final" and bool(profile.enhancers()) and not isinstance(state.get("flow"), dict)}  # a Flow chain carries every frame's stale text
        if path in ("template", "refine"):
            built = profile.build(state, settle=True, rung=rung)
        else:
            built, extra = await self._tool(path, rung, ask)
            tag.update(extra)
        self.emit({"kind": "rung", "phase": "start", **tag})
        return await self._render(state, "rung", 0, 0, built=built, tag=tag, profile=profile)

    async def _tool(self, path: str, rung: str, ask: dict[str, Any]) -> tuple[tuple[dict[str, Any], list], dict[str, Any]]:
        """A tool template's graph over the output a snapshot has: its picture (or clip) slot takes that output, its
        multiplier slot the scale the rung needs (a hi-res rung's long side over the picture's), its seed the snapshot's."""
        output = self.outputs.get(ask.get("from") or "")
        if output is None:
            raise LookupError("unknown picture: the rung below is no longer held; graduate it again")
        data = await output.to_bytes()
        tool = await self._profile(LADDER["tools"][path])
        if rung in HIRES:
            multiplier = round(HIRES[rung] / max(await asyncio.to_thread(dimensions, data)), 3)
        else:
            multiplier = LADDER.get("interpolate_multiplier", 2)
        scale = next((slot["address"] for slot in tool.slots if "multiplier" in (slot.get("label") or slot.get("name") or "")
                      and slot.get("type") in ("INT", "FLOAT")), None)
        value = int(multiplier) if scale and tool._slot(scale)["type"] == "INT" else multiplier
        graph = tool._base({"prompt": "", "seed": ask["state"].get("seed", 0), "values": {scale: value} if scale else {}})
        for node_id in [n for n, node in graph.items() if node["class_type"] == "ImageCompare"]:
            del graph[node_id]  # a before/after viewer: no output the ladder wants
        sid = self.register(data, output.content_type or "image/png")
        address = next(iter(media_slots(tool.wf, tool.slots)))  # the tool's picture or clip slot
        takes = [(node, key, sid) for node, key in tool._slot(address)["targets"] if node in graph]
        return (graph, takes), {"tool": tool.name, "multiplier": multiplier}

    async def save(self, keep: dict[str, Any]) -> dict[str, Any]:
        """Keep, standalone: the highest rung's original picture (or clip) on disk with a JSON beside it (words,
        seed, template, values, pictures by id with their library parents, layout boxes and pin, lineage, the rung's recipe)."""
        snapshot = keep.get("snapshot") or {}
        output = self.outputs.get(keep.get("job") or "")
        if output is not None:
            data, kind = await output.to_bytes(), output.content_type or "image/png"
        elif str(keep.get("frame", "")).startswith("data:"):  # a snapshot only ever shown live: its frame
            head, body = keep["frame"].split(",", 1)
            data, kind = base64.b64decode(body), head[5:].split(";")[0]
        else:
            raise LookupError("unknown picture: nothing held for that snapshot; let it settle first")
        self.keep.mkdir(parents=True, exist_ok=True)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{snapshot.get('seed', 0)}-{snapshot.get('rung') or 'live'}"
        path = self.keep / f"{stem}.{kind.split('/')[-1].replace('jpeg', 'jpg')}"
        path.write_bytes(data)
        sources = [{**s, **({"parent": self.sources[s["id"]]["parent"]} if "parent" in self.sources.get(s.get("id"), {}) else {})}
                   for s in snapshot.get("sources") or []]
        record = {"schema": "gpu-vision.keep", "file": path.name, "words": snapshot.get("prompt"), "seed": snapshot.get("seed"),
                  "template": snapshot.get("template"), "values": snapshot.get("values") or {}, "sources": sources,
                  "panel": snapshot.get("panel"), "rung": snapshot.get("rung") or "live", "recipe": snapshot.get("recipe"),
                  "boxes": snapshot.get("boxes") or [], "pin": snapshot.get("pin"), "model": snapshot.get("model"), "lineage": keep.get("lineage") or [], "kept": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (self.keep / f"{stem}.json").write_text(json.dumps(record, indent=1))
        return {"path": str(path), "json": str(self.keep / f"{stem}.json")}

    async def download(self, item: dict[str, Any]) -> tuple[bytes, str, str]:
        """Download, apart from Keep: a picture at its rung as the original ComfyUI made (settled and up), or a live
        picture's frame as a PNG; a clip as its MP4. A PNG carries ComfyUI's metadata: `prompt`, the API graph that made
        it (ComfyUI's own chunk when its file has one, else the graph this sidecar submitted or, for a live frame, the
        live graph rebuilt with its decode), so dropping it on ComfyUI opens that workflow; and `gpu-vision`: words,
        seed, template, values, pictures by id, panel, rung, lineage. Returns (bytes, content type, file name)."""
        snapshot = item.get("snapshot") or {}
        output, graph = self.outputs.get(item.get("job") or ""), self.graphs.get(item.get("job") or "")
        if output is not None:
            data, kind = await output.to_bytes(), output.content_type or "image/png"
        elif (snapshot.get("rung") or "live") in ("live", "draft") and str(item.get("frame", "")).startswith("data:"):
            head, body = item["frame"].split(",", 1)
            data, kind = base64.b64decode(body), head[5:].split(";")[0]
            if snapshot.get("template") and kind.startswith("image"):
                with contextlib.suppress(Exception):
                    graph = (await self._profile(snapshot["template"])).build({**snapshot, "v": None}, settle=False)[0]
        else:
            raise LookupError("unknown picture: the original of that rung is no longer held; let it settle (or graduate it) again")
        if not kind.startswith("image"):
            return data, kind, download_name(snapshot, kind.split("/")[-1].replace("quicktime", "mov"))
        sources = [{**s, **({"parent": self.sources[s["id"]]["parent"]} if "parent" in self.sources.get(s.get("id"), {}) else {})}
                   for s in snapshot.get("sources") or []]
        record = {"schema": "gpu-vision.picture", "words": snapshot.get("prompt"), "seed": snapshot.get("seed"), "template": snapshot.get("template"),
                  "values": snapshot.get("values") or {}, "sources": sources, "panel": snapshot.get("panel"), "rung": snapshot.get("rung") or "live",
                  "recipe": snapshot.get("recipe"), "variation": snapshot.get("variation"), "boxes": snapshot.get("boxes") or [],
                  "pin": snapshot.get("pin"), "model": snapshot.get("model"), "lineage": item.get("lineage") or []}
        png = data if data[:8] == b"\x89PNG\r\n\x1a\n" else await asyncio.to_thread(as_png, data)
        chunks = {"gpu-vision": json.dumps(record)}
        if graph:
            chunks["prompt"] = json.dumps({k: v for k, v in graph.items() if k != "gv:label"})
        return png_text(png, chunks, keep=() if graph else ("prompt",)), "image/png", download_name(snapshot, "png")

    async def bundle(self, items: list[dict[str, Any]]) -> bytes:
        """Several downloads as one zip, with `manifest.json` listing each file and its record."""
        out, names, manifest = io.BytesIO(), set(), []
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
            for item in items:
                try:
                    data, kind, name = await self.download(item)
                except LookupError as error:
                    manifest.append({"snapshot": (item.get("snapshot") or {}).get("id"), "error": str(error)})
                    continue
                stem, ext = name.rsplit(".", 1)
                n = 1
                while name in names:
                    n += 1
                    name = f"{stem}-{n}.{ext}"
                names.add(name)
                archive.writestr(name, data)
                snap = item.get("snapshot") or {}
                manifest.append({"file": name, "type": kind, "snapshot": snap.get("id"), "words": snap.get("prompt"), "seed": snap.get("seed"),
                                 "template": snap.get("template"), "rung": snap.get("rung") or "live", "panel": snap.get("panel"),
                                 "values": snap.get("values") or {}, "sources": snap.get("sources") or [], "lineage": item.get("lineage") or []})
            archive.writestr("manifest.json", json.dumps({"schema": "gpu-vision.download", "files": manifest}, indent=1))
        return out.getvalue()

    async def _foreign(self) -> list[dict[str, Any]]:
        """The jobs in ComfyUI's queue that are not ours winding down after a cancel, each with its
        owner as its label names it: `{"id", "app": "gpu-vision" | None, "lane"}`.

        v2 reports a cancelled job `canceled` at once while ComfyUI finishes its current
        step, so the truth is ComfyUI's own queue; each queue item carries its graph, and the
        graph its label. One GET per check; a client of the endpoint's status stream could track
        this without polling.
        """
        async with httpx.AsyncClient(timeout=2) as http:
            queue = (await http.get(self.comfy + "/queue")).json()
        found = []
        for item in queue.get("queue_running", []) + queue.get("queue_pending", []):
            label = str(((item[2] if len(item) > 2 and isinstance(item[2], dict) else {}).get("gv:label") or {}).get("inputs", {}).get("value", ""))
            app, owner, lane = (label.split() + [None, None, None])[:3]
            if item[1] in self.mine or owner == self.owner:
                continue
            found.append({"id": item[1], "app": app if app == "gpu-vision" else None, "lane": lane})
        return found

    @staticmethod
    def _yields(ahead: list[dict[str, Any]]) -> bool:
        """Whether everything ahead is another VorpalFusion's idle work (a seed tile): it ends within a
        tile and its own loop stops filling the field once ours is waiting, so ours queues behind it."""
        return all(job["app"] == "gpu-vision" and job["lane"] in IDLE_LANES for job in ahead)

    def _busy(self, ahead: list[dict[str, Any]]) -> None:
        owner = "gpu-vision" if all(job["app"] == "gpu-vision" for job in ahead) else None
        self.emit({"kind": "busy", "ahead": len(ahead), "owner": owner})


CORS = [(b"access-control-allow-origin", b"*"), (b"access-control-allow-headers", b"content-type"),
        (b"access-control-allow-methods", b"GET, POST, OPTIONS"), (b"access-control-expose-headers", b"x-filename")]  # the module runs in an opaque-origin sandbox


async def read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def reply(send, status: int, body: bytes = b"", kind: bytes = b"application/json", name: str | None = None) -> None:
    named = [(b"content-disposition", f'attachment; filename="{name}"'.encode()), (b"x-filename", name.encode())] if name else []
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", kind), *named, *CORS]})
    await send({"type": "http.response.body", "body": body})


def listing(rows: list[dict[str, Any]], detected: bool = False) -> list[dict[str, Any]]:
    """The templates that make a picture or a clip, by title, grouped Image / Edit / Clip; `untuned` outside the TUNED
    families, `checking` while the sidecar still checks one, `detected` when the sidecar found it (no Saddle to list it)."""
    groups = {"Image": 0, "Edit": 1, "Clip": 2}
    found = [{"name": row["name"], "title": title(row), "untuned": not TUNED.match(row["name"]), "checking": bool(row.get("checking")), "detected": detected,
              "group": "Clip" if row.get("kind") == "video" else "Edit" if row.get("job") == "edit" else "Image"}
             for row in rows if row.get("role") == "creation" and row.get("kind") in ("image", "video")
             and row.get("compatibility", row.get("validation_status")) != "blocked"]
    return sorted(found, key=lambda row: (groups[row["group"]], row["title"].lower()))


async def app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return
    live: Live = scope["app_state"]
    path, method = scope["path"], scope.get("method")
    if method == "OPTIONS":
        await reply(send, 204)
    elif path in ("/", "/host.html") and method == "GET":
        page = "live.html" if path == "/" else "host.html"
        await reply(send, 200, (HERE / page).read_bytes(), b"text/html; charset=utf-8")
    elif path == "/templates" and method == "GET":
        await reply(send, 200, json.dumps(listing(await live.catalog(), detected=live.detecting)).encode())
    elif path == "/template" and method == "POST":
        try:
            described = await live.use(json.loads(await read_body(receive))["name"])
        except (TemplateError, ValueError, OSError) as error:  # comfy-cli could not build the record
            await reply(send, 422, json.dumps({"message": f"VorpalFusion could not read that template: {error}"}).encode())
        else:
            await reply(send, 200, json.dumps(described).encode())
    elif path == "/state" and method == "POST":
        live.update(json.loads(await read_body(receive)))
        await reply(send, 204)
    elif path == "/source" and method == "POST":
        kind = dict(scope["headers"]).get(b"content-type", b"image/png").decode()
        data = await read_body(receive)
        if not kind.startswith("image/") or not data:
            await reply(send, 415, json.dumps({"message": "That file is not a picture."}).encode())
        else:
            await reply(send, 200, json.dumps({"id": live.register(data, kind)}).encode())
    elif path == "/source/library" and method == "POST":
        try:
            await reply(send, 200, json.dumps(await live.library(json.loads(await read_body(receive)).get("rel", ""))).encode())
        except (ValueError, LookupError, httpx.HTTPError) as error:
            await reply(send, 404, json.dumps({"message": f"VorpalFusion could not fetch that library picture ({error})."}).encode())
    elif path.startswith("/library") and not live.saddle:  # no Saddle, no library: the page hides the drawer
        await reply(send, 404)
    elif path == "/library" and method == "GET":  # Saddle's library page, for the drawer (the module may not reach Saddle)
        query = dict(part.split("=", 1) for part in scope.get("query_string", b"").decode().split("&") if "=" in part)
        params = {key: value for key, value in query.items() if key in ("q", "board", "kind", "offset")}
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(f"{live.saddle}/api/results", params={"kind": "image", "limit": "60", **{k: unquote(v) for k, v in params.items()}})
            boards = (await http.get(f"{live.saddle}/api/boards")).json().get("boards", []) if "offset" not in params else []
        page = r.json() if r.status_code == 200 else {"items": []}
        items = [{"id": row["id"], "rel": row["rel"], "title": row.get("title") or row.get("prompt") or row["id"], "kind": row.get("kind"),
                  "thumb": f"library/thumb/{row['id']}"} for row in page.get("items", []) if LIBRARY_REL.fullmatch(row.get("rel", ""))]
        await reply(send, 200, json.dumps({"items": items, "boards": [{"id": b["id"], "name": b.get("name")} for b in boards],
                                           "next": page.get("next_offset")}).encode())
    elif path.startswith("/library/thumb/") and method == "GET" and re.fullmatch(r"[A-Za-z0-9_.-]+", path[15:]):
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(f"{live.saddle}/api/artifacts/{path[15:]}/thumbnail")
        await reply(send, r.status_code, r.content, r.headers.get("content-type", "image/jpeg").encode())
    elif path == "/download" and method == "POST":
        try:
            data, kind, name = await live.download(json.loads(await read_body(receive)))
            await reply(send, 200, data, kind.encode(), name)
        except LookupError as error:
            await reply(send, 404, json.dumps({"message": plain(str(error))}).encode())
    elif path == "/download/zip" and method == "POST":
        items = json.loads(await read_body(receive)).get("items") or []
        await reply(send, 200, await live.bundle(items), b"application/zip", f"gpu-vision-{len(items)}-pictures.zip")
    elif path == "/cancel" and method == "POST":
        live.halt()
        await reply(send, 204)
    elif path == "/graduate" and method == "POST":
        live.graduate(json.loads(await read_body(receive)))
        await reply(send, 204)
    elif path == "/keep" and method == "POST":
        try:
            await reply(send, 200, json.dumps(await live.save(json.loads(await read_body(receive)))).encode())
        except LookupError as error:
            await reply(send, 404, json.dumps({"message": plain(str(error))}).encode())
    elif path.startswith("/view/") and method == "GET" and path[6:] in live.views:
        data, kind = live.views[path[6:]]
        await reply(send, 200, data, kind.encode())
    elif path == "/source/use" and method == "POST":
        sid = json.loads(await read_body(receive)).get("id")
        known = live.sources.get(sid)
        await reply(send, 200 if known else 404, json.dumps({"id": sid, "type": known["type"]} if known else {}).encode())
    elif path.startswith("/source/") and method == "GET" and path[8:] in live.sources:
        source = live.sources[path[8:]]
        await reply(send, 200, source["data"], source["type"].encode())
    elif path.startswith("/output/") and method == "GET" and path[8:] in live.outputs:
        output = live.outputs[path[8:]]
        await reply(send, 200, await output.to_bytes(), (output.content_type or "image/png").encode())
    elif path == "/frames" and method == "GET":
        queue: asyncio.Queue = asyncio.Queue()
        live.listeners.add(queue)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-store"), *CORS]})
        try:
            while True:
                frame = await queue.get()
                await send({"type": "http.response.body", "body": f"data: {json.dumps(frame)}\n\n".encode(), "more_body": True})
        finally:
            live.listeners.discard(queue)
    else:
        await reply(send, 404)


async def serve(live: Live, port: int, handler=None) -> None:
    import uvicorn

    loop_task = asyncio.create_task(live.run())
    loop_task.add_done_callback(lambda task: task.cancelled() or task.exception() is None
                                or __import__("traceback").print_exception(task.exception()))

    async def bound(scope, receive, send):
        scope["app_state"] = live
        await (handler or app)(scope, receive, send)

    # The frame streams never end on their own: a stop must not wait for them.
    server = uvicorn.Server(uvicorn.Config(bound, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1))
    try:
        await server.serve()
    finally:
        loop_task.cancel()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8189", help="the endpoint's v2 API: comfy-api-proxy in front of ComfyUI, previews unthrottled (default: the proxy's own 8189)")
    parser.add_argument("--comfy", default="http://127.0.0.1:8188", help="ComfyUI itself, for its queue and templates (default: ComfyUI's own 8188)")
    parser.add_argument("--saddle", help="a Saddle server whose imported templates Live reads; without it, the endpoint's own templates")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--trace", help="append a CSV row per job event here (wall ms, lane, template, job, ms, event, fields)")
    parser.add_argument("--keep", help="where Keep saves, standalone (default ~/Pictures/VorpalFusion)")
    parser.add_argument("--go-ahead", action="store_true", help="queue the next live frame behind the running one: two jobs on the endpoint at once")
    args = parser.parse_args()
    Live.go_ahead = args.go_ahead
    os.environ["COMFY_BASE_URL"] = args.api
    from comfy_sdk import AsyncComfy

    async def start() -> None:
        await serve(Live(AsyncComfy(client_info="saddle-gpu-vision"), args.comfy, args.saddle, args.trace, args.keep), args.port)

    asyncio.run(start())


if __name__ == "__main__":
    main()
