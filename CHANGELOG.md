# Changelog

All notable changes to this fork are documented here.

This project is a fork of
[tecnomanu/remove-background-local](https://github.com/tecnomanu/remove-background-local).
The upstream history up to v1.15.2 is not repeated here; this changelog starts
at the point the fork diverged.

## [1.0.0] - 2026-08-30

First release of this fork, focused on product and spare-part photography:
subjects with holes, mesh and thin structures, where general-purpose models
tend to fill the gaps in.

Everything below is relative to the upstream project it was forked from.

### Added

- Full-size viewer for results, with three modes over the same pair: the
  cut-out alone, a draggable before/after slider, and side by side. Zoom to
  1:1, pan, switchable backdrop, keyboard shortcuts. The slider's divider
  counter-scales so it stays usable at high zoom.
- Compare models: run one image through several models at once, keeping every
  result instead of replacing the previous one as Reprocess does. Models that
  are not downloaded are shown but cannot be selected.
- Metadata is carried from the source into the result: EXIF (camera, date,
  artist, copyright), ICC colour profile, DPI and PNG text chunks. Orientation
  and Software are dropped on purpose - the first is already baked into the
  pixels, the second described the source's encoder. `RBL_PRESERVE_METADATA=0`
  strips everything. Works for every accepted input format (JPG, PNG, WEBP),
  since the metadata is read from the decoded image rather than the container.
  A plain PNG download keeps all of it; a JPG download keeps the EXIF, which is
  spliced back into its APP1 marker after the canvas encodes; trimming, a solid
  backdrop or a WEBP export rebuild the pixels and cannot keep any.
- The background can be applied on the server (`bgcolor`), which is the only
  way a flattened result keeps its EXIF, colour profile and DPI: a backdrop
  painted in the browser goes through a canvas, and canvas writes no metadata.
- `server.py batch FOLDER` processes a whole folder from the command line,
  loading the model once and skipping files that already have a result, so an
  interrupted run resumes by repeating the command. Accepts `--model`,
  `--bgcolor`, `--vitmatte`, `--decontaminate`, `--only-mask` and `--overwrite`.
- The alpha channel can be downloaded as a greyscale mask (`only_mask`), at the
  source resolution, for retouching the original in an image editor.
- Trim to content on export: crops each downloaded image to the subject's
  bounding box, with optional padding. Export-only; the on-screen result is
  unchanged.
- Batch download as a single ZIP, with the model name in each filename and
  automatic de-duplication so a comparison's results do not collide. Written
  with a small store-only ZIP writer rather than a new dependency.
- Redesigned interface. Same layout and components in the same places, new
  visual language: a design-token system (type scale, 4px spacing scale,
  radii), tighter typography, hairline borders instead of heavy ones, filled
  state tints instead of outlines, and an underline for the active tab. Every
  text/background pair meets WCAG AA.
- Light, dark and system colour themes, switchable from the header and
  remembered across reloads. The system option follows the OS as it changes.
  Applied before the first paint, so there is no flash of the wrong theme.
- Five new segmentation models, bringing the total to 11:
  - `birefnet-dis` — trained on DIS5K, for objects with holes and thin
    structures. The recommended model for parts and products.
  - `birefnet-massive` — largest training set of the BiRefNet family.
  - `birefnet-hrsod` — high-resolution salient object detection.
  - `birefnet-cod` — low-contrast subjects that blend into the background.
  - `bria-rmbg` — BRIA RMBG-2.0. Note: CC BY-NC 4.0, non-commercial only.
- Edge refinement options on `/remove`, all opt-in:
  - `vitmatte` — learned edge refinement, replacing the hard mask with a real
    alpha channel. Measured 2.6x more soft-edge pixels than a plain cut-out.
  - `decontaminate` — unmixes background colour left in the edge band (halo).
  - `post_process_mask` — removes speckle from the mask.
- Advanced options panel in the UI for the above, with ViTMatte and classic
  alpha matting kept mutually exclusive (the server rejects both at once).
- Guidance in the UI and README on lowering the alpha-matting erode value to
  2-5 for thin structures; the default of 10 eats fine detail.

### Fixed

- The sidebar's storage figure did not update after deleting an image or a
  session, only after a reload. The IndexedDB deletes were fired without being
  awaited, so `navigator.storage.estimate()` still reported the pre-delete
  total. It also hid itself once no cards were left, which suggested the disk
  had been cleared when it had not.
- Changing a result's background rewrote the whole stored record - the source
  image and the cut-out, tens of MB - to save one colour string. Six colours
  tried across ten images wrote over a gigabyte to disk. Only the changed field
  is patched now.
- The viewer's image sat off centre and the split divider no longer matched
  the seam it was supposed to drag. The pane is sized in pixels by
  `layoutViewer()`, but as a flex item in a centring stage it was free to
  shrink below that, so the clip-path and the handle ended up measuring
  different boxes.
- The viewer's split mode did not line the two halves up exactly. `.v-out`
  carried a 1px border which, with `box-sizing: border-box` and `inset: 0`,
  ate a pixel of its content box and shifted the result half against the
  original - visible once zoomed in. The border is gone (the divider is drawn
  by `.v-handle` anyway), both layers are positioned identically, and the
  images are sized to their layer rather than to their own file.
- Result cards displayed the full-resolution images at ~320px, leaving the
  browser to rescale several megapixels on every repaint: seven results meant
  about half a gigabyte of decoded pixels. Cards now show a 640px copy built
  once per image - 22x less - while the viewer, the exporter and the ZIP keep
  using the originals. Images are also lazy-loaded and decoded off the main
  thread.
- Scrolling a long result list stuttered: the label and the zoom button in each
  half of every card carried a `backdrop-filter`, so ten images meant sixty
  composited layers repainting. The blur was invisible against their almost
  opaque backgrounds and is gone; cards also skip layout and paint while
  off-screen. Measured 0.37 ms to 0.20 ms per scroll step.
- Changing a result's background jumped the page back to the top: the handler
  rebuilt every card, which discards the scroll position. Only the card that
  changed is repainted now.
- The time shown on each card covered the inference alone, under-reporting the
  actual wait by about 25% (2.33s reported against 3.12s measured): loading the
  model, queueing behind another image and encoding the PNG were all outside
  the timer. It now covers the whole request, and `X-Inference-Time` carries
  the inference alone for anyone comparing models rather than waits.
- The memory warning claimed a run would be refused when the server would have
  accepted it ("Needs about 8.8 GB; only 9.0 GB free"). Two causes: the client
  compared the estimate against free memory alone, without the memory the
  process already holds and reuses, and it kept its own drifted copy of the
  estimate formula. The server now sends every term of the budget, the client
  mirrors it exactly, and the figures are re-read when the model or the options
  change rather than being fixed at page load. The per-model peaks were also
  brought closer to what was measured: excess padding is a run refused that
  would in fact have fitted.
- The memory guard refused every image after the first. It compared the
  transient inference peak against free RAM alone, ignoring that the process
  already holds most of that memory and reuses it: with the arena disabled a
  BiRefNet run settles back to ~1.8 GB between images even though it peaks near
  7.5 GB. The budget now counts the process's own resident memory, so a batch
  runs without having to restart the server.
- The Models page shows which models are holding RAM and can free one on
  demand, instead of waiting for the 10-minute idle timer.
- Inference was capped at two threads on every machine, which cost 18% of the
  speed (27.8s against 22.7s on four cores) and saved no memory: the peak comes
  from the network's activations, not from how many threads walk them. The
  thread count is left to onnxruntime again.
- A model already on disk still had to be read back into RAM more often than
  necessary, and the wait was labelled "Downloading model…" while it happened.
  Two separate faults. The pinned model only changed when the dropdown was
  clicked, so a model reached any other way was left unpinned and the idle
  evictor reclaimed it; processing with it counts as using it now. And the idle
  TTL was ten minutes, shorter than an ordinary pause between batches, so the
  reload was paid repeatedly — measured 5.0s on an SSD and reported far worse on
  a mechanical drive. The TTL is thirty minutes (`RBL_MODEL_IDLE_TTL`), and the
  Models page can still free a model on demand.
- The progress badge said "Downloading model…" whenever a model was not already
  in RAM, including when it was sitting on disk and only being loaded. The
  status endpoint always reported `downloaded` correctly; the UI ignored it. The
  two waits are now told apart: "Downloading model…" and "Loading model…".
- A browser configured to block this page's storage was reported as an
  unexplained failure, and then, once the quota check was added, as a full disk.
  Chrome raises `UnknownError: The user denied permission to access the
  database` when site data is turned off for the address - through an
  enterprise policy, a "clear site data on exit" rule, or the per-site data
  toggle - and nothing about the disk is wrong. Being told to delete older
  sessions sends the user off doing something that cannot help. The three cases
  are now told apart and answered differently: a refused database, an exhausted
  quota, and an unknown failure that says so honestly. The refused case is also
  reported at startup, where opening the database fails the same way, instead of
  only after an image had been processed and waited through.
- Opening the database could hang rather than fail. `indexedDB.open` throws
  outright when the browser blocks site data - reading the property is what
  throws, so it cannot be guarded with a plain existence check - and `onblocked`
  fires when another tab holds the database against an upgrade. Neither settled
  a promise wired only to success and error.
- Running out of browser storage was reported as an unexplained failure -
  `Could not save "..." for later` - rather than as a full disk. Only a clean
  `QuotaExceededError` was recognised, but Chrome on Windows tends to abort the
  write with a generic error instead, which fell through to the catch-all
  message. How full the quota actually is now decides it. The notice also says
  how much space is in use, and is raised when the images are added rather than
  after the processing has been waited through. A 3000x3000 photo costs about
  28 MB once stored (the source plus a PNG cut-out with an alpha channel), so a
  modest quota fills faster than it looks.
- IndexedDB writes could hang instead of failing. The promises settled on
  `complete` and `error` but not on `abort`, and a transaction the browser kills
  for lack of disk space aborts without ever firing `onerror`, so the save never
  settled either way.
- The storage warning fired at most once per page load and stayed silent
  afterwards, including after the user had freed space and filled it again.
  Deleting results re-arms it.
- Memory tuning for onnxruntime: the CPU arena is disabled and execution is
  sequential on two threads, cutting a BiRefNet run from ~9.1 GB to ~7.5 GB for
  about 20% more time. This is what makes the full BiRefNet models usable on a
  16 GB machine. `RBL_TUNE_MEMORY=0` restores the defaults. (int8 quantisation
  was measured and rejected: it did not reduce the peak, which is dominated by
  intermediate activations rather than weights.)
- Out-of-memory crashes. A BiRefNet model plus ViTMatte peaked at ~11.5 GB,
  which on a 16 GB machine meant minutes of swapping or an OOM kill that also
  took down the editor the server had been launched from. The server now
  estimates the peak before starting and returns 507 with an explanation, the
  UI warns while options are being chosen, and images larger than
  `RBL_MAX_PROCESS_PX` (1600 by default) have their mask computed from a
  reduced copy. Output resolution and pixel values are unchanged: measured
  bit-for-bit identical RGB, with 99.95% mask agreement.
- Viewer: dragging painted a text selection across the image area and left it
  there, and panning did not work at all, because the browser's native image
  drag swallowed the pointer stream. The stage is now a proper manipulation
  surface, and the split divider has a 22px grab area instead of 2px.
- Viewer: releasing the mouse outside the stage left it stuck mid-drag.
- Stored XSS: a file whose name contained HTML executed it when the result card
  rendered, and again on every reload since the name is persisted.
- Deleting an image while it was being processed left a "ghost" job that came
  back on reload, because the finished request still wrote it to IndexedDB.
- `pump()` recursed once per queued image instead of looping.
- A full storage quota was swallowed silently: results looked saved but were
  gone after a reload. Failures are now reported, and the sidebar shows how
  much space the saved results use.
- Five silent `catch {}` blocks now log with context.
- Model cache detection was broken against rembg 2.0.80+, which moved the cache
  from a flat `~/.u2net/` to `~/.rembg/models/<name>/`. Every model reported as
  "not downloaded", the delete button freed nothing and the progress bar never
  moved. Paths now come from rembg's own session classes, which also still find
  models left in the legacy directory, so nothing is re-downloaded.
- Interrupted downloads left `tmp*` files that nothing ever collected. These
  are now reclaimed on delete and at startup (877 MB recovered on the first
  run of a real cache). The startup sweep only touches models that are fully
  downloaded, so it can never kill a download in progress.

### Fixed (Windows)

- `kirinuki desktop` failed with `Error: spawn EINVAL` after downloading
  Electron. It was launched through `node_modules/.bin/electron.cmd`, a batch
  file, and Node has refused to spawn one without a shell since the fix for
  CVE-2024-27980. Enabling the shell would only have traded that for a quoting
  problem, since the path runs through the user's home directory and may
  contain spaces, so the real executable is used instead - the electron package
  records its name in `path.txt`.
- An interrupted Electron download could not be recovered from. The npm
  package and the ~100 MB binary it runs are fetched in two separate steps, and
  only the first is `npm install`; if the second is interrupted the package is
  left installed but empty. npm then reports "up to date" for every later
  attempt, so re-running the install - which is what the launcher did - could
  never fix it, and the failure was reported as "Could not install Electron"
  directly under npm's own claim of success. The package's own downloader is
  run instead, which repairs the install in place, and the message left when
  even that fails explains the two-step fetch and gives the command to clear it.
- Two further faults were hidden behind that one, since nothing reached them
  while the spawn failed: `electron/main.js` could not resolve the `electron`
  module (it ships in the package directory, but the module is installed into
  the state directory, so the upward search never reached it - `NODE_PATH` now
  points at it), and launching from a terminal inside VS Code or Cursor
  inherited `ELECTRON_RUN_AS_NODE=1`, which tells Electron to behave as a plain
  Node runtime, leaving `require("electron")` with no app object and killing
  main.js on its first API call. Both are cleared for the child process.

- The desktop app left the Python server running after the window closed:
  `kill()` only signals the direct child on Windows, and that process can hold
  several GB. Both the app and `kirinuki stop` now end the process tree.
- Python detection tried `python` first, which on Windows is usually a
  Microsoft Store stub that opens the Store instead of running anything. The
  `py -3` launcher is tried first there, and the version floor was corrected
  from 3.9 to 3.11 (what rembg actually needs) so the failure is a clear
  message rather than a confusing pip error later.
- The window and taskbar now use the bundled `.ico` on Windows; the PNG
  rendered blurry.

### Changed

- **Renamed to `kirinuki`** (Japanese for "cut-out"). The upstream project holds
  `remove-background-local` on npm, so the fork could not be published under it
  and the two would have been easy to confuse. The command is now `kirinuki`
  (short form `kiri`) instead of `rm-bg`, and the app is branded "kiri.nuki
  background remover". An existing `~/.remove-background-local` state directory
  is still used when present, so an installed copy does not rebuild its
  virtualenv; new installs use `~/.kirinuki`.
- New logo and icons. The mark is generated at every size the platforms ask
  for, with a tighter crop below 64px where the surrounding frame would leave
  the letter too small to read.
- The per-model times shown on the Models page and in the README are now
  whole-request figures for a 3000x3000 photo, matching what each card reports.
  They previously quoted inference alone, which is not what anyone waits for.
- Startup and shutdown moved from FastAPI's deprecated event decorators to a
  lifespan context, which is the supported API and will not break on a future
  FastAPI release. `fastapi` and `uvicorn` are now capped below their next
  major for the same reason.
- The frontend is split into `static/index.html`, `static/css/app.css` and
  `static/js/app.js` — it was one 795-line file. No behaviour change; the
  extracted content is byte-identical apart from file header comments. The
  stylesheet and script are now separately cacheable.
- Frontend guard tests added (`tests/test_frontend.py`).
- `rembg` pinned to `>=2.0.80,<3`; the cache layout and ViTMatte both depend on
  that floor.
- Minimum Python raised to 3.11, following rembg.
- Documented the per-model licences: BiRefNet is MIT, BRIA RMBG-2.0 is
  CC BY-NC 4.0 and needs a separate agreement for commercial use.
