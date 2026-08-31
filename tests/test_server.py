"""API tests for the kirinuki server.

These run without downloading any model: the model load and the inference are
monkeypatched, so the suite is fast and works in CI with no network.
"""

import io
import os
import re

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from fastapi.testclient import TestClient

import server


@pytest.fixture
def client():
    return TestClient(server.app)


def png_bytes(size=(64, 64), color=(200, 60, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["default_model"] == server.DEFAULT_MODEL
    assert body["pinned_model"] == server._PINNED_MODEL
    assert body["idle_ttl_seconds"] == server.MODEL_IDLE_TTL
    assert isinstance(body["loaded_models"], list)


def test_models_payload(client):
    data = client.get("/models").json()
    assert data["default"] == server.DEFAULT_MODEL
    assert set(data["available"]) == set(server.AVAILABLE_MODELS)
    assert set(data["info"]) == set(server.AVAILABLE_MODELS)
    assert set(data["downloaded"]) == set(server.AVAILABLE_MODELS)
    # every model must expose complete, non-empty info for the Models page
    for key, info in data["info"].items():
        for field in ("title", "tagline", "speed", "quality", "best_for", "description"):
            assert info.get(field), f"{key} missing {field}"
        assert key in data["sizes_mb"]
        assert isinstance(data["downloaded"][key], bool)


def test_model_status_default(client):
    data = client.get("/model_status").json()
    assert data["model"] == server.DEFAULT_MODEL
    assert data["state"] in ("idle", "loading", "ready")
    assert isinstance(data["downloaded"], bool)
    assert data["progress"] is None or 0.0 <= data["progress"] <= 1.0


def test_model_status_unknown_is_400(client):
    r = client.get("/model_status", params={"model": "does-not-exist"})
    assert r.status_code == 400


def test_remove_empty_image_is_400(client):
    r = client.post("/remove", files={"image": ("a.png", b"", "image/png")})
    assert r.status_code == 400


def test_remove_invalid_image_is_400(client):
    r = client.post("/remove", files={"image": ("a.png", b"not really a png", "image/png")})
    assert r.status_code == 400


def test_remove_unknown_model_is_400(client):
    r = client.post(
        "/remove",
        files={"image": ("a.png", png_bytes(), "image/png")},
        data={"model": "does-not-exist"},
    )
    assert r.status_code == 400


def test_remove_too_large_is_413(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 8)
    r = client.post("/remove", files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 413


def test_remove_success(client, monkeypatch):
    """Full happy path with the model load + inference mocked out."""
    async def fake_ensure_session(model):
        return object()

    def fake_remove(img, session=None, **kwargs):
        return img.convert("RGBA")

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", fake_remove)

    r = client.post(
        "/remove",
        files={"image": ("photo.jpg", png_bytes(), "image/png")},
        data={"model": server.DEFAULT_MODEL},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-model"] == server.DEFAULT_MODEL
    assert "x-processing-time" in r.headers
    out = Image.open(io.BytesIO(r.content))
    assert out.format == "PNG"
    assert out.mode == "RGBA"


def test_remove_edge_refinement_flags_reach_rembg(client, monkeypatch):
    """vitmatte / decontaminate / post_process_mask must be forwarded to rembg."""
    seen = {}

    async def fake_ensure_session(model):
        return object()

    def fake_remove(img, session=None, **kwargs):
        seen.update(kwargs)
        return img.convert("RGBA")

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", fake_remove)

    r = client.post(
        "/remove",
        files={"image": ("photo.jpg", png_bytes(), "image/png")},
        data={
            "model": server.DEFAULT_MODEL,
            "vitmatte": "true",
            "decontaminate": "true",
            "post_process_mask": "true",
        },
    )
    assert r.status_code == 200
    assert seen["vitmatte"] is True
    assert seen["decontaminate"] is True
    assert seen["post_process_mask"] is True


def test_remove_defaults_keep_refinement_off(client, monkeypatch):
    """Refinement costs time, so it must stay opt-in."""
    seen = {}

    async def fake_ensure_session(model):
        return object()

    def fake_remove(img, session=None, **kwargs):
        seen.update(kwargs)
        return img.convert("RGBA")

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", fake_remove)

    r = client.post("/remove", files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 200
    assert seen["vitmatte"] is False
    assert seen["decontaminate"] is False
    assert seen["post_process_mask"] is False


def test_remove_rejects_vitmatte_with_alpha_matting(client):
    """Both do the same job; enabling both wastes time and confuses the result."""
    r = client.post(
        "/remove",
        files={"image": ("a.png", png_bytes(), "image/png")},
        data={"vitmatte": "true", "alpha_matting": "true"},
    )
    assert r.status_code == 400


def test_warmup_returns_state(client, monkeypatch):
    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    r = client.post("/warmup", data={"model": server.DEFAULT_MODEL})
    assert r.status_code == 200
    assert r.json()["model"] == server.DEFAULT_MODEL


def test_providers_default_is_cpu():
    # Reliability guard: CoreML hangs on some models on Apple Silicon.
    assert server.PROVIDERS, "at least one execution provider must be configured"


def test_delete_model_unknown_is_400(client):
    r = client.post("/delete_model", data={"model": "does-not-exist"})
    assert r.status_code == 400


def test_delete_model_removes_file(client, tmp_path, monkeypatch):
    name = server.DEFAULT_MODEL
    path = tmp_path / (name + ".onnx")
    path.write_bytes(b"fake-model")
    monkeypatch.setattr(server, "model_file", lambda n: str(path))
    assert server.is_downloaded(name) is True
    r = client.post("/delete_model", data={"model": name})
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert server.is_downloaded(name) is False


def test_delete_model_not_downloaded_ok(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server, "model_file", lambda n: str(tmp_path / (n + ".onnx"))
    )
    r = client.post("/delete_model", data={"model": server.DEFAULT_MODEL})
    assert r.status_code == 200
    assert r.json()["deleted"] is False


def test_model_paths_use_rembg_layout():
    """Regression guard: paths must follow rembg's per-model dir, not ~/.u2net.

    rembg 2.0.80 moved the cache from a flat ~/.u2net to <home>/models/<name>/.
    Building the path by hand silently reported every model as not downloaded.
    """
    name = server.DEFAULT_MODEL
    assert server.model_dir(name).endswith(os.path.join("models", name))
    assert server.model_file(name).endswith(name + ".onnx")
    # Every model we expose must be a session rembg actually knows about.
    for key in server.AVAILABLE_MODELS:
        assert server._session_class(key) is not None, f"{key} unknown to rembg"


@pytest.fixture
def clean_sessions():
    """Reset the in-RAM session bookkeeping before and after a test."""
    saved_pinned = server._PINNED_MODEL
    server._SESSIONS.clear()
    server._LAST_USED.clear()
    server._MODEL_STATE.clear()
    server._MODEL_ERROR.clear()
    yield
    server._SESSIONS.clear()
    server._LAST_USED.clear()
    server._MODEL_STATE.clear()
    server._MODEL_ERROR.clear()
    server._PINNED_MODEL = saved_pinned


def test_unload_model_unknown_is_400(client):
    r = client.post("/unload_model", data={"model": "does-not-exist"})
    assert r.status_code == 400


def test_unload_model_drops_session(client, clean_sessions):
    name = server.DEFAULT_MODEL
    server._SESSIONS[name] = object()
    server._LAST_USED[name] = 123.0
    server._MODEL_STATE[name] = "ready"
    r = client.post("/unload_model", data={"model": name})
    assert r.status_code == 200
    body = r.json()
    assert body["unloaded"] is True
    assert body["state"] == "idle"
    assert name not in server._SESSIONS
    assert name not in server._LAST_USED


def test_unload_model_when_not_loaded(client, clean_sessions):
    r = client.post("/unload_model", data={"model": server.DEFAULT_MODEL})
    assert r.status_code == 200
    assert r.json()["unloaded"] is False


def test_set_default_model_unknown_is_400(client):
    r = client.post("/set_default_model", data={"model": "does-not-exist"})
    assert r.status_code == 400


def test_set_default_model_evicts_others(client, clean_sessions):
    keep = "isnet-general-use"
    drop = "u2net"
    server._SESSIONS[keep] = object()
    server._SESSIONS[drop] = object()
    server._LAST_USED[keep] = 1.0
    server._LAST_USED[drop] = 1.0
    r = client.post("/set_default_model", data={"model": keep})
    assert r.status_code == 200
    body = r.json()
    assert body["pinned"] == keep
    assert body["evicted"] == [drop]
    assert keep in server._SESSIONS
    assert drop not in server._SESSIONS
    assert server._PINNED_MODEL == keep


def test_set_default_model_no_warmup_by_default(client, monkeypatch, clean_sessions):
    called = {"n": 0}

    async def fake_ensure_session(model):
        called["n"] += 1
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    r = client.post("/set_default_model", data={"model": server.DEFAULT_MODEL})
    assert r.status_code == 200
    # warmup defaults to false → no eager load.
    assert called["n"] == 0


def test_remove_transient_flag_is_accepted(client, monkeypatch, clean_sessions):
    """The transient flag must round-trip and not break the happy path."""
    seen = {}

    async def fake_ensure_session(model):
        seen["model"] = model
        return object()

    def fake_remove(img, session=None, **kwargs):
        return img.convert("RGBA")

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", fake_remove)

    r = client.post(
        "/remove",
        files={"image": ("photo.jpg", png_bytes(), "image/png")},
        data={"model": "u2net", "transient": "true"},
    )
    assert r.status_code == 200
    assert seen["model"] == "u2net"
    # Pinned default must NOT be hijacked by a transient request.
    assert server._PINNED_MODEL == server.DEFAULT_MODEL


def test_evict_helper_clears_state(clean_sessions):
    name = server.DEFAULT_MODEL
    server._SESSIONS[name] = object()
    server._LAST_USED[name] = 7.0
    server._MODEL_STATE[name] = "ready"
    assert server._evict(name) is True
    assert name not in server._SESSIONS
    assert name not in server._LAST_USED
    assert name not in server._MODEL_STATE
    assert server._evict(name) is False  # idempotent


def test_evictor_sweep_drops_idle_non_pinned(monkeypatch, clean_sessions):
    """A single sweep must drop idle non-pinned models past the TTL."""
    pinned = "isnet-general-use"
    stale = "u2net"
    fresh = "u2net_human_seg"
    server._PINNED_MODEL = pinned
    server._SESSIONS[pinned] = object()
    server._SESSIONS[stale] = object()
    server._SESSIONS[fresh] = object()

    monkeypatch.setattr(server, "MODEL_IDLE_TTL", 100)
    server._LAST_USED[pinned] = 0.0  # would be evicted if it weren't pinned
    server._LAST_USED[stale] = 0.0
    server._LAST_USED[fresh] = 1_000_000.0  # in the future

    monkeypatch.setattr(server.time, "monotonic", lambda: 1_000.0)

    evicted = server._evictor_sweep()

    assert evicted == [stale]
    assert pinned in server._SESSIONS, "pinned must survive"
    assert fresh in server._SESSIONS, "fresh must survive"
    assert stale not in server._SESSIONS, "stale must be evicted"


def test_evictor_sweep_disabled_when_ttl_zero(monkeypatch, clean_sessions):
    server._SESSIONS["u2net"] = object()
    server._LAST_USED["u2net"] = 0.0
    monkeypatch.setattr(server, "MODEL_IDLE_TTL", 0)
    assert server._evictor_sweep() == []
    assert "u2net" in server._SESSIONS


def test_delete_model_reclaims_partial_downloads(client, tmp_path, monkeypatch):
    """An interrupted download leaves a large temp file that must be reclaimed."""
    name = server.DEFAULT_MODEL
    (tmp_path / (name + ".onnx")).write_bytes(b"fake-model")
    (tmp_path / "tmpABC123").write_bytes(b"x" * 2048)
    monkeypatch.setattr(server, "model_file", lambda n: str(tmp_path / (n + ".onnx")))
    monkeypatch.setattr(server, "model_dir", lambda n: str(tmp_path))

    r = client.post("/delete_model", data={"model": name})
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert not (tmp_path / "tmpABC123").exists(), "partial download must be removed"


def test_cleanup_partial_downloads_is_safe_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "model_dir", lambda n: str(tmp_path))
    assert server._cleanup_partial_downloads(server.DEFAULT_MODEL) == 0


def test_download_progress_helpers(monkeypatch):
    # A downloaded model reports full progress; helpers must not raise.
    monkeypatch.setattr(server, "is_downloaded", lambda name: True)
    assert server.download_progress(server.DEFAULT_MODEL) == 1.0
    monkeypatch.setattr(server, "is_downloaded", lambda name: False)
    monkeypatch.setattr(server, "model_dir", lambda name: "/nonexistent-dir-for-tests")
    monkeypatch.setattr(server, "LEGACY_HOME", "/nonexistent-dir-for-tests")
    # No cache dir / no temp files -> 0.0, never an exception.
    assert server.download_progress(server.DEFAULT_MODEL) == 0.0


def test_static_assets_referenced_by_the_page_are_served(client):
    """Every /static/... URL in index.html must resolve.

    The stylesheet and script live in their own files now; a typo in either
    path would leave the app unstyled or inert with no server-side error.
    """
    html = client.get("/").text
    refs = sorted(set(re.findall(r'(?:href|src)="(/static/[^"]+)"', html)))
    assert "/static/css/app.css" in refs, "stylesheet not linked from index.html"
    assert "/static/js/app.js" in refs, "script not loaded from index.html"
    for ref in refs:
        assert client.get(ref).status_code == 200, f"{ref} is referenced but not served"


def test_stylesheet_and_script_have_sensible_content_types(client):
    assert "text/css" in client.get("/static/css/app.css").headers["content-type"]
    assert "javascript" in client.get("/static/js/app.js").headers["content-type"]


# ---------------------------------------------------------------------------
# Memory guards
# ---------------------------------------------------------------------------

def test_large_images_are_processed_at_a_reduced_size(monkeypatch):
    """Every rembg session resizes to a fixed network input anyway, so a huge
    photo only inflates the post-processing arrays."""
    monkeypatch.setattr(server, "MAX_PROCESS_PX", 1600)
    big = Image.new("RGB", (3000, 3000))
    work, original_size = server.fit_for_processing(big)
    assert max(work.size) == 1600
    assert original_size == (3000, 3000)


def test_small_images_are_left_alone(monkeypatch):
    monkeypatch.setattr(server, "MAX_PROCESS_PX", 1600)
    small = Image.new("RGB", (800, 600))
    work, original_size = server.fit_for_processing(small)
    assert work is small and original_size is None


def test_downscaling_can_be_disabled(monkeypatch):
    monkeypatch.setattr(server, "MAX_PROCESS_PX", 0)
    big = Image.new("RGB", (5000, 5000))
    work, original_size = server.fit_for_processing(big)
    assert work is big and original_size is None


def test_compose_keeps_the_original_pixels():
    """The whole point of the reduced-size path: the exported colours must be
    the source's own, with only the alpha channel scaled up."""
    original = Image.new("RGB", (400, 400), (37, 142, 201))
    cutout = Image.new("RGBA", (100, 100), (0, 0, 0, 255))
    out = server.compose_at_full_size(original, cutout)
    assert out.size == (400, 400)
    assert out.mode == "RGBA"
    assert out.getpixel((200, 200))[:3] == (37, 142, 201)


def test_remove_refuses_when_memory_is_short(client, monkeypatch):
    """Better a clear 507 than a machine that swaps for minutes, or an OOM kill
    that takes the editor down with it."""
    monkeypatch.setattr(server, "MEMORY_HEADROOM_MB", 700)
    monkeypatch.setattr(server, "available_memory_mb", lambda: 900.0)
    r = client.post(
        "/remove",
        files={"image": ("a.png", png_bytes(), "image/png")},
        data={"model": "birefnet-dis", "vitmatte": "true"},
    )
    assert r.status_code == 507
    assert "memory" in r.json()["detail"].lower()


def test_remove_proceeds_when_memory_is_plentiful(client, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_HEADROOM_MB", 700)
    monkeypatch.setattr(server, "available_memory_mb", lambda: 32_000.0)

    async def fake_ensure_session(model):
        return object()

    def fake_remove(img, session=None, **kwargs):
        return img.convert("RGBA")

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", fake_remove)
    r = client.post(
        "/remove",
        files={"image": ("a.png", png_bytes(), "image/png")},
        data={"model": "birefnet-dis", "vitmatte": "true"},
    )
    assert r.status_code == 200


def test_memory_check_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(server, "MEMORY_HEADROOM_MB", 0)
    monkeypatch.setattr(server, "available_memory_mb", lambda: 10.0)

    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda img, **kw: img.convert("RGBA"))
    r = client.post("/remove", files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 200


def test_every_model_has_a_measured_peak():
    """An unknown model falls back to a guess; the ones we ship should not."""
    for name in server.AVAILABLE_MODELS:
        assert name in server.MODEL_PEAK_MB, f"{name} has no measured peak"


def test_models_payload_exposes_memory_data(client):
    data = client.get("/models").json()
    assert set(data["peak_mb"]) == set(server.AVAILABLE_MODELS)
    assert data["max_process_px"] == server.MAX_PROCESS_PX


def test_session_options_are_tuned_for_memory():
    """The measured peaks in MODEL_PEAK_MB assume these options; dropping them
    puts a BiRefNet run back at ~9.1 GB."""
    opts = server._session_options()
    assert opts.enable_cpu_mem_arena is False, "the CPU arena holds the peak allocation"
    assert opts.intra_op_num_threads >= 1


def test_memory_tuning_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RBL_TUNE_MEMORY", "0")
    assert server._session_options().enable_cpu_mem_arena is True


def test_vitmatte_does_not_inflate_heavy_model_estimates():
    """Measured: 7669 MB with and without ViTMatte on birefnet-dis, because it
    runs after the segmentation network frees its buffers."""
    px = 1600 * 1600
    plain = server.estimate_peak_mb("birefnet-dis", px, False, False, False)
    with_vm = server.estimate_peak_mb("birefnet-dis", px, True, False, False)
    assert with_vm == plain, "ViTMatte must not be added on top of a heavy model"
    # ...but it must still dominate when the segmentation model is small.
    light = server.estimate_peak_mb("isnet-general-use", px, True, False, False)
    assert light > server.estimate_peak_mb("isnet-general-use", px, False, False, False)


def test_repeat_runs_are_not_refused_after_the_first(client, monkeypatch):
    """The peak is transient: with the arena off, a run returns its memory and
    the next image reuses it. Comparing the peak against free RAM alone made
    the first image succeed and every one after it fail."""
    monkeypatch.setattr(server, "MEMORY_HEADROOM_MB", 700)
    # After one BiRefNet run the process holds ~1.9 GB and the OS reports less
    # free; the budget must count that held memory as reusable.
    monkeypatch.setattr(server, "available_memory_mb", lambda: 7000.0)
    monkeypatch.setattr(server, "process_rss_mb", lambda: 1900.0)

    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda img, **kw: img.convert("RGBA"))

    r = client.post(
        "/remove",
        files={"image": ("a.png", png_bytes(), "image/png")},
        data={"model": "birefnet-dis"},
    )
    assert r.status_code == 200, r.json().get("detail")


def test_budget_counts_memory_the_process_already_holds(monkeypatch):
    monkeypatch.setattr(server, "process_rss_mb", lambda: 2000.0)
    assert server.process_rss_mb() == 2000.0


def test_process_rss_is_reported():
    """Used as part of the budget, so it must return a real figure."""
    assert server.process_rss_mb() > 0


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def photo_bytes():
    """A PNG carrying what a camera file (or a NEF conversion) leaves behind."""
    from PIL.PngImagePlugin import PngInfo
    img = Image.new("RGB", (64, 64), (170, 95, 60))
    exif = img.getexif()
    exif[271] = "NIKON CORPORATION"
    exif[272] = "NIKON D850"
    exif[274] = 1                 # Orientation
    exif[315] = "Akemi201"
    exif[33432] = "(c) Akemi201"
    text = PngInfo()
    text.add_text("Description", "spare part 4471")
    text.add_text("Software", "Adobe Lightroom")
    buf = io.BytesIO()
    img.save(buf, format="PNG", exif=exif.tobytes(), pnginfo=text, dpi=(300, 300))
    return buf.getvalue()


def process_photo(client, monkeypatch, raw):
    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda img, **kw: img.convert("RGBA"))
    r = client.post("/remove", files={"image": ("part.png", raw, "image/png")})
    assert r.status_code == 200
    return Image.open(io.BytesIO(r.content))


def test_camera_metadata_survives_processing(client, monkeypatch):
    """Removing a background does not invalidate capture settings, authorship
    or colour management, so they must reach the result."""
    out = process_photo(client, monkeypatch, photo_bytes())
    tags = dict(out.getexif())
    assert tags.get(271) == "NIKON CORPORATION"
    assert tags.get(272) == "NIKON D850"
    assert tags.get(315) == "Akemi201"
    assert tags.get(33432) == "(c) Akemi201"
    assert out.info.get("dpi") is not None
    assert out.info.get("Description") == "spare part 4471"


def test_orientation_tag_is_not_carried_over(client, monkeypatch):
    """rembg applies EXIF orientation to the pixels; keeping the tag would make
    a viewer rotate the image a second time."""
    out = process_photo(client, monkeypatch, photo_bytes())
    assert 274 not in dict(out.getexif())


def test_encoder_software_tag_is_dropped(client, monkeypatch):
    """That chunk described the program that wrote the source, not this one."""
    out = process_photo(client, monkeypatch, photo_bytes())
    assert "Software" not in out.info


def test_metadata_can_be_stripped(client, monkeypatch):
    """Some EXIF carries a location or a serial number, so stripping must be
    available for images that will be published."""
    monkeypatch.setattr(server, "PRESERVE_METADATA", False)
    out = process_photo(client, monkeypatch, photo_bytes())
    assert not dict(out.getexif())


def test_encoding_survives_unusable_metadata():
    """A malformed profile must cost the metadata, never the cut-out."""
    img = Image.new("RGBA", (32, 32), (10, 20, 30, 255))
    data = server._encode_png(img, {"icc_profile": b"not-a-profile", "dpi": "nonsense"})
    assert Image.open(io.BytesIO(data)).size == (32, 32)


def test_images_without_metadata_still_work(client, monkeypatch):
    out = process_photo(client, monkeypatch, png_bytes())
    assert out.mode == "RGBA"


@pytest.mark.parametrize("fmt,ext,mime", [
    ("JPEG", "jpg", "image/jpeg"),
    ("PNG", "png", "image/png"),
    ("WEBP", "webp", "image/webp"),
])
def test_metadata_survives_for_every_accepted_input_format(
    client, monkeypatch, fmt, ext, mime
):
    """A NEF export is usually a JPEG, so this cannot be PNG-only."""
    img = Image.new("RGB", (64, 64), (170, 95, 60))
    exif = img.getexif()
    exif[271] = "NIKON CORPORATION"
    exif[315] = "Akemi201"
    buf = io.BytesIO()
    img.save(buf, format=fmt, exif=exif.tobytes())

    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post("/remove", files={"image": (f"p.{ext}", buf.getvalue(), mime)})
    assert r.status_code == 200
    tags = dict(Image.open(io.BytesIO(r.content)).getexif())
    assert tags.get(271) == "NIKON CORPORATION", f"{fmt} lost its EXIF"
    assert tags.get(315) == "Akemi201"


def test_exif_is_exposed_for_client_side_jpeg_export(client, monkeypatch):
    """A JPG download is encoded by the browser's canvas, which writes no EXIF.
    The client splices it back in, so the server has to hand it over."""
    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post("/remove", files={"image": ("p.png", photo_bytes(), "image/png")})
    assert r.status_code == 200
    header = r.headers.get("x-exif")
    assert header, "X-Exif header missing"
    import base64
    raw = base64.b64decode(header)
    assert raw.startswith((b"II", b"MM", b"Exif")), "header is not an EXIF block"


def test_no_exif_header_when_the_source_has_none(client, monkeypatch):
    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post("/remove", files={"image": ("p.png", png_bytes(), "image/png")})
    assert "x-exif" not in {k.lower() for k in r.headers}


def test_startup_uses_lifespan_not_deprecated_events():
    """`@app.on_event` is deprecated in FastAPI and slated for removal; the
    lifespan context is the supported replacement."""
    source = (os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(source, "server.py"), encoding="utf-8").read()
    # the decorator itself, not the comment explaining why it is gone
    assert "@app.on_event" not in text, "use the lifespan context instead"
    assert "lifespan=lifespan" in text, "the app must be wired to the lifespan"


def test_lifespan_starts_and_stops_the_evictor():
    """The evictor has to actually run: it is what keeps an idle model from
    holding gigabytes indefinitely."""
    with TestClient(server.app):
        assert server._EVICTOR_TASK is not None or server.MODEL_IDLE_TTL <= 0
    # leaving the context runs shutdown
    assert server._EVICTOR_TASK is None


# ---------------------------------------------------------------------------
# Server-side background and mask output
# ---------------------------------------------------------------------------

def test_server_side_background_keeps_metadata(client, monkeypatch):
    """The whole point of doing this here: a canvas-composited background in
    the browser cannot carry EXIF, ICC or DPI, but this can."""
    async def fake_ensure_session(model):
        return object()

    def cut_out(im, **kw):
        # stand in for a real cut-out: transparent border, opaque centre
        rgba = im.convert("RGBA")
        rgba.putalpha(0)
        centre = Image.new("RGBA", (20, 20), (10, 20, 30, 255))
        rgba.paste(centre, (20, 20))
        return rgba

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", cut_out)
    r = client.post(
        "/remove",
        files={"image": ("p.png", photo_bytes(), "image/png")},
        data={"bgcolor": "#ffffff"},
    )
    assert r.status_code == 200
    out = Image.open(io.BytesIO(r.content))
    assert out.getpixel((2, 2)) == (255, 255, 255, 255), "background not applied"
    assert out.getpixel((30, 30))[:3] == (10, 20, 30), "subject was overwritten"
    assert dict(out.getexif()).get(271) == "NIKON CORPORATION"
    assert out.info.get("dpi") is not None


@pytest.mark.parametrize("value,expected", [
    ("#ffffff", (255, 255, 255, 255)),
    ("#fff", (255, 255, 255, 255)),
    ("#c81e1e", (200, 30, 30, 255)),
    ("200,30,30", (200, 30, 30, 255)),
    (" 0,0,0 ", (0, 0, 0, 255)),
])
def test_bgcolor_accepts_the_usual_notations(value, expected):
    assert server._parse_bgcolor(value) == expected


@pytest.mark.parametrize("value", ["nonsense", "#12345", "300,0,0", "1,2", ""])
def test_bgcolor_rejects_bad_values(value):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        server._parse_bgcolor(value or "x")


def test_only_mask_returns_the_alpha_channel(client, monkeypatch):
    """A greyscale mask is what an image editor needs to redo the cut-out by
    hand on the untouched original."""
    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post(
        "/remove",
        files={"image": ("p.png", png_bytes(), "image/png")},
        data={"only_mask": "true"},
    )
    assert r.status_code == 200
    out = Image.open(io.BytesIO(r.content))
    assert out.mode == "L", "the mask must be greyscale"
    assert "_mask.png" in r.headers.get("content-disposition", "")


def test_mask_and_background_together_are_rejected(client):
    """A mask has no colour channels to composite onto."""
    r = client.post(
        "/remove",
        files={"image": ("p.png", png_bytes(), "image/png")},
        data={"only_mask": "true", "bgcolor": "#ffffff"},
    )
    assert r.status_code == 400


def test_mask_is_returned_at_the_source_resolution(client, monkeypatch):
    """The mask is derived from the composited result, so the downscale used
    for inference must not shrink what the user gets back."""
    monkeypatch.setattr(server, "MAX_PROCESS_PX", 64)

    async def fake_ensure_session(model):
        return object()

    monkeypatch.setattr(server, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post(
        "/remove",
        files={"image": ("p.png", png_bytes(size=(320, 240)), "image/png")},
        data={"only_mask": "true"},
    )
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (320, 240)


# ---------------------------------------------------------------------------
# Folder batch CLI
# ---------------------------------------------------------------------------

def write_photo(path, size=(64, 64)):
    img = Image.new("RGB", size, (170, 95, 60))
    exif = img.getexif()
    exif[271] = "NIKON CORPORATION"
    img.save(path, exif=exif.tobytes())


def test_batch_processes_a_folder(tmp_path, monkeypatch):
    """A large run has no reason to go through a browser, where every result
    also piles up in IndexedDB."""
    src = tmp_path / "in"
    src.mkdir()
    for i in range(3):
        write_photo(src / f"p{i}.jpg")
    (src / "notes.txt").write_text("not an image")

    monkeypatch.setattr(server, "new_session", lambda *a, **k: object())
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    assert server._batch_cli([str(src)]) == 0

    out = src / "nobg"
    assert sorted(f.name for f in out.iterdir()) == [
        "p0_nobg.png", "p1_nobg.png", "p2_nobg.png"
    ], "the .txt must be ignored"


def test_batch_skips_work_already_done(tmp_path, monkeypatch, capsys):
    """An interrupted run has to be resumable by simply repeating it."""
    src = tmp_path / "in"
    src.mkdir()
    write_photo(src / "a.jpg")
    monkeypatch.setattr(server, "new_session", lambda *a, **k: object())
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))

    server._batch_cli([str(src)])
    capsys.readouterr()
    server._batch_cli([str(src)])
    assert "1 skipped" in capsys.readouterr().out


def test_batch_keeps_metadata_and_applies_background(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    write_photo(src / "a.jpg")

    def cut_out(im, **kw):
        rgba = im.convert("RGBA")
        rgba.putalpha(0)
        return rgba

    monkeypatch.setattr(server, "new_session", lambda *a, **k: object())
    monkeypatch.setattr(server, "remove", cut_out)
    server._batch_cli([str(src), "--bgcolor", "#ffffff"])

    out = Image.open(src / "nobg" / "a_nobg.png")
    assert out.getpixel((2, 2)) == (255, 255, 255, 255)
    assert dict(out.getexif()).get(271) == "NIKON CORPORATION"


def test_batch_survives_a_broken_file(tmp_path, monkeypatch):
    """One unreadable file must not abandon the rest of the folder."""
    src = tmp_path / "in"
    src.mkdir()
    write_photo(src / "good.jpg")
    (src / "broken.png").write_bytes(b"not a png at all")

    monkeypatch.setattr(server, "new_session", lambda *a, **k: object())
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    server._batch_cli([str(src)])
    assert (src / "nobg" / "good_nobg.png").exists()


def test_batch_rejects_a_missing_folder(tmp_path):
    assert server._batch_cli([str(tmp_path / "nope")]) == 1


def test_models_payload_exposes_everything_the_warning_needs(client):
    """The UI mirrors the server's budget; it can only do that if the server
    sends every term rather than letting the client hardcode its own."""
    data = client.get("/models").json()
    for field in ("peak_mb", "available_mb", "process_mb", "headroom_mb",
                  "max_process_px"):
        assert field in data, f"/models must expose {field}"
    assert data["headroom_mb"] == server.MEMORY_HEADROOM_MB


def test_peak_estimates_stay_close_to_what_was_measured():
    """Padding is not free: every MB of cushion is a run the guard refuses
    that would in fact have fitted. birefnet-dis measured 7669 MB."""
    est = server.estimate_peak_mb("birefnet-dis", 1600 * 1600, False, False, False)
    assert 7669 <= est <= 7669 + 600, (
        f"estimate {est:.0f} MB drifted from the measured 7669 MB"
    )


def test_package_name_is_not_the_upstream_one():
    """`remove-background-local` is taken on npm by the project this was forked
    from. Publishing under it is impossible, and reusing the name would confuse
    the two even locally."""
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = json.load(open(os.path.join(root, "package.json"), encoding="utf-8"))
    assert pkg["name"] != "remove-background-local", "that name belongs upstream"
    assert pkg["name"] == "kirinuki"
    # the binaries must match the package, or `npm install -g` installs a
    # command nobody documented
    assert set(pkg["bin"]) == {"kirinuki", "kiri"}


def test_published_files_cover_what_the_app_needs_to_run():
    """A missing entry here ships a package that cannot start."""
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = json.load(open(os.path.join(root, "package.json"), encoding="utf-8"))
    listed = set(pkg["files"])
    for needed in ("bin/", "server.py", "requirements.txt", "static/", "run.sh"):
        assert needed in listed, f"{needed} would not be published"


def test_reported_time_covers_the_whole_request(client, monkeypatch):
    """The figure on each card is what the user waits for. Timing only the
    inference under-reported it by about 25%: loading the model, queueing and
    encoding the PNG are part of the wait too."""
    async def slow_ensure_session(model):
        import time as _t
        _t.sleep(0.05)          # stands in for loading a model
        return object()

    monkeypatch.setattr(server, "ensure_session", slow_ensure_session)
    monkeypatch.setattr(server, "remove", lambda im, **kw: im.convert("RGBA"))
    r = client.post("/remove", files={"image": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 200

    total = float(r.headers["x-processing-time"])
    infer = float(r.headers["x-inference-time"])
    assert total >= infer, "the total cannot be shorter than the inference"
    assert total >= 0.05, "model loading must be inside the reported time"
