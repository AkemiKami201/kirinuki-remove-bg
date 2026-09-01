"""
kirinuki
Local server to remove image backgrounds using SOTA open-source models.

Available models (rembg):
- isnet-general-use  : Default. Fast, very good quality
- u2netp             : Smallest. The one that fits a low-RAM machine
- u2net              : Classic, good speed/quality balance
- u2net_human_seg    : People only, very fast
- birefnet-general-lite : High quality, lighter backbone
- birefnet-general   : Best overall quality - 2024
- birefnet-portrait  : Optimized for people / portraits
- birefnet-dis       : Objects with holes / thin structures (parts, tools)
- birefnet-massive   : Largest training set
- birefnet-hrsod     : High-resolution detail
- birefnet-cod       : Low-contrast subjects
- bria-rmbg          : BRIA RMBG-2.0. Note: CC BY-NC 4.0, non-commercial

Endpoints:
- GET  /             -> UI (HTML)
- POST /remove       -> Receives an image, returns a PNG with a transparent
                        background. Optional edge refinement: vitmatte,
                        decontaminate, post_process_mask
- GET  /models       -> List of available models (with approx download sizes)
- GET  /model_status -> Load state of a model (idle / loading / ready / error)
- POST /warmup       -> Start loading a model in the background (non-blocking)
- POST /delete_model -> Delete a model's cached file from disk
- GET  /health       -> Server status

Design notes:
- The first time a model is used it is downloaded (170-980 MB) and then cached
  in ~/.rembg/models/<name>/. Both the download and the inference are blocking,
  so they run in a worker thread (run_in_threadpool). That keeps the event loop
  free, so /model_status and /health stay responsive while a model downloads.
  Cache paths come from rembg's own session classes rather than being rebuilt
  here, which also picks up models left in the pre-2.0.80 flat ~/.u2net dir.
- Inference is serialized with a lock: requests form a queue and are processed
  one at a time, which is both safe and predictable for a single-user tool.
- Execution provider: defaults to CPU. The onnxruntime CoreML provider is fast
  for some models but hangs on others on Apple Silicon (large BiRefNet/ISNet
  models in particular), so CPU is the reliable default. CPU is also plenty fast
  for the lighter models (ISNet ~0.7s, U2Net ~0.3s). Override with the
  REMBG_PROVIDERS env var, e.g. REMBG_PROVIDERS=CoreMLExecutionProvider,CPUExecutionProvider
"""

from __future__ import annotations

import io
import os
import base64
import gc
import glob
import json
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from rembg import new_session, remove
from rembg.sessions import sessions_class
from rembg.sessions.base import BaseSession

# Optional: the memory guard falls back to /proc (Linux only) without it.
try:
    import psutil
except ImportError:
    psutil = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kirinuki")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"


def _pkg_version() -> str:
    """Single source of truth for the version: package.json (next to this file)."""
    try:
        return json.loads((BASE_DIR / "package.json").read_text("utf-8")).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


APP_VERSION = _pkg_version()

AVAILABLE_MODELS = {
    "isnet-general-use": "ISNet General (default — fast, very good quality)",
    "u2netp": "U2Net Lite (smallest — for low-RAM machines)",
    "u2net": "U2Net (classic, fastest)",
    "u2net_human_seg": "U2Net Human (people only, fast)",
    "birefnet-general-lite": "BiRefNet Lite (high quality, slower)",
    "birefnet-general": "BiRefNet General (best quality, slowest)",
    "birefnet-portrait": "BiRefNet Portrait (people, best quality, slow)",
    "birefnet-dis": "BiRefNet DIS (objects with holes and fine detail)",
    "birefnet-massive": "BiRefNet Massive (largest training set)",
    "birefnet-hrsod": "BiRefNet HRSOD (high-resolution detail)",
    "birefnet-cod": "BiRefNet COD (low-contrast subjects)",
    "bria-rmbg": "BRIA RMBG-2.0 (state of the art, non-commercial licence)",
}

# Approximate download size in MB (one-time, cached afterwards).
MODEL_SIZES_MB = {
    "isnet-general-use": 170,
    "u2netp": 5,
    "u2net": 170,
    "u2net_human_seg": 170,
    "birefnet-general-lite": 224,
    "birefnet-general": 930,
    "birefnet-portrait": 930,
    "birefnet-dis": 930,
    "birefnet-massive": 930,
    "birefnet-hrsod": 930,
    "birefnet-cod": 930,
    "bria-rmbg": 980,
}

# Rich, human-readable details for each model. Shown on the "Models" page in the
# UI so people can pick the right one.
MODEL_INFO = {
    "isnet-general-use": {
        "title": "ISNet General",
        "tagline": "Recommended for everything",
        "speed": "Fast (~3s)",
        "quality": "Very good",
        "best_for": "Default for any image",
        "description": (
            "The default. A strong balance of speed and quality, reliable on "
            "photos, product shots and people. Fast on CPU and accurate in most "
            "situations, which makes it the best first choice."
        ),
    },
    "u2netp": {
        "title": "U2Net Lite",
        "tagline": "Smallest — for low-RAM machines",
        "speed": "Fastest (~1s)",
        "quality": "Fair",
        "best_for": "Machines with little RAM",
        "description": (
            "The small version of U2Net: a 5 MB download and by far the "
            "lightest model here, which makes it the one that runs on a 4 GB "
            "laptop where everything else is refused for want of memory. Edges "
            "are rougher and fine detail is lost, so prefer ISNet when the "
            "machine has the RAM for it."
        ),
    },
    "u2net": {
        "title": "U2Net",
        "tagline": "Fast, simple subjects",
        "speed": "Fastest (~2s)",
        "quality": "Good",
        "best_for": "Simple subjects, products",
        "description": (
            "The classic background-removal model. Very fast and good for "
            "subjects with clear edges, such as product photos on plain "
            "backgrounds. Edges can be rougher than ISNet or BiRefNet."
        ),
    },
    "u2net_human_seg": {
        "title": "U2Net Human",
        "tagline": "Recommended for people",
        "speed": "Fastest (~2s)",
        "quality": "Good (people only)",
        "best_for": "People, quick cut-outs",
        "description": (
            "Trained specifically to segment people. Fast and handy for "
            "portraits or full-body shots when you do not need the very best "
            "hair detail. Not meant for objects or animals."
        ),
    },
    "birefnet-general-lite": {
        "title": "BiRefNet Lite",
        "tagline": "High quality, a bit slower",
        "speed": "Slower (~9s)",
        "quality": "High",
        "best_for": "High quality, reasonable wait",
        "description": (
            "A lighter version of BiRefNet. Close to the full model in quality "
            "but noticeably faster. A good step up from ISNet when you want "
            "cleaner edges and can wait a few seconds."
        ),
    },
    "birefnet-general": {
        "title": "BiRefNet General",
        "tagline": "Best quality, slow",
        "speed": "Slowest (~22s)",
        "quality": "Best",
        "best_for": "Maximum quality, any image",
        "description": (
            "State-of-the-art general segmentation (2024). The highest quality "
            "for difficult images, with the finest edges. Large (~930 MB) and "
            "slow on CPU, so use it when quality matters more than speed."
        ),
    },
    "birefnet-portrait": {
        "title": "BiRefNet Portrait",
        "tagline": "Best for people, slow",
        "speed": "Slow (~22s)",
        "quality": "Best (people)",
        "best_for": "People with difficult hair",
        "description": (
            "BiRefNet tuned for people. The best option for portraits with "
            "loose or fly-away hair, especially combined with alpha matting in "
            "the advanced options. Large and slow, like BiRefNet General."
        ),
    },
    "birefnet-dis": {
        "title": "BiRefNet DIS",
        "tagline": "Recommended for parts and cut-outs",
        "speed": "Slowest (~22s)",
        "quality": "Best (fine detail)",
        "best_for": "Objects with holes, mesh, thin structures",
        "description": (
            "Trained on DIS5K, a dataset built specifically for dichotomous "
            "image segmentation: objects with holes, gaps, grilles and thin "
            "structures. The strongest choice for machine parts, tools and "
            "products where the background shows through the subject and other "
            "models fill the gaps in. Same size and speed as BiRefNet General."
        ),
    },
    "birefnet-massive": {
        "title": "BiRefNet Massive",
        "tagline": "Largest training set",
        "speed": "Slowest (~22s)",
        "quality": "Best",
        "best_for": "Difficult images, second opinion",
        "description": (
            "BiRefNet trained on the largest combined dataset of the family. "
            "Very close to BiRefNet DIS on parts and cut-outs, and worth trying "
            "as a second opinion when DIS leaves an edge you do not like. Large "
            "and slow on CPU."
        ),
    },
    "birefnet-hrsod": {
        "title": "BiRefNet HRSOD",
        "tagline": "High-resolution detail",
        "speed": "Slowest (~22s)",
        "quality": "Best (detail)",
        "best_for": "Large photos with fine edges",
        "description": (
            "Tuned for high-resolution salient object detection. Keeps more of "
            "the fine edge detail on big, sharp photos of a single clear "
            "subject. A good alternative when DIS or Massive soften an edge you "
            "want to keep crisp."
        ),
    },
    "birefnet-cod": {
        "title": "BiRefNet COD",
        "tagline": "Low-contrast subjects",
        "speed": "Slowest (~22s)",
        "quality": "Best (low contrast)",
        "best_for": "Subjects that blend into the background",
        "description": (
            "Trained for concealed object detection: subjects that blend into "
            "their background. Useful for dark or metallic parts photographed "
            "on a grey or similarly coloured surface, where the other models "
            "lose the outline."
        ),
    },
    "bria-rmbg": {
        "title": "BRIA RMBG-2.0",
        "tagline": "State of the art, non-commercial licence",
        "speed": "Slowest (~22s)",
        "quality": "Best",
        "best_for": "Maximum quality on any image",
        "description": (
            "State-of-the-art background removal from BRIA AI, built on the "
            "BiRefNet architecture with BRIA's own training. Excellent, very "
            "clean edges across product shots and people. Note the licence: "
            "RMBG-2.0 is released under CC BY-NC 4.0, so commercial use needs a "
            "separate agreement with BRIA. The BiRefNet models are MIT and have "
            "no such restriction."
        ),
    },
}

DEFAULT_MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Set RBL_MAX_PROCESS_PX=0 to disable and always process at full resolution.
MAX_PROCESS_PX = int(os.environ.get("RBL_MAX_PROCESS_PX", "1600"))

# Upper bound on decoded pixels, checked from the header before the decode.
# 120 MP covers any real camera (a 100 MP medium format included) and stops a
# small file that expands to gigabytes of RGBA.
MAX_IMAGE_PIXELS = int(os.environ.get("RBL_MAX_IMAGE_PIXELS", str(120_000_000)))

# Refuse a request when the estimated peak would leave the machine with less
# than this much RAM free.
MEMORY_HEADROOM_MB = int(os.environ.get("RBL_MEMORY_HEADROOM_MB", "700"))

# Carry EXIF, ICC profile, DPI and text chunks from the source into the result.
PRESERVE_METADATA = os.environ.get("RBL_PRESERVE_METADATA", "1") == "1"

# How long an idle (unused) model session stays in RAM before being evicted.
# Set to 0 to disable the background evictor entirely.
#
# Evicting is not free: reloading a BiRefNet model means reading ~930 MB back
# from disk, which is a few seconds on an SSD but was measured near a minute on
# a mechanical drive. Ten minutes was short enough that a normal pause between
# batches paid that cost again, so the wait people blamed on the model was
# really the model being re-read. Thirty minutes is long enough to sit through
# a coffee break and still bounded, and the Models page can free one on demand.
MODEL_IDLE_TTL = int(os.environ.get("RBL_MODEL_IDLE_TTL", "1800"))
# How often the evictor wakes up to check for idle models.
MODEL_EVICTOR_INTERVAL = int(os.environ.get("RBL_MODEL_EVICTOR_INTERVAL", "30"))

# Execution providers for onnxruntime. CPU is the reliable default (CoreML hangs
# on some models on Apple Silicon). Override with REMBG_PROVIDERS if you want to
# experiment, e.g. "CoreMLExecutionProvider,CPUExecutionProvider".
PROVIDERS = [
    p.strip()
    for p in os.environ.get("REMBG_PROVIDERS", "CPUExecutionProvider").split(",")
    if p.strip()
]

# Where rembg caches the ONNX model files. Since rembg 2.0.80 each model lives
# in its own subdirectory (``<home>/models/<name>/<name>.onnx``) instead of the
# old flat ``~/.u2net`` layout.
REMBG_HOME = BaseSession.rembg_home()
LEGACY_HOME = BaseSession.legacy_home()

# model id -> rembg session class, for the path helpers below.
sessions_class_by_name = {}
for _cls in sessions_class:
    try:
        sessions_class_by_name[_cls.name()] = _cls
    except Exception:  # noqa: BLE001 - a session that needs args is not one we expose
        pass


def _session_class(name: str):
    """The rembg session class for a model id, or None if rembg doesn't know it."""
    return sessions_class_by_name.get(name)


def model_dir(name: str) -> str:
    """Directory rembg stores this model in (created on download, not by us)."""
    cls = _session_class(name)
    if cls is None:
        return os.path.join(REMBG_HOME, "models", name)
    return cls.model_dir()


def model_file(name: str) -> str:
    """Path of the model's .onnx file.

    Returns the copy that actually exists (per-model dir, or the legacy flat
    dir), falling back to the canonical per-model path when nothing is
    downloaded yet -- that is where a future download will land.
    """
    cls = _session_class(name)
    if cls is not None:
        existing = cls.resolve_existing(name + ".onnx")
        if existing:
            return existing
    return os.path.join(model_dir(name), name + ".onnx")


def is_downloaded(name: str) -> bool:
    return os.path.isfile(model_file(name))


def download_progress(name: str):
    """Best-effort download progress (0..1) while a model is being fetched.

    rembg/pooch downloads to a temporary ``tmp*`` file next to the final model
    and renames it to ``<name>.onnx`` when finished, so we estimate progress
    from the size of the newest temp file vs the model's expected size. Returns
    1.0 when the final file already exists, or None if it cannot be estimated.
    """
    if is_downloaded(name):
        return 1.0
    total = MODEL_SIZES_MB.get(name, 0) * 1024 * 1024
    if not total:
        return None
    try:
        # Temp files land in the model's own directory (rembg >= 2.0.80); older
        # versions dropped them straight into the flat legacy dir.
        tmps = glob.glob(os.path.join(model_dir(name), "tmp*"))
        tmps += glob.glob(os.path.join(LEGACY_HOME, "tmp*"))
        if not tmps:
            return 0.0
        newest = max(tmps, key=os.path.getmtime)
        return min(0.99, os.path.getsize(newest) / total)
    except OSError:
        return None


# Cached model sessions + load state.
_SESSIONS: dict[str, object] = {}
_MODEL_STATE: dict[str, str] = {}
_MODEL_ERROR: dict[str, str] = {}
_LAST_USED: dict[str, float] = {}
_LOAD_LOCKS: dict[str, asyncio.Lock] = {}
_PINNED_MODEL: str = DEFAULT_MODEL
_INFER_LOCK: asyncio.Lock | None = None


def _touch(model_name: str) -> None:
    _LAST_USED[model_name] = time.monotonic()


def _evict(model_name: str) -> bool:
    """Drop a loaded session from RAM. Does NOT touch the on-disk .onnx file."""
    if model_name not in _SESSIONS:
        return False
    _SESSIONS.pop(model_name, None)
    _LAST_USED.pop(model_name, None)
    _MODEL_STATE.pop(model_name, None)
    _MODEL_ERROR.pop(model_name, None)
    gc.collect()
    log.info("Unloaded model %s from RAM", model_name)
    return True


def _evictor_sweep() -> list[str]:
    """One pass of the idle-TTL eviction logic. Returns the names evicted.

    Pure (no awaits) so it can be unit-tested without driving the event loop.
    """
    if MODEL_IDLE_TTL <= 0:
        return []
    now = time.monotonic()
    evicted: list[str] = []
    for name in [n for n in list(_SESSIONS) if n != _PINNED_MODEL]:
        last = _LAST_USED.get(name, now)
        if now - last > MODEL_IDLE_TTL and _evict(name):
            evicted.append(name)
    return evicted


async def _evictor_loop() -> None:
    """Background task: drops loaded models that have been idle past the TTL.

    The pinned model is never evicted by the timer — it's the one the user is
    actively working with. Everything else (overrides, leftovers from a model
    switch) is fair game.
    """
    while True:
        try:
            await asyncio.sleep(MODEL_EVICTOR_INTERVAL)
            _evictor_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Model evictor loop error (continuing)")


def get_infer_lock() -> asyncio.Lock:
    global _INFER_LOCK
    if _INFER_LOCK is None:
        _INFER_LOCK = asyncio.Lock()
    return _INFER_LOCK


def _session_options():
    """onnxruntime options tuned for memory rather than raw speed.

    rembg's default is a bare SessionOptions(), which leaves the CPU arena on.
    The arena grows to the largest allocation a run ever needed and keeps it,
    so a BiRefNet model holds gigabytes between requests. Measured here on
    birefnet-dis at 1600px:

        default                                 9138 MB   31.7s
        arena off                               7746 MB   35.0s
        arena off + sequential, 2 threads       7473 MB   38.5s   <- used

    That is an 18% cut for roughly 20% more time, which is the right trade for
    a desktop tool that must share the machine with a browser and an editor.
    Sequential execution also stops several operators allocating their working
    buffers at once, which is what makes the peak spiky.

    Dynamic int8 quantisation was measured too and did NOT help (7615 MB): the
    peak is dominated by intermediate activations, not by the weights.

    Set RBL_TUNE_MEMORY=0 to fall back to onnxruntime's defaults.
    """
    import onnxruntime as ort

    opts = ort.SessionOptions()
    if os.environ.get("RBL_TUNE_MEMORY", "1") != "1":
        return opts
    opts.enable_cpu_mem_arena = False
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # The thread count is left to onnxruntime. Capping it at two was measured
    # to cost 18% of the speed (27.8s against 22.7s on four cores) and saved no
    # memory at all: the peak comes from the network's activations, not from
    # how many threads walk them.
    return opts


def _check_model(model_name: str) -> None:
    if model_name not in AVAILABLE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {model_name}. Options: {list(AVAILABLE_MODELS.keys())}",
        )


def state_of(model_name: str) -> str:
    if model_name in _SESSIONS:
        return "ready"
    return _MODEL_STATE.get(model_name, "idle")


async def ensure_session(model_name: str):
    """Return a rembg session, loading (and downloading) it if needed.

    The blocking load runs in a worker thread so the event loop stays free.
    A per-model lock makes sure we only load each model once even if several
    requests arrive at the same time.
    """
    _check_model(model_name)
    if model_name in _SESSIONS:
        _touch(model_name)
        return _SESSIONS[model_name]

    lock = _LOAD_LOCKS.setdefault(model_name, asyncio.Lock())
    async with lock:
        if model_name in _SESSIONS:
            _touch(model_name)
            return _SESSIONS[model_name]
        _MODEL_STATE[model_name] = "loading"
        _MODEL_ERROR.pop(model_name, None)
        log.info("Loading model %s (first use may download ~%d MB)...",
                 model_name, MODEL_SIZES_MB.get(model_name, 0))
        t0 = time.time()
        try:
            session = await run_in_threadpool(
                new_session, model_name, sess_opts=_session_options(), providers=PROVIDERS
            )
        except Exception as exc:  # noqa: BLE001
            _MODEL_STATE[model_name] = "error"
            _MODEL_ERROR[model_name] = str(exc)
            log.exception("Failed to load model %s", model_name)
            raise
        _SESSIONS[model_name] = session
        _MODEL_STATE[model_name] = "ready"
        _touch(model_name)
        log.info("Model %s ready in %.1fs", model_name, time.time() - t0)
        return session


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_EVICTOR_TASK: asyncio.Task | None = None


async def _sweep_partial_downloads() -> None:
    """Reclaim disk from downloads interrupted in a previous run.

    A model is either fully downloaded or not; a leftover temp file is always
    dead weight, and never resumed by pooch.
    """
    freed = 0
    for name in AVAILABLE_MODELS:
        if is_downloaded(name):
            freed += _cleanup_partial_downloads(name)
    if freed:
        log.info("Reclaimed %.0f MB from interrupted downloads", freed / 1048576)


async def _start_evictor() -> None:
    global _EVICTOR_TASK
    if MODEL_IDLE_TTL > 0 and _EVICTOR_TASK is None:
        _EVICTOR_TASK = asyncio.create_task(_evictor_loop())
        log.info(
            "Model evictor running: idle TTL %ds, check every %ds (pinned=%s)",
            MODEL_IDLE_TTL, MODEL_EVICTOR_INTERVAL, _PINNED_MODEL,
        )


async def _stop_evictor() -> None:
    global _EVICTOR_TASK
    if _EVICTOR_TASK is not None:
        _EVICTOR_TASK.cancel()
        try:
            await _EVICTOR_TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _EVICTOR_TASK = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup and shutdown work.

    FastAPI's startup/shutdown event decorators are deprecated and will be
    removed; a lifespan context is the supported replacement and keeps the
    start and stop halves side by side.
    """
    await _sweep_partial_downloads()
    await _start_evictor()
    yield
    await _stop_evictor()


app = FastAPI(title="kirinuki", version=APP_VERSION, lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"error": "static/index.html not found"}, status_code=500)
    return FileResponse(str(index), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "default_model": DEFAULT_MODEL,
        "pinned_model": _PINNED_MODEL,
        "loaded_models": list(_SESSIONS.keys()),
        "idle_ttl_seconds": MODEL_IDLE_TTL,
    }


@app.get("/models")
async def models():
    return {
        "default": DEFAULT_MODEL,
        "available": AVAILABLE_MODELS,
        "sizes_mb": MODEL_SIZES_MB,
        "info": MODEL_INFO,
        "downloaded": {name: is_downloaded(name) for name in AVAILABLE_MODELS},
        "peak_mb": {name: MODEL_PEAK_MB.get(name, 2000) for name in AVAILABLE_MODELS},
        "available_mb": available_memory_mb(),
        "max_process_px": MAX_PROCESS_PX,
        "max_upload_mb": MAX_UPLOAD_MB,
        "loaded": list(_SESSIONS.keys()),
        "process_mb": round(process_rss_mb()),
        "headroom_mb": MEMORY_HEADROOM_MB,
    }


@app.get("/model_status")
async def model_status(model: str = DEFAULT_MODEL):
    _check_model(model)
    state = state_of(model)
    downloaded = is_downloaded(model)
    if state == "loading":
        progress = download_progress(model)
    elif state == "ready" or downloaded:
        progress = 1.0
    else:
        progress = 0.0
    return {
        "model": model,
        "state": state,
        "downloaded": downloaded,
        "progress": progress,
        "size_mb": MODEL_SIZES_MB.get(model),
        "error": _MODEL_ERROR.get(model),
    }


@app.post("/warmup")
async def warmup(model: str = Form(DEFAULT_MODEL)):
    """Start loading a model without waiting for it to finish.

    Returns immediately; poll /model_status to know when it is ready.
    """
    _check_model(model)
    if model not in _SESSIONS and state_of(model) != "loading":
        async def _bg():
            try:
                await ensure_session(model)
            except Exception:
                pass  # state already recorded in _MODEL_STATE / _MODEL_ERROR
        asyncio.create_task(_bg())
    return {"model": model, "state": state_of(model), "size_mb": MODEL_SIZES_MB.get(model)}


@app.post("/unload_model")
async def unload_model(model: str = Form(...)):
    """Drop a loaded model from RAM without touching its on-disk .onnx cache.

    Useful for freeing memory when a model is no longer needed. The next
    request that uses it will reload it from the local cache (no re-download).
    """
    _check_model(model)
    unloaded = _evict(model)
    return {"model": model, "unloaded": unloaded, "state": state_of(model)}


@app.post("/set_default_model")
async def set_default_model(model: str = Form(...), warmup: bool = Form(False)):
    """Pin a new active default model and evict every other loaded model.

    The UI calls this when the user changes the model in the main dropdown so
    that switching from BiRefNet to ISNet (or vice-versa) doesn't leave both
    sitting in RAM. Per-image overrides (/remove with transient=true) do NOT
    use this — they go through the idle TTL instead.
    """
    global _PINNED_MODEL
    _check_model(model)
    _PINNED_MODEL = model
    evicted = []
    for name in list(_SESSIONS):
        if name != model and _evict(name):
            evicted.append(name)
    if warmup and model not in _SESSIONS and state_of(model) != "loading":
        async def _bg():
            try:
                await ensure_session(model)
            except Exception:
                pass
        asyncio.create_task(_bg())
    return {
        "pinned": _PINNED_MODEL,
        "evicted": evicted,
        "loaded": list(_SESSIONS.keys()),
        "state": state_of(model),
    }


def _cleanup_partial_downloads(model: str) -> int:
    """Delete leftover ``tmp*`` files from interrupted downloads. Returns bytes freed.

    pooch writes to a temp file and renames it on success, so an interrupted
    download leaves a partial file behind that nothing ever collects. They can
    be large (a half-finished BiRefNet is hundreds of MB), so both the delete
    endpoint and startup sweep them.
    """
    freed = 0
    for tmp in glob.glob(os.path.join(model_dir(model), "tmp*")):
        try:
            size = os.path.getsize(tmp)
            os.remove(tmp)
            freed += size
            log.info("Removed partial download %s (%.0f MB)", tmp, size / 1048576)
        except OSError:
            pass  # best effort: a temp file we cannot remove must not fail the request
    return freed


@app.post("/delete_model")
async def delete_model(model: str = Form(...)):
    """Delete a model's cached .onnx file from disk and unload it from memory."""
    _check_model(model)
    _SESSIONS.pop(model, None)
    _LAST_USED.pop(model, None)
    _MODEL_STATE.pop(model, None)
    _MODEL_ERROR.pop(model, None)
    path = model_file(model)
    removed = False
    if os.path.isfile(path):
        try:
            os.remove(path)
            removed = True
            log.info("Deleted model %s (%s)", model, path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not delete: {exc}")
    freed = _cleanup_partial_downloads(model)
    return {
        "model": model,
        "deleted": removed,
        "downloaded": is_downloaded(model),
        "freed_mb": round(freed / 1048576) if freed else 0,
    }


def available_memory_mb() -> float | None:
    """RAM the machine can hand out right now, in MB, or None if unknown.

    "Available" (not "free") is the number that matters: it counts reclaimable
    cache. psutil reports it on all three platforms; /proc is the fallback for
    a Linux box where psutil failed to install. Windows and macOS used to get
    None here, which silently skipped the whole memory guard below.
    """
    if psutil is not None:
        try:
            return psutil.virtual_memory().available / 1048576
        except Exception:  # noqa: BLE001 - a psutil failure must not break a request
            pass
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


# Peak RSS for one inference, in MB, measured on this project with the tuned
# session options above (CPU, onnxruntime 1.29), at a 1600px working copy.
# These are the segmentation network's own peak; the request total adds the
# constant and per-megapixel terms in estimate_peak_mb below.
MODEL_PEAK_MB = {
    "isnet-general-use": 1100,
    "u2net": 1100,
    "u2net_human_seg": 1100,
    "u2netp": 600,
    "birefnet-general-lite": 2600,
    "birefnet-general": 6800,
    "birefnet-portrait": 6800,
    "birefnet-dis": 6800,
    "birefnet-massive": 6800,
    "birefnet-hrsod": 6800,
    "birefnet-cod": 6800,
    # Measured at 8501-8608 MB plain and 8723 with vitmatte, against an
    # estimate of 8179: the old 7100 left the guard ~600 MB short on the model
    # that peaks highest, which is where being short matters most.
    "bria-rmbg": 7750,
}

def process_rss_mb() -> float:
    """Resident memory this process already holds, in MB.

    Counted as part of the budget for the next run: a loaded model and the
    allocator's retained pages are reused rather than requested again.
    psutil first so Windows and macOS get a real number instead of 0.
    """
    if psutil is not None:
        try:
            return psutil.Process().memory_info().rss / 1048576
        except Exception:  # noqa: BLE001
            pass
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0


def estimate_peak_mb(model: str, px: int, vitmatte: bool, decontaminate: bool,
                     alpha_matting: bool) -> float:
    """Rough peak RSS for one request, in MB.

    Two components: a fixed cost per model (the inference arena, measured
    above) and a per-megapixel cost for the post-processing arrays, which is
    what MAX_PROCESS_PX bounds. Deliberately generous - over-estimating costs a
    refused request, under-estimating costs a machine that swaps for minutes or
    an OOM kill that takes the editor down with it.
    """
    mp = max(1.0, px / 1_000_000)                     # megapixels
    model_mb = MODEL_PEAK_MB.get(model, 2000)
    per_mp = 320 if model_mb >= 1000 else 120
    base = 260 + model_mb + mp * per_mp

    if vitmatte:
        # A max(), not a sum: rembg runs vitmatte_alpha() only after
        # session.predict() has returned, so the two never hold their buffers
        # at once. On a heavy model the segmentation peak already dominates
        # (birefnet-dis: 7711 MB plain, 7870 with vitmatte); on a light one
        # vitmatte is what sets the peak (u2netp: 557 -> 2565).
        # Measured on u2netp at 0.64/1.44/2.56 MP: 2244/2435/2585 MB, which
        # fits 2151 + 175*MP. The old 2500 + 900*MP claimed 5064 MB at 2.56 MP,
        # nearly double the worst light model measured (2750).
        base = max(base, 260 + 2000 + mp * 250)
    elif alpha_matting:
        base += mp * 500
    if decontaminate:
        base += mp * 550
    return base


def fit_for_processing(img: Image.Image) -> tuple[Image.Image, tuple[int, int] | None]:
    """Return the image to run inference on, plus the original size if reduced.

    Every rembg session already resizes to a fixed network input (1024x1024 for
    the BiRefNet family), so shrinking a very large photo first costs no mask
    detail - it only avoids allocating the huge intermediate arrays that the
    post-processing step builds at the source resolution. The caller scales the
    resulting mask back up and composites it onto the untouched original, so the
    exported pixels are still the camera's own.
    """
    if MAX_PROCESS_PX <= 0:
        return img, None
    longest = max(img.width, img.height)
    if longest <= MAX_PROCESS_PX:
        return img, None
    scale = MAX_PROCESS_PX / longest
    small = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.Resampling.LANCZOS,
    )
    return small, (img.width, img.height)


def compose_at_full_size(original: Image.Image, cutout: Image.Image) -> Image.Image:
    """Apply a mask computed at reduced size back onto the full-resolution image.

    Only the alpha channel is scaled up; the colour channels come straight from
    the original, so nothing the camera captured is resampled.
    """
    alpha = cutout.getchannel("A").resize(original.size, Image.Resampling.LANCZOS)
    full = original.convert("RGBA")
    full.putalpha(alpha)
    return full


# PNG text chunks that describe the encoder rather than the photograph.
_DROPPED_TEXT_KEYS = {"Software", "date:create", "date:modify", "date:timestamp"}


def _flatten_onto(cutout: Image.Image, rgba: tuple[int, int, int, int]) -> Image.Image:
    """Composite a cut-out onto a solid colour, keeping the soft edge intact."""
    plate = Image.new("RGBA", cutout.size, rgba)
    return Image.alpha_composite(plate, cutout.convert("RGBA"))


def _parse_bgcolor(value: str) -> tuple[int, int, int, int]:
    """Accept ``#rrggbb``, ``#rgb`` or ``r,g,b`` and return an RGBA tuple.

    Applying the background here rather than in the browser is what lets the
    result keep its metadata: a canvas round-trip drops EXIF, ICC and DPI.
    """
    text = value.strip()
    try:
        if text.startswith("#"):
            hexpart = text[1:]
            if len(hexpart) == 3:
                hexpart = "".join(c * 2 for c in hexpart)
            if len(hexpart) != 6:
                raise ValueError("expected #rgb or #rrggbb")
            rgb = tuple(int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
        else:
            parts = [int(p) for p in text.split(",")]
            if len(parts) != 3:
                raise ValueError("expected r,g,b")
            rgb = tuple(parts)
        if not all(0 <= c <= 255 for c in rgb):
            raise ValueError("channels must be 0-255")
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bgcolor {value!r}: {exc}. Use #rrggbb or r,g,b.",
        )
    return (*rgb, 255)


def _collect_metadata(src: Image.Image) -> dict:
    """Metadata worth carrying from the source into the cut-out.

    Photos out of a camera (or converted from NEF) carry EXIF, an ICC profile,
    a DPI setting and often descriptive text. None of that is invalidated by
    removing a background, and losing it means losing capture settings,
    authorship and colour management. Collected here and re-attached on save.
    """
    meta = {}

    try:
        exif = src.getexif()
        if exif:
            exif.pop(274, None)
            if exif:
                meta["exif"] = exif.tobytes()
    except Exception as exc:
        log.debug("Could not read EXIF: %s", exc)

    icc = src.info.get("icc_profile")
    if icc:
        meta["icc_profile"] = icc

    dpi = src.info.get("dpi")
    if dpi:
        meta["dpi"] = dpi

    text = {
        k: v for k, v in src.info.items()
        if isinstance(v, str) and k not in _DROPPED_TEXT_KEYS
    }
    if text:
        meta["text"] = text

    return meta


def _encode_png(out_img: Image.Image, meta: dict | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {"format": "PNG", "optimize": True}

    if meta:
        if "exif" in meta:
            kwargs["exif"] = meta["exif"]
        if "icc_profile" in meta:
            kwargs["icc_profile"] = meta["icc_profile"]
        if "dpi" in meta:
            kwargs["dpi"] = meta["dpi"]
        if "text" in meta:
            info = PngInfo()
            for key, value in meta["text"].items():
                try:
                    info.add_text(key, value)
                except Exception as exc:
                    log.debug("Skipped text chunk %r: %s", key, exc)
            kwargs["pnginfo"] = info

    try:
        out_img.save(buf, **kwargs)
    except Exception as exc:
        log.warning("Could not write metadata (%s); saving without it", exc)
        buf = io.BytesIO()
        out_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@app.post("/remove")
async def remove_background(
    image: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    alpha_matting: bool = Form(False),
    alpha_matting_foreground_threshold: int = Form(240),
    alpha_matting_background_threshold: int = Form(10),
    alpha_matting_erode_size: int = Form(10),
    vitmatte: bool = Form(False),
    decontaminate: bool = Form(False),
    post_process_mask: bool = Form(False),
    bgcolor: str = Form(""),
    only_mask: bool = Form(False),
    transient: bool = Form(False),
):
    """Process an image and return a PNG with a transparent background.

    When ``transient`` is true the request is treated as a one-off override:
    the model is loaded if needed but the pinned default is left alone, so the
    idle TTL will reclaim this model later. Used for per-image reprocessing
    with a different model from the UI.

    Edge refinement (all off by default, since each costs time):
    - ``vitmatte``: learned edge refinement. Turns the model's hard mask into a
      real alpha channel with a network instead of pymatting's solver, which
      recovers more fine detail and cannot fail to converge. Downloads a ~114 MB
      model on first use. Supersedes ``alpha_matting``; do not enable both.
    - ``decontaminate``: unmix the background colour left in the soft edge band.
      Removes the halo a plain cut-out keeps.
    - ``post_process_mask``: clean up speckle in the mask before compositing.

    Output shape:
    - ``bgcolor``: composite the cut-out onto this colour here rather than in
      the browser (``#rrggbb`` or ``r,g,b``). The browser's canvas cannot write
      metadata, so a background applied there costs the EXIF, colour profile
      and DPI; applied here they survive. The usual case is a white catalogue
      background.
    - ``only_mask``: return the alpha channel as a greyscale PNG instead of the
      cut-out, for retouching the original by hand in an image editor.
    """
    if vitmatte and alpha_matting:
        raise HTTPException(
            status_code=400,
            detail="Use either vitmatte or alpha_matting, not both: they are two "
                   "ways of doing the same edge refinement.",
        )
    if only_mask and bgcolor:
        raise HTTPException(
            status_code=400,
            detail="only_mask returns the alpha channel, so there is nothing to "
                   "composite a background onto.",
        )
    rgba_bg = _parse_bgcolor(bgcolor) if bgcolor else None
    # Validate size
    raw = await image.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty image")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large (max {MAX_UPLOAD_MB} MB)",
        )

    # Validate that it is an image
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()  # integrity check only
        img = Image.open(io.BytesIO(raw))  # reopen to use
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")

    # A few MB of PNG can decode to hundreds of megapixels, and the memory
    # check below runs after the decode. Reject on the header instead.
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Image too large: {img.width}x{img.height} is "
                f"{img.width * img.height / 1_000_000:.0f} MP, over the "
                f"{MAX_IMAGE_PIXELS // 1_000_000} MP limit."
            ),
        )

    refinements = [
        n for n, on in (
            ("vitmatte", vitmatte),
            ("alpha-matting", alpha_matting),
            ("decontaminate", decontaminate),
            ("post-process", post_process_mask),
        ) if on
    ]

    source_meta = _collect_metadata(img) if PRESERVE_METADATA else {}
    original = img
    work, reduced_from = fit_for_processing(img)

    log.info(
        "Processing %s (%dx%d, %.1f KB) with model=%s%s%s%s",
        image.filename, original.width, original.height, len(raw) / 1024, model,
        " (transient)" if transient else "",
        " [" + ", ".join(refinements) + "]" if refinements else "",
        f" [mask at {work.width}x{work.height}]" if reduced_from else "",
    )
    if only_mask:
        log.info("  returning the mask only")
    elif rgba_bg:
        log.info("  compositing onto rgb%s", rgba_bg[:3])

    # Decline rather than let the machine swap itself to a standstill.
    if MEMORY_HEADROOM_MB > 0:
        avail = available_memory_mb()
        if avail is not None:
            need = estimate_peak_mb(
                model, work.width * work.height, vitmatte, decontaminate, alpha_matting
            )
            budget = avail + process_rss_mb() - MEMORY_HEADROOM_MB
            if need > budget:
                log.warning(
                    "Refusing %s: needs ~%.0f MB, budget ~%.0f MB "
                    "(%.0f MB free + %.0f MB already held)",
                    image.filename, need, budget, avail, process_rss_mb(),
                )
                raise HTTPException(
                    status_code=507,
                    detail=(
                        f"Not enough free memory: this needs about "
                        f"{need / 1024:.1f} GB but only {budget / 1024:.1f} GB "
                        f"is usable. Close some applications, pick a lighter "
                        f"model, or turn off edge refinement."
                    ),
                )

    t0 = time.time()

    # A non-transient request means this is the model the user is working with,
    # so pin it. Without this the pin only moved when the dropdown was clicked:
    # process with anything else and the evictor reclaimed it mid-session, and
    # the next image paid a full reload from disk for no reason.
    if not transient:
        global _PINNED_MODEL
        _PINNED_MODEL = model

    # Loads/downloads the model if needed (in a worker thread).
    session = await ensure_session(model)

    # Serialize inference: requests queue up and run one at a time.
    t_infer = time.time()
    try:
        async with get_infer_lock():
            out = await run_in_threadpool(
                remove,
                work,
                session=session,
                alpha_matting=alpha_matting,
                alpha_matting_foreground_threshold=alpha_matting_foreground_threshold,
                alpha_matting_background_threshold=alpha_matting_background_threshold,
                alpha_matting_erode_size=alpha_matting_erode_size,
                vitmatte=vitmatte,
                decontaminate=decontaminate,
                post_process_mask=post_process_mask,
            )
            if reduced_from:
                out = await run_in_threadpool(compose_at_full_size, original, out)
            if only_mask:
                out = out.getchannel("A")
            elif rgba_bg:
                out = await run_in_threadpool(_flatten_onto, out, rgba_bg)
    except Exception as exc:
        log.exception("Error processing image")
        raise HTTPException(status_code=500, detail=f"Processing error: {exc}")
    infer_seconds = time.time() - t_infer

    png_bytes = await run_in_threadpool(_encode_png, out, source_meta)
    _touch(model)

    del out, work, original, img
    gc.collect()
    elapsed = time.time() - t0
    log.info("Done in %.2fs (inference %.2fs)", elapsed, infer_seconds)

    headers = {
        "X-Processing-Time": f"{elapsed:.2f}",
        "X-Inference-Time": f"{infer_seconds:.2f}",
        "X-Model": model,
        "Content-Disposition": (
            f'inline; filename="{Path(image.filename or "out").stem}'
            f'{"_mask" if only_mask else "_nobg"}.png"'
        ),
    }

    if source_meta.get("exif"):
        headers["X-Exif"] = base64.b64encode(source_meta["exif"]).decode("ascii")
        headers["Access-Control-Expose-Headers"] = "X-Exif, X-Model, X-Processing-Time"
    return Response(content=png_bytes, media_type="image/png", headers=headers)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _arg_model(rest):
    for i, a in enumerate(rest):
        if a in ("--model", "-m") and i + 1 < len(rest):
            return rest[i + 1]
    for a in rest:
        if not a.startswith("-"):
            return a
    return None


def _models_cli(args) -> int:
    """`python server.py models [ls|pull|rm] ...` — manage cached models."""
    sub = args[0] if args else "ls"
    if sub in ("ls", "show", "list"):
        print("Models (cache: %s)" % os.path.join(REMBG_HOME, "models"))
        for name in AVAILABLE_MODELS:
            mark = "*" if is_downloaded(name) else " "
            state = "downloaded" if is_downloaded(name) else "not downloaded"
            default = "  (default)" if name == DEFAULT_MODEL else ""
            print(f" {mark} {name:<22} {MODEL_SIZES_MB.get(name, 0):>4} MB   {state}{default}")
        return 0
    if sub == "pull":
        model = _arg_model(args[1:]) or DEFAULT_MODEL
        if model not in AVAILABLE_MODELS:
            print(f"Unknown model: {model}"); return 1
        print(f"Downloading {model} (~{MODEL_SIZES_MB.get(model, 0)} MB)...")
        new_session(model, providers=PROVIDERS)
        print("Done.")
        return 0
    if sub in ("rm", "remove", "delete"):
        model = _arg_model(args[1:])
        if not model or model not in AVAILABLE_MODELS:
            print("Usage: models rm --model <name>"); return 1
        path = model_file(model)
        if os.path.isfile(path):
            os.remove(path); print(f"Deleted {model}")
        else:
            print(f"{model} is not downloaded")
        return 0
    print("Usage: models [ls | pull --model <name> | rm --model <name>]")
    return 1


def _batch_cli(args) -> int:
    """`python server.py batch IN [OUT] [options]` - process a whole folder.

    Useful for a large run: no browser, so nothing accumulates in IndexedDB,
    and the model is loaded once for the entire folder. Files that already have
    a result are skipped, so an interrupted run can simply be repeated.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="server.py batch", add_help=True)
    ap.add_argument("input", help="folder of images to process")
    ap.add_argument("output", nargs="?", help="where to write (default: INPUT/nobg)")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=list(AVAILABLE_MODELS))
    ap.add_argument("--bgcolor", default="", help="flatten onto a colour, e.g. #ffffff")
    ap.add_argument("--vitmatte", action="store_true", help="learned edge refinement")
    ap.add_argument("--decontaminate", action="store_true", help="remove edge colour cast")
    ap.add_argument("--only-mask", action="store_true", help="write the alpha channel instead")
    ap.add_argument("--overwrite", action="store_true", help="redo files that already have a result")
    opts = ap.parse_args(args)

    src = Path(opts.input)
    if not src.is_dir():
        print(f"Not a folder: {src}")
        return 1
    dst = Path(opts.output) if opts.output else src / "nobg"
    dst.mkdir(parents=True, exist_ok=True)

    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    files = sorted(f for f in src.iterdir() if f.suffix.lower() in exts and f.is_file())
    if not files:
        print(f"No images found in {src}")
        return 1

    rgba_bg = None
    if opts.bgcolor:
        try:
            rgba_bg = _parse_bgcolor(opts.bgcolor)
        except HTTPException as exc:
            print(exc.detail)
            return 1

    suffix = "_mask" if opts.only_mask else "_nobg"
    print(f"{len(files)} image(s) -> {dst}  (model: {opts.model})")
    session = new_session(opts.model, sess_opts=_session_options(), providers=PROVIDERS)

    done = skipped = failed = 0
    for i, path in enumerate(files, 1):
        out_path = dst / f"{path.stem}{suffix}.png"
        if out_path.exists() and not opts.overwrite:
            skipped += 1
            continue
        try:
            with Image.open(path) as img:
                img.load()
                meta = _collect_metadata(img) if PRESERVE_METADATA else {}
                work, reduced = fit_for_processing(img)
                out = remove(
                    work, session=session,
                    vitmatte=opts.vitmatte, decontaminate=opts.decontaminate,
                )
                if reduced:
                    out = compose_at_full_size(img, out)
                if opts.only_mask:
                    out = out.getchannel("A")
                elif rgba_bg:
                    out = _flatten_onto(out, rgba_bg)
                out_path.write_bytes(_encode_png(out, meta))
            done += 1
            print(f"  [{i}/{len(files)}] {path.name} -> {out_path.name}")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            failed += 1
            print(f"  [{i}/{len(files)}] {path.name} FAILED: {exc}")
        finally:
            gc.collect()

    print(f"\nDone: {done} processed, {skipped} skipped, {failed} failed.")
    return 1 if failed and not done else 0


def main():
    import sys

    argv = sys.argv[1:]
    if argv and argv[0] == "models":
        raise SystemExit(_models_cli(argv[1:]))
    if argv and argv[0] == "batch":
        raise SystemExit(_batch_cli(argv[1:]))

    import socket
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7860"))

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    in_use = probe.connect_ex((host, port)) == 0
    probe.close()
    if in_use:
        log.error(
            "Port %d is already in use. If kirinuki is already running, "
            "open http://%s:%d in your browser. Otherwise free the port or set a "
            "different one, e.g. PORT=8000.", port, host, port,
        )
        raise SystemExit(1)

    log.info("=" * 60)
    log.info("kirinuki starting on http://%s:%d", host, port)
    log.info("Default model: %s", DEFAULT_MODEL)
    log.info("Execution providers: %s", ", ".join(PROVIDERS))
    log.info("Max upload size: %d MB", MAX_UPLOAD_MB)
    log.info("=" * 60)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
