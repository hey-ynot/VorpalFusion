"""The node pack inside a ComfyUI checkout, on the CPU (no GPU, no server):

    CUDA_VISIBLE_DEVICES= <ComfyUI's python> nodes/test_nodes.py <ComfyUI folder>

Loaded by ComfyUI's own custom-node loader; then store and recall by (session, frame), a missing frame's sentence,
the bounds, and the previewer built once per format.
"""

import asyncio
import os
import sys
import time

root = sys.argv[1]
sys.argv = ["main.py", "--cpu", "--preview-method", "taesd"]
sys.path.insert(0, root)
import comfy.options  # noqa: E402

comfy.options.enable_args_parsing()
import torch  # noqa: E402

import comfy.latent_formats as formats  # noqa: E402
import latent_preview  # noqa: E402
import nodes  # noqa: E402  (ComfyUI's)

here = os.path.dirname(os.path.abspath(__file__))
assert asyncio.run(nodes.load_custom_node(here)) is not False
assert {"GVLatentStore", "GVLatentRecall"} <= set(nodes.NODE_CLASS_MAPPINGS), "ComfyUI did not load the pack"
pack = sys.modules[here.replace(".", "_x_")]

latent = {"samples": torch.randn(1, 128, 32, 32)}
assert nodes.NODE_CLASS_MAPPINGS["GVLatentStore"]().store(latent, "s1", 7) == {}
got = nodes.NODE_CLASS_MAPPINGS["GVLatentRecall"]().recall("s1", 7)[0]
assert torch.equal(got["samples"], latent["samples"]), "recall is not the stored latent"
try:
    pack.GVLatentRecall().recall("s1", 8)
    raise AssertionError("a missing frame recalled")
except ValueError as error:
    assert "not held" in str(error)
for i in range(pack.KEPT_FRAMES + 5):
    pack.put("s2", i, latent)
assert len(pack.STORE["s2"]) == pack.KEPT_FRAMES and 0 not in pack.STORE["s2"], "frames not bounded"
for i in range(pack.KEPT_SESSIONS + 2):
    pack.put(f"x{i}", 0, latent)
assert len(pack.STORE) == pack.KEPT_SESSIONS, "sessions not bounded"

assert latent_preview.get_previewer.gv_kept, "previewer not wrapped"
cpu = torch.device("cpu")
t = time.perf_counter()
first = latent_preview.get_previewer(cpu, formats.Flux2())
built = 1000 * (time.perf_counter() - t)
t = time.perf_counter()
again = latent_preview.get_previewer(cpu, formats.Flux2())
kept = 1000 * (time.perf_counter() - t)
assert first is again and isinstance(first, latent_preview.TAESDPreviewerImpl), type(first)
assert latent_preview.get_previewer(cpu, formats.Flux()) is not first, "formats share a previewer"
with torch.inference_mode():  # as inside a sampler, on a job after the one that built it
    image = first.decode_latent_to_preview_image("JPEG", torch.randn(1, 128, 32, 32))
assert image[1].size == (512, 512), image[1].size
print(f"ok: loaded by ComfyUI, store/recall, bounds, previewer built {built:.1f} ms then kept {kept:.3f} ms")
