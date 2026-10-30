"""A live profile for any template, from Saddle's own slot reading; no model is named here.

Live renders the template's graph smaller and with fewer steps, its enhancers off and a clip
short. The settle starts from the live frame where the graph allows it (a KSampler feeding the
decode): the live graph, which ComfyUI serves from cache, with its picture decoded, resized to
the template's size in pixel space and encoded again, then refined, so the picture sharpens
instead of recomposing. Elsewhere the settle renders the template as its authors tuned it.

Pictures go in by slot: each of the template's image slots takes one (the first is the source,
the rest references, labelled by the role Saddle's summary gives them). A template with no
image slot takes a source by swapping the empty latent its sampler starts from.

A template outside the tuned families (live.py's TUNED) gets the generic profile: its own
schedule at the live-steps rule below, the smallest live size, no speed dial.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from vf_templates import drop_media, job_size, media_roles, media_slots, optional_media, prompt_slot, seed_slot

LIVE_MEGAPIXELS = {"image": 0.25, "video": 0.1}
# Live sizes an image job may take (MP), each one shape per aspect, so a size never escapes the set: the speed dial
# picks one, and fast typing (keys under 120 ms apart) takes the next smaller one until the first pause.
LIVE_BUCKETS = (0.1, 0.16, 0.25, 0.36, 0.5)
LIVE_SECONDS = 2
# Knobs worth scrubbing live, by slot label, with ranges a person can drag through.
KNOBS = {"shift": (1.0, 10.0, 0.1), "cfg": (1.0, 8.0, 0.1), "guidance": (1.0, 10.0, 0.1), "strength_model": (-1.0, 2.0, 0.05),
         "strength": (-1.0, 2.0, 0.05)}
# Flow: each live frame starts from the last one's latent (measured on FLUX 3 Klein, README "Flow"):
# (steps, denoise) on the template's own 4-step schedule by how much the words changed; a leaky anchor toward the newest
# frame whose words changed; the chain restarts from its last picture every FLOW_ROOT frames so it never grows.
FLOW_LEVELS = {"same": (1, 0.0625), "word": (2, 0.5), "phrase": (3, 0.75)}
FLOW_ANCHOR = 0.85
FLOW_ROOT = 10
PIN_GRID = 256  # the pin's mask grid: about 2 px a cell at live size, stretched to the latent by the sampler
REFINE_DENOISE = 0.5  # how much the settle may redraw the live frame: detail, not composition
FINAL_MTP = ""  # the Final's prompt enhancer's mtp ("auto": speculative decoding with the checkpoint's MTP head); "" keeps the template's
FINAL_DENOISE = 0.3  # how much Final may redraw the settled frame (its enhancer's words, its full steps): detail only
HIRES_DENOISE = 0.3  # the hi-res refine: the template's own schedule, its last part, over the upscaled Final
HIRES = {"2k": 2048, "4k": 4096}  # the long side of each hi-res rung
# A clip's model attends with ComfyUI's INT8 attention unless its template picks an attention node of its own (FastH3
# does). Pika H3 Flex's 5 s clip at 0.98 MP on an RTX 5090: 12.8 s a step dense, 7.2 with it, the same picture (SSIM
# 0.82 and 0.69 on two prompts, where another seed scores 0.55); at a draft's 0.1 MP it neither helps nor hurts.
ATTENTION = {"class_type": "ModelAttentionBackend", "inputs": {"attention": "comfy kitchen attention"}}
ATTENTION_NODES = ("ModelAttentionBackend", "BlockSparseAttention")
TILE = {"tile_size": 512, "overlap": 64, "temporal_size": 64, "temporal_overlap": 8}  # ComfyUI's tiled VAE defaults
ASPECTS = [("1:1", 1.0), ("4:5", 0.8), ("3:4", 0.75), ("9:16", 9 / 16), ("16:9", 16 / 9), ("21:9", 21 / 9)]
SIZE_CLASSES = {"Draft": 0.25, "HD": 1.0, "Max": 2.0}  # of the template's own area; Max is a fixed 2x (a ceiling calibrated per GPU would fit each card)
DURATIONS = (2, 5, 10)  # seconds, the clip chips
ROLE_WORDS = ("style", "subject", "background")  # a reference's role chip, from the template's own words
LATENT = "latent"  # the source slot of a template with no image slot: the sampler's empty latent
ROLE_LABELS = {"first_frame": "first frame", "last_frame": "last frame", "mask": "mask"}
MODEL_FILES = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
WORDS = {"t2i": "text to image", "t2v": "text to video", "i2v": "image to video", "r2v": "reference to video",
         "flf2v": "first and last frame to video", "ltx": "LTX", "fasth3": "FastH3", "minimax": "MiniMax", "kv": "KV",
         "fastvideo": "", "image": "image", "video": "video"}


def _word(word: str) -> str:
    if word in WORDS:
        return WORDS[word]
    if re.fullmatch(r"\d+b|h\d", word):  # 9b, h3
        return word.upper()
    head = re.match(r"[a-z]*", word)[0]  # ltx2.5 -> LTX2.5
    return WORDS.get(head, head) + word[len(head):]


def title(record: dict[str, Any]) -> str:
    """A template's title: Saddle's label, or its name made readable when the label is the bare name."""
    label, name = record.get("label") or "", record["name"]
    if label and label != name:
        return label
    words = re.sub(r"(\d)_(\d)", r"\1.\2", name).split("_")[1:] or [name]  # ltx2_5 -> ltx2.5; drop image_/video_
    text = " ".join(word for word in map(_word, words) if word)
    return text[:1].upper() + text[1:]


def live_steps(steps: int) -> int:
    return max(2, steps // 2) if steps <= 4 else min(steps, max(4, round(steps / 4)))


def thinned(sigmas: str) -> str:
    """A hand-written schedule (`ManualSigmas`) at the live-steps count: the first knot, then every other one counted
    back from the last step. Whether a draft holds its picture this way is measured per template (the manifest's
    `thin_drafts`): Pika H3 Flex's six steps at four (1000/750/250/40) do, where three recompose it; LTX 2.5's two
    stages thinned recompose it too, so it keeps its own."""
    knots = [knot.strip() for knot in sigmas.split(",") if knot.strip()]
    n = len(knots) - 1
    if n < 2:
        return sigmas
    keep = sorted({0} | {max(0, n - 1 - 2 * j) for j in range(live_steps(n) - 1)})
    return sigmas if len(keep) >= n else ", ".join([knots[i] for i in keep] + [knots[-1]])


class Profile:
    def __init__(self, record: dict[str, Any]) -> None:
        self.name = record["name"]
        self.title = title(record)
        self.wf: dict[str, Any] = record["wf"]
        self.slots: list[dict[str, Any]] = record["inputs"]["slots"]
        self.prompt = prompt_slot(self.wf, self.slots)
        self.seed = seed_slot(self.slots)
        self.size = job_size(self.wf)
        self.kind = "video" if any("Video" in node["class_type"] for node in self.wf.values()
                                   if node["class_type"].startswith("Save")) else "image"
        # The image slots in order, each with the role Saddle's summary names (first frame, reference, ...).
        summary = {item["address"]: item.get("role") for item in (record.get("meta") or {}).get("media") or [] if isinstance(item, dict)}
        roles, optional = media_roles(self.wf, self.slots), set(optional_media(self.wf, self.slots))
        self.media: list[dict[str, Any]] = []
        for address in (address for address, kind in media_slots(self.wf, self.slots).items() if kind == "image"):
            role = summary.get(address) or roles.get(address, "")
            label = ROLE_LABELS.get(role) or ("source" if not self.media else f"reference {sum(m['label'].startswith('reference') for m in self.media) + 1}")
            self.media.append({"address": address, "role": role, "label": label, "optional": address in optional})
        self.edits = bool(self.media)  # a picture is required: the first slot
        if not self.media:
            self.media = [{"address": LATENT, "role": "source", "label": "source", "optional": True}]
        self.duration: dict[str, Any] | None = None
        self.moments()
        # Live steps, from a sweep on an RTX 5090 at a quarter megapixel against each template's full-step render:
        # a long schedule holds its picture at a quarter of its steps but not below 4 (Krea 2 falls apart at 2);
        # a short distilled one (4 steps or fewer) holds at half, at least 2 (FLUX.2 klein keeps likeness at 2).
        steps = int(self.size["steps"][0]) if "steps" in self.size else None
        self.live_steps = None if steps is None else live_steps(steps)
        # The installed precision Saddle stages for a missing file (precision variants).
        self.swaps = {gap["detail"].split("/")[-1]: gap["uses"]
                      for gap in record.get("compatibility", {}).get("gaps", []) if gap.get("kind") == "model_variant"}
        self.finetunes: list[dict[str, Any]] = []  # the Model choices the endpoint has for this template's files (the sidecar fills it)
        self.drafts: dict[str, str] = {}  # a video VAE file -> the tiny decoder a draft clip decodes with (the sidecar fills it)
        self.attention = False  # the endpoint has ATTENTION's node (the sidecar sets it)
        self.thin = False  # a draft thins its hand-written schedule (`thinned`; the sidecar sets it from the manifest)
        self.sized = False  # the endpoint has GetLatentSize: a live graph's output is the latent's size, not the latent as text
        self.tuned = True  # a measured family (the sidecar sets it): else the generic profile, at the smallest live size

    def start(self) -> str | None:
        """What the template's sampler starts from: `source` when a picture slot feeds its latent, `empty` when the
        latent is made from nothing; None when no sampler takes a latent, or one takes a saved latent."""
        pictures = {str(node) for slot in self.slots if slot["address"] in {m["address"] for m in self.media}
                    for node, _key in slot["targets"]}
        for node in self.wf.values():
            link = node["inputs"].get("latent_image")
            if "Sampler" not in node["class_type"] or not isinstance(link, list):
                continue
            behind, todo = set(), [str(link[0])]
            while todo:
                node_id = todo.pop()
                if node_id not in behind and node_id in self.wf:
                    behind.add(node_id)
                    todo += [str(v[0]) for v in self.wf[node_id]["inputs"].values() if isinstance(v, list) and len(v) == 2]
            if not any("LoadLatent" in self.wf[node_id]["class_type"] for node_id in behind):
                return "source" if behind & pictures else "empty"
        return None

    def enhancers(self) -> list[str]:
        """The slots that switch a prompt enhancer (an upsampler that rewrites the words) on or off."""
        return [slot["address"] for slot in self.slots if slot.get("type") == "BOOLEAN"
                and any(word in (slot.get("label") or slot.get("name") or "").lower() for word in ("enhance", "upsampler"))]

    def knobs(self) -> list[dict[str, Any]]:
        """The template's numbers worth scrubbing (shift, guidance, a LoRA's strength), never
        seed, steps or size, never a text model's sampling settings."""
        found = []
        for slot in self.slots:
            label = (slot.get("label") or slot.get("name") or "").lower()
            if slot.get("type") != "FLOAT" or label not in KNOBS or not isinstance(slot.get("current_value"), (int, float)):
                continue
            low, high, step = KNOBS[label]
            found.append({"address": slot["address"], "label": label.replace("_", " "), "value": slot["current_value"],
                          "min": low, "max": high, "step": step})
        return found

    def _at(self, node: str, key: str) -> dict[str, Any] | None:
        """The slot that sets `key` on `node`, if one does."""
        return next((slot for slot in self.slots if [node, key] in [list(t) for t in slot.get("targets", [])]), None)

    def sizing(self) -> dict[str, Any] | None:
        """One size control for the template: its own aspect choice (a node with an aspect enum and a megapixels
        input) as chips with a size class on its megapixels, or, where a latent's width and height are slots,
        aspect chips and a size class mapped to width x height at the template's area, rounded to the multiple
        its defaults use. `raw` names the slots it sets, which the drawer keeps under node plumbing."""
        for node_id, node in self.wf.items():
            aspect = next((slot for key in node["inputs"] if (slot := self._at(node_id, key)) and slot.get("enum")
                           and any(word in key for word in ("aspect", "resolution"))), None)
            area = self._at(node_id, "megapixels")
            if aspect and area:
                return {"kind": "choice", "aspect": aspect["address"], "choices": aspect["enum"], "value": aspect["current_value"],
                        "area": area["address"], "megapixels": area["current_value"], "min": area.get("min"), "max": area.get("max"),
                        "classes": SIZE_CLASSES, "raw": [aspect["address"], area["address"]]}
        if "megapixels" not in self.size:
            return None
        _mp, (node, key) = self.size["megapixels"]
        if key == "megapixels" and (area := self._at(node, key)):
            return {"kind": "area", "area": area["address"], "megapixels": area["current_value"], "min": area.get("min"),
                    "max": area.get("max"), "classes": SIZE_CLASSES, "raw": [area["address"]]}
        width, height = self._at(node, "width"), self._at(node, "height")
        if key != "width" or not width or not height:
            return None
        w, h = int(width["current_value"]), int(height["current_value"])
        return {"kind": "free", "width": width["address"], "height": height["address"], "value": [w, h], "area": w * h,
                "multiple": 32 if w % 32 == 0 and h % 32 == 0 else 16, "min": width.get("min") or 64,
                "max": min(width.get("max") or 4096, 4096), "aspects": ASPECTS, "classes": SIZE_CLASSES, "raw": [width["address"], height["address"]]}

    @staticmethod
    def wh(area: float, aspect: float, multiple: int, low: int = 64, high: int = 4096) -> tuple[int, int]:
        """Width x height at `area` pixels and `aspect`, each side a multiple of `multiple`."""
        fit = lambda side: max(low, min(high, round(side / multiple) * multiple))
        return fit(math.sqrt(area * aspect)), fit(math.sqrt(area / aspect))

    def clip_chips(self) -> dict[str, Any] | None:
        """A clip's duration and frame rate as chips: 2, 5 and 10 s; the frame rate's own choices (its enum,
        else the usual rates and its own)."""
        if self.kind != "video":
            return None
        found = {}
        for slot in self.slots:
            label = (slot.get("label") or slot.get("name") or "").lower()
            if slot.get("type") not in ("INT", "FLOAT") or slot.get("advanced"):
                continue
            if label in ("duration", "length", "seconds") and "duration" not in found:
                found["duration"] = {"address": slot["address"], "value": slot["current_value"], "choices": list(DURATIONS)}
            elif label in ("fps", "frame_rate", "fram_rate", "framerate") and "fps" not in found:
                rates = slot.get("enum") or sorted({24, 30, int(slot["current_value"])})
                found["fps"] = {"address": slot["address"], "value": slot["current_value"], "choices": rates}
        return found or None

    def audio(self) -> bool:
        """Whether the template's clip carries sound (a video node whose audio input is wired)."""
        return self.kind == "video" and any(isinstance(node["inputs"].get("audio"), list) for node in self.wf.values()
                                            if "Video" in node["class_type"])

    def _primitive(self, link: Any, through: bool = False) -> tuple[dict[str, Any] | None, bool]:
        """The slot of the number a link comes from, through math expressions: (slot, whether it passed one)."""
        while isinstance(link, list) and link[0] in self.wf:
            node = self.wf[link[0]]
            if slot := self._at(link[0], "value"):
                return slot, through
            link, through = next((v for k, v in node["inputs"].items() if isinstance(v, list) and k.startswith("values.")), None), True
        return None, through

    def moments(self) -> None:
        """For a clip, where each picture sits on its timeline: `at` 0 for a first frame, 1 for a last frame,
        a guide's own position (`moment`: the slot holding it, in seconds when an expression scales it to
        frames). A reference that is no frame gets a role chip (subject, style, background) from the
        template's own words."""
        duration = (self.clip_chips() or {}).get("duration")
        for item in self.media:
            if self.kind != "video":
                if item["role"] == "reference" or item["label"].startswith("reference") or "reference" in self.name:
                    item["chip"] = next((word for word in ROLE_WORDS if word in self.name.lower()), "subject")
                continue
            node = next((t[0] for slot in self.slots if slot["address"] == item["address"] for t in slot["targets"]), None)
            readers = [(other, key, value) for other in self.wf.values() for key, value in other["inputs"].items()
                       if isinstance(value, list) and value[0] == node]
            if item["role"] == "first_frame":
                item["at"] = 0
            elif item["role"] == "last_frame":
                item["at"] = 1
            for other, _key, _value in readers:
                index = other["inputs"].get("frame_idx")
                if isinstance(index, (int, float)) and "at" not in item:
                    item["at"] = 1 if index < 0 else 0 if index == 0 else None
                elif isinstance(index, list):
                    slot, seconds = self._primitive(index)
                    if slot:
                        item["moment"] = {"address": slot["address"], "value": slot["current_value"], "unit": "s" if seconds else "frame"}
                        if duration is None and seconds:  # the clip's length: the number its length input comes from
                            length = next((v for n in self.wf.values() for k, v in n["inputs"].items() if k == "length" and isinstance(v, list)), None)
                            duration = {"address": (found := self._primitive(length)[0]) and found["address"], "value": found and found["current_value"]}
            if "at" not in item and "moment" not in item and item["address"] != LATENT:
                words = f"{self.name} {item['role']}".lower()
                item["chip"] = next((word for word in ROLE_WORDS if word in words), "subject")
        self.duration = duration

    def settings(self) -> list[dict[str, Any]]:
        """Every slot as a typed control (number, switch, choice, text), the words, seed and
        pictures excepted; node plumbing (advanced slots, model files, file names) is marked
        `advanced`."""
        skip = {self.prompt, self.seed, *(item["address"] for item in self.media)}
        sized = set((self.sizing() or {}).get("raw", ()))  # the size control sets these: raw numbers are plumbing
        found = []
        for slot in self.slots:
            kind, value = slot.get("type"), slot.get("current_value")
            if slot["address"] in skip or kind not in ("INT", "FLOAT", "BOOLEAN", "STRING", "COMBO"):
                continue
            label = (slot.get("label") or slot.get("name") or slot["address"]).replace("_", " ")
            knob = label.lower().replace(" ", "_") in KNOBS  # a knob worth scrubbing is never plumbing
            control = {"address": slot["address"], "label": label, "value": value, "advanced": slot["address"] in sized or not knob and bool(
                slot.get("advanced") or str(value).endswith(MODEL_FILES) or "filename" in label)}
            if slot.get("enum"):
                control.update(control="choice", choices=slot["enum"])
            elif kind == "BOOLEAN":
                control["control"] = "switch"
            elif kind in ("INT", "FLOAT"):
                low, high = slot.get("min"), slot.get("max")
                low, high, step = KNOBS.get(label.lower().replace(" ", "_")) or (low, high, slot.get("step") or (1 if kind == "INT" else 0.01))
                ranged = isinstance(low, (int, float)) and isinstance(high, (int, float)) and high - low <= 100_000
                control.update(control="slider" if ranged else "number", min=low if ranged else None,
                               max=high if ranged else None, step=step)
            else:
                control["control"] = "text"
            found.append(control)
        return found

    def describe(self) -> dict[str, Any]:
        clip = self.clip_chips()
        if clip is not None and "duration" not in clip and (self.duration or {}).get("address"):  # a length only a guide's math names
            clip["duration"] = {**self.duration, "choices": list(DURATIONS)}
        return {"name": self.name, "title": self.title, "kind": self.kind, "tuned": self.tuned, "prompt": self.prompt, "seed": self.seed,
                "steps": self.size.get("steps", (None,))[0], "live_steps": self.live_steps,
                "megapixels": self.size.get("megapixels", (None,))[0], "media": self.media, "edits": self.edits,
                "settle": "refines the live frame" if self.refines() else "renders the template", "knobs": self.knobs(),
                "enhancer": bool(self.enhancers()), "sizing": self.sizing(), "clip": clip,
                "audio": self.audio(), "duration": self.duration, "panels": self._sampling() is not None and self.kind == "image",
                "models": [entry["label"] for entry in self.finetunes], "layout": self.layout() is not None, "flow": self.kind == "image" and self.flows(), "pin": self.kind == "image" and self._empty(self.wf) is not None,
                "settings": self.settings()}

    def missing(self, state: dict[str, Any]) -> str | None:
        """The label of the picture the template cannot run without, when it has none."""
        held = {source["slot"] for source in state.get("sources") or []}
        return self.media[0]["label"] if self.edits and self.media[0]["address"] not in held else None

    def _slot(self, address: str | None) -> dict[str, Any] | None:
        return next((slot for slot in self.slots if slot["address"] == address), None)

    def _set(self, graph: dict[str, Any], address: str | None, value: Any) -> None:
        for node, key in (self._slot(address) or {}).get("targets", []):
            if node in graph:
                graph[node]["inputs"][key] = value

    def _sampler(self) -> str | None:
        """The node whose output the decode reads, when the settle can refine from it: a KSampler
        that holds the steps, or a SamplerCustomAdvanced fed by the scheduler that holds them."""
        if "steps" not in self.size or "megapixels" not in self.size or self.kind != "image":
            return None
        if self.size["megapixels"][0] <= LIVE_MEGAPIXELS["image"]:
            return None
        holder = self.size["steps"][1][0]
        candidates = [holder] if self.wf[holder]["class_type"] == "KSampler" else [
            node_id for node_id, node in self.wf.items()
            if node["class_type"] == "SamplerCustomAdvanced" and node["inputs"].get("sigmas") == [holder, 0]]
        for sampler in candidates:
            if any(node["class_type"] == "VAEDecode" and node["inputs"].get("samples") == [sampler, 0] for node in self.wf.values()):
                return sampler
        return None

    def refines(self) -> bool:
        """Whether the settle can start from the live frame."""
        return self._sampler() is not None

    def _base(self, state: dict[str, Any]) -> dict[str, Any]:
        """The template with the creator's words, seed and settings, at its own size."""
        graph = json.loads(json.dumps(self.wf))
        tune = next((entry for entry in self.finetunes if entry["label"] == state.get("model")), None)
        for node in graph.values():
            for key, value in node["inputs"].items():
                if isinstance(value, str) and value in self.swaps:
                    node["inputs"][key] = value = self.swaps[value]
                if tune and value == tune["replaces"]:  # the creator's Model choice: a fine-tune of this file
                    node["inputs"][key] = tune["name"]
            if node["class_type"] == "SaveImage":  # frames stay out of the output folder; Keep is Saddle's
                node["class_type"] = "PreviewImage"
                node["inputs"].pop("filename_prefix", None)
        self._set(graph, self.prompt, state["prompt"])
        self._set(graph, self.seed, state["seed"])
        types = {control["address"]: self._slot(control["address"])["type"] for control in self.settings()}
        cast = {"INT": int, "FLOAT": float, "BOOLEAN": bool}
        for address, value in (state.get("values") or {}).items():
            if address in types:
                self._set(graph, address, cast.get(types[address], lambda v: v)(value))
        self._layout(graph, state.get("boxes"))
        held = {source["slot"] for source in state.get("sources") or []}
        unheld = [item["address"] for item in self.media if item["optional"] and item["address"] != LATENT and item["address"] not in held]
        return drop_media(graph, self.slots, unheld)[0] if unheld else graph

    def layout(self) -> tuple[str, str] | None:
        """The template's layout pair: a `CreateBoundingBoxes` node and the node that reads its elements into the words
        (a STRING `prompt` beside an input linked to the boxes' elements output), when it has one."""
        for boxes, node in self.wf.items():
            if node["class_type"] != "CreateBoundingBoxes":
                continue
            for reader, other in self.wf.items():
                if "prompt" in other["inputs"] and [boxes, 2] in other["inputs"].values():
                    return boxes, reader
        return None

    def _encoder(self, graph: dict[str, Any]) -> str | None:
        """The text encoder behind the sampler's positive words (through references, guiders and the like)."""
        node = graph.get(self._sampling() or "")
        todo = [v[0] for k, v in (node or {}).get("inputs", {}).items() if k in ("positive", "guider", "conditioning") and isinstance(v, list)]
        seen: set[str] = set()
        while todo:
            node_id = todo.pop(0)
            if node_id in seen or node_id not in graph:
                continue
            seen.add(node_id)
            inputs = graph[node_id]["inputs"]
            if "TextEncode" in graph[node_id]["class_type"] and "text" in inputs:
                return node_id
            todo += [v[0] for k, v in inputs.items() if k in ("positive", "conditioning", "guider", "conditioning_1") and isinstance(v, list)]
        return None

    def _layout(self, graph: dict[str, Any], boxes: list[dict[str, Any]] | None) -> None:
        """Layout boxes (`[{bbox: [ymin, xmin, ymax, xmax] on 0-1000, desc}]`) into the template's own layout nodes: the
        boxes node takes them as its upstream JSON, and the layout node sits between the words and the encoder (a
        template that leaves it on a preview gets it wired in). No boxes: the graph stays the template's."""
        found = self.layout()
        encoder = self._encoder(graph) if boxes and found else None
        if not encoder:
            return
        node, reader = found
        rows = [{"bbox": [max(0, min(1000, round(float(v)))) for v in box["bbox"]], "desc": str(box.get("desc") or "")} for box in boxes]
        graph[node]["inputs"].update(bboxes=json.dumps(rows), last_incoming={"__value__": []})
        behind, todo = set(), [graph[encoder]["inputs"]["text"]]
        while todo:  # is the layout node already upstream of the encoder?
            link = todo.pop()
            if isinstance(link, list) and len(link) == 2 and link[0] in graph and link[0] not in behind:
                behind.add(link[0])
                todo += list(graph[link[0]]["inputs"].values())
        if reader not in behind:
            graph[reader]["inputs"]["prompt"] = graph[encoder]["inputs"]["text"]
            graph[encoder]["inputs"]["text"] = [reader, 0]

    def build(self, state: dict[str, Any], settle: bool, upscale: str = "pixel", partial: bool = False,
              rung: str | None = None) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """The graph to submit and the (node, input, source id) triples that take the pictures.
        `partial`: a live image's frame is its last step preview, so its graph stops at the sampler.
        `rung` climbs the ladder from the same words, seed, pictures and settings: `final` refines the
        settled frame at the template's full steps with its enhancer on (a template the settle renders
        whole renders whole with it on); `2k` and `4k` upscale the Final in pixel space and refine it tiled.
        `state.panels` > 1 renders that many seeds' pictures in one batch; `state.panel` picks the one that settles."""
        if isinstance(state.get("flow"), dict) and state["flow"].get("frames") and not settle and rung is None:
            return self._flowed(state, partial)
        if rung in HIRES:
            if not self.refines():
                raise ValueError("this template has no sampler to refine a hi-res picture with")
            return self._hires(state, HIRES[rung])
        if rung == "final" and self.refines():
            return self._finalized(state)
        if settle and self.refines():
            return self._refined(state, upscale)
        if (state.get("vary") or state.get("variation")) and self.refines():
            return self._varied(state)
        panels = self.panels(state)
        graph = self._base(state)
        if partial and not settle and self.kind == "image" and panels == 1:
            self._stop_at_latent(graph)
        if rung == "final":
            for address in self.enhancers():
                self._set(graph, address, True)
        if not settle:
            if self.live_steps is not None:
                node, key = self.size["steps"][1]
                graph[node]["inputs"][key] = state.get("live_steps") or self.live_steps
            for node in graph.values() if self.thin else ():  # a schedule written knot by knot has no steps slot
                if node["class_type"] == "ManualSigmas" and isinstance(node["inputs"].get("sigmas"), str):
                    node["inputs"]["sigmas"] = thinned(node["inputs"]["sigmas"])
            if "megapixels" in self.size:
                self._scale(graph, self.live_size(state))
            for address in self.enhancers():
                self._set(graph, address, False)
            for node in self._references(graph):  # references at the live bucket's size, not the template's
                graph[node]["inputs"].update({"resolution": "resize", "resolution.megapixels": self.live_size(state)})
            for slot in self.slots:
                label = (slot.get("label") or slot.get("name") or "").lower()
                if slot.get("type") in ("INT", "FLOAT") and label in ("duration", "length", "seconds"):
                    self._set(graph, slot["address"], LIVE_SECONDS)
            if self.kind == "video":
                self._tiny(graph)
                if "megapixels" not in self.size:
                    self._shrink(graph)
            if state.get("lock") and panels == 1 and self.kind == "image" and not any(s["slot"] == LATENT for s in state.get("sources") or []):
                self._lock(graph, state["lock"])
        if self.kind == "video" and rung != "final":  # Final renders the template as its authors tuned it
            self._attend(graph)
        takes: list[tuple[str, str, str]] = self._pin(graph, state.get("pin")) if self.kind == "image" else []
        for source in state.get("sources") or []:
            if source["slot"] == LATENT and not self.edits:
                takes += [(node, key, source["id"]) for node, key in self._latent_from_picture(graph, state)]
            elif slot := self._slot(source["slot"]):
                takes += [(node, key, source["id"]) for node, key in slot["targets"] if node in graph]
        if panels > 1 and not settle:
            self._batch(graph, panels)
        return graph, takes

    def live_size(self, state: dict[str, Any]) -> float:
        """The live job's megapixels: the bucket nearest the dial's `live_mp` (a quarter megapixel by default), one
        bucket smaller while the creator types fast (`fast`). A clip keeps its own live size; an untuned template takes
        the smallest."""
        if self.kind != "image":
            return LIVE_MEGAPIXELS[self.kind]
        if not self.tuned:
            return LIVE_BUCKETS[0]
        mp = min(LIVE_BUCKETS, key=lambda b: abs(b - (state.get("live_mp") or LIVE_MEGAPIXELS["image"])))
        return LIVE_BUCKETS[max(0, LIVE_BUCKETS.index(mp) - 1)] if state.get("fast") else mp

    def panels(self, state: dict[str, Any]) -> int:
        return max(1, min(8, int(state.get("panels") or 1))) if self.kind == "image" and self._sampling() else 1

    def _sampling(self) -> str | None:
        """The node that samples the picture: the KSampler holding the steps, or the SamplerCustomAdvanced its scheduler feeds."""
        if "steps" not in self.size:
            return None
        holder = self.size["steps"][1][0]
        if self.wf[holder]["class_type"] == "KSampler":
            return holder
        return next((node_id for node_id, node in self.wf.items()
                     if node["class_type"] == "SamplerCustomAdvanced" and node["inputs"].get("sigmas") == [holder, 0]), None)

    def _batch(self, graph: dict[str, Any], n: int) -> None:
        """`n` pictures in one job: the empty latent the sampler starts from makes a batch of `n` (one noise draw,
        item i its i-th slice), or a picture's latent is repeated `n` times. The words are encoded once."""
        sampler = self._sampling()
        link = (graph.get(sampler) or {}).get("inputs", {}).get("latent_image")
        if not isinstance(link, list):
            return
        up = graph.get(link[0]) or {}
        if "Empty" in up.get("class_type", "") and isinstance(up["inputs"].get("batch_size"), int):
            up["inputs"]["batch_size"] = n
        else:
            graph["gv:repeat"] = {"class_type": "RepeatLatentBatch", "inputs": {"samples": link, "amount": n}}
            graph[sampler]["inputs"]["latent_image"] = ["gv:repeat", 0]

    def _refined(self, state: dict[str, Any], upscale: str) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """The live graph, its picture brought to the template's size and refined from there.

        `pixel` (the default): decode the live latent, resize it with Lanczos, encode it again.
        `latent`: upscale the latent itself; cheaper, but it cracks faces and doubles objects
        on FLUX 3 (see README, "Settle quality"). Of a batch of panels, the picked one."""
        graph, takes = self.build(state, settle=False)
        sampler = self._sampler()
        steps = int(self.size["steps"][0])
        target = job_size(self._base(state)).get("megapixels", self.size["megapixels"])[0]
        decode = self._decode()
        out = graph[decode]["inputs"]["samples"]  # the sampler's latent; a variation's; the head of a Flow
        vae = graph[decode]["inputs"]["vae"]
        picked = out
        if state.get("variation"):  # a variation tile settles as (seed, index) of its batch
            graph["gv:pick"] = {"class_type": "LatentFromBatch", "inputs": {"samples": out, "batch_index": int(state["variation"].get("index", 0)), "length": 1}}
            picked = ["gv:pick", 0]
        elif self.panels(state) > 1:
            graph["gv:pick"] = {"class_type": "LatentFromBatch", "inputs": {"samples": picked, "batch_index": int(state.get("panel") or 0), "length": 1}}
            picked = ["gv:pick", 0]
        if upscale == "pixel":
            graph["gv:look"] = {"class_type": "VAEDecode", "inputs": {"samples": picked, "vae": vae}}
            graph["gv:resize"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                "image": ["gv:look", 0], "upscale_method": "lanczos", "megapixels": round(target, 3), "resolution_steps": 16}}
            graph["gv:up"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["gv:resize", 0], "vae": vae}}
        else:
            live = job_size(graph).get("megapixels", (LIVE_MEGAPIXELS["image"],))[0]
            graph["gv:up"] = {"class_type": "LatentUpscaleBy",
                              "inputs": {"samples": picked, "upscale_method": "bislerp", "scale_by": round(math.sqrt(target / live), 3)}}
        if "gv:pinmask" in graph:  # the pin holds at the settle's size too
            graph["gv:uppin"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["gv:up", 0], "mask": ["gv:pinmask", 0]}}
        up = ["gv:uppin", 0] if "gv:uppin" in graph else ["gv:up", 0]
        if graph[sampler]["class_type"] in ("KSampler", "KSamplerAdvanced"):
            plain = self._plain(graph, sampler)
            if isinstance(state.get("flow"), dict):  # a Flow's head refines on the words as they are now
                plain = {**plain, "positive": self._copied(graph, plain["positive"], self._words_at(state["prompt"]), "gv:w:")}
            graph["gv:refine"] = {"class_type": "KSampler", "inputs": {
                **plain, "positive": self._full(graph, plain["positive"]), "negative": self._full(graph, plain["negative"]),
                "latent_image": up, "steps": steps, "denoise": REFINE_DENOISE}}
        else:  # a custom sampler: the template's schedule at its own steps, its last part only
            holder, key = self.size["steps"][1]
            graph["gv:sigmas"] = {"class_type": graph[holder]["class_type"], "inputs": {**graph[holder]["inputs"], key: steps}}
            graph["gv:tail"] = {"class_type": "SplitSigmasDenoise", "inputs": {"sigmas": ["gv:sigmas", 0], "denoise": REFINE_DENOISE}}
            graph["gv:refine"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                **graph[sampler]["inputs"], "noise": self.wf[sampler]["inputs"]["noise"], "latent_image": up, "sigmas": ["gv:tail", 1],
                "guider": self._full(graph, graph[sampler]["inputs"]["guider"])}}
        graph[decode]["inputs"]["samples"] = ["gv:refine", 0]
        return graph, takes

    def _decode(self) -> str:
        """The template's decode of its sampler's picture."""
        return next(node_id for node_id, node in self.wf.items() if node["class_type"] == "VAEDecode"
                    and node["inputs"].get("samples") == [self._sampler(), 0])

    def _words_at(self, words: str) -> dict[tuple[str, str], Any]:
        return {(node, key): words for node, key in (self._slot(self.prompt) or {}).get("targets", [])}

    def flows(self) -> bool:
        """Whether a Flow can run on this template: a KSampler whose picture the settle refines."""
        return self.refines() and self.wf[self._sampler()]["class_type"] == "KSampler"

    def _flowed(self, state: dict[str, Any], partial: bool) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """Flow: the newest frame of `state.flow.frames` ([{words, seed, steps, denoise}], the first from noise) as one
        graph holding every earlier frame verbatim, so ComfyUI serves the chain from cache and runs only the new tail.
        The first frame is the live graph on its words and seed; each later one is `LatentInterpolate(previous, anchor,
        FLOW_ANCHOR)` into a KSampler at its steps and denoise (the template's sampler, its own schedule) on its words;
        the anchor is the newest frame whose words changed. With `state.flow.roots` ({frame, anchor}: picture ids) the
        chain starts from those two pictures encoded instead (a re-root: the first frame then samples on them too).
        `partial`: the frame is the last step preview, so the outputs preview the head latent; else the template's
        decode reads the head (the settle refines from there)."""
        flow = state["flow"]
        if flow.get("session"):
            return self._recalled(state, partial)
        frames, roots = flow["frames"], flow.get("roots")
        graph, takes = self.build({**state, "prompt": frames[0]["words"], "seed": frames[0]["seed"], "flow": None}, settle=False)
        sampler, decode = self._sampling(), self._decode()
        plain, vae = self._plain(graph, sampler), graph[decode]["inputs"]["vae"]
        prev = anchor = [sampler, 0]
        if roots:
            for name, sid in (("gv:fl:load", roots["frame"]), ("gv:fl:aload", roots["anchor"])):
                graph[name] = {"class_type": "LoadImage", "inputs": {"image": f"{name[6:]}.png"}}
                graph[f"{name}:enc"] = {"class_type": "VAEEncode", "inputs": {"pixels": [name, 0], "vae": vae}}
                takes.append((name, "image", sid))
            prev, anchor = ["gv:fl:load:enc", 0], ["gv:fl:aload:enc", 0]
        encoded = {frames[0]["words"]: plain["positive"]}
        for i in range(0 if roots else 1, len(frames)):
            frame = frames[i]
            if frame["words"] not in encoded:
                encoded[frame["words"]] = self._copied(graph, plain["positive"], self._words_at(frame["words"]), f"gv:fl{i}:")
            graph[f"gv:fl{i}:mix"] = {"class_type": "LatentInterpolate", "inputs": {"samples1": prev, "samples2": anchor, "ratio": FLOW_ANCHOR}}
            graph[f"gv:fl{i}"] = {"class_type": "KSampler", "inputs": {
                **plain, "positive": encoded[frame["words"]], "latent_image": [f"gv:fl{i}:mix", 0], "seed": int(frame["seed"]),
                "steps": int(frame["steps"]), "denoise": float(frame["denoise"])}}
            prev = [f"gv:fl{i}", 0]
            if i > 0 and frame["words"] != frames[i - 1]["words"]:
                anchor = prev
        if partial:
            for node_id, node in list(graph.items()):
                if node["class_type"] in ("PreviewImage", "SaveImage"):
                    self._sink(graph, node_id, prev)
        else:
            graph[decode]["inputs"]["samples"] = prev
        return graph, takes

    def _recalled(self, state: dict[str, Any], partial: bool) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """Flow on VorpalFusion's node pack (`nodes/`, `state.flow.session` set): the newest frame is the live graph on
        its words whose sampler starts from `GVLatentRecall` of the frame before, through `LatentInterpolate` toward
        the anchor's (`frame.anchor`), at its steps and denoise; `GVLatentStore` keeps its latent in ComfyUI for the
        next frame. The graph is the same size on every frame and the anchor is its exact latent: nothing re-roots."""
        flow = state["flow"]
        head = flow["frames"][-1]
        graph, takes = self.build({**state, "prompt": head["words"], "seed": head["seed"], "flow": None}, settle=False)
        sampler, decode, session = self._sampling(), self._decode(), flow["session"]
        if "anchor" in head:  # a frame after the first: from the last one's latent
            for name, n in (("gv:fl:prev", flow["frames"][-2]["n"]), ("gv:fl:anchor", head["anchor"])):
                graph[name] = {"class_type": "GVLatentRecall", "inputs": {"session": session, "frame": int(n)}}
            graph["gv:fl:mix"] = {"class_type": "LatentInterpolate", "inputs": {"samples1": ["gv:fl:prev", 0], "samples2": ["gv:fl:anchor", 0], "ratio": FLOW_ANCHOR}}
            graph[sampler]["inputs"].update(latent_image=["gv:fl:mix", 0], steps=int(head["steps"]), denoise=float(head["denoise"]))
        if partial:  # the store is the frame's output: no picture output runs
            for node_id, node in list(graph.items()):
                if node["class_type"] in ("PreviewImage", "SaveImage"):
                    del graph[node_id]
            graph["gv:fl:store"] = {"class_type": "GVLatentStore", "inputs": {"samples": [sampler, 0], "session": session, "frame": int(head["n"])}}
        else:
            graph[decode]["inputs"]["samples"] = [sampler, 0]
        return graph, takes

    def _varied(self, state: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """The seed field's Variations: the live latent (ComfyUI's cache) repeated `count` times and refined with new
        noise at `strength`, at live size and steps, in one batched job whose noise seed is `seed`; tile i is
        (seed, i). A picked variation (`state.variation`) renders this same graph, served from cache."""
        vary = state.get("vary") or state["variation"]
        seed, strength, count = int(vary["seed"]), float(vary.get("strength", 0.4)), max(1, min(8, int(vary.get("count", 8))))
        graph, takes = self.build({k: v for k, v in state.items() if k not in ("vary", "variation", "panels", "panel")}, settle=False)
        sampler = self._sampler()
        decode = next(node_id for node_id, node in graph.items() if node["class_type"] == "VAEDecode" and node["inputs"].get("samples") == [sampler, 0])
        graph["gv:vrep"] = {"class_type": "RepeatLatentBatch", "inputs": {"samples": [sampler, 0], "amount": count}}
        if graph[sampler]["class_type"] in ("KSampler", "KSamplerAdvanced"):
            plain = self._plain(graph, sampler)
            graph["gv:vary"] = {"class_type": "KSampler", "inputs": {**plain, "latent_image": ["gv:vrep", 0], "seed": seed, "denoise": strength}}
        else:
            holder, _key = self.size["steps"][1]
            graph["gv:vtail"] = {"class_type": "SplitSigmasDenoise", "inputs": {"sigmas": [holder, 0], "denoise": strength}}
            graph["gv:vnoise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
            graph["gv:vary"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                **graph[sampler]["inputs"], "noise": ["gv:vnoise", 0], "latent_image": ["gv:vrep", 0], "sigmas": ["gv:vtail", 1]}}
        graph[decode]["inputs"]["samples"] = ["gv:vary", 0]
        return graph, takes

    def _climb(self, graph: dict[str, Any], below: str, name: str, latent: list, denoise: float, steps: int | None,
               enrich: bool = False) -> None:
        """One more refine of `latent` named `name`, from the sampler `below` it copies, at `denoise`: a KSampler at
        `steps`, or a custom sampler on the tail of the template's own schedule; its words enhanced if asked."""
        inputs = graph[below]["inputs"]
        if graph[below]["class_type"] == "KSampler":
            positive = self._enriched(graph, inputs["positive"]) if enrich else inputs["positive"]
            graph[name] = {"class_type": "KSampler", "inputs": {**inputs, "latent_image": latent, "steps": steps or inputs["steps"],
                                                                "denoise": denoise, "positive": positive}}
        else:
            graph[f"{name}:tail"] = {"class_type": "SplitSigmasDenoise", "inputs": {"sigmas": ["gv:sigmas", 0], "denoise": denoise}}
            guider = self._enriched(graph, inputs["guider"]) if enrich else inputs["guider"]
            graph[name] = {"class_type": "SamplerCustomAdvanced", "inputs": {**inputs, "latent_image": latent,
                                                                             "sigmas": [f"{name}:tail", 1], "guider": guider}}

    def _finalized(self, state: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """The settle, then Final: the settled latent refined at the template's full steps with its enhancer
        on, at a denoise low enough that the composition holds. The settle half is served from cache."""
        graph, takes = self._refined(state, "pixel")
        decode = next(node_id for node_id, node in graph.items() if node["class_type"] == "VAEDecode" and node["inputs"].get("samples") == ["gv:refine", 0])
        self._climb(graph, "gv:refine", "gv:final", ["gv:refine", 0], FINAL_DENOISE, int(self.size["steps"][0]), enrich=bool(self.enhancers()))
        for node in graph.values():  # the enhancer's text model (it runs only here) decodes with its multi-token head
            if "mtp" in node["inputs"] and FINAL_MTP:
                node["inputs"]["mtp"] = FINAL_MTP
        graph[decode]["inputs"]["samples"] = ["gv:final", 0]
        return graph, takes

    def _hires(self, state: dict[str, Any], side: int) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        """Final, upscaled with Lanczos to `side` on its long side, encoded tiled, refined on the tail of the template's own
        schedule at HIRES_DENOISE and decoded tiled: core nodes only, the Final half served from cache."""
        graph, takes = self._finalized(state)
        decode = next(node_id for node_id, node in graph.items() if node["class_type"] == "VAEDecode" and node["inputs"].get("samples") == ["gv:final", 0])
        vae = graph[decode]["inputs"]["vae"]
        graph["gv:hup"] = {"class_type": "ImageScaleToMaxDimension", "inputs": {"image": [decode, 0], "upscale_method": "lanczos", "largest_size": side}}
        graph["gv:henc"] = {"class_type": "VAEEncodeTiled", "inputs": {"pixels": ["gv:hup", 0], "vae": vae, **TILE}}
        self._climb(graph, "gv:final", "gv:hires", ["gv:henc", 0], HIRES_DENOISE, max(2, math.ceil(int(self.size["steps"][0]) * HIRES_DENOISE)))
        graph["gv:hdec"] = {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["gv:hires", 0], "vae": vae, **TILE}}
        for node in graph.values():
            if node["class_type"] == "PreviewImage":
                node["inputs"]["images"] = ["gv:hdec", 0]
        return graph, takes

    @staticmethod
    def _references(graph: dict[str, Any]) -> list[str]:
        """The nodes that resize reference pictures before encoding them (a `resolution.megapixels` beside `images.*` inputs)."""
        return [node_id for node_id, node in graph.items() if not node_id.startswith("gv:r:") and "resolution" in node["inputs"]
                and any(key.startswith("images.") for key in node["inputs"])]

    def _full(self, graph: dict[str, Any], link: Any) -> Any:
        """`link` (conditioning, or a guider holding it) with its references at the template's own size: the live graph
        encodes them at the live bucket's, the settle and every rung above it at the template's."""
        on = {(node, key): self.wf[node]["inputs"].get(key) for node in self._references(graph) if node in self.wf
              for key in ("resolution", "resolution.megapixels") if key in self.wf[node]["inputs"]}
        return self._copied(graph, link, on, "gv:r:") if on and isinstance(link, list) else link

    def _enriched(self, graph: dict[str, Any], link: list) -> list:
        """`link` (the words' conditioning, or a guider holding it) with the enhancers on: the refine reads the rewritten
        words while every node the switch does not reach (loaders, pictures, the live half) stays shared and cached."""
        on = {(node, key): True for address in self.enhancers() for node, key in (self._slot(address) or {}).get("targets", [])}
        return self._copied(graph, link, on, "gv:e:")

    def _worded(self, graph: dict[str, Any], link: list, words: str) -> list:
        """`link` (conditioning, or a guider holding it) for other words: the prompt's encode chain copied with them."""
        return self._copied(graph, link, {(node, key): words for node, key in (self._slot(self.prompt) or {}).get("targets", [])}, "gv:w:")

    @staticmethod
    def _copied(graph: dict[str, Any], link: list, on: dict[tuple[str, str], Any], prefix: str) -> list:
        """`link` with the inputs in `on` set: a copy (named `prefix` + id) of each node behind it that those inputs
        reach, so every node they do not reach stays shared and cached."""
        behind, todo = set(), [link[0]]
        while todo:
            node_id = todo.pop()
            if node_id in behind or node_id not in graph:
                continue
            behind.add(node_id)
            todo += [v[0] for v in graph[node_id]["inputs"].values() if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)]
        reached: set[str] = set()
        while grown := {node_id for node_id in behind - reached if any(
                (node_id, key) in on or (isinstance(value, list) and len(value) == 2 and value[0] in reached)
                for key, value in graph[node_id]["inputs"].items())}:
            reached |= grown
        for node_id in reached:
            node = json.loads(json.dumps(graph[node_id]))
            for key, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and value[0] in reached:
                    node["inputs"][key] = [f"{prefix}{value[0]}", value[1]]
                if (node_id, key) in on:
                    node["inputs"][key] = on[(node_id, key)]
            graph[f"{prefix}{node_id}"] = node
        return [f"{prefix}{link[0]}", link[1]] if link[0] in reached else link

    def _lock(self, graph: dict[str, Any], lock: dict[str, Any]) -> bool:
        """Step lock: the composition fixed at step k. The sampler splits in two: steps 0..k on the locked words and
        seed (ComfyUI caches that half while they hold), k..n on the words as they are now, without new noise. A
        KSampler becomes two KSamplerAdvanced; a custom sampler splits its sigmas (`SplitSigmas`) and the second half
        runs on `DisableNoise`. A picture-led job (denoise below 1) does not lock."""
        sampler = self._sampling()
        node = graph.get(sampler or "")
        if not node or not lock.get("prompt"):
            return False
        inputs = node["inputs"]
        if node["class_type"] == "KSampler":
            if float(inputs.get("denoise", 1)) < 1 or isinstance(inputs.get("steps"), list):
                return False
            steps = int(inputs["steps"])
            k = max(1, min(steps - 1, int(lock.get("step") or steps // 2)))
            common = {key: inputs[key] for key in ("model", "cfg", "sampler_name", "scheduler", "negative")}
            graph["gv:lock"] = {"class_type": "KSamplerAdvanced", "inputs": {
                **common, "positive": self._worded(graph, inputs["positive"], lock["prompt"]), "latent_image": inputs["latent_image"],
                "add_noise": "enable", "noise_seed": int(lock.get("seed", inputs["seed"])), "steps": steps, "start_at_step": 0,
                "end_at_step": k, "return_with_leftover_noise": "enable"}}
            graph[sampler] = {"class_type": "KSamplerAdvanced", "inputs": {
                **common, "positive": inputs["positive"], "latent_image": ["gv:lock", 0], "add_noise": "disable",
                "noise_seed": inputs["seed"], "steps": steps, "start_at_step": k, "end_at_step": 10000, "return_with_leftover_noise": "disable"}}
            return True
        if node["class_type"] == "SamplerCustomAdvanced" and "steps" in self.size:
            holder, key = self.size["steps"][1]
            steps = int(graph[holder]["inputs"][key])
            if steps < 2 or float(graph[holder]["inputs"].get("denoise", 1)) < 1:
                return False
            k = max(1, min(steps - 1, int(lock.get("step") or steps // 2)))
            noise = inputs["noise"]
            if isinstance(noise, list) and graph.get(noise[0], {}).get("class_type") == "RandomNoise":
                graph["gv:locknoise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(lock.get("seed", graph[noise[0]]["inputs"]["noise_seed"]))}}
                noise = ["gv:locknoise", 0]
            graph["gv:split"] = {"class_type": "SplitSigmas", "inputs": {"sigmas": inputs["sigmas"], "step": k}}
            graph["gv:lock"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                **inputs, "noise": noise, "guider": self._worded(graph, inputs["guider"], lock["prompt"]), "sigmas": ["gv:split", 0]}}
            graph["gv:nonoise"] = {"class_type": "DisableNoise", "inputs": {}}
            graph[sampler] = {"class_type": "SamplerCustomAdvanced", "inputs": {
                **inputs, "noise": ["gv:nonoise", 0], "sigmas": ["gv:split", 1], "latent_image": ["gv:lock", 0]}}
            return True
        return False

    def _plain(self, graph: dict[str, Any], sampler: str) -> dict[str, Any]:
        """The sampler's inputs as a KSampler's, a locked one (two KSamplerAdvanced) read back as one: for a refine to copy."""
        node = graph[sampler]
        if node["class_type"] != "KSamplerAdvanced":
            return node["inputs"]
        i = node["inputs"]
        first = graph.get("gv:lock", {}).get("inputs", {})
        return {"model": i["model"], "seed": i["noise_seed"], "steps": i["steps"], "cfg": i["cfg"], "sampler_name": i["sampler_name"],
                "scheduler": i["scheduler"], "positive": i["positive"], "negative": i["negative"],
                "latent_image": first.get("latent_image", i["latent_image"]), "denoise": 1.0}

    def _sink(self, graph: dict[str, Any], node_id: str, latent: list) -> None:
        """`node_id` becomes an output that makes ComfyUI run what `latent` needs and nothing more: a text preview of
        the latent's size (`GetLatentSize`) where the endpoint has it (printing the GPU tensor as text costs about 5 ms
        of a 100 ms one-step frame on FLUX 3 Klein, measured: `checks/realtime.py --sink`), else of the latent itself."""
        if self.sized:
            graph[f"{node_id}:size"] = {"class_type": "GetLatentSize", "inputs": {"latent": latent}}
            latent = [f"{node_id}:size", 0]
        graph[node_id] = {"class_type": "PreviewAny", "inputs": {"source": latent}}

    def _stop_at_latent(self, graph: dict[str, Any]) -> None:
        """Each picture output becomes a text preview of the latent its decode reads: ComfyUI still runs the
        sampler (an output depends on it) but no full decode and no PNG."""
        for node_id, node in list(graph.items()):
            if node["class_type"] not in ("PreviewImage", "SaveImage"):
                continue
            up = node["inputs"].get("images")
            while isinstance(up, list) and up[0] in graph and "VAEDecode" not in graph[up[0]]["class_type"]:
                up = next((v for v in graph[up[0]]["inputs"].values() if isinstance(v, list) and len(v) == 2), None)
            if isinstance(up, list) and up[0] in graph:
                self._sink(graph, node_id, graph[up[0]]["inputs"]["samples"])

    def _shrink(self, graph: dict[str, Any]) -> None:
        """A clip with no size of its own takes its pictures' size (Pika H3 Flex FL2VA): its draft takes them at the live
        size, or a 2 s draft from a 0.9 MP picture runs 6.5 s a step instead of 0.2."""
        loads = {node for item in self.media for node, _key in (self._slot(item["address"]) or {}).get("targets", []) if node in graph}
        for load in loads:
            for node in graph.values():
                for key, link in node["inputs"].items():
                    if link == [load, 0]:
                        node["inputs"][key] = [f"gv:small:{load}", 0]
            graph[f"gv:small:{load}"] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
                "image": [load, 0], "upscale_method": "area", "megapixels": LIVE_MEGAPIXELS["video"], "resolution_steps": 1}}

    def _attend(self, graph: dict[str, Any]) -> None:
        """ATTENTION between each guider or sampler and its model, when the template sets no attention of its own."""
        if not self.attention or any(node["class_type"] in ATTENTION_NODES for node in graph.values()):
            return
        for node in list(graph.values()):
            model = node["inputs"].get("model")
            if isinstance(model, list) and ("Guider" in node["class_type"] or node["class_type"].startswith("KSampler")):
                graph[f"gv:attention:{model[0]}"] = {"class_type": ATTENTION["class_type"], "inputs": {**ATTENTION["inputs"], "model": model}}
                node["inputs"]["model"] = [f"gv:attention:{model[0]}", 0]

    def _tiny(self, graph: dict[str, Any]) -> None:
        """A draft clip decodes its frames with the tiny decoder paired with its VAE file, untiled; the sound keeps its own."""
        for node_id, node in list(graph.items()):
            vae = node["inputs"].get("vae")
            if node["class_type"] not in ("VAEDecode", "VAEDecodeTiled") or not isinstance(vae, list):
                continue
            if tiny := self.drafts.get((graph.get(vae[0]) or {}).get("inputs", {}).get("vae_name")):
                graph["gv:tiny"] = {"class_type": "VAELoader", "inputs": {"vae_name": tiny}}
                graph[node_id] = {"class_type": "VAEDecode", "inputs": {"samples": node["inputs"]["samples"], "vae": ["gv:tiny", 0]}}

    def _scale(self, graph: dict[str, Any], megapixels: float) -> None:
        (current, (node, key)) = job_size(graph).get("megapixels", self.size["megapixels"])
        if key in ("width", "height"):
            scale = math.sqrt(megapixels / current)
            for side in ("width", "height"):
                graph[node]["inputs"][side] = max(64, int(graph[node]["inputs"][side] * scale) // 16 * 16)
        elif current > megapixels:
            graph[node]["inputs"][key] = megapixels

    def _empty(self, graph: dict[str, Any]) -> dict[str, Any] | None:
        """The empty latent (with a width and a height) the sampler starts from, if it starts from one."""
        link = graph.get(self._sampling() or "", {}).get("inputs", {}).get("latent_image")
        empty = graph.get(link[0]) if isinstance(link, list) else None
        return empty if empty and "Empty" in empty["class_type"] and {"width", "height"} <= set(empty["inputs"]) else None

    def _pin(self, graph: dict[str, Any], pin: dict[str, Any] | None) -> list[tuple[str, str, str]]:
        """Pin: the region `pin.rect` ([ymin, xmin, ymax, xmax] on 0-1000) of the picture `pin.id` stays exactly as it is.
        The sampler starts from that picture, fitted to its empty latent's size and encoded, under a noise mask of 1 - pin
        (feathered a few pixels) at denoise 1.0: everything else is drawn new from the words, the pinned latents are
        reset to the picture's every step. The mask is built from core nodes on a 256 grid the sampler stretches to any
        size, so the settle reuses it. A template whose sampler starts from no empty latent does not pin."""
        sampler, empty = graph.get(self._sampling() or ""), self._empty(graph)
        vae = next((node["inputs"]["vae"] for node in graph.values() if node["class_type"] == "VAEDecode"), None)
        if not pin or not pin.get("id") or not empty or vae is None:
            return []
        g = PIN_GRID / 1000
        y0, x0, y1, x1 = (max(0, min(PIN_GRID, round(float(v) * g))) for v in pin["rect"])
        graph["gv:pinload"] = {"class_type": "LoadImage", "inputs": {"image": "pin.png"}}
        graph["gv:pinfit"] = {"class_type": "ImageScale", "inputs": {"image": ["gv:pinload", 0], "upscale_method": "lanczos",
                                                                    "width": empty["inputs"]["width"], "height": empty["inputs"]["height"], "crop": "center"}}
        graph["gv:pinenc"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["gv:pinfit", 0], "vae": vae}}
        graph["gv:pinall"] = {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": PIN_GRID, "height": PIN_GRID}}
        graph["gv:pinhole"] = {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": max(1, x1 - x0), "height": max(1, y1 - y0)}}
        graph["gv:pinmix"] = {"class_type": "MaskComposite", "inputs": {"destination": ["gv:pinall", 0], "source": ["gv:pinhole", 0],
                                                                       "x": x0, "y": y0, "operation": "multiply"}}
        graph["gv:pinimg"] = {"class_type": "MaskToImage", "inputs": {"mask": ["gv:pinmix", 0]}}
        graph["gv:pinblur"] = {"class_type": "ImageBlur", "inputs": {"image": ["gv:pinimg", 0], "blur_radius": 4, "sigma": 2.0}}
        graph["gv:pinmask"] = {"class_type": "ImageToMask", "inputs": {"image": ["gv:pinblur", 0], "channel": "red"}}
        graph["gv:pinned"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["gv:pinenc", 0], "mask": ["gv:pinmask", 0]}}
        sampler["inputs"]["latent_image"] = ["gv:pinned", 0]
        if "denoise" in sampler["inputs"]:
            sampler["inputs"]["denoise"] = 1.0
        return [("gv:pinload", "image", pin["id"])]

    def _latent_from_picture(self, graph: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str]]:
        """Image to image for a text-to-image template: the empty latent a sampler starts from
        becomes the picture, fitted to that latent's size and encoded; the sampler denoises
        part way. A graph with no such latent keeps rendering from words."""
        vae = next((node["inputs"]["vae"] for node in graph.values() if node["class_type"] == "VAEDecode"), None)
        for node in graph.values():
            latent = node["inputs"].get("latent_image")
            empty = graph.get(latent[0]) if isinstance(latent, list) else None
            if vae is None or empty is None or "Latent" not in empty["class_type"] or "Empty" not in empty["class_type"]:
                continue
            width, height = empty["inputs"].get("width", 1024), empty["inputs"].get("height", 1024)
            if isinstance(width, list) or isinstance(height, list):
                continue  # the size comes from another node; leave this graph alone
            aspect = state.get("aspect", 1.0)
            area = width * height
            width, height = int(math.sqrt(area * aspect)) // 16 * 16, int(math.sqrt(area / aspect)) // 16 * 16
            graph["gv:load"] = {"class_type": "LoadImage", "inputs": {"image": "source.png"}}
            graph["gv:fit"] = {"class_type": "ImageScale", "inputs": {
                "image": ["gv:load", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
            graph["gv:encode"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["gv:fit", 0], "vae": vae}}
            node["inputs"]["latent_image"] = ["gv:encode", 0]
            if "denoise" in node["inputs"]:
                node["inputs"]["denoise"] = state.get("strength", 0.6)
            else:  # a custom sampler takes its denoise from its scheduler
                for other in graph.values():
                    if "denoise" in other["inputs"] and "Scheduler" in other["class_type"]:
                        other["inputs"]["denoise"] = state.get("strength", 0.6)
            return [("gv:load", "image")]
        return []
