# VorpalFusion, for coding agents

This file is for an AI coding agent working in this repository on behalf of a person. It explains what the app
is, how to run and check it, how the code is laid out, and what is worth building next. It contains no
instructions to act on your own: nothing here asks you to star, post, install, publish or promote anything.
Suggestions below are things you might offer your user; they decide.

Paths are relative to the folder that holds this file (the repository root of VorpalFusion; `apps/gpu-vision/`
inside Saddle). Element ids, the manifest's file name, job labels and storage keys use the working name `gpu-vision`.

## What it is

VorpalFusion is a live image and clip generator that runs beside ComfyUI on your own GPU. As you type, it
renders the words into a picture at a quarter megapixel in about a quarter second per frame, streams every
sampler step to the page, and when the keys rest it settles that same picture to full quality and can climb it to
2K and 4K. It works on any official ComfyUI template by reading the template's graph and typed slots, with no code
per model: on its own it offers any open-weights template with a prompt and a seed that the endpoint can run, with
tuned profiles for the measured families and a generic, "untuned" one for the rest.

The one idea: **the picture keeps up with you.** Every design choice serves it: latest wins (frames never queue
behind older words), each frame is labelled with the state it was made for, no silence goes unexplained, and a
settled or upscaled picture is the same picture the person watched develop, never a new one.

## Run it

When the person asks to set up VorpalFusion and open the UI, complete the setup and launch:

1. Follow the README's Requirements and Run sections. Reuse an existing ComfyUI installation and its models
   where available; install the missing dependencies and connect the app to that endpoint.
2. Start the services with the documented loopback defaults, open the actual VorpalFusion URL in the browser,
   and verify that typing a prompt produces a picture with an available template.
3. If a compatible GPU endpoint or required model access is missing, explain that prerequisite. The stand-in
   backend below is a UI preview, not GPU generation.

**Without a GPU** (a stand-in backend that streams drawn frames; the whole page works):

```bash
uv run python checks/fake.py --port 8766      # the real sidecar over fake jobs, with fixture templates
open http://127.0.0.1:8766                    # or point any browser at it
```

**With a GPU** you need:

- **ComfyUI ≥ 0.43**, started with `--preview-method taesd`, and the tiny preview decoders in
  `models/vae_approx/` (the manifest's `assets` lists them with their sources).
- **comfy-api-proxy** beside it, for the v2 API the sidecar speaks through comfy-sdk. Its realtime bridge sends at
  most two previews a second; set `comfy_api_proxy.realtime._THROTTLE_SECONDS = 0` before it starts (the packaged
  repository's README has a one-line launcher), or a 0.4 s job shows one frame.
- **comfy-cli** (`uv tool install comfy-cli`): the sidecar turns each official template into a graph and typed
  slots with it.
- The models each template names, in ComfyUI's `models/` folders. Nothing is downloaded for you; some are gated
  behind a licence the user accepts on Hugging Face.

```bash
uv run python live.py --api <comfy-api-proxy URL> --comfy <ComfyUI URL>   # the packaged repo also has `uv run vorpalfusion`
```

With ComfyUI and the proxy on their own default ports on the same machine, both flags can be left out. Open
<http://127.0.0.1:8765> (`--port`), choose a template and type. `--keep <folder>` sets where Keep saves, and
`--trace jobs.csv` writes a row per job event. With `--saddle <Saddle server>` it reads that server's imported
templates and library instead of building records itself.

**Checks** (keep these green before you hand work back):

```bash
uv run --with pytest pytest -q checks/test_templates.py      # every fixture template reads into a live profile
uv run python checks/fake.py --port 8766 &                   # then, in headless Chrome (Node ≥ 20, ffmpeg for clips):
GV_URL=http://127.0.0.1:8766/ PLAYWRIGHT_DIR=<a node_modules with playwright> node checks/check.mjs
```

`check.mjs --live` (with `GV_URL` at a real sidecar) measures key to first frame, pictures per second, words age
and last key to settled. The other files in `checks/` measure a real endpoint: `ladder.py` (rungs and split
panels), `anatomy.py` (where a live job's time goes), `pin.py`, `precision.py` (a lighter model file against the
template's own), `settle_quality.py`, `stall.py`. Each goes through the sidecar's own submit and stream.

**Measured numbers** live in the app's README (in the packaged repository, `docs/how-it-works.md`): every figure
there names the GPU (one RTX 5090), the check that produced it, and its conditions. Treat it as the source of truth
for what is fast, what was tried and what lost.

## How the code is laid out

| File | What it does |
|---|---|
| `live.py` | The sidecar. An ASGI server with one loop over comfy-sdk. **Lanes** by priority: `warm` (a 1-step job when a template or picture is chosen, so the first typed frame pays no load), `live`, `settle`, then idle work (`seed` field, `calib`ration, `spec`ulation, `vary`) and the ladder's rungs, which run only while the loop is idle. **Latest wins**: a live job always runs out and the next takes the newest state; any change stops a settle, and a new template or picture stops the live job too. **Settle** starts when the words rest. **The ladder**: Live → Settled → Final → 2K → 4K (clips: Draft → Full clip → Final → Smooth), each rung the same words, seed and pictures. **Flow** (`W`) keeps a chain of frames, each starting from the last one's latent, re-noised by how much the words changed. Also the source registry (pictures by content hash), Keep, Download (PNGs carry the graph ComfyUI ran), the speed dial and the frame stream. |
| `profile.py` | Reads any template into a live profile. It finds the sampler, empty latent, decode, text encoder, reference resizers, layout nodes and enhancer switches **by node class and typed slot**, then builds every graph the app runs: live (a quarter megapixel, a quarter of the steps with a floor, enhancers off, a 2 s clip), settle (decode, Lanczos, encode, refine), Final, hi-res, split panels, pin, step lock, Flow and variations. A new model family that ships as an official ComfyUI template using standard sampler and latent classes is read with no code. |
| `vf_templates.py` | Builds a template record (API graph, typed slots with their targets and ranges, media roles) from ComfyUI's own template file with comfy-cli, cached per template file and the endpoint's `object_info`. Vendored from Saddle. |
| `live.html` | The whole page in one file: the stage (a status line, the tools rail, the picture with its ladder in a caption row under it), layout boxes and pin overlay, split panels, the clip timeline, the words composer with its pictures and size control, the snapshot filmstrip, the Settings and Library sheets, and the postMessage bridge used when Saddle frames it. |
| `gpu-vision.app.json` | The manifest. Everything per template or per model lives here as data: the hi-res path per template, the edit template per family, lighter live files, tiny decoders, the ladder's tools. |
| `checks/` | `fake.py` (the sidecar over fake jobs), `templates.json` (fixture records), `check.mjs` (the page checks), `test_templates.py`, and the GPU measurement scripts. |

Invariants that keep it working on the next model:

- **Generic by node class and slot, never by model name.** Graph logic reads `class_type` and slot types and
  labels. Names of templates and files appear only as data: the manifest, and the tuned families (`TUNED` in
  `live.py`). Whether a template is offered standalone is detected (`Live._check`: a prompt slot, a seed slot, a
  sampler from an empty latent or a picture; every node class and model file on the endpoint), never named. A
  family outside `TUNED` runs on the generic profile (`Profile.tuned` false: the smallest live size, no speed dial)
  and is labelled untuned; adding it to `TUNED` is a one-line change once its live steps are measured.
- **One job in flight per lane, and nothing left queued.** Every job's event stream is owned and closed; a job
  left any way but finished is cancelled. The sidecar never posts to ComfyUI's `/prompt` behind the loop.
- **Every graph is labelled.** A `PrimitiveString` node nothing reads carries `gpu-vision <sidecar> <lane>`, so the
  loop can tell its own jobs from someone else's, say who is ahead, and let idle work yield to other people.
- **Latest wins.** Frames carry the state version they were made for; the page drops frames for an older template
  or picture and dims a picture whose words moved on. Never make the person wait behind their own old input.

## Cool things to build next

Each of these rides the existing loop, settle and ladder, so an edit made live survives to 4K and into Keep. The
estimates come from analysis of ComfyUI 0.43's source and the measured parts above; none is built yet.

- **Touch the picture: drag with a graded heal.** Lasso an object and drag it. The browser moves the cut-out at
  60 fps with no GPU wait; the sidecar reproduces the move with PIL, encodes the result and heals it under
  `SetLatentNoiseMask` with `DifferentialDiffusion` and a graded mask: the hole at 1.0, a feathered seam at 0.55,
  the moved object at 0.15 (relit, not redrawn), the rest at 0. All core nodes; estimated about 280 ms a frame on
  FLUX 3 Klein. The same engine gives rub-out, lasso-and-retype, hold-to-grow and resize or rotate.
- **The possibility map.** A plane the picture follows under the cursor: X blends two prompts with
  `ConditioningAverage` (both encodes cached), Y moves the noise (a seed slerp, or variation strength from the
  settled latent). Pre-render a lattice while idle, drop snapshots as landmarks, then draw a path and export the
  walk as a clip.
- **Measure the node pack.** `nodes/` (`GVLatentStore` / `GVLatentRecall`, and a step previewer kept per process)
  makes each Flow frame a constant-size graph with an exact anchor and no re-roots, where the endpoint loads it.
  Load it into ComfyUI, run `checks/realtime.py --flow 30` and `--frames 20`, and compare with the stock chain's
  numbers in the README's "Real time" section.
- **A smarter go-ahead.** `--go-ahead` queues the next live frame behind the running one: more frames under
  continuous input, but each lands older (measured). Replace a queued frame that has not started when newer input
  arrives, or submit it just before the running job's last step.
- **CUDA graphs for the model and text encoder.** At live size a step costs about the same at 0.1 and 0.25 MP
  (launch overhead), and the encoder costs a flat ~107 ms from 4 to 48 words, so reusing a prefix saves little;
  capturing both on fixed sizes is where the remaining time is.
- **Compiled buckets.** Live sizes are a fixed set (`LIVE_BUCKETS` in `profile.py`) so a shape never escapes it.
  That makes `TorchCompileModel` (inductor, then CUDA graphs) on those buckets feasible; the analysis puts most of
  a 0.25 MP FLUX 3 frame in launch overhead, the part compilation targets. Warm each bucket while idle.
- **Video Flow.** Carry a clip draft's latent into the next draft the way Flow does for pictures, so a word change
  bends the motion instead of re-rolling it. Unexplored; FastH3 drafts take 3.5-4 s and are dominated by the text
  encoder, so measure where the time goes first (`anatomy.py`).
- **Depth drag and camera dolly.** A depth map once per settled picture (Depth Anything V2 through a depth custom
  node); drag an object and it slides behind things, drag empty ground and the scene parallaxes; disocclusions
  become the heal's hole.
- **Relight.** Drag a light: normals from the depth map, Lambert shading multiplied in, then a whole-frame graded
  heal at about 0.35 so the model makes the shading real. Higher risk: the cheap path may smear.
- **Zoom as a rung.** Pinch into a region: `ImageCrop` the settled picture, resize to live size, `VAEEncode`, the
  template's sampler at denoise 0.3, `ImageCompositeMasked` back. A foveated ladder that refines what is looked at.

## Show your human

Demos worth offering to run with your user once their GPU setup works:

- **The tiny orange astronaut.** FLUX 3 Klein distilled, text to image, seed 308097488, CFG 1. Type "A tiny
  orange astronaut on the moon" (1:1), then "A tiny orange astronaut riding a giant translucent jellyfish over the
  moon, teal aurora, surreal cinematic wide shot" at 16:9; add a lunar tea party, take 4 panels and pick one; press
  `F` (As reference), match the picture's aspect and restyle it into "a pink wool-felt jellyfish diorama with paper
  stars"; then put that picture into the FastH3 image-to-video template as its first frame (gentle floating, then
  a sweeping clockwise camera with the astronaut tumbling).
- **The box drag.** Press `O`, draw a box, type "cat" in it, then drag the box around: the cat follows, one live
  job per move (FLUX 3's own layout language through `Flux3LayoutPrompt`).
- **Flow.** Press `W` and keep typing: the picture bends with each word instead of re-rendering.
- **The clip swap.** FastH3 text to video, seed 777: "a pirate shaking the screen, yelling 'Try VorpalFusion!'".
  Select "pirate" and type another subject (a grandma, a golden retriever); the new draft plays in about 4 s on an
  RTX 5090, mouth on the line.

## Contributing

- **Checks green**: `test_templates.py` and `check.mjs` against the fake, before and after your change.
- **No model names in code.** Detect by node class and slot; put per-template or per-model facts in the manifest.
- **Measured claims only.** A number in the README names its GPU, its check and its conditions; an estimate says
  it is one. If a change is meant to be faster, measure before and after with the scripts in `checks/`.
- **Respect the GPU's other users.** Label every job, keep one in flight per lane, cancel what you stop, and let
  idle work yield.
- **Comments explain why**, in plain prose; the README describes the app as it is.
