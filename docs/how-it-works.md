# How VorpalFusion works

VorpalFusion was developed as GPU Vision, a Saddle app; element ids, file names, job labels and storage
keys keep that working name. Every number here was measured on one RTX 5090 with ComfyUI 0.43 through comfy-api-proxy.

## What the loop does

- **One status line, in plain words.** "Loading FLUX 3 · ~8 s", "Live · 0.4 s",
  "Refining 8/16", "Finishing (decoding 1 MP)", "Settled · 3.2 s", "GPU shared · 3
  ahead", "Waiting for another VorpalFusion · 1 ahead", "Waiting on the GPU", "Offline:
  reconnecting", "Drop a picture to edit". A failure reads as a
  sentence with what to do (the endpoint's own words sit in its tooltip), and it
  clears on the next frame or template. Every frame carries the version of the state
  it was rendered for: a frame made for an earlier template or picture is dropped,
  and a picture whose words have moved on while nothing renders (a failure, a busy
  GPU, offline, empty words, a job gone silent) is dimmed and labelled "older
  picture". **No silence goes unexplained:** a running job that says nothing for 1 s
  makes the sidecar say what it waits on ("Loading Z-Image…" when the silent node is a
  loader or a sampler before its first step, "Waiting on the GPU" otherwise), and words
  that got neither a frame nor a phase within 1.5 s read "Waiting on the GPU" on the
  page's own watchdog. The `i` button shows key to first frame, the last job's time
  and the ms per step. Phase changes, never step counts, are announced to a screen
  reader.
- **Warm on choose.** Choosing a template (or a picture) loads it at once with a
  1-step job while the line says "Loading …" (with the last load's time for that
  template), so the first typed frame does not pay the load. An edit or reference
  template never submits without its picture; the line says "Drop a picture to
  edit".
- **Live.** Each change to the words, the seed, a setting or a picture (debounced
  60 ms) submits the template's graph at its live profile. Words go out on a word
  boundary: while a word is being typed, the words stop before it (the first word
  excepted, so the first frame waits for nothing), and the whole text goes out once
  the keys rest 250 ms; no job is spent on a fragment the encoder cannot read, and a
  sentence typed at 10 characters a second costs 11 live jobs instead of 56. The
  live profile: about a quarter
  megapixel, a quarter of its steps (at least 4; half, at least 2, for a schedule of 4 or fewer), enhancers off, a 2 s clip. A live
  job always runs out; the next one takes the newest words, so frames keep coming at
  the GPU's pace while the creator types. Each sampler step's preview streams to the
  page as it lands (the model's estimate of the finished image, so step 1 already
  shows the composition), cross-faded, with a blur that clears as steps finish. A
  live image's last step preview is its frame, sharp with a tiny decoder, so the
  live graph stops at the sampler: each picture output becomes a text preview of the
  latent its decode would read (`PreviewAny`), so ComfyUI runs no full decode and
  writes no PNG nobody looks at. A new template or picture stops the live job at
  once. ← and → (outside the words) walk back through the step previews of the job on
  stage; the next frame returns to live. A clip's draft decodes its frames with the tiny
  decoder the manifest pairs with its VAE (H3's `taeh3`; its sound keeps its own VAE), and
  since a draft takes seconds, a clip renders whole words only: words sent mid-sentence
  stop what runs (the settle, a draft for older words) and start nothing, and the whole
  words go out once the keys rest 250 ms.
- **Settle.** When the words rest 450 ms, the loop settles from the live frame: the
  live graph, which ComfyUI serves from cache, has its picture decoded, resized to
  the template's size with Lanczos and encoded again, then refined at denoise 0.5,
  so the picture sharpens in place instead of recomposing (a latent upscale cracks
  faces and redraws objects on FLUX 3; see "Settle quality"). A KSampler refines with
  a second KSampler (Z-Image, Krea 2, FLUX 3 Klein and its edit); a custom sampler
  (FLUX.2 klein) refines with its own schedule rebuilt at the template's steps and
  cut to its last half (`SplitSigmasDenoise`). Clips settle by rendering the
  template as its authors tuned it. Enhancers stay off in the settle, as in live, so
  it refines what was watched. The settled picture shows as a JPEG (quality 92) that
  ComfyUI makes beside the GPU from its own PNG (`/view?preview=jpeg;92`, a seventh of
  the bytes over the wire); the sidecar fetches the original only when Keep asks for
  it (`GET /output/<job>`). Typing again cancels the settle at once, even mid-step.
- **The ladder: Live → Settled → Final → 2K → 4K.** Every rung keeps the snapshot's words, seed,
  pictures and settings: the same picture with more fidelity, never a new one. *Final* refines the
  settled frame at the template's full steps with its enhancer on at denoise 0.3, so the composition
  holds (only the nodes an enhancer switch reaches are copied; the loaders, the pictures and the settle
  half stay cached). *2K* and *4K* climb from the Final by the path the manifest names per template
  (`ladder.hires`): Saddle's SeedVR2 upscale tool on the Final's picture, or a tiled refine with core
  nodes (Lanczos to the long side, `VAEEncodeTiled`, the template's sampler on the last 30 % of its own
  schedule, `VAEDecodeTiled`). A clip climbs Draft → Full clip (the template's own size and length, same
  seed and first frame; the line says its motion can differ from the draft) → Final (when it has an
  enhancer) → Smooth (Saddle's frame-interpolation tool). Graduate (the stage's ▲, or U on any tree node)
  asks for the next rung; the sidecar runs rungs one at a time in a lane below live and settle: any input
  stops a running rung and live keeps working, and the rung runs again from the top once the loop rests.
  The line names the rung and its progress ("Final 9/16", "2K · 4.4 s · 2048×2048"); each rung lands as a
  child snapshot with a badge in the tree, so B and side by side compare one rung with another, and the
  stage takes it when the creator still looks at that snapshot. A rung shows by URL from a JPEG ComfyUI
  makes beside the GPU. **Keep** (K) keeps the highest rung of what is on stage: standalone, the original
  (fetched only now) and a JSON beside it (words, seed, template, values, pictures by id with their library
  parents, the panel, the rung and its recipe, the lineage); framed, the rung's full recipe goes to Saddle
  as `keep`.
- **Enrich, and what the model read.** For a template with a prompt enhancer (an upsampler that rewrites
  the words: FLUX 3 Klein, Krea 2) the Final is the Enrich (E). A window beside the stage shows "You
  wrote" and "The model read", read from the Final's text previews in ComfyUI's history: words kept,
  words added marked, words dropped struck, and "Use these words" puts them in the box.
- **Split panels.** Panels 1 / 2 / 4 render that many seeds' pictures in one batched job (`batch_size` on
  the template's empty latent, or `RepeatLatentBatch` over a picture's latent; the words encoded once).
  Panel 1 streams its step previews; the others land with the job's output. A panel is (seed, index),
  recorded in snapshots and reproduced that way (`LatentFromBatch` before the settle). Nothing settles
  until a pick: a click or 1-4 settles that panel and climbs from it, and the others stay in the tree as
  alternatives. 2 panels sit side by side, or stacked when the stage is taller than wide; 4 in a square.
- **Pictures by role.** The template's image slots each take a picture: the first
  is the source, the rest references, labelled by the role Saddle's summary gives
  them (first frame, last frame, reference 1, 2); a reference carries a role chip
  (subject, style, background) from the template's own words. The tray shows one drop
  target per slot; a picture shows on the stage the moment it is chosen. A slot takes
  a file, a Saddle library result, a snapshot dragged from the tree or a seed tile.
- **The clip timeline.** For a clip template the tray becomes a strip under the stage:
  a card per picture slot at its moment (a first frame at 0, a last frame at the end, a
  guide at the time its own slot holds, found by tracing the guide's `frame_idx` back to
  its number), references apart with their role chip. Drop onto a card from anywhere; drag
  a card along the strip to move a guide, onto another card to swap their pictures, off
  the strip to remove it; the draft renders again. Keys on a card: ← → move a guide 0.5 s
  (a fixed frame swaps with its neighbour), Delete removes, Enter picks a file; each
  change is announced.
- **Library.** Standalone, a Library drawer reads the Saddle server's library through the
  sidecar (`GET /library`: Saddle's `/api/results` with search and its boards as filters,
  thumbnails from `/api/artifacts/<id>/thumbnail`); results drag onto any slot, a card or
  the stage, and Enter puts one in the first empty slot. A library picture comes by its
  rel (`POST /source/library {rel}`: the sidecar fetches it from Saddle's API and keeps it
  like a dropped one), with its library id as the picture's parent for lineage.
- **One size control.** A template whose slots set its size shows one control: its own
  aspect choice as chips (FLUX 3 Klein, Krea 2, LTX-2.5, H3 and FastH3 ship one) or, where
  a latent's width and height are slots (Z-Image), aspect chips 1:1, 4:5, 3:4, 9:16,
  16:9, 21:9 (and "match picture" once a source is in), mapped to width × height at the
  template's area rounded to the multiple its defaults use; and a size class: Draft (a
  quarter of the area), HD (its own), Max (twice). A clip's duration (2 / 5 / 10 s) and
  frame rate show as chips too. The raw numbers stay in the drawer under node plumbing;
  the live profile still shortens drafts. A text-to-image
  template takes its source by replacing the empty latent its sampler starts from,
  denoised as far as the "change" slider says. An optional slot left empty is
  dropped from the graph.
- **Source registry.** `POST /source` keeps a picture by its content hash, starts
  its upload at once and returns `{id}`; `POST /source/use {id}` confirms the
  sidecar still holds it (404 after a restart, and the page asks for the picture
  again); `GET /source/<id>` serves it back. States, snapshots and the bridge name
  pictures by id: `sources: [{slot, id}]`.
- **All settings.** The Settings drawer (standalone only) shows the template's
  slots as typed controls: a number with a range as a slider, a wide one as a
  field, a boolean as a switch, choices as a select, text as a field. Node plumbing
  (advanced slots, model files, file names) shows only with "Show node plumbing".
  Values go in `state.values` by address and apply to live and settle alike; the
  live profile still sets steps, size and enhancers for live only.
- **Clips loop.** For a video template the window plays the latest draft clip
  on a loop. The next draft develops in a corner thumbnail, and when it lands it
  flows in at the playing clip's playhead with a 280 ms cross-fade, so the motion
  carries on where it was. The settle flows in the same way. A thin bar along the
  clip's bottom edge fills with its elapsed part each loop (2 px, 6 px with the time on
  hover or focus; click or drag to seek, ← → step 0.5 s); a sound toggle (M; muted by
  default, the choice kept) shows for a clip whose file carries a sound track. Both fade
  in on hover, focus or pause and stay out of the picture's middle.
- **Seed field.** Once the settled picture has sat 1.2 s, the GPU fills its eight
  seed neighbours at live size, one tile at a time, and any input cancels it.
  Seeds (or G) opens the 3×3 grid; the settled picture is the first tile, and
  choosing any tile locks its seed. R shows the next seed the field already rendered
  at once and locks it (⇧R goes back); ⌥← and ⌥→ step the seed; a neighbour already in
  the field shows at once. The seed itself is a field, readable and editable. The
  field is idle work: it stops filling as soon as anyone else is waiting for the GPU.
- **Scrub any knob.** The template's own numbers worth scrubbing show as sliders,
  found from its slots by label (shift, guidance, a LoRA's strength; never seed,
  steps, size or a text model's sampling settings): Z-Image gets shift and CFG,
  Krea 2 its LoRA strength and CFG. Dragging one streams a live frame at the GPU's
  pace, and the picture morphs in place on its seed.
- **Snapshots, a tree that branches as you work.** A snapshot holds the whole
  condition: template, words, seed, every setting, the pictures by id, the change
  strength, and the frame. S (or the stage's pin) takes one; each settle takes a
  small, faded one; the same condition twice is one snapshot. Restoring a snapshot
  shows its frame at once and makes it the head, so editing from there grows a new
  branch while the old line stays. The tree draws a lane per branch, newest right:
  hovering (or focusing) a node previews it, choosing it restores, holding B compares
  the current picture with it (or, by default, with the snapshot before), and
  dragging one node onto another (or Shift+Enter on one, then another) opens them
  side by side. Each snapshot knows its parent, so Keep can carry lineage. The tree
  persists across reloads (small thumbnails in local storage; the oldest automatic
  snapshots go first when it fills) and exports or imports as a small JSON file.
- **Compare is an overlay.** The compared picture shows in its own layer above the
  stage, labelled with its words, seed and template; the stage itself is never
  touched, so releasing B always returns to the current picture.
- **Clips in the tree** show their poster frame with a ▶ badge.
- **Robust page.** Fetch failures are caught; going offline says so; the frame
  stream reconnects through one pending retry (the `online` event and the backoff
  never race), clears "Offline" the moment it is open, and the page resends its
  template, pictures and words. A reload restores the template, words, seed, pictures,
  settings and the tree. Unreadable files and empty words say so. In an
  opaque-origin sandbox (no storage) it works for the session; the sidecar answers
  CORS.
- **Layout and access.** The picture takes the stage. Above it, one status line: the
  phase with a dot, the GPU meter after a rule, the live job's progress along its
  bottom edge, and `i` for timings. On the left, the tools rail: Box O, Pin P, Flow W,
  As reference F, Lock L, Seeds G, New seed R, Snapshot S and the panels, each with its
  key and its pressed state in amber. Under the picture, its caption row: what it is
  (an older picture, a compared snapshot) and the ladder as one group, the rung, ▲ the
  next one, Enrich, Keep, Download. Under that (a clip template: under its timeline) the
  words box: its pictures above it, the template, model and size chips below it, and
  Settings, a sheet with the speed dial, live steps, seed, the template's knobs and all
  its settings. The snapshot tree runs along the foot as a filmstrip. From 320 px to
  1920 px nothing scrolls sideways: under 720 px the rail becomes a row of tools under
  the picture and the meter steps aside; a 40-snapshot tree scrolls inside its own lane
  at 375 px. Every control is reachable by
  keyboard; the tree and the seed grid are one tab stop each, with the arrow keys
  inside; tree nodes (40 px) and seed tiles are buttons with their words, seed and
  template as names; Import shows on an empty tree; the closed seed field is inert;
  Esc closes the field, the drawer, the side by side or the words window and returns
  focus to where it was, and leaves the words box so S, G, B and R work; the stage picture's alt text is its words;
  `prefers-reduced-motion` turns cross-fades, blur and glow off.
- **Never queues behind anyone, and knows who is ahead.** Every job carries a label
  in its graph, `gpu-vision <sidecar> <lane>`, on a `PrimitiveString` node nothing
  reads: ComfyUI never executes it, but its queue shows it (the proxy keeps comfy-sdk's
  metadata map out: it takes one opaque string). A live job that reports a queue
  position reads ComfyUI's own queue: its own jobs winding down never count; another
  VorpalFusion's seed tile is idle work it queues behind; anything else makes it cancel,
  say "GPU shared · N ahead" (or "Waiting for another VorpalFusion · N ahead"), and
  submit nothing, polling every 250 ms, until the GPU is free; then it renders the
  newest words.

## Speed, feel and Download

- **Speed dial and GPU meter.** Live-on calibrates a template on this GPU while the loop rests (1- and 2-step live
  jobs at 0.16 and 0.25 MP, fresh words and seed) to a fixed cost plus ms per step per megapixel, kept in
  `~/.cache/gpu-vision/calibration.json` by template, graph hash, GPU and model files. The dial (auto, snappy 250 ms,
  balanced 400 ms, pretty 700 ms) picks the largest live size that meets its target at the step floor, then as many
  steps as still fit. The meter shows ms/step, resident or partly offloaded (a job 1.6x slower than calibrated), the
  steps and size, and who is ahead.
- **Fast typing** (keys under 120 ms apart) renders one size bucket smaller (0.1 / 0.16 / 0.25 / 0.36 / 0.5 MP) until the
  first pause; the settle is never smaller.
- **Step lock** (Lock, L): the composition held at the middle step on the locked words and seed (two `KSamplerAdvanced`,
  or `SplitSigmas` with `DisableNoise` on a custom sampler), new words after it; the line says "Composition locked at
  step k of n".
- **Variations** (the seed field's second mode, with a strength): the live latent repeated eight times and refined in
  one batched job; a tile is (seed, index) and settles that picture (`LatentFromBatch`).
- **Speculation** when the loop rests after the field: the next R tile, the active knob's next positions, and the
  Final's first step once Graduate has been used; each lands in the frame cache, so moving there shows the picture
  from ComfyUI's cache at once. Any input stops it; it yields to anyone waiting.
- **Clips early.** A clip's step previews show on the stage until a clip plays, with Cancel: nothing renders again
  until the next change.
- **Download** (D; the stage's ⤓, a button on the node or tile under the pointer; shift-click or X selects nodes for one
  zip with `manifest.json`): the picture at its rung, the original ComfyUI made (a live picture: its frame as a PNG),
  named `<words>_seed<seed>_<rung>.png` (`_p<n>` for a panel); the PNG carries ComfyUI's `prompt` chunk (its own, or
  the graph submitted) and a `gpu-vision` chunk (words, seed, template, values, pictures by id, rung, lineage).
  ComfyUI's frontend (`scripts/metadata/png.ts`, `app.ts` `loadApiJson`) reads `tEXt`/`iTXt` chunks and loads an
  API graph whose node ids hold colons by remapping them. A clip downloads as its MP4.

Measured on an RTX 5090 (FLUX 3 Klein through the page, `check.mjs --live --ladder`, GV_LADDER=final,2k,4k;
another VorpalFusion page ran 7 jobs on the same GPU during it): Final 8.1 s after the click, 2K 6.7 s, 4K 14.1 s
(4096x4096), the line naming each rung and its steps. Clips (`--clips`, same run): FastH3 first preview 3.0 s after
the last key, draft 4.8 s, full clip 27.0 s, Smooth 11.8 s more (drift draft to full 61.9, full to Smooth 23.2);
LTX-2.5 draft 45.9 s (cold, behind the other page), full clip 68.5 s, Final 33.9 s.

The typing loop on a quiet card (three gated runs: no other job on the GPU for 30 s and the card under 10 %
for 10 s; medians): key to first frame Z-Image 226, FLUX 3 Klein 224, Krea 2 255 ms; words age p50 425 / 542 /
621 ms; last key to settled 3.43 / 3.77 / 5.86 s. A run with a neighbour ComfyUI computing on the same card (100 %,
500 W) read 1.2-2.4 s to the first frame on Z-Image, which is why the gate watches the card's load as well as the queue.

## Real time: what stands between an act and its picture

Real time here means the picture answers the thing the creator just did (a word, a box, a knob) before they do the
next one, moves there from where it was, and never shows an older answer without saying so. Three costs stand in the
way of any template: the GPU idling between frames while the loop does paperwork, the per-job setup ComfyUI repeats
for a frame, and a step that is launch overhead rather than arithmetic at live size. The loop attacks the first two
with comfy-sdk and a small node pack; the third needs the model captured as a CUDA graph on fixed sizes.

- **The next frame goes ahead** (with `--go-ahead`). Input that arrives while a live job runs submits its frame at once, its stream
  open, queued behind the running one; ComfyUI starts a queued prompt 1-2 ms after the one before ends. A loop
  that submits only after the end leaves the GPU idle while the end comes back and the next graph goes out. One
  frame goes ahead at a time and is never replaced, so it lands carrying the input of when it was submitted, and its
  version says so (measured below: more frames, each older). A new template, picture or Cancel cancels it before it starts. A Flow re-root, a warm-up, panels
  and variations wait for the end.
- **The node pack** (`nodes/`, link it into ComfyUI's `custom_nodes/` as `gpu_vision` and restart ComfyUI):
  `GVLatentStore` / `GVLatentRecall` keep a session's latents in the ComfyUI process by (session, frame). Where the
  endpoint lists them, Flow is a session there: each frame's graph is the live graph on its words whose sampler
  starts from the recalled latent of the frame before, mixed toward the recalled anchor, and stores its own. The
  graph is one size on every frame, the anchor is its exact latent (no picture round trip, so no re-root and no
  colour shift at one), and a frame that went ahead finds the latent of the one before it when its turn comes. The
  pack also keeps the step previewer: ComfyUI builds it (the tiny decoder, read from disk) at the start of every
  sampler. `nodes/test_nodes.py` loads the pack through ComfyUI's own loader on the CPU and checks both.
- **The live graph's output is the latent's size.** A live frame is its last step preview, so the graph ends in a
  text preview that only makes ComfyUI run the sampler; where the endpoint has `GetLatentSize` that preview is of the
  latent's size, not the latent printed as text.

Measured on the live lane (FLUX 3 Klein, 0.25 MP, `checks/realtime.py` through the sidecar's submit and stream, one
job at a time, medians of frames with no other client's job before them; another client's video jobs shared the
lane between runs):

| | |
|---|---|
| A one-step frame, back to back (12-30 frames a run, four runs) | exec 96-119 ms; submit 21-46, end reaching the sidecar 40-105, e2e 171-239 ms |
| GPU idle between two frames submitted after each end | **83-140 ms**, as long as the frame itself |
| GPU idle when the next prompt is already queued (ComfyUI's history, 13 such pairs) | **1-2 ms** (median 8 ms over all under 30 ms) |
| Text encoder by length (new words minus the same words on another seed) | 4 words 107 ms, 16 words 120, 48 words 107, 120 words 157: flat inside one 80-token pad, so reusing a prefix's KV buys little; it is launch-bound like the model |
| One step by live size (3 steps minus 1, 10 frames each) | 0.1 MP 37 ms, 0.25 MP 35 ms, 0.5 MP 64 ms: below a third of a megapixel a step costs the same whatever its tokens |
| Fixed cost of a job (one-step exec minus one step) | 60-75 ms; the previewer's construction is 12.6 ms of it on the CPU (`nodes/test_nodes.py`), then 0.015 ms kept |
| Output a text preview of the latent's size, 30 interleaved pairs | paired median -5 ms (-4 mean) of a 100 ms frame |
| Flow, stock chain, through the sidecar's loop (30 frames, one at a time) | same words exec 120 ms / e2e 197 ms, 39 nodes; a word 154 / 271 ms, 43 nodes; a phrase 199 / 345 ms, 51 nodes |

`--go-ahead` against one job at a time, on the live lane with the lane to itself (FLUX 3 Klein, 4 live steps,
fresh sidecar per run, alternating; GPU idle from ComfyUI's history):

| | One at a time | `--go-ahead` |
|---|---|---|
| Typing through the page, `check.mjs --live`, three runs each | words age p50 516 / 429 / 526 ms, p90 704 / 535 / 1446; 1.55-1.89 pictures/s; last key to settled 3.2 / 3.9 / 3.2 s | words age p50 471 / 441 / 429 ms, p90 607 / 737 / 507; 1.89-1.90 pictures/s; last key to settled 4.4 / 3.1 / 4.3 s (the settle job itself 2.1 s in all six) |
| Frames that went ahead while typing | | 0-1 a sentence: words go out on word boundaries, about two a second, and a 0.2 s frame has usually ended before the next |
| A knob scrub through the sidecar's loop (a new seed every 60 ms for 8 s, three runs each) | 2.24 / 2.12 / 1.75 frames/s; age p50 476 / 490 / 557 ms, p90 547-645; GPU idle between frames median 234-256 ms | 2.37 / 2.61 / 2.36 frames/s; age p50 774 / 638 / 727 ms, p90 790-884; GPU idle median 18-25 ms in two runs (about half the frames queued ahead), 267 in one |

So the next frame going ahead keeps the GPU busy (+6-35 % frames under continuous input) but each frame is older
when it lands (+150-300 ms at p50): it carries the input of when it was submitted and runs after the one before,
while a frame submitted after the end takes the newest input. It stays opt-in. Typing, where the page's word
boundaries already pace the loop, shows no difference beyond run to run noise.

Unmeasured on the GPU: Flow on the node pack and the kept previewer (they need the endpoint restarted with the
pack). The same checks on the same code as above, one job at a time and the pack absent: a one-step frame exec 105
ms (98 with the latent's size as output); Flow same words 108 / 211 ms, 39 nodes; a word 214 / 274 ms, 43 nodes; a
phrase 267 / 365 ms, 51 nodes.

## Touch the picture: boxes, pin, references

- **Layout boxes** (Box, O; any template with `CreateBoundingBoxes` feeding a layout node such as `Flux3LayoutPrompt`):
  draw a box on the stage and type what goes there; drag it or pull its corner, and every move renders a live job
  (one every 60 ms at most, the sidecar's latest wins, as typing). `state.boxes = [{bbox: [ymin, xmin, ymax, xmax] on
  0-1000, desc}]`; the profile writes them as the boxes node's upstream JSON and, where a template leaves the layout node
  on a preview (FLUX 3 Klein's text to image does), wires it between the words and the encoder; no boxes, the graph is
  the template's. The model reads the boxes as text appended to the words (FLUX 3's own layout language, no mask).
  Snapshots, the tree, the settle, every rung, Keep's JSON and Download's chunk carry them. The overlay is a thin
  outline with its word; H hides it; a focused box moves with the arrows (shift resizes), Delete removes it.
- **Pin** (P; a template whose sampler starts from an empty latent): draw a region and it stays exactly as it is
  while the words, seed and knobs change the rest. The picture on stage goes to the sidecar's registry; the sampler
  starts from it, fitted to its empty latent's size and encoded, under `SetLatentNoiseMask` of 1 − pin (core nodes:
  `SolidMask`, `MaskComposite`, a 4-cell `ImageBlur` on a 256 grid, about 8 px of feather at live size) at denoise
  1.0 with the template's own sampler; the settle carries the same mask at its size. The line says "Pinned · typing
  changes the rest"; Esc or P again unpins.
- **References at live size.** In live jobs every reference node (`resolution.megapixels` beside `images.*` inputs,
  `ReferenceMultiLatent` on FLUX 3 and FLUX.2 klein) takes the live bucket's megapixels; the settle and every rung
  refine on a copy of it at the template's own size (`gv:r:`), so only live pays less. On FLUX 3 Klein's edit
  template the empty latent is the first reference's, so this also brings its live picture to the live size.
- **Flow** (W; a template whose KSampler the settle refines): each live frame starts from the last one's latent, so the
  picture bends with every word instead of re-rendering. The sidecar keeps the chain since its last reset and diffs the
  words (punctuation and case aside): new words, or any change but the words, start from noise (the normal live job);
  one word added, dropped or replaced re-noises the last frame at 2 steps / denoise 0.5 of the template's own 4-step
  schedule, a phrase at 3 / 0.75, the same words at 1 / 0.0625, each through `LatentInterpolate(previous, anchor,
  0.85)` toward the newest frame whose words changed, on a fresh seed per frame. A frame's graph holds every earlier
  frame verbatim, so ComfyUI serves the chain from cache and runs only the new tail; every 10 frames the chain re-roots
  on its last picture and the anchor's (`LoadImage` → `VAEEncode`, both kept fixed until the next re-root, so the
  graph stays under 80 nodes and a frame's cost stays flat). On an endpoint with the node pack (`nodes/`, "Real
  time") the chain lives in ComfyUI instead: each frame recalls the last one's latent and the anchor's by number and
  stores its own, so the graph is one size and nothing re-roots. The live frame previews the head latent; the settle and
  every rung refine the chain's head on the words as they are now. The line says "Flow · a word · 232 ms". The levels
  come from a go/no-go test on FLUX 3 Klein: gentler levels never land a word
  and 1-step frames without the anchor cook within 5-10 frames.
- **Model** (a select beside the template, shown only when it has entries): fine-tunes of the template's own model file,
  `{name, folder, replaces, label}` from the manifest's `finetunes` and the user's own `~/.config/gpu-vision/finetunes.json`
  (read on each template choice). An entry is offered where the template uses `replaces` (after precision swaps) and
  the endpoint lists `name` in its folder; `state.model` swaps the file in live, settle and every rung, and snapshots,
  Keep's JSON and Download's chunk record it.
- **Pin as reference** (F, "As reference"): the picture on stage goes into the first reference slot; from a template
  with no picture slot, VorpalFusion switches to the family's edit template the manifest lists (`edit_templates`) and
  keeps the words, so the next words edit what was on stage.

Measured on an RTX 5090 (FLUX 3 Klein unless named, exec times from ComfyUI's history; other clients shared the GPU
at times, and a frame queued behind one is left out):

| | before | after |
|---|---|---|
| Box check, one "cat" box at 4 live steps, lower left then upper right | | the cat lands in its box both times, and in the settle |
| Pin, left half pinned, new words and seed, 16×16 gray (0-255) | | inside drift 1.0, outside 31.8 (`checks/pin.py`) |
| Edit template live job, portrait reference (exec, four runs each, uncontended) | 1.27-1.28 s at 1 MP (1024²) | 0.44-0.51 s at 0.25 MP (512²) |
| Edit template, submit to first step preview | 331-365 ms | 182-246 ms |
| Likeness of the edit to its reference, 64² | 0.957-0.963 | 0.945-0.969 |
| Flow frame exec, preview only (medians of uncontended frames) | live job from noise 323 ms | same words 118 ms, a word 232, a phrase 312, a re-root 161-339; submit to frame 230-300 ms for same words through an SSH tunnel |
| Final with the enhancer's `mtp` (two pairs, each on new words) | off 7.1 / 5.2 s | auto 8.4 / 11.4 s: slower, so the Final keeps the template's `off` (`FINAL_MTP`) |

## Where a live job's time goes

`checks/anatomy.py` splits a live job on a real endpoint, per family, on fresh seeds and fresh words so
ComfyUI's cache never voids a timing: the encoder is words changed minus seed changed, one step is one more
step minus seed changed, the fixed part (noise, sampler setup, the full decode and the output node) is what is
left of a seed-changed job, the transport is the identical graph end to end minus its 5-15 ms inside ComfyUI.
The live process's own log (`--verbose DETAIL`) gives each model's resident share per job (dynamic VRAM's
`Model loaded: ... ram_mb vram_mb`).

**Warm, models resident** (ComfyUI's history over hundreds of jobs; seed changed is the seed
field, words changed is typing; one step from the step sweep on the same ComfyUI):

| Family (live size, steps) | Words changed | Seed changed | Encoder | One step | Steps | Fixed (decode, PNG, setup) |
|---|---|---|---|---|---|---|
| Z-Image Turbo bf16 (496², 4) | 465 ms | 410 ms | 55 ms | 74 ms | 296 ms | 114 ms |
| FLUX 3 Klein int8 (0.25 MP, 4) | 360 ms | 280 ms | 80 ms | 40 ms | 160 ms | 120 ms |
| Krea 2 Turbo fp8 (0.25 MP, 4) | 651 ms | 565 ms | 86 ms | 120 ms | 480 ms | 85 ms |
| FLUX.2 klein 9B + a reference (0.25 MP) | 1182 ms | 965 ms | 217 ms | | | |

**Warm, the card free** (measured with `anatomy.py`, the ComfyUI under test alone on the 5090 but for 3 GB held by other processes,
`--verbose DETAIL`; cold is each family's 1-step warm-up after the others):

| Family (live size, steps) | Cold warm-up | Words changed | Seed changed | Encoder | One step | Fixed | Seed changed, live graph stopping at the sampler | Transport | First preview |
|---|---|---|---|---|---|---|---|---|---|
| Z-Image Turbo bf16 (496², 4) | 2.3 s | 477 ms | 417 ms | 60 ms | 74 ms | 121 ms | 354 ms (-63) | 21 ms | 180 ms |
| FLUX 3 Klein int8 (0.25 MP, 4) | 1.9 s | 396 ms | 282 ms | 114 ms | 48 ms | 90 ms | 232 ms (-50) | 15 ms | 156 ms |
| Krea 2 Turbo fp8 (0.25 MP, 4) | 11.0 s | 669 ms | 575 ms | 94 ms | 119 ms | 99 ms | 528 ms (-47) | 12 ms | 186 ms |
| FLUX.2 klein 9B + a reference (0.25 MP, 2) | 16.0 s | 768 ms | 546 ms | 222 ms | 185 ms | 176 ms | | 284 ms | 322 ms |

Even with the card free, dynamic VRAM kept 69 % of Z-Image's diffusion model resident: the live process itself
held the other families it had staged (29 GB in all).

**Starved: a neighbour holds the card** (another ComfyUI on the same 5090 held 27.5 GB staged after its own job;
the live process got 0.7 GB and streamed 96-99 % of each diffusion model from host RAM on every step):

| Family | Cold warm-up | Words changed | Seed changed | Encoder | Per step (stream) | Resident share | Transport |
|---|---|---|---|---|---|---|---|
| Z-Image Turbo | 9.0 s | 1652 ms | 1340 ms | 312 ms | 296 ms | 4 % | 51 ms |
| FLUX 3 Klein | 3.2 s | 914 ms | 656 ms | 258 ms | 138 ms | 4 % | 34 ms |
| Krea 2 Turbo | 8.7 s | 1732 ms | 1404 ms | 328 ms | 285 ms | 4 % | 21 ms |
| FLUX.2 klein 9B edit | fails: "VRAM grow failed" in its text encoder | | | | | | |

Starved, the live graph stopping at the sampler took 100-125 ms off each seed-changed job (Z-Image 1340 → 1220 ms,
FLUX 3 Klein 656 → 530 ms, Krea 2 1404 → 1301 ms).

The queue wait (proxy `created_at` to ComfyUI's `execution_start`, one clock) is 0-1 ms; the sidecar is 27-44 ms
from ComfyUI through an SSH tunnel.

**Attack order.** Resident, the fixed part is as large as the encoder and the decode-and-PNG half of it is work
nobody looks at, so the live graph stops at the sampler (100-125 ms off every seed-changed job, measured below).
Sampling is next (Krea 2 most; FLUX 3 least), then the encoder on word changes. Starved, residency is everything:
4 steps cost 0.55-1.2 s streamed against 0.16-0.48 s resident, which is why a VRAM contract on the neighbours and
smaller live files come before any other speed work.

## Where a clip's time goes

Pika H3 Flex text to video on an RTX 5090 (MiniMax H3's pruned int8 file, Pika's bf16 LoRA at strength 1, six
`ManualSigmas` steps, the 32B NVFP4 text encoder), ComfyUI's history on fresh seeds, the same seed across variants:

| | Before | After |
|---|---|---|
| Clip at the template's size (5 s, 16:9 0.98 MP) | 85 s (12.8 s a step) | 52 s (7.2 s a step) |
| Clip at FastH3's size (5 s, 1:1 0.4 MP) | 26.6 s (2.9 s a step) | 15.3 s (2.0 s a step); FastH3's own template 19.6 s |
| Draft, seed changed (2 s, 0.1 MP) | 1.8 s | 1.3 s |
| FL2VA draft from a 0.9 MP first frame, seed changed | 43-56 s (6.5 s a step) | 1.5-3.9 s (0.1 MP) |

- **Attention is the clip.** Dense PyTorch attention over a 5 s clip is most of each step. ComfyUI's INT8 attention
  (`ModelAttentionBackend`, "comfy kitchen attention", which FastH3's template already sets) takes a step from 12.8 s
  to 7.2 with the same picture (SSIM 0.82 and 0.69 against the dense clip on two prompts, where another seed scores
  0.55), so a clip's draft and settle use it unless the template sets its own attention; Final renders the template as
  tuned. LTX 2.5's clip: 19.0 s to 17.8. At a draft's 0.1 MP it neither helps nor hurts. Training-free sparse attention
  (`BlockSparseAttention`, sol-attn, tau 1.0-1.6) runs 4.6-6.4 s a step depending on the content, and moves the
  picture further (SSIM 0.63-0.69), so it is not the default.
- **The LoRA costs nothing while the model is resident.** Without the LoRA the same clip steps at 12.9 s. Dynamic VRAM
  applies the LoRA once to each weight it holds; a weight it streams from host RAM gets the LoRA again on every step.
  With a neighbouring ComfyUI holding 5 GB, a seed-changed draft took 3.75 s with the LoRA and 1.25 s without it, and a
  words-changed draft 7.8 s against 3.0; with the card free both run at 1.2 s and 2.9 s. Headroom on the neighbours
  (above) is the cure. A merged file is not: requantizing the int8 weights after adding the LoRA moves each weight
  about twice as far as the LoRA does (a merged file drew a different clip, SSIM 0.53), and an fp8 base requantizes
  more coarsely still.
- **The text encoder** re-runs on each word change: 1.6-2.2 s at a draft, partly streamed (the 15 GB encoder and the
  20 GB transformer do not both fit beside the decoders).
- **Fewer knots for drafts.** Six steps at four (1000/750/250/40) hold the draft's composition (SSIM 0.52-0.94 against
  the six-step draft, 0.43-0.71 for another seed); three recompose it. LTX 2.5 recomposes when thinned, so only the
  templates the manifest's `thin_drafts` lists thin.
- **A clip sized by its picture.** Pika FL2VA has no size control: its latent takes the first frame's size, so its
  draft drew a 2 s clip at the picture's full size. A draft now takes such a template's pictures at the live size.
- The full clip's fixed part (the full VAE decode and the MP4) is 7-8 s. Smooth (18.5 s against FastH3's 8.2) is
  the interpolation tool over 2.45 times the pixels.

## The loop, change by change

`checks/check.mjs --live` types "a woman in a red coat walking through neon rain at night" at 10 characters a second on
an RTX 5090 (warm, the card free but for 3 GB held by other processes, each run gated on 30 s with no other job on
the GPU and discarded if anyone else's job ran during it; medians of 2-3 runs). Words age is how long ago the last key a
live picture's words include was pressed, when it is painted; drift is the mean difference (0-255, 16×16) from the
last live picture to the settled one.

| Loop | Key to first frame | Pictures/s typing | Words age p50 / p90 | Last key to settled | Drift |
|---|---|---|---|---|---|
| Baseline loop, Z-Image / FLUX 3 Klein | 255 / 230 ms | 1.82 / 2.62 | 586/892 · 475/816 ms | 5.96 / 4.67 s | 11.8 / 18.5 |
| + cached jobs end at once (stream polls the job) | 248 / 244 ms | 2.01 / 2.78 | 563/631 · 456/511 ms | 5.68 / 4.67 s | 12.2 / 23.2 |
| + live graph stops at the sampler | 260 / 248 ms | 1.22 / 3.14 | 586/2796 · 414/594 ms | 5.64 / 4.65 s | 8.0 / 20.5 |
| + words on a word boundary (11 live jobs a sentence, not 56) | 383 / 320 ms | 0.96 / 1.83 | 567/640 · 408/515 ms | 9.30* / 3.75 s | 8.1 / 15.1 |
| + preempting live jobs under half done (removed) | 396 / 315 ms | 1.31 / 1.57 | 577/876 · 401/716 ms | 5.04 / 3.91 s | 7.8 / 19.6 |
| preempting with a state per key | 5.9 s / 5.9 s | 0 / 0 | | 5.19 / 4.31 s | |
| **The loop as shipped**: first word as typed, no settle mid-sentence, cached jobs show their picture, warm-ups that load, settle JPEG beside the GPU, Z-Image int8 | **230 / 216 ms**† | **1.85 / 1.91** | **400/482 · 363/438 ms** | **4.06 / 4.12 s** | **7.7 / 21.9** |
| The loop as shipped, Krea 2 Turbo | 256 ms | 1.57 | 680/946 ms | 6.45 s | 16.1 |

\* one submit took 11.9 s inside comfy-sdk (1 of ~2000 submits measured). † once 1.67 s, a load the warm-up did not
cover. Pictures/s follows the word rate (about two words a second at 10 characters a second): fewer pictures,
each of whole words, and words age and last key to settled both shorter.

Each change on its own, where it is separable:

| Change | Before → after |
|---|---|
| Stream polls a silent job (`checks/stall.py`, 40 cached jobs) | 4 over 1 s (3.9, 23.1, 24.7, 24.7 s) → none (slowest 331 ms) |
| Live graph stops at the sampler (`anatomy.py`, seed-changed exec) | warm: Z-Image 417 → 354, FLUX 3 282 → 232, Krea 2 575 → 528 ms; starved: 100-125 ms off |
| Settle JPEG made by ComfyUI (`/view?preview=jpeg;92`) | after the settle ends: 350 / 511 → 124 / 0 ms; last key to settled Z-Image 5.36 → 5.05 s, FLUX 3 4.19 → 3.80 s |
| Z-Image int8_convrot for bf16 (`precision.py`, 4 seeds) | 412 → 340 ms a live job, 0.987-0.997 alike |
| `--preview-size 1024` (one clean run each; not kept) | Z-Image last key to settled 4.06 → 3.3-3.7 s and a sharp 1 MP last preview at 2.6-2.8 s, but words age +100-350 ms and Z-Image drift 7.7 → 22-26 |
| `--fast fp8_matrix_mult cublas_ops autotune` (not kept) | slower on all three: last key to settled Z-Image 5.8, FLUX 3 5.9, Krea 2 7.7 s |

The 3-minute soak (Z-Image, continuous typing; the baseline loop's first soak had two 10 s windows without a frame
and the line saying nothing):

| Loop | 10 s windows without a picture | Longest gap | Updates/s | Retained heap (forced GC) |
|---|---|---|---|---|
| Baseline loop, alone on the GPU | 0 of 18 | 4.5 s | 5.2 | 2.0 → 2.6 MB |
| The loop as shipped, alone on the GPU | 0 of 18 | 1.8 s | 2.8 | 2.0 → 2.8 MB |
| The loop as shipped, while another VorpalFusion page ran LTX-2.5 and H3 clips on the same GPU | 4 of 18, each saying "Waiting for another VorpalFusion · 1 ahead" | 23.9 s | 1.8 | 2.0 → 2.8 MB |

The heap growth seen in that first soak (2.8 → 17 MB in 90 s) did not reproduce with either loop; the glow is a
32 px canvas rather than a data-URL background per frame in both.

## The ladder, measured

`checks/ladder.py --rungs` on an RTX 5090 (every other ComfyUI on the card started with
`--vram-headroom 16`; every family's run gated on 20 s
without another client's job and run again from the top, fresh seeds, if one overlapped: none did). Each
rung is ComfyUI's exec time with the rung below cached; drift is from the rung below (16×16 mean
difference, 0-255); peak is the live process's GPU memory (nvidia-smi every 100 ms, staged weights
included):

| Rung | FLUX 3 Klein (16 steps) | Z-Image Turbo (8) | Krea 2 Turbo (8) |
|---|---|---|---|
| Live (0.25 MP, 4 steps, stopping at the sampler) | 0.37 s | 0.36 s | 0.66 s |
| Settled (1 MP, from the live frame) | 2.3 s · drift 9.3 | 1.9 s · drift 4.7 | 4.2 s · drift 9.6 |
| Final (full steps at denoise 0.3, enhancer on) | 9.4 s · drift 6.6 · 20.7 GB | 1.8 s · drift 2.2 | 5.7 s · drift 4.5 |
| 2K, tiled refine | 6.6 s · drift 6.8 · 26.9 GB | 7.2 s · drift 1.9 | 8.7 s · drift 3.5 |
| 2K, SeedVR2 | **2.8 s** · drift 5.1 · 16.4 GB | **2.8 s** · drift 2.0 | **4.0 s** · drift 2.1 |
| 4K, tiled refine | 50.0 s · drift 7.4 · 24.5 GB | 53.2 s · drift 2.2 | 77.9 s · drift 2.1 |
| 4K, SeedVR2 | **8.1 s** · drift 1.3 · 9.4 GB | **8.5 s** · drift 1.2 | **15.4 s** · drift 1.0 |

FLUX 3 Klein's Final is mostly its 9B prompt upsampler writing the words; Z-Image has no enhancer, and its
Final is one more refine. In the detail crops (the same regions at each rung's own pixels, the Final
upscaled with Lanczos beside them) the tiled refine at denoise 0.3 smooths away what the Final had (the
coat's weave, skin, rain streaks), while SeedVR2 adds texture and keeps every edge in place, at 2-6× less
GPU time; on Z-Image it also sharpens the Final's own blotchy skin into a painterly texture. SeedVR2 is
the hi-res path for all three (`ladder.hires` in the manifest; a template may name `refine`). Without that
headroom on the neighbours, both 4K paths ran out of memory on Z-Image ("Allocation on device 0 would exceed
allowed memory").


Through the page (`check.mjs --live --ladder`, FLUX 3 Klein, one pass, the GPU shared with another
VorpalFusion page): Graduate to Final landed 11.6 s after the click, 2K (SeedVR2) 4.4 s after the next, and
Keep saved the 2K original with its JSON. The originals are megabytes over an SSH tunnel to the GPU (2K
5-8 MB, 4K 16-32 MB, 15-320 s through it), so a rung shows from ComfyUI's JPEG and only Keep fetches the
original.

## Split panels, measured

`checks/ladder.py --batch` (the same card and gate): one job of n pictures at live size and steps, the
words cached, a fresh seed each, decoding the batch; "one picture" is the live job (one picture, stopping
at the sampler). Exec ms, the median of two; peak process GPU memory:

| Family | One picture | 1 panel | 2 panels | 4 panels | 8 panels | Per panel at 4 / 8, against one picture | Peak, 1 → 8 |
|---|---|---|---|---|---|---|---|
| FLUX 3 Klein | 215 | 288 | 374 | 622 | 1112 | 0.72× / 0.65× | 14.1 → 15.0 GB |
| Z-Image Turbo | 252 | 328 | 469 | 770 | 1432 | 0.76× / 0.71× | 18.7 → 21.1 GB |
| Krea 2 Turbo | 1424 | 1619 | 2683 | 5097 | 8858 | 0.89× / 0.78× | 18.0 → 20.7 GB |
| FLUX.2 klein 9B + a reference | 715 | 852 | 1125 | 2402 | 4744 | 0.84× / 0.83× | 27.1 → 30.2 GB |

Krea 2 ran at 1.4 s a picture in this run against 0.66 s in the rung run: the families shared the
process and Krea 2 was the least resident. A picked panel settles that exact picture: its settle drifted
10.7 / 4.3 / 10.4 / 11.0 from the panel, against 9.8 / 4.7 / 9.4 / 13.0 for one picture's settle (FLUX 3
Klein, Z-Image, Krea 2, FLUX.2 klein; one of each, `--pick`). Through the page, 4 panels landed 2.7 s after
the last key and a pick settled in 3.2 s (FLUX 3 Klein, the pass above).

Eight seeds batched cost 0.65-0.83× of eight single jobs; the seed field still renders single jobs: its
tiles are seeds (⌥← ⌥→ and R lock a seed any template reproduces alone at live cost), and a batched field
would make every tile (seed, index), reproducible only by rendering its whole batch.

## Measured (RTX 5090, models resident)

The Z-Image Turbo loop, with the settle as a 2× latent upscale and refine of the
live frame:

| What | Time |
|---|---|
| First preview after a job is sent | 130–150 ms |
| One live picture (512², 4 steps, 4 previews + final) | 0.39–0.62 s |
| Visual updates while typing | ~8/s (a new picture every ~0.6 s, previews between) |
| Settle (2× hires refine, live half cached) | 1.5 s |
| Raw ComfyUI, 512² 2 steps / 4 steps / 1024² 4 steps | 0.26 / 0.44 / 1.4 s |
| Identical graph re-run (all cached) | 38 ms |
| Same job through the default proxy | 0.70–1.1 s, 0–1 previews |

Every family through the same generic profile (`profile.py`), warm, one render:

| Template at live settings | Live render |
|---|---|
| FLUX 3 Klein distilled, 4 steps, 0.25 MP, upsampler off | 0.29 s |
| FLUX 3 Klein distilled, 6 steps, 0.25 MP | 0.36 s |
| Z-Image Turbo, 4 steps, 0.25 MP | 0.38 s |
| FLUX.2 klein 9B with a reference picture, 2 / 4 steps, 0.25 MP | 0.57 / 0.93 s |
| Krea 2 Turbo, 4 / 8 steps, 0.25 MP | 0.58 / 1.06 s |
| FastH3 text to video, 4 steps, 0.1 MP (320², 5.2 s with audio) | 2.97 s |
| FastH3 image to video, 4 steps, 0.1 MP | 3.62 s |
| LTX-2.5 text / image to video, 0.15 MP, 2 s (512×256) | 3.78 / 3.85 s |

Through the page, end to end on an RTX 5090 (`checks/check.mjs --live`, typing
"a woman in a red coat walking through neon rain at night" at 10 characters a
second, models resident):

| Template | Load on choose | Key to first frame | Frames while typing | Last key to settled |
|---|---|---|---|---|
| Z-Image Turbo | 0.36 s | 330 ms | 7.0/s (1.7 pictures/s) | 5.2 s (settle job 3.6 s) |
| FLUX 3 Klein distilled | 0.88 s | 298 ms | 9.4/s (2.3 pictures/s) | 4.1 s (settle job 3.2 s) |
| FLUX 3 Klein edit, with a portrait | 7.9 s (cold) | | | 12.5 s (live job 4.3 s, settle job 4.9 s) |

On an RTX 4090 (24 GB), inside ComfyUI with another ComfyUI holding 10-17.5 GB of the same card, so the models stream
in per job: Z-Image Turbo cold (models loading during the typing) 21.9 s from the first key to the first frame and
21.9 s from the last key to settled; warm, through the sidebar tab, 1.9 s to the first frame, 3.8 frames/s while
typing (0.76 pictures/s) and 5.9 s from the last key to settled (settle job 3.6 s), with a live job at 0.5 s. FLUX 3
Klein distilled on a first visit (its encoders in fp8 and int8 in place of the files the template names): 8.6 s from
opening the page to the first frame and 13.1 s to settled (settle job 3.7 s), with a warm live job at 0.24 s.

Settling from the live frame at 1 MP, warm (the live frame, its door, bicycle,
plants and cat, kept their places in every family; the settle adds detail):

| Template | Settle |
|---|---|
| FLUX 3 Klein distilled, 16 steps at denoise 0.5 | 2.2 s |
| Z-Image Turbo, 8 steps at denoise 0.5 | 3.3 s |
| Krea 2 Turbo, 8 steps at denoise 0.5 | 4.6–4.9 s |

On a live ComfyUI of its own (`--highvram --preview-method taesd`, tiny decoders
`taef1`, `taef2`, `taeh3` in `vae_approx`), warm, through comfy-sdk: the first sharp
preview in 163–209 ms; FLUX 3 Klein samples 4 steps at 512² at 28 steps a second
(0.24 s end to end), Z-Image in 0.35 s; the FLUX 3 seed field fills eight tiles in
2.1 s (0.27–0.34 s each). Switching family reloads models (2–4 s). `--highvram` is wrong for
it: it never unloads, so a few family switches ran the card out of memory (27 GB
held). Started without it, ComfyUI's dynamic VRAM keeps the most
recently used family, which is the live one.

FLUX.2 klein 9B, live with a portrait as its reference ("a yellow raincoat, a snowy
mountain behind her"): the likeness held from the photo to the live frame to the
settle; the live job takes 1.1 s (first preview 430–540 ms) and the settle, from the
live frame, 1.7 s.

Live steps, swept at a quarter megapixel against each template's own full-step render on the same
seed (similarity of 64² grayscale thumbnails, 1.0 the same picture; warm exec time):

| Template (full steps) | Too few | Live | Similarity at live | Live time |
|---|---|---|---|---|
| Z-Image Turbo (8) | 2 (soft) | 4 | 0.95 | 0.40 s |
| Krea 2 Turbo (8) | 2 (falls apart) | 4 | 0.76–0.80 | 0.55 s |
| FLUX 3 Klein distilled (16) | 2 (rough) | 4 | 0.81–0.96 | 0.30 s |
| FLUX.2 klein 9B with a reference (4) | 1 (soft) | 2 | 0.89 | 0.54 s |

CFG stays at each template's own value (1 for all four: they are CFG-distilled). FLUX 3 at 8 steps is
nearly its 16-step picture (0.97–0.99).

Quality held at these settings for every image family (the red coat, the rain, the
neon and the reference's likeness all read). Cold, the first render after another
family took 3–17 s: model loading, which the warm-up absorbs.

**Settle quality.** `checks/settle_quality.py` settles the same live frame both
ways on fresh seeds (a woman in neon rain under a clear umbrella; a cat on a red
velvet chair by a green door), timed from submit to the downloaded picture; drift is
the mean difference (0–255) between the live frame and the settle at 16×16:

| Template | Latent upscale | Pixel upscale |
|---|---|---|
| FLUX 3 Klein distilled | 2.37 / 2.30 s, drift 14.6 / 10.5 | 2.66 / 2.92 s, drift 15.9 / 8.7 |
| Z-Image Turbo | 3.17 / 3.66 s, drift 5.1 / 6.4 | 3.50 / 3.53 s, drift 4.2 / 5.7 |

On FLUX 3 the latent upscale gave the woman a different face with a magenta blotch
across the cheek and redrew the chair; the pixel upscale kept her pose, expression
and the chair, and added detail. On Z-Image both held; the pixel upscale drifted
less. The pixel upscale costs FLUX 3 about 0.3–0.6 s and is the settle.

**Clips loop.** For a video template, the live window plays the latest draft clip
on a loop. The next draft develops in a corner thumbnail (its step previews and
a counter), and when it lands it flows in at the playing clip's playhead with a
280 ms cross-fade, so the motion carries on where it was rather than restarting.
The settle (the template's full clip) flows in the same way.

**Clip drafts, measured: "a ____ shaking the screen, yelling 'Try VorpalFusion!'".** The subject swap, on
an RTX 5090 (warm; other ComfyUI processes on the card, idle), seed 777, three
subjects (pirate, grandma, golden retriever); the line judged by ear and by a speech-to-text pass (faster-whisper
small): "Try Vorpal Fusion" or "Tri Vorpal Fusion" counts as intelligible.

| Template | Draft with new words (2 s, live size) | Full clip | Line intelligible (drafts / full) | The gag |
|---|---|---|---|---|
| FastH3 text to video | 4.1–4.3 s (320², 4 steps, 2.3 s) | 18.7–19.2 s (640², 8 steps, 5.2 s) | 3/3 / 3/3 | hands grip the screen's edges, face at the lens, mouth on the line; every subject keeps it |
| LTX-2.5 text to video | 9.9–17.0 s (384×256, 2.0 s) | 18.6–19.5 s | 2/3 / 3/3 (the dog's draft: no words) | waves and lunges; the shake reads, the grip does not; "grandma" came out a middle-aged woman |

LTX-2.5's draft pays its 12B text encoder and its 22B transformer trading places on the card whenever the words
change (17 s, then 10–12 s); FastH3's encoder change costs about 2 s. FastH3 is the clip for the swap.

FastH3 draft levers, each on fresh subjects (the words change every job, as in a swap), median of three, then the
line by ear:

| Lever | Draft | Line intelligible | Kept |
|---|---|---|---|
| Live profile without the levers (320², 4 steps, 2.3 s, full VAE) | 3.78 s | 3/3 | |
| Tiny decoder for the frames (`taeh3`) | 3.52 s (A/B through `profile.build`: 3.76 → 3.57 s) | 3/3 (the sound is untouched) | yes |
| Half the live steps (2) | 3.07 s | 4/6 (with and without the tiny decoder) | no: the line slurs ("Tri-portal", "Trival Beaufusion") |
| 3 steps | 3.87 s | 2/3 | no |
| Shorter (1.5 s: 39 frames) | 4.05 s | 3/3 | no: not faster |
| Smaller (256², under the selector's 0.1 MP floor) | 3.99 s | 2/3 | no: not faster |
| Tiny decoder, 2 steps, 256² | 2.60 s | 0/3 | no |

With the words unchanged (a new seed) the same draft takes 1.72 s; tiny decoder and 2 steps 1.03 s. The draft is
the text encoder's: H3's 32B encoder and its transformer do not fit the card together, and the encoder runs inside
`MiniMaxH3ImageToVideo`, so any change to the words, size or length re-encodes.

The swap through the page (select the subject word, type the new one at 9 keys a second; last key to the
new draft playing in the loop, sound in): sending every half-typed word of a mid-text edit ("A g grabs", "A gr
grabs"), each a whole draft that ran out, took 6.0 and 6.6 s (21 and 11 s while another workload held the GPU);
rendering whole words only, 3.8, 4.1, 4.2 and 4.4 s, one draft per swap, the running settle stopped at the
first key.

When other model families crowd VRAM (26.5 GB held after video work), the live
model is partly offloaded and live jobs slow about 3× (1.5 s). The live model
needs to stay resident.

## Findings

1. **comfy-sdk carries it.** `AsyncComfy.submit`, `AsyncJob.events()`
   (`Preview` bytes per step, `Progress` with step/steps, `StatusChange` with
   queue position) and `AsyncJob.cancel()` are the whole transport. Per-job
   cancel matters: ComfyUI's `/interrupt` would kill other clients' work on a
   shared GPU.
2. **The proxy throttle is the one blocker.** `comfy-api-proxy` coalesces
   progress, previews and outputs to one every 0.5 s and drops what arrives in
   between (`realtime.py`, `_THROTTLE_SECONDS`), so a 0.4 s job shows at most
   one frame and ends late. With it at 0, every step's preview arrives within
   ~25 ms of the step. Upstream asks: a `--stream-interval` flag (0 for local),
   keep the latest frame instead of dropping it, and push the latest preview on
   connect as the spec says.
3. **v2 says `canceled` before ComfyUI stops.** The cancelled job finishes its
   current step, so the next job briefly queues behind it. The loop asks
   ComfyUI's queue whether what is ahead is its own.
4. **The stream can end before the output event.** The final image is read
   from the finished job's outputs when no output event came.
5. **Previews are latent2rgb.** `--preview-method auto` always means latent2rgb
   (`latent_preview.py`); sharp previews need `--preview-method taesd` (or
   `extra_data.preview_method` per prompt, which v2 does not pass) and the tiny
   decoders in `models/vae_approx/`: `taef1_decoder` (Z-Image), `taef2_decoder`
   (FLUX.2 klein, FLUX 3 Klein), `taeh3` (H3), `lighttaew2_1` (Krea 2,
   unverified). LTX-2.5's latent format names no decoder, so its previews stay
   latent2rgb without a custom node.
6. **v2 cannot reach some ComfyUI levers:** `front` (jump the queue),
   per-job `preview_method`, `partial_execution_targets`. None is needed for
   the loop; `front` would let a live job pass a queued settle.
7. **A job served whole from cache can end before its stream connects.** The
   proxy's bridge snapshots the job, then opens ComfyUI's websocket; a 5-10 ms
   cached job ends in between, and the bridge waits its 15 s heartbeat for an end
   it missed. This is the "28 s without a frame under Live" a soak showed:
   every 15-20 s gap followed such a job (7 of 309 cached jobs, none of 262
   others) and ComfyUI executed nothing during the gap. `checks/stall.py`
   reproduces it (4 of 40 cached jobs took 3.9-24.7 s to end); the owned stream
   asks the job whether it ended whenever the stream is silent for 250 ms
   (0 of 40 over 1 s, the slowest 331 ms). Upstream ask: reconcile once right
   after the websocket connects.
8. **"GPU shared" means other clients.** On a shared ComfyUI, a step sweep posting
   straight to `/prompt` and a second VorpalFusion page both ran between the loop's
   own jobs; the loop's own cancels never count (v2 job ids are ComfyUI's prompt
   ids). Jobs carry a label in their graph, so the line can
   say whose job is ahead, idle work yields, and harnesses run through the
   sidecar's own submit and stream, never `/prompt`.
9. **The card's VRAM belongs to whoever grew last.** Dynamic VRAM (comfy-aimdo)
   keeps each process's staged weights until that process itself needs room;
   an idle neighbour that grew into freed memory keeps it, and the live process
   then fails to grow ("VRAM grow failed": FLUX.2 klein 9B could not run) or
   streams 96-99 % of each model from host RAM per step (2.5-4× slower). The
   contract therefore sits on the neighbours: `--vram-headroom <live set + 2>`
   (dynamic VRAM's own knob, which counts other processes) on every other
   ComfyUI on the card.
10. **`--verbose DEBUG` hung ComfyUI** after 25 minutes (every thread
   in a futex wait, no HTTP; PIL and aimdo log every chunk and allocation). Use
   `--verbose DETAIL`: it keeps the load and residency lines
   (`Model loaded: ... ram_mb vram_mb`).
11. **A rung's original belongs on the GPU's side of a tunnel.** Through an SSH tunnel a 2K PNG took 15-50 s
   and a 4K one up to 320 s, against 3-15 s of GPU work. The page shows ComfyUI's own JPEG of a rung and only
   Keep fetches the original; a sidecar on the GPU machine removes the wait.
12. **Chrome's decoded-audio count does not say whether a clip has sound.** `webkitAudioDecodedByteCount`
   carries over from the element's previous clip, so a silent clip after one with sound read as sounding.
   The sound toggle reads the clip's own file instead: an MP4 handler of type `soun`.

## Design for Saddle

**Two tiers.** No family model (FLUX 3, Krea 2, H3, LTX-2.5) is live-fast on
one 5090: FLUX 3 Klein is 16 steps (~4 s per megapixel), Krea 2 Turbo 8 steps.
So:

- **Live tier:** a small turbo model, resident, on the loop above: Z-Image
  Turbo (0.4 s at 512²). FLUX.2 klein 4B (Apache-2.0, 4 steps) is the
  stronger candidate when the endpoint has it; its community FluxRT port
  reports 20–40 fps at 512² on a 5090 with a spatial KV cache.
- **Settle tier:** the template the creator chose, on the same words and seed
  once they rest: the hires refine for the live model, or the family model's
  own render, with sharp step previews from a tiny decoder.

**Any model, no per-model code.** A new model reaches Saddle the way FLUX 3,
LTX-2.5 and H3 did: as an official ComfyUI template on day one, or a workflow
file. Saddle already turns any template into typed slots, and Live is derived
from those slots by rules that never name a model:

- *What to vary:* the prompt slot (`prompt_slot`), the seed slot
  (`seed_slot`), the sampler steps and output size (`job_size`), boolean slots
  labelled enhance or upsampler, a clip's duration or length, and media slots
  with their roles (`media_slots`, `media_roles`).
- *The live profile:* the same graph at about a quarter megapixel, fewer steps,
  enhancers off, a short clip. A text-to-image template takes a source picture
  by swapping the empty latent feeding its sampler for the encoded picture,
  denoised part way.
- *Self-calibration:* the first time Live runs on a template, Saddle times a
  couple of steps at live size on this GPU and picks the size and step count
  that meet the latency budget (first frame ≤ 300 ms, a live picture ≤ 700 ms),
  never below a quality floor (a quarter of the template's own steps, at
  least 4). The profile is kept per template, capability hash and GPU, and
  recalibrates when a model file changes.
- *The settle is the template as its authors tuned it,* with the creator's
  settings, so quality never rests on a guess.
- *Hints are data, not code:* a recipe may add a `live` hint (a step floor, or a
  faster sibling that draws the live frames, such as FLUX.2 klein for FLUX 3).
- *Previews are generic:* latent2rgb decodes any latent ComfyUI knows; a tiny
  decoder sharpens them when the endpoint has one.

Where the rules stop: a graph whose steps are a sigma string (LTX-2.5's
distilled schedule) keeps its steps, and Live reduces only size and duration;
a sampler Saddle cannot find leaves the template running at its own settings,
at live size.

**The rule it bends.** Saddle settles that nothing reaches the GPU without a
confirmed plan. Live is a mode the creator turns on: turning it on is the
confirmation for live renders while the queue is free. Keeping a frame is a
normal plan the creator confirms, so custody, lineage and the plan gate stay
whole.

## As a Saddle app

VorpalFusion is a Saddle app with an interface module of its own: a live surface
Saddle's own components cannot render. The boundary:

- **Saddle draws the frame and the chrome.** The module runs in a sandboxed frame
  Saddle positions and labels with the app's name, source and accent. Everything
  that spends GPU or keeps data stays in Saddle's chrome outside it: the **Live**
  switch (the confirmation), **Keep** (stages the settle as a plan and confirms
  it into the library), the GPU status, and Cancel. The module cannot draw
  look-alikes of those controls.
- **The bridge is postMessage.** Messages are objects with a `type`; the module's
  carry `source: "gpu-vision"`. Saddle to the module:
  - `live {on}`: the Live switch. Framed, the module starts with Live off and
    submits nothing (not even the warm-up) until it is on.
  - `template {name}`: the chosen template.
  - `state {state}`: the full turn state from Saddle's box: `template`, `prompt`,
    `seed`, `values` (by slot address), `sources: [{slot?, rel, src?}]` (a library
    picture by its rel; it is kept in the sidecar's registry once and known by its rel
    after that).
  - `source {rel, src?, slot?}`: one library picture, by default as the source. A library rel
    (`/api/artifacts/<id>/content`) is fetched by the sidecar from Saddle's API and the library id
    kept as the picture's parent; `src` is only needed for a picture that is not in the library.
  - `keep {}`: Saddle's Keep; the module answers with `keep {recipe}`.
  - `busy {busy, ahead}`: the GPU is someone else's; the module pauses and says so.
  - `theme {accent, scheme}`.

  The module to Saddle: `state {state}` on every change (pictures by id and rel),
  `settled {snapshot}` (the whole condition with its parent, the displayed frame and
  `original`, the sidecar's URL for the full picture, which Keep stages),
  `status {phase, text, key_to_frame_ms, last_job_ms}` on each phase change, and
  `keep {recipe}` (the highest rung's template, words, seed, values, pictures with their
  rels and library parents, panel, rung and its ladder recipe, `original`, lineage), which
  Saddle stages as a normal plan the creator confirms. Framed,
  the module hides its own chooser and settings drawer (Saddle's box drives) and
  keeps the stage, the pictures, the seed field and the tree. `host.html` plays
  Saddle for checks: it frames the module with `sandbox="allow-scripts"` and drives
  every message.
- **The sidecar is Saddle's to run.** Saddle starts it with the endpoint's
  addresses and stops it with the app. The live model stays resident only if the
  other ComfyUIs on the card leave it room: the manifest's
  `neighbour_vram_headroom_gb` is what Saddle starts them with (`--vram-headroom`).

## Accelerating it on one 5090

From the ComfyUI source and model cards, ranked by gain for this loop; numbers
not measured here are marked.

- **A VRAM contract on the neighbours.** Dynamic VRAM has no pin and an idle
  process never gives back what it staged, so the live process cannot claim
  room: every other ComfyUI on the card needs `--vram-headroom <live set + 2>`
  (it counts other processes' memory). Starved, the live loop streams 96-99 % of
  each model per step and runs 2.5-4× slower, or fails outright ("Where a live
  job's time goes").
- **Lighter files, measured on the same seeds** (`checks/precision.py`, warm):
  Z-Image Turbo int8_convrot 340 ms against bf16's 412 at 0.25 MP, 0.99 alike, the
  live file (the manifest's `live_model`); nvfp4 417 ms and 0.74-0.94 alike,
  so on Blackwell at 512² NVFP4 buys VRAM, not speed. FLUX 3 Klein's and FLUX.2
  klein 9B's nvfp4 files (BFL) are not measured here yet.
- **Fewer steps with distill LoRAs** (Z-Image 2-step, Krea 2 2-step): Z-Image at 2
  steps measured 0.26 s against 0.44 s at 4.
- **Skip the full decode for live frames** (done): the live graph stops at the
  sampler; 47-63 ms off each job warm, 100-125 ms starved.
- **Downscale references to the live size** (done): FLUX 3 Klein's edit live job 1.27 → 0.44 s, likeness unchanged
  ("Touch the picture").
- **FLUX.2 klein KV** (`FluxKVCache`) reuses reference tokens across steps within a
  job: up to 2.5× on multi-reference edits, nothing for text to image.
- **Smaller gains:** INT8 or Sage attention, `TorchCompileModel` (warm it when Live
  turns on). Step caches (EasyCache and kin) do nothing at 2–4 steps; FluxRT's
  spatial cache helps only when most of the frame holds still (brush, camera),
  and its 20–40 fps is a RIFE-interpolated display rate, 5.5 native.
- **FLUX 3 Klein wants its prompt upsampler** (a 9B model writing ~220 words): live
  and settle keep it off so the settle refines what was watched; Enrich runs it
  on request and shows what it wrote.
- **Preempting a live job for newer words loses**: with a state per key nothing
  ever finishes; with word-boundary sends it costs pictures for no gain in words
  age. Live jobs run out.

## What to build next, ranked by how alien it feels per day of work

1. **Load into Saddle:** the interface-module host (a sandboxed frame and the
   postMessage bridge), the Live switch and Keep in Saddle's chrome, the
   sidecar's lifecycle. Needs the proxy flag upstream or a bundled patch.
2. **Live brush.** Each stroke uploads the mask (`assets.from_bytes` dedups
   unchanged frames) and runs a 2–4 step inpaint on the live model; FLUX 3
   Klein's box-locked edit settles it.
3. **Speculative next turn.** While the creator reads a result, pre-render the
   likely next moves (Vary, Upscale, the kernel's suggestion) at live size, so
   the click lands instantly. Preemptible the moment they act.
4. **Sharp previews for clips.** `taeh3` is installed, so H3 and FastH3 previews
   develop sharply; LTX-2.5 needs a preview node, and a
   clip's first-step preview should let a bad clip be cancelled in seconds.
5. **Camera in.** A webcam or screen as the live source (img2img per frame at
   512²). Live video models that fit a 5090 (JoyAI-Video-Edit, 840×480 at 24 fps;
   Wan-1.3B causal models at 12–28 fps) run outside ComfyUI's queue today, so
   this is a sidecar, later.
