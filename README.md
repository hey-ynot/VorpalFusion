<div align="center">

# VorpalFusion

**The picture keeps up with you.**

Live image and clip generation over any ComfyUI template, on your own GPU.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![ComfyUI 0.43+](https://img.shields.io/badge/ComfyUI-0.43%2B-172dd7)](https://github.com/comfyanonymous/ComfyUI)
[![comfy-sdk 0.5](https://img.shields.io/badge/comfy--sdk-0.5-172dd7)](https://pypi.org/project/comfy-sdk/)
[![Open weights only](https://img.shields.io/badge/models-open%20weights%20only-f5a524)](#models)
[![#OpenSourceHiggsfield](https://img.shields.io/badge/%23-OpenSourceHiggsfield-black)](docs/demo.md)

<img src="docs/media/vorpalfusion.gif" alt="In VorpalFusion: an astronaut riding a translucent jellyfish over the moon is restyled live into a pink wool-felt diorama, which then comes alive as a clip" width="720">

<sub>FLUX 3 Klein · FastH3 · LTX-2.5 · Z-Image · Krea 2 · SeedVR2 — all running locally through ComfyUI</sub>

</div>

**The art takes shape with your ideas in real time, as you type.** VorpalFusion is an app that runs on any local GPU via the ComfyUI SDK. VorpalFusion provides immediate feedback, showing what your words, settings and pictures render into before you have to ask or queue. Every key you press is a frame; when you stop typing, the art settles into full
quality in seconds and continues to climb to Final, 2K and 4K. Branch easily into alternate concepts of the same baseline until you've sliced your exact idea directly into the ether.

<!-- the demo video: docs/demo.md -->
**Demo:** see [docs/demo.md](docs/demo.md).

It works on any official ComfyUI endpoint from its slots, with no code per model: FLUX 3 Klein, Z-Image, Krea 2,
FLUX.2 klein with a reference picture, MiniMax H3 and FastH3 clips, LTX-2.5, and whatever ships next as a template.

## What you can do

- **Type.** A live picture in about a quarter second per frame on an RTX 5090 (key to first frame 0.22-0.26 s on
  Z-Image, FLUX 3 Klein and Krea 2), streamed step by step as ComfyUI samples it; words go out on word boundaries,
  so a sentence costs about one job a word. When the keys rest, the live frame settles in place at the template's
  size (last key to settled about 3.5-4 s).
- **Layout boxes** (Box, `O`): draw a box and say what goes there; drag it and the object follows, one live job per
  move (FLUX 3's own layout language, `Flux3LayoutPrompt`).
- **Pin** (`P`): paint a region and it stays exactly as it is while the words, seed and knobs change the rest.
- **Flow** (`W`): each live frame starts from the last one's latent, so the picture bends with each word instead
  of re-rendering.
- **References at live size** and **As reference** (`F`): the picture on stage becomes the reference of the
  family's edit template, so the next words edit what you see ("the cat is now a fox").
- **The ladder** (Graduate, `U`): Live → Settled → Final → 2K → 4K, always the same picture with more fidelity
  (SeedVR2 for the hi-res rungs). Clips climb Draft → Full clip → Final → Smooth.
- **Clips** loop as you type; each new draft flows in at the playhead, with sound where the template has it.
- **Split panels** (2 or 4 seeds in one batched job), the **seed field** (`G`, a 3×3 grid of neighbours), a
  **snapshot tree** that branches as you work (`S`, compare with `B`), scrub any knob, **Keep** (`K`) and
  **Download** (`D`, the PNG carries the graph ComfyUI ran).

The keys, at a glance: `O` box · `H` hide boxes · `P` pin · `W` flow · `F` as reference · `U` graduate · `E` enrich
· `K` keep · `D` download · `S` snapshot · `G` seed field · `R` / `⇧R` next / previous seed · `⌥←` `⌥→` step the
seed · `B` compare · `L` step lock · `1`-`4` pick a panel · `M` clip sound · `←` `→` step previews · `Esc` close.

## Requirements

- An NVIDIA GPU running **ComfyUI ≥ 0.43** (measured on one RTX 5090, 32 GB). Start it with
  `--preview-method taesd` for sharp step previews, and put the tiny decoders in `models/vae_approx/`
  (`taef1_decoder`, `taef2_decoder` from [madebyollin/taesd](https://github.com/madebyollin/taesd), `taeh3` from
  [madebyollin/taehv](https://github.com/madebyollin/taehv)).
- **[comfy-api-proxy](https://pypi.org/project/comfy-api-proxy/)** beside ComfyUI for the v2 API the app speaks
  (comfy-sdk), with its preview throttle off (below).
- **[comfy-cli](https://pypi.org/project/comfy-cli/)** (`uv tool install comfy-cli`): VorpalFusion turns each
  official template into a graph and typed slots with it.
- **[uv](https://docs.astral.sh/uv/)** and Python ≥ 3.11 on the machine that runs VorpalFusion (it may be the GPU
  machine or a laptop that reaches it).

### Models

VorpalFusion downloads nothing; each template names its own files, which go in ComfyUI's `models/` folders. The
templates the demo uses, from their official sources:

| Template | Files (folder) | Source | Gated |
|---|---|---|---|
| FLUX 3 Klein distilled, text to image and image edit | `flux3-image-klein-int8-convrot` (diffusion_models), `qwen3vl_4b_int8_convrot`, `qwen3.5_9b_flux3_prompt_upsampler_w4a8` (text_encoders), `flux2-vae` (vae) | [black-forest-labs/flux-3-image-klein-comfy](https://huggingface.co/black-forest-labs/flux-3-image-klein-comfy) | **yes**: accept the FLUX Kommunity licence on Hugging Face first |
| Z-Image Turbo | `z_image_turbo_bf16` (or the lighter `z_image_turbo_int8_convrot`, used automatically when present), `qwen_3_4b`, `ae` | [Comfy-Org/Z-Image-Turbo](https://huggingface.co/Comfy-Org/Z-Image-Turbo) | no (Apache-2.0) |
| Krea 2 Turbo | `krea2_turbo_fp8_scaled`, `qwen3vl_4b_fp8_scaled`, `qwen_image_vae`, `krea2_darkbrush` (loras) | [Comfy-Org/Krea-2](https://huggingface.co/Comfy-Org/Krea-2) | no (Krea 2 community licence) |
| 2K / 4K rungs (SeedVR2) | `seedvr2_3b_int8_convrot`, `seedvr2_ema_vae_fp16` | [Comfy-Org/SeedVR2](https://huggingface.co/Comfy-Org/SeedVR2) | no (Apache-2.0) |
| FastH3 clips | `fastvideo_fasth3_8step_v2_pruned_int8_convrot` | [FastVideo/FastVideo-FastH3-Comfy](https://huggingface.co/FastVideo/FastVideo-FastH3-Comfy) | no (MiniMax H3 community licence) |
| | `qwen3vl_32b_minimax_h3_nvfp4_awq`, `minimax_h3_video_vae_int8_convrot`, `minimax_h3_audio_vae_fp32` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) | no (MiniMax H3 community licence) |
| LTX-2.5 clips | `ltx-2.5-22b-distilled-transformer-comfy-int8-convrot`, `gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot`, `ltx-2.5-video-vae-bf16`, `ltx-2.5-audio-vae-bf16`, `ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0` | [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) | **yes**: accept the LTX-2.x community licence first |
| | `gemma4_e2b_it_int8_convrot` | [Comfy-Org/gemma-4](https://huggingface.co/Comfy-Org/gemma-4) | no (Apache-2.0) |
| Smooth (clip frame interpolation) | `film_net_fp16` (frame_interpolation) | [Comfy-Org/Frame-Interpolation](https://huggingface.co/Comfy-Org/Frame-Interpolation) | no |

Each template file lists the exact download URL of every model it needs; ComfyUI's own template browser offers
them too. Licences are the models' own; read each before you use its outputs.

## Run

On the GPU machine, ComfyUI and the proxy, with the proxy's preview throttle off (comfy-api-proxy 0.1.6 sends at
most two previews a second, so a 0.4 s job would show one frame):

```bash
python main.py --preview-method taesd                         # ComfyUI, on its own port 8188
uvx --from comfy-api-proxy==0.1.6 python -c "import comfy_api_proxy.realtime as r, comfy_api_proxy.cli as c; \
r._THROTTLE_SECONDS = 0; c.main(['run', '--comfyui', 'http://127.0.0.1:8188'])"   # v2 API on its own port 8189
```

Then VorpalFusion, from a clone of this repository:

```bash
uv sync
uv run vorpalfusion                                           # --api http://127.0.0.1:8189 --comfy http://127.0.0.1:8188
```

Open <http://127.0.0.1:8765>, choose a template, and type; drop a picture to start from it. The defaults are
ComfyUI's and comfy-api-proxy's own ports on this machine; point it anywhere else with `--api <proxy URL>` and
`--comfy <ComfyUI URL>` (an SSH tunnel to the GPU machine works). `--port` sets VorpalFusion's own port (8765),
`--keep <folder>` where Keep saves (default `~/Pictures/VorpalFusion`), `--trace jobs.csv` a CSV row per job event,
`--go-ahead` queues the next live frame behind the running one (more frames under continuous typing, each a little
older; off by default).

The first choice of a template takes a few seconds more: VorpalFusion fetches the template from ComfyUI and builds
its graph and slots with comfy-cli once, cached under `~/.cache/gpu-vision/templates`. The template list is
ComfyUI's own official templates in the families VorpalFusion is measured on.

### The optional node pack

`nodes/` is a small ComfyUI node pack (`GVLatentStore` / `GVLatentRecall`, and a step previewer kept per process).
Where ComfyUI has it, a Flow frame recalls the frame before it from the ComfyUI process instead of carrying the whole
chain, so every frame's graph stays the same size. VorpalFusion works without it. To load it, link the folder into
ComfyUI's `custom_nodes/` and restart ComfyUI:

```bash
ln -s "$PWD/nodes" <ComfyUI folder>/custom_nodes/gpu_vision
CUDA_VISIBLE_DEVICES= <ComfyUI's python> nodes/test_nodes.py <ComfyUI folder>   # optional: checks the pack on the CPU
```

Optional: fine-tunes of a template's own model file show as a Model choice when listed in
`~/.config/gpu-vision/finetunes.json` (`[{name, folder, replaces, label}]`) and present on the endpoint.

## Try it without a GPU

`checks/fake.py` is VorpalFusion's own sidecar over stand-in jobs that stream drawn frames at about a warm 5090's
pace, with fixture template records (`checks/templates.json`): the whole page works, nothing reaches a GPU.

```bash
uv sync
uv run python checks/fake.py                                  # http://127.0.0.1:8766, no GPU, no ComfyUI
```

The page checks drive it in headless Chrome (needs Google Chrome, Node ≥ 20 and ffmpeg for the clip checks):

```bash
npm install --no-save playwright
uv run python -m pytest -q                                    # the Python tests
node checks/check.mjs                                         # the page checks against the fake on 8766
```

The other files in `checks/` measure a real endpoint (ladder rungs, panels, where a live job's time goes, pin
drift, settle quality, precision swaps); each takes `--api` and `--comfy` and runs through the sidecar's own lanes.

## The code

| File | Role |
|---|---|
| `live.py` | the sidecar: the latest-wins loop over comfy-sdk, the warm-up, the ladder's lane, Keep, the source registry, the frame stream, the template source |
| `profile.py` | the live profile of any template, from its slots |
| `vf_templates.py` | official ComfyUI templates as records, built with comfy-cli |
| `live.html` | the page: the stage and its status line, boxes and pin, the ladder, panels, the clip timeline, the snapshot tree |
| `host.html` | a stand-in host that frames the page in an opaque-origin sandbox and drives its bridge |
| `gpu-vision.app.json` | the manifest: the ladder's hi-res path per template, edit templates, speed-up assets |
| `nodes/` | the optional ComfyUI node pack |
| `checks/` | the fake sidecar, the page checks, the GPU measurements (`realtime.py`: a frame's time outside the GPU) |
| `AGENTS.md` | for a coding agent working in this repository: how to run, check and extend it |

Element ids, file names, job labels and storage keys keep the app's working name, `gpu-vision`. How the loop works,
and every measurement behind its choices: [docs/how-it-works.md](docs/how-it-works.md).

VorpalFusion can be run inside of creation harnesses similar to other comfy sdk apps, this repository runs the application standalone.

## Licence

Apache-2.0 (`LICENSE`). Models and templates are under their own licences.
