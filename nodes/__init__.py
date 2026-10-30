"""VorpalFusion's ComfyUI node pack: what a frame loop needs from the executor that core ComfyUI does not keep.

The ComfyUI extension (the folder above, in `custom_nodes/`) loads it. For a standalone sidecar, link this folder
into ComfyUI's `custom_nodes/` (as `gpu_vision`) instead, not both, and restart ComfyUI. Nothing here names a
model: it serves any template.

- `GVLatentStore` / `GVLatentRecall`: a session's latents kept in the ComfyUI process between prompts, by
  (session, frame). A Flow frame recalls the frame before it (and its anchor) and stores its own, so its graph is the
  same size on every frame, the anchor is the exact latent, and a frame submitted while the one before still runs
  finds that one's latent when its turn comes (ComfyUI runs prompts in order).
- The previewer, kept: ComfyUI builds the step previewer (a tiny decoder read from disk) at the start of every
  sampler, 10-20 ms a job; this pack builds each image one once per device, format and preview method.
  `GV_KEEP_PREVIEWER=0` turns it off.
"""

from __future__ import annotations

import os
from collections import OrderedDict

KEPT_FRAMES = 64  # per session: a Flow's frames and its anchor (0.5 MB each at a quarter megapixel)
KEPT_SESSIONS = 8
STORE: OrderedDict[str, OrderedDict[int, dict]] = OrderedDict()


def put(session: str, frame: int, latent: dict) -> None:
    frames = STORE.setdefault(session, OrderedDict())
    STORE.move_to_end(session)
    frames[frame] = {**latent, "samples": latent["samples"].detach()}
    frames.move_to_end(frame)
    while len(frames) > KEPT_FRAMES:
        frames.popitem(last=False)
    while len(STORE) > KEPT_SESSIONS:
        STORE.popitem(last=False)


def get(session: str, frame: int) -> dict:
    found = STORE.get(session, {}).get(frame)
    if found is None:
        raise ValueError(f"VorpalFusion: frame {frame} of session {session} is not held here (ComfyUI restarted, or the "
                         f"frame was dropped): start the stream again")
    return found


class GVLatentStore:
    CATEGORY = "gpu-vision"
    DESCRIPTION = "Keeps a latent in this ComfyUI process for later prompts of the same session."
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "store"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",), "session": ("STRING", {"default": ""}),
                             "frame": ("INT", {"default": 0, "min": 0, "max": 2**53})}}

    def store(self, samples, session, frame):
        put(session, frame, samples)
        return {}


class GVLatentRecall:
    CATEGORY = "gpu-vision"
    DESCRIPTION = "A latent an earlier prompt of the same session kept with GVLatentStore."
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "recall"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"session": ("STRING", {"default": ""}), "frame": ("INT", {"default": 0, "min": 0, "max": 2**53})}}

    def recall(self, session, frame):
        return (get(session, frame),)


def keep_previewer() -> None:
    """Wrap `latent_preview.get_previewer` so an image previewer is built once per (device, format, method)."""
    import latent_preview

    if getattr(latent_preview.get_previewer, "gv_kept", False):
        return
    build, kept = latent_preview.get_previewer, {}

    def get_previewer(device, latent_format):
        key = (str(device), type(latent_format).__name__, str(latent_preview.args.preview_method))
        if key not in kept:
            previewer = build(device, latent_format)
            if isinstance(previewer, latent_preview.TAEHVPreviewerImpl):  # a video decoder is a managed model: as before
                return previewer
            kept[key] = previewer
        return kept[key]

    get_previewer.gv_kept = True
    latent_preview.get_previewer = get_previewer


if os.environ.get("GV_KEEP_PREVIEWER", "1") != "0":
    try:
        keep_previewer()
    except ImportError:  # imported outside ComfyUI (the tests import the store alone)
        pass

NODE_CLASS_MAPPINGS = {"GVLatentStore": GVLatentStore, "GVLatentRecall": GVLatentRecall}
NODE_DISPLAY_NAME_MAPPINGS = {"GVLatentStore": "Latent Store (VorpalFusion)", "GVLatentRecall": "Latent Recall (VorpalFusion)"}
