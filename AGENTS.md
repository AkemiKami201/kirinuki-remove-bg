# AGENTS.md

Guide for AI agents (and humans) working on **Kirinuki**. Read this before
making changes. It explains the architecture, the conventions, and — importantly
— how to test, because **every commit must keep the test suite green**.

## What this project is

A local web app to remove image backgrounds. A FastAPI backend wraps
[`rembg`](https://github.com/danielgatis/rembg) (ISNet / U2Net / BiRefNet ONNX
models) and serves a small web UI. Everything runs locally; images never
leave the machine.

## Repository layout

```
kirinuki/
├── server.py              # FastAPI backend (all endpoints + model handling)
├── static/index.html      # Frontend markup
├── static/css/app.css     # Frontend styles
├── static/js/app.js       # Frontend behaviour
├── run.sh                 # Start the server (creates/repairs the .venv)
├── run_tests.sh           # Run the test suite
├── requirements.txt       # Runtime dependencies
├── requirements-dev.txt   # Test dependencies (pytest, httpx)
├── conftest.py            # Puts repo root on sys.path for tests
├── tests/                 # pytest suite
└── .github/workflows/     # CI: runs pytest on push / PR
```

## Run it

```bash
./run.sh                 # http://127.0.0.1:7860
```

`run.sh` auto-creates `.venv`, and **rebuilds it if it was moved/copied** (a
virtualenv stores absolute paths, so a relocated `.venv` is broken — `run.sh`
detects a prefix mismatch and recreates it).

Useful env vars: `HOST`, `PORT`, `REMBG_MODEL`, `MAX_UPLOAD_MB`, `REMBG_PROVIDERS`.

## Testing — required before every commit

```bash
./run_tests.sh           # sets up deps if needed, then runs pytest
# or, if the venv is ready:
.venv/bin/python -m pytest -q
```

The suite (`tests/`) is fast (~1s) and needs **no network**: model loading and
inference are monkeypatched. CI (`.github/workflows/tests.yml`) runs the same
`pytest -q` on every push and pull request, so:

- **Do not commit with failing tests.** Run them first.
- When you change behavior, update or add tests in `tests/`.
- New endpoints, validation rules, or model metadata should come with a test.

## Architecture notes (don't regress these)

- **Execution provider is CPU by default** (`PROVIDERS`, env `REMBG_PROVIDERS`).
  The onnxruntime **CoreML provider hangs** on several models on Apple Silicon
  (it sits at 0% CPU forever, spamming "Context leak detected"). CPU is reliable
  and fast for the light models. Do not switch the default back to CoreML.
- **Default model is `isnet-general-use`** — fast + high quality. BiRefNet is the
  best quality but large (~930 MB) and slow on CPU, so it is not the default.
- **Model cache paths come from rembg, never rebuilt by hand.** rembg 2.0.80
  moved the cache from a flat `~/.u2net/` to `~/.rembg/models/<name>/`, which
  silently made every model report as not downloaded. `model_dir()` /
  `model_file()` delegate to the session class (`model_dir()`,
  `resolve_existing()`), which also finds models left in the legacy dir. Do not
  reintroduce a hand-built path or a `U2NET_HOME` constant.
- **Interrupted downloads leave `tmp*` files** that pooch never resumes and
  nothing else collects (a half-finished BiRefNet is hundreds of MB).
  `_cleanup_partial_downloads()` clears them on delete and at startup — the
  startup sweep only touches models that are fully downloaded, so it can never
  kill a download in progress.
- **Non-blocking model loading.** `ensure_session` runs the blocking
  `new_session` in a worker thread (`run_in_threadpool`) so `/health` and
  `/model_status` stay responsive while a model downloads.
- **Inference is serialized** with `get_infer_lock()` — a lazily created
  `asyncio.Lock`. It must stay lazy: on Python 3.9 a module-level
  `asyncio.Lock()` binds to the wrong loop and raises "got Future attached to a
  different loop" on concurrent requests.
- **Queue** is both client-side (sequential job processing, results kept per
  card) and server-side (the inference lock). Adding an image while another is
  processing must never overwrite an existing result.
- **First-run setup overlay** in the UI polls `/model_status` and shows the
  one-time model download with a real progress bar (computed server-side from
  the size of the partial `tmp*` file in the model's cache dir vs the expected
  size).
- **`/model_status` reports `downloaded` and `progress`**; `/models` includes a
  `downloaded` map and a `tagline` per model. The Models page and the model
  dropdown use these to show which models are on disk and to drive the
  per-model Download buttons with progress.

## Frontend state & persistence (static/js/app.js)

- **Single session** model: the sidebar shows the processed images of the
  current session (a flat list). They **persist in IndexedDB** (DB
  `removebg-local`, store `results`, input + output Blobs) so a reload never
  loses work. "Clear" wipes the session (jobs + IndexedDB). Per-image trash
  deletes one; per-card close (x) only hides a card from the results view.
- The root (`/`) is served with `Cache-Control: no-cache` so a reload always
  gets the latest UI (avoids stale cached versions).
- **Result background**: a global default picker plus an independent per-card
  picker. Changing a card only affects that card (`job.bg`); the global is the
  default for non-overridden cards (`effectiveBg = job.bg ?? resultBg`).
- **Each result card** shows the model it used and a human-readable
  "processed X ago" time, plus a Model selector + Reprocess button (Reprocess
  confirms, then re-runs that image with the chosen model, replacing the old
  result).
- **Downloads** are produced client-side via canvas in PNG / WEBP / JPG, baking
  in the chosen background (JPG always gets a solid background since it has no
  alpha). "Download all" exports every done result.
- The **model selector** is a custom shadcn-style dropdown (not a native
  `<select>`) showing each model's title, size, a muted tagline, and a
  downloaded indicator dot.

## Naming / legal

- The product name everywhere - UI, README, installers, npm package - is
  **Kirinuki**. Do NOT use the "remove.bg"
  wordmark as the product name or imitate their visual identity. remove.bg may
  be mentioned only as a contextual comparison, alongside the disclaimer that
  this is an unofficial project, not affiliated with remove.bg / Canva.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | Web UI |
| POST | `/remove` | Remove background → transparent PNG (`multipart/form-data`). Edge refinement: `vitmatte`, `decontaminate`, `post_process_mask` |
| GET  | `/models` | Models, sizes, and rich `info` for the Models page |
| GET  | `/model_status?model=NAME` | `idle` / `loading` / `ready` / `error` |
| POST | `/warmup` | Start loading a model in the background (non-blocking) |
| GET  | `/health` | Status |

## Edge refinement

`/remove` exposes four opt-in refinements, all off by default because each costs
time:

- `vitmatte` — learned edge refinement (rembg 2.0.80+). Downloads a ~114 MB
  ONNX model on first use; no torch needed. Best option for fine detail.
- `alpha_matting` — the older pymatting solver. Same job as `vitmatte`, so the
  endpoint returns 400 if both are set and the UI keeps them mutually exclusive.
  For thin structures, `alpha_matting_erode_size` should drop to 2-5; the
  default of 10 erodes fine detail before refining.
- `decontaminate` — unmixes background colour from the soft edge band (halo).
- `post_process_mask` — removes speckle from the mask.

BiRefNet sessions and ViTMatte both run at a fixed 1024x1024 internally, so
detail on much larger source images is lost to that downscale no matter what is
enabled. Raising that ceiling means loading a `BiRefNet_HR` ONNX outside rembg's
catalogue, which would also mean owning download, caching and progress by hand.

## Conventions

- **Language: English** for UI text, code, comments, and docs.
- **NO EMOJIS** in the UI or backend. The user dislikes them. Use inline **SVG
  icons** instead (see `ICON_CLOSE`, `ICON_TRASH`, `ICON_WARN` in
  `static/index.html`, and the coffee SVG in the footer). This is enforced by
  `tests/test_no_emojis.py`, which scans the three frontend files, `server.py`,
  and `run.sh`. Examples of what NOT to put in those files: 🗑 ✕ ⚠ ☕ ✅ 🚀 — and any
  other emoji. Markdown docs (this file, `README.md`) may mention emojis.
- **Frontend split three ways.** Markup in `static/index.html`, styles in
  `static/css/app.css`, behaviour in `static/js/app.js`. They used to share one
  795-line file, which made UI changes risky and left the JS untestable. Do not
  reintroduce inline `<style>` or `<script>` blocks — `tests/test_frontend.py`
  rejects them. There is no build step and no module system: `app.js` is one
  plain script loaded with `defer`, so top-level `$()` lookups are safe. Split
  it further only when it genuinely gets hard to navigate.
- **Design tokens, not literals.** `static/css/app.css` opens with a token
  block: colours, a type scale (`--fs-*`), a 4px spacing scale (`--sp-*`) and
  radii (`--r-*`). Use them instead of raw values, so spacing and type stay on
  the scale. Colour tokens must exist in every palette; structural tokens are
  defined once and never overridden per theme, because text must not resize or
  reshape when the theme changes.
- **Visual direction: a quiet tool.** The images being cut out are the content;
  the chrome stays out of the way. Flat surfaces, hairline borders, depth from
  `--shadow-1/2` rather than large tonal jumps, and accent colour reserved for
  what is active or selected. Selection is shown with an accent rail or a tint,
  not a full ring - a ring around one item in a list reads as an error.
- **Theming.** Three choices: light, dark, and system (the default, following
  the OS). The dark palette lives on `:root`, the light one on
  `:root[data-theme="light"]`, and the same light values are repeated inside a
  `prefers-color-scheme: light` media query for the "system" case. Every colour
  must be a `var(--...)`; `tests/test_frontend.py` rejects literals outside the
  palette blocks, because a literal cannot follow the theme. The choice is kept
  in `localStorage` under `rmbg-theme` and applied by a small inline script in
  `<head>` — `app.js` is deferred, so applying it there would flash the wrong
  theme on every load. That inline block is the only one allowed in the markup.
- **The checkerboard is not inverted between themes.** It represents
  transparency, and every image editor draws it light-on-white; a dark
  checkerboard in a light UI reads as a real background instead.
- **The viewer owns the top z-index band** (120, above the first-run overlay at
  100 and the compare dialog at 110) so a result can be inspected while a model
  is still downloading. Its pane is sized in CSS pixels to the image's natural
  size and then scaled, which keeps the split fraction and the pan independent
  of the zoom; anything that must not shrink with the zoom multiplies by
  `--inv-zoom`, published by `applyViewerTransform`.
- **Compare adds, Reprocess replaces.** `runCompare` clones the job once per
  model and never frees the existing result; `reprocessJob` deliberately does
  replace. Keep that distinction - it is the reason both exist.
- **`_session_options()` is why the heavy models fit.** rembg's default leaves
  onnxruntime's CPU arena on, which holds the high-water allocation between
  requests. Disabling it plus sequential execution costs ~20% time and saves
  ~18% memory. Do not drop it without re-measuring; the numbers in
  `MODEL_PEAK_MB` assume it.
- **Batch behaviour is verified, not assumed.** 40 sequential 3000x3000 images
  through birefnet-dis: 0 failures, RSS drift -416 MB (1805 -> 1331 MB). If a
  change makes RSS climb across a batch, something is retaining buffers.
- **`estimatePeakMb` in app.js must mirror `estimate_peak_mb` in server.py.**
  They are two copies of one formula and they drift: the client once kept an
  addition for ViTMatte after the server moved to a `max()`, and hardcoded the
  megapixel count. `/models` sends `peak_mb`, `headroom_mb`, `process_mb` and
  `max_process_px` so the client has no reason to invent any term of its own.
- **The memory budget counts the process's own RSS.** The inference peak is
  transient - with the arena disabled it is returned when a run ends, so the
  next image reuses it. Comparing the peak against `MemAvailable` alone let the
  first image through and refused every one after it. `process_rss_mb()` is
  part of the budget for that reason; do not remove it.
- **`bgcolor` and `only_mask` are applied after the full-size composite**, not
  by asking rembg for them: the mask has to be scaled back to the source size
  anyway, and `compose_at_full_size` already does that. Asking rembg for
  `only_mask` directly would return it at the reduced working size.
- **Metadata is collected before the pixels are touched.** `_collect_metadata`
  runs on the source image at the top of `/remove`, because the reduced working
  copy and rembg's output both lose it. Orientation (274) is stripped - rembg
  applies it to the pixels, so keeping the tag double-rotates in viewers. On
  the client, `canUseServerBlob` returns the server's bytes untouched for a
  plain PNG export; any canvas round-trip discards metadata. JPEG is the one
  format that can be repaired afterwards - `withJpegExif` inserts an APP1
  segment after the SOI marker, skipping whatever APP0/APP1 the encoder wrote
  so they cannot stack. PNG and WEBP need their metadata written by the
  encoder, which canvas will not do.
- **Memory is the binding constraint, not speed.** `MODEL_PEAK_MB` holds
  measured peaks; keep it honest, because `/remove` refuses a request whose
  estimate does not fit (507). The BiRefNet figure is the ONNX execution arena
  and is independent of input size - it peaked at 9.1 GB with a 512x512 input -
  so `MAX_PROCESS_PX` bounds only the per-megapixel post-processing term.
- **`MAX_PROCESS_PX` must never change the output.** `fit_for_processing`
  shrinks the copy given to the model; `compose_at_full_size` scales only the
  alpha channel back up and takes the colour channels from the untouched
  original. Verified bit-for-bit in `test_compose_keeps_the_original_pixels`.
- **Layout contract:** the header and footer are fixed; only the results pane
  (and the sidebar / Models page) scroll. The controls, drop zone, and options
  stay fixed. Keep `.card { flex: 0 0 auto }` so result cards don't collapse.

## How to add a model

1. Check the id is a session rembg actually ships:
   `python -c "from rembg.sessions import sessions_names; print(sessions_names)"`.
   `test_model_paths_use_rembg_layout` fails if it is not.
2. Add it to `AVAILABLE_MODELS` (id → short label) in `server.py`.
3. Add its real download size to `MODEL_SIZES_MB` (check `Content-Length` on the
   release URL rather than guessing — the UI shows this before downloading).
4. Add a full entry to `MODEL_INFO` (`title`, `tagline`, `speed`, `quality`,
   `best_for`, `description`) — this powers the Models page.
5. `test_models_payload` will check the new entry has complete info. Run tests.

Note any non-permissive licence in the `description`: `bria-rmbg` is CC BY-NC
4.0 (non-commercial), while every BiRefNet model is MIT. Skip `withoutbg` — it
is the one session with `is_local() == False` and would send images to a remote
service, breaking the offline guarantee.

## Versioning

The version lives in **one place**: `package.json`. `server.py` reads it
(`APP_VERSION`, exposed in `/health`) and the UI footer fills from `/health`.
Never hardcode the version elsewhere.

This fork has **no release automation**: no CI workflows and no release-please.
Bump `package.json` by hand, add an entry to `CHANGELOG.md`, then publish:

```bash
npm pack --dry-run   # check what would ship before sending it
npm publish
```

Keep using Conventional Commit prefixes (`feat:`, `fix:`, `chore:`) so the
history stays readable and automation can be added later without rewriting it.

The package is `kirinuki`. The upstream project is a separate package,
`remove-background-local`, published by its own author - the rename exists so
the two are never confused for one another.
