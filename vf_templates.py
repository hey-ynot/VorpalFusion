"""Official ComfyUI templates as records the app reads, built with comfy-cli: vendored from Saddle.

From Saddle's `src/saddle/templates.py` (the record builder, the slot readers and the precision-variant rule), `recipes.json` (the media roles
it pins) and `domain/identity.py` (the content hash), Apache-2.0, the same author. VorpalFusion reads a record from a
Saddle server when it has one (`--saddle`); without one it builds the record here, the way Saddle does:

The endpoint serves the template file (`/templates/<name>.json`, the version that matches its nodes). comfy-cli
(`uv tool install comfy-cli`) lists its typed slots and converts it to an API graph; this module calls the CLI,
never imports it. A record:

- ``wf``: the API-format graph with the template's own values;
- ``inputs.slots``: each settable input with its label, type, current value, enum, whether it is an inner
  (advanced) input, the ``targets`` (``[node_id, input]``) it sets in ``wf``, and for a number the ``min``,
  ``max`` and ``step`` its node declares in ``object_info``;
- ``meta.media``: the media roles pinned for the template (first frame, last frame, reference).

Targets are found by converting again with every slot set to a unique sentinel, and once per boolean with it
flipped, then reading where each value lands. Records are cached per template file and the endpoint's object_info.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import deque
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

COMFY_CLI = os.environ.get("VF_COMFY_CLI", "comfy")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SENTINEL_BASE = 900_000_000
_SETTABLE = {"STRING", "INT", "FLOAT", "COMBO", "BOOLEAN"}
IMPORTER = "vf1"  # in the cache key: a change to how records are built rebuilds them
# Media roles Saddle's recipe table pins by template, over the roles found by shape (media_roles).
ROLES = {
    "video_minimax_h3_i2v": {"114.image": "first_frame"},
    "video_minimax_h3_i2v_continuation": {"114.image": "first_frame"},
    "video_ltx2_5_i2v": {"395.image": "first_frame"},
    "video_fastvideo_fasth3_i2v": {"136.image": "first_frame"},
    "video_ltx2_5_flf2v": {"31.image": "first_frame", "39.image": "last_frame"},
    "video_minimax_h3_r2v": {"137.image": "reference", "139.image": "reference"},
}


class TemplateError(RuntimeError):
    """The template could not be fetched, converted, or introspected."""


def content_hash(value: Any) -> str:
    """sha256 of canonical JSON (sorted keys, no spaces), as ``sha256:<hex>``."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _limits(slot: Mapping[str, Any], graph: Mapping[str, Any], object_info: Mapping[str, Any]) -> dict[str, Any]:
    """``min``, ``max``, ``step`` a number slot's nodes declare; over several targets, the tightest."""
    found: dict[str, Any] = {}
    for node_id, key in slot["targets"]:
        spec = (object_info.get(str(graph.get(node_id, {}).get("class_type"))) or {}).get("input") or {}
        declared = (spec.get("required") or {}).get(key) or (spec.get("optional") or {}).get(key)
        options = declared[1] if isinstance(declared, list) and len(declared) > 1 and isinstance(declared[1], Mapping) else {}
        for name, pick in (("min", max), ("max", min), ("step", max)):
            value = options.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found[name] = pick(found[name], value) if name in found else value
    return found


def _comfy(*args: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run([COMFY_CLI, *args], capture_output=True, text=True, timeout=180, check=False,
                                env=dict(env) if env is not None else None)
    except FileNotFoundError as error:
        raise TemplateError("comfy-cli is not installed (uv tool install comfy-cli)") from error
    # Split on "\n" only: envelopes may carry U+2028 in text, which splitlines() breaks on.
    lines = [line for line in result.stdout.split("\n") if line.startswith("{")]
    try:
        envelope = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise TemplateError(f"comfy {args[0]} returned no JSON envelope: {result.stderr[-300:]}") from error
    if not envelope.get("ok"):
        message = (envelope.get("error") or {}).get("message") or "failed"
        raise TemplateError(f"comfy {' '.join(args[:2])}: {message}")
    return envelope.get("data") or {}


def comfy_target(endpoint_url: str) -> tuple[list[str], dict[str, str]]:
    """comfy-cli arguments and environment that point it at ``endpoint_url``."""
    parts = urlsplit(endpoint_url)
    return (["--host", parts.hostname or "127.0.0.1", "--port", str(parts.port or 8188)],
            {**os.environ, "COMFY_LOCAL_URL": endpoint_url})


def get_json(url: str) -> Any:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=60) as response:
        return json.loads(response.read())


def _labels(workflow: Mapping[str, Any]) -> dict[str, str]:
    """``{"<instance>.<input>": label}`` for subgraph inputs their authors labelled."""
    defs = {str(sg.get("id")): sg for sg in (workflow.get("definitions") or {}).get("subgraphs") or []}
    labels = {}
    for node in workflow.get("nodes") or []:
        definition = defs.get(str(node.get("type")))
        if definition is None:
            continue
        declared = {str(item.get("name")): item.get("label") for item in definition.get("inputs") or []}
        for item in node.get("inputs") or []:
            label = item.get("label") or declared.get(str(item.get("name")))
            if label:
                labels[f"{node.get('id')}.{item.get('name')}"] = str(label)
    return labels


def _fill_dropped_inputs(workflow: Mapping[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """Restore subgraph-input values the conversion dropped: ``comfy run --print-prompt`` omits a subgraph input
    wired to a lazy input (a ``ComfySwitchNode``'s ``on_false``); such an input takes the value the same subgraph
    input wrote to its other targets."""
    defs = {str(sg.get("id")): sg for sg in (workflow.get("definitions") or {}).get("subgraphs") or []}

    def definition(path: str) -> Mapping[str, Any] | None:
        nodes, found = workflow.get("nodes") or [], None
        for part in path.split(":"):
            node = next((n for n in nodes if str(n.get("id")) == part), None)
            found = defs.get(str(node.get("type"))) if node else None
            if found is None:
                return None
            nodes = found.get("nodes") or []
        return found

    for path in {node_id.rsplit(":", 1)[0] for node_id in graph if ":" in node_id}:
        sub = definition(path)
        if sub is None:
            continue
        inner = {str(n.get("id")): n for n in sub.get("nodes") or []}
        fed: dict[int, list[tuple[str, str]]] = {}
        for link in sub.get("links") or []:
            if isinstance(link, Mapping) and link.get("origin_id") == -10 and str(link.get("target_id")) in inner:
                inputs = inner[str(link["target_id"])].get("inputs") or []
                if link.get("target_slot", -1) < len(inputs):
                    fed.setdefault(link["origin_slot"], []).append(
                        (f"{path}:{link['target_id']}", inputs[link["target_slot"]].get("name")))
        for targets in fed.values():
            landed = [graph[n]["inputs"][k] for n, k in targets if n in graph and k in graph[n]["inputs"]]
            values = [value for value in landed if not isinstance(value, list)]
            if not values:
                continue
            for node_id, key in targets:
                if node_id in graph and key not in graph[node_id]["inputs"]:
                    graph[node_id]["inputs"][key] = values[0]
    return graph


def _sentinel(slot: Mapping[str, Any], index: int) -> Any:
    kind = slot["type"]
    if kind == "INT":
        return _SENTINEL_BASE + index
    if kind == "FLOAT":
        return _SENTINEL_BASE + index + 0.5
    return f"__vf_slot_{index}__"


def _assignment(address: str, value: Any) -> str:
    return f"{address}={value if isinstance(value, str) else json.dumps(value)}"


def import_template(endpoint_url: str, name: str, cache_dir: Path, object_info: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The record for official template ``name`` as this endpoint serves it: ``{name, label, wf, inputs, meta}``
    (``object_info``: the endpoint's, when the caller already holds it)."""
    if not _NAME.fullmatch(name):
        raise ValueError("template name must be a simple identifier")
    try:
        template = get_json(f"{endpoint_url}/templates/{name}.json")
        object_info = object_info or get_json(f"{endpoint_url}/api/object_info")
    except OSError as error:
        raise TemplateError(f"the endpoint did not serve template {name!r}: {error}") from error
    graph, slots = import_workflow(endpoint_url, template, object_info, cache_dir)
    pinned = ROLES.get(name, {})
    return {"name": name, "label": name, "wf": graph, "inputs": {"slots": slots},
            "meta": {"media": [{"address": address, "role": role} for address, role in pinned.items()]}}


def import_workflow(endpoint_url: str, template: Mapping[str, Any], object_info: Mapping[str, Any],
                    cache_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(graph, slots)`` for a UI-format workflow on this endpoint, built once and cached."""
    if not isinstance(template, Mapping) or not isinstance(template.get("nodes"), list):
        raise ValueError("workflow must be a ComfyUI UI-format file (nodes and links)")
    key = f"{content_hash(template)[7:23]}.{content_hash(object_info)[7:23]}.{IMPORTER}"
    cache = Path(cache_dir) / f"{key}.json"
    if cache.is_file():
        cached = json.loads(cache.read_text())
        return cached["graph"], cached["slots"]
    where, comfy_env = comfy_target(endpoint_url)
    with tempfile.TemporaryDirectory(prefix="vf-template-") as tmp:
        ui = Path(tmp) / "template.json"
        info = Path(tmp) / "object_info.json"
        ui.write_text(json.dumps(template))
        info.write_text(json.dumps(object_info))

        def convert(path: Path) -> dict[str, Any]:
            prompt = _comfy("run", "--workflow", str(path), "--print-prompt", *where, env=comfy_env)["prompt"]
            return _fill_dropped_inputs(json.loads(path.read_text()), prompt)

        slots = [
            slot for slot in _comfy("workflow", "slots", str(ui), "--input", str(info))["slots"]
            # Linked inputs are driven by the graph, and inputs the template does
            # not store (newer node versions) have no widget for set-slot to write.
            if slot.get("type") in _SETTABLE and "linked_from" not in slot and slot.get("current_value") is not None
        ]
        graph = convert(ui)

        def marked(assignments: dict[str, Any]) -> dict[str, Any]:
            data = _comfy("workflow", "set-slot", str(ui), *(_assignment(a, v) for a, v in assignments.items()),
                          "--stdout", "--input", str(info))["workflow_json"]
            path = Path(tmp) / "marked.json"
            path.write_text(data if isinstance(data, str) else json.dumps(data))
            return convert(path)

        def targets(marked_graph: Mapping[str, Any], value: Any, flip: bool) -> list[list[str]]:
            return [[node_id, key] for node_id, node in marked_graph.items() for key, landed in node["inputs"].items()
                    if not isinstance(landed, list) and landed == value and type(landed) is type(value)
                    and (not flip or graph.get(node_id, {}).get("inputs", {}).get(key) != value)]

        # One pass for every unique sentinel, one per boolean (flips are not unique).
        sentinels = {slot["address"]: _sentinel(slot, index) for index, slot in enumerate(slots) if slot["type"] != "BOOLEAN"}
        if sentinels:
            all_marked = marked(sentinels)
        for slot in slots:
            if slot["type"] == "BOOLEAN":
                value = not slot.get("current_value")
                slot["targets"] = targets(marked({slot["address"]: value}), value, flip=True)
            else:
                slot["targets"] = targets(all_marked, sentinels[slot["address"]], flip=False)

    labels = _labels(template)
    for slot in slots:
        if slot["type"] in {"INT", "FLOAT"}:
            slot.update(_limits(slot, graph, object_info))
        slot["advanced"] = "/" in slot["address"]  # inputs the template exposes come first; inner node inputs are advanced
        slot["label"] = labels.get(slot["address"]) or str(slot.get("name") or slot["address"]).replace("_", " ")
    slots = [slot for slot in slots if slot["targets"]]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"graph": graph, "slots": slots}))
    return graph, slots


# A model file's precision: one suffix before its extension (``_int8_convrot``, ``-fp8``, ``.int8_convrot``).
_FILE_PRECISION = re.compile(
    r"[-_.](?:bf16|fp16|fp32|fp8(?:[-_](?:scaled|e4m3fn|e5m2))?|int8(?:[-_]convrot)?|w4a8|fp4(?:[-_]mixed)?|nvfp4)$",
    re.IGNORECASE)


def model_stem(name: str) -> str:
    """``name`` without one precision suffix: ``krea2_turbo_fp8_scaled.safetensors`` and
    ``krea2_turbo_bf16.safetensors`` are both ``krea2_turbo.safetensors``; a subfolder stays."""
    base, dot, ext = name.rpartition(".")
    return f"{_FILE_PRECISION.sub('', base)}.{ext}" if dot else _FILE_PRECISION.sub("", name)


def same_model(name: str, installed: Collection[str]) -> list[str]:
    """The installed files that are ``name`` in another precision (never another stem)."""
    stem = model_stem(name)
    return [other for other in installed if other != name and model_stem(other) == stem]


def gaps(graph: Mapping[str, Any], object_info: Mapping[str, Any]) -> tuple[list[str], dict[str, str]]:
    """``(missing, swaps)`` for running ``graph`` on the endpoint ``object_info`` describes: each node class it lacks
    or that calls a partner API, and each choice not among the endpoint's own (a model file it does not have);
    ``swaps`` maps a missing file the endpoint has in another precision to that file, which counts as present."""
    missing: list[str] = []
    swaps: dict[str, str] = {}
    for node in graph.values():
        kind = str(node.get("class_type"))
        spec = object_info.get(kind)
        if spec is None or spec.get("api_node"):
            missing.append(f"{kind} (a partner API node)" if spec else kind)
            continue
        declared = {**((spec.get("input") or {}).get("optional") or {}), **((spec.get("input") or {}).get("required") or {})}
        for key, value in (node.get("inputs") or {}).items():
            spec_of = declared.get(key) or [None]
            choices = spec_of[0] if spec_of[0] != "COMBO" else (spec_of[1] if len(spec_of) > 1 else {}).get("options")
            if (kind, key) in MEDIA_INPUTS or not isinstance(value, str) or not isinstance(choices, list) or value in choices:
                continue  # a picture slot takes the creator's own file
            if others := same_model(value, choices):
                swaps[value] = others[0]  # the first variant found; choosing by size and free VRAM would pick better
            else:
                missing.append(value)
    return sorted(set(missing)), swaps


# Loader inputs that take a file from the endpoint's input folder.
MEDIA_INPUTS = {
    ("LoadImage", "image"): "image",
    ("LoadImageMask", "image"): "image",
    ("LoadVideo", "file"): "video",
    ("LoadAudio", "audio"): "audio",
}
# An indexed media input on the node a loader feeds: ``image1``, ``image_2``, ``images.image_3``.
_INDEXED_INPUT = re.compile(r"(?:image|video|audio)s?_?(\d+)$")


def _media_loaders(graph: Mapping[str, Any], slots: list[Mapping[str, Any]]) -> dict[str, tuple[str, str, str]]:
    """``{address: (kind, loader class, indexed input it feeds)}`` for slots that set a media loader, in order: by
    the index of the nearest indexed input each loader feeds (an edit template's image 1, then image 2), then the
    rest by address."""
    readers: dict[str, list[tuple[str, str]]] = {}
    for node_id, node in graph.items():
        for key, value in node.get("inputs", {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
                readers.setdefault(str(value[0]), []).append((node_id, key))

    def index(loader: str) -> tuple[int | None, str]:
        seen, queue = {loader}, deque([loader])  # breadth first, so the nearest indexed input wins
        while queue:
            for reader, key in readers.get(queue.popleft(), ()):
                if match := _INDEXED_INPUT.search(key):
                    return int(match[1]), key
                if reader not in seen:
                    seen.add(reader)
                    queue.append(reader)
        return None, ""

    found = {}
    for slot in slots:
        for node_id, key in slot["targets"]:
            loader = str(graph.get(node_id, {}).get("class_type"))
            kind = MEDIA_INPUTS.get((loader, key))
            if kind:
                found[slot["address"]] = (kind, loader, *index(node_id))

    def order(address: str) -> tuple:
        position = found[address][2]
        return position is None, position or 0, [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", address)]

    return {address: (found[address][0], found[address][1], found[address][3]) for address in sorted(found, key=order)}


def media_slots(graph: Mapping[str, Any], slots: list[Mapping[str, Any]]) -> dict[str, str]:
    """``{address: "image" | "video" | "audio"}`` for slots that set a media loader, in order."""
    return {address: kind for address, (kind, _loader, _input) in _media_loaders(graph, slots).items()}


def media_roles(graph: Mapping[str, Any], slots: list[Mapping[str, Any]]) -> dict[str, str]:
    """``{address: role}`` for each media slot: ``mask`` for a mask loader, ``reference`` for a loader feeding a
    reference input (``ref_images.ref_image_0``), ``image_<n>`` for the n-th other image in order, and ``source``
    for other media."""
    roles: dict[str, str] = {}
    for address, (kind, loader, indexed) in _media_loaders(graph, slots).items():
        if loader == "LoadImageMask":
            roles[address] = "mask"
        elif "ref" in indexed:
            roles[address] = "reference"
        elif kind == "image":
            roles[address] = f"image_{sum(role.startswith('image_') for role in roles.values()) + 1}"
        else:
            roles[address] = "source"
    return roles


def _readers(graph: Mapping[str, Any], node_id: str) -> list[tuple[str, str]]:
    """``(node, input)`` for every input linked to ``node_id``'s outputs."""
    return [(reader, key) for reader, node in graph.items() for key, value in (node.get("inputs") or {}).items()
            if isinstance(value, list) and len(value) == 2 and str(value[0]) == node_id]


def optional_media(graph: Mapping[str, Any], slots: list[Mapping[str, Any]]) -> list[str]:
    """Media slots after the first whose loader feeds only entries of a growing input list (``images.image_2``,
    ``ref_images.ref_image_1``): the graph runs without them."""
    found = []
    for address in list(media_slots(graph, slots))[1:]:
        node_id = next(target[0] for slot in slots if slot["address"] == address for target in slot["targets"])
        readers = _readers(graph, str(node_id))
        if readers and all("." in key for _reader, key in readers):
            found.append(address)
    return found


def drop_media(graph: Mapping[str, Any], slots: list[Mapping[str, Any]], addresses: Collection[str]):
    """``(graph, slots)`` without the loaders of ``addresses`` and the list entries they fed."""
    graph = json.loads(json.dumps(graph))
    for address in addresses:
        node_id = str(next(target[0] for slot in slots if slot["address"] == address for target in slot["targets"]))
        for reader, key in _readers(graph, node_id):
            del graph[reader]["inputs"][key]
        graph.pop(node_id, None)
    kept = [slot for slot in slots if all(str(node) in graph for node, _key in slot["targets"])]
    return graph, kept


def _positive(graph: Mapping[str, Any]) -> set[str]:
    """The nodes a sampler's ``positive`` conditioning reads from: upstream of each ``positive`` input, following
    only ``positive`` through a node that takes both, so a negative prompt is never among them."""
    found: set[str] = set()
    todo = [str(node["inputs"]["positive"][0]) for node in graph.values() if isinstance((node.get("inputs") or {}).get("positive"), list)]
    while todo:
        current = todo.pop()
        if current in found or current not in graph:
            continue
        found.add(current)
        inputs = graph[current].get("inputs") or {}
        follow = {"positive": inputs["positive"]} if isinstance(inputs.get("positive"), list) else inputs
        todo += [str(value[0]) for value in follow.values()
                 if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int))]
    return found


def prompt_slot(graph: Mapping[str, Any], slots: list[Mapping[str, Any]]) -> str | None:
    """The address of the template's prompt: of the text slots that reach a sampler's positive conditioning (every
    text slot when no sampler takes one), the first labelled or named prompt or text, else the first basic one,
    never a negative prompt."""
    # A math node's expression may feed the conditioning (a frame count) but is no prompt.
    text = [slot for slot in slots if slot.get("type") == "STRING" and slot.get("name") not in {"filename_prefix", "expression"}]
    positive = _positive(graph)
    if positive:
        text = [slot for slot in text if any(node in positive for node, _key in slot.get("targets") or ())]
    named = next((slot for slot in text if slot.get("label") == "prompt" or slot.get("name") in {"prompt", "text"}), None)
    basic = next((slot for slot in text if not slot.get("advanced", "/" in slot["address"])), None)
    return (named or basic or {}).get("address")


def seed_slot(slots: list[Mapping[str, Any]]) -> str | None:
    """The address of the template's first integer slot labelled or named seed."""
    return next((slot["address"] for slot in slots if slot.get("type") == "INT" and (
        slot.get("label") == "seed" or slot.get("name") in {"seed", "noise_seed"})), None)


def _number(graph: Mapping[str, Any], node_id: str, key: str) -> tuple[Any, tuple[str, str] | None]:
    """``(value, (node, key))`` where an input's number is set: on the node, or on a primitive node feeding it;
    ``(None, None)`` when another node computes it."""
    value = graph[node_id]["inputs"].get(key)
    if isinstance(value, list) and len(value) == 2 and str(value[0]) in graph:
        source = graph[str(value[0])]
        if not str(source.get("class_type", "")).startswith("Primitive"):
            return None, None
        node_id, key, value = str(value[0]), "value", source["inputs"].get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, None
    return value, (node_id, key)


def job_size(graph: Mapping[str, Any]) -> dict[str, tuple[float, tuple[str, str]]]:
    """The largest ``batch`` (``batch_size``), sampler ``steps`` and output ``megapixels`` (a latent's width ×
    height, or a ``megapixels`` input) the graph sets, each with the ``(node, key)`` that sets it."""
    found: dict[str, tuple[float, tuple[str, str]]] = {}

    def keep(name: str, value: Any, where: tuple[str, str] | None) -> None:
        if where is not None and value > found.get(name, (0, None))[0]:
            found[name] = (value, where)

    for node_id, node in graph.items():
        inputs = node.get("inputs") or {}
        for name, key in (("batch", "batch_size"), ("steps", "steps"), ("megapixels", "megapixels")):
            if key in inputs:
                keep(name, *_number(graph, node_id, key))
        if {"batch_size", "width", "height"} <= inputs.keys():
            (width, where), (height, _) = _number(graph, node_id, "width"), _number(graph, node_id, "height")
            if width and height:
                keep("megapixels", width * height / 1e6, where)
    return found
