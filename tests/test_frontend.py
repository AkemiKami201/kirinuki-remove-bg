"""Guard tests for the frontend.

The UI has no JS test runner, so these are static checks against
`static/index.html`, `static/js/app.js` and `static/css/app.css`. They exist to
stop specific bugs from coming back, not to test rendering: each one below maps
to a defect that was actually shipped.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
APP_JS = ROOT / "static" / "js" / "app.js"
APP_CSS = ROOT / "static" / "css" / "app.css"


def index_text() -> str:
    return INDEX.read_text(encoding="utf-8")


def script_body() -> str:
    """The application's JavaScript."""
    return APP_JS.read_text(encoding="utf-8")


def test_user_filenames_are_escaped_in_html():
    """File names reach innerHTML, so they must go through esc().

    A file named `<img src=x onerror=...>.png` would otherwise run its handler,
    and since the name is persisted in IndexedDB it would run again on reload.

    Only innerHTML assignments matter here: job.name in a toast, a confirm() or
    a console warning is plain text and safe as-is.
    """
    js = script_body()
    assert "function esc(" in js, "the esc() helper must exist"

    offenders = []
    for line in js.splitlines():
        if "innerHTML" not in line:
            continue
        # strip the escaped uses, then look for what is left
        stripped = line.replace("esc(job.name)", "")
        if "job.name" in stripped:
            offenders.append(line.strip()[:100])
    assert not offenders, (
        "job.name reaches innerHTML unescaped; wrap it in esc(): " + str(offenders)
    )


def test_esc_covers_the_dangerous_characters():
    js = script_body()
    body = js[js.index("function esc("):]
    body = body[: body.index("\n}")]
    for ch in ("&", "<", ">", '"', "'"):
        assert f'"{ch}"' in body or f"/{ch}/g" in body or ch in body, (
            f"esc() must handle {ch!r}"
        )


def test_pump_is_not_self_recursive():
    """The queue drains in a loop; calling pump() from its own finally grew
    the call depth with the queue length."""
    js = script_body()
    start = js.index("async function pump(")
    body = js[start : js.index("\n}", start)]
    # skip the declaration line itself, which of course contains "pump("
    inner = body.split("\n", 1)[1]
    assert "pump()" not in inner, "pump() must not call itself; use a loop"
    assert "while (true)" in inner, "pump() should drain the queue iteratively"


def test_deleted_jobs_are_not_resurrected():
    """A job deleted mid-request must not be written back to IndexedDB."""
    js = script_body()
    start = js.index("async function runJob(")
    body = js[start : js.index("\npersistJob", start) if "\npersistJob" in js else len(js)]
    assert "jobs.includes(job)" in body, (
        "runJob must check the job still exists before persisting it"
    )


def test_storage_quota_failures_are_reported():
    """A failed IndexedDB write used to be swallowed: work looked saved but was
    gone after a reload."""
    js = script_body()
    start = js.index("async function persistJob(")
    body = js[start : js.index("\n}", start)]
    assert "catch {}" not in body, "persistJob must not swallow write failures"
    assert "QuotaExceededError" in js, "a full quota must be reported to the user"


def test_no_silent_catches_outside_known_fallbacks():
    """`catch {}` hides real failures.

    The only acceptable shape is the fetch error-detail fallback: parsing the
    body is best-effort, and whatever happens a better error is thrown on the
    same line. Anything else must log through warn().
    """
    js = script_body()
    silent = [ln.strip() for ln in js.splitlines() if "catch {}" in ln]
    for line in silent:
        assert "throw new Error(" in line, (
            f"silent catch without a rethrow: {line[:100]}"
        )


def test_referenced_element_ids_exist():
    """$('id') lookups must match an id in the markup, or the UI throws on a
    null element at runtime."""
    text = index_text()
    js = script_body()
    defined = set(re.findall(r'id="([\w-]+)"', text))
    # ids created dynamically by renderers, not present in the static markup
    dynamic = {"dl-", "card-"}
    missing = set()
    for used in set(re.findall(r'\$\("([\w-]+)"\)', js)):
        if any(used.startswith(p) for p in dynamic):
            continue
        if used not in defined:
            missing.add(used)
    assert not missing, f"$() references ids not in the markup: {sorted(missing)}"


def test_frontend_stays_split_into_three_files():
    """Markup, styles and behaviour live in separate files.

    They used to share one 795-line index.html, which made every UI change
    risky and left the JS untestable. Inline <style>/<script> blocks would
    silently undo that, so they are rejected here.
    """
    assert APP_JS.exists(), "static/js/app.js is missing"
    assert APP_CSS.exists(), "static/css/app.css is missing"

    text = index_text()
    assert "<style>" not in text, "styles belong in static/css/app.css"
    assert 'href="/static/css/app.css"' in text, "index.html must link the stylesheet"
    assert 'src="/static/js/app.js"' in text, "index.html must load the script"

    # One inline <script> is allowed: the pre-paint theme bootstrap. It has to
    # run before the first paint, which a deferred external file cannot do, so
    # it stays inline -- but it must remain that one small block.
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
    assert len(inline) <= 1, (
        f"{len(inline)} inline <script> blocks; behaviour belongs in static/js/app.js"
    )
    if inline:
        body = inline[0]
        assert "rmbg-theme" in body, (
            "the only allowed inline script is the pre-paint theme bootstrap"
        )
        assert len(body.splitlines()) <= 12, (
            "the theme bootstrap must stay minimal; real logic goes in app.js"
        )


def test_script_is_deferred():
    """app.js runs top-level $() lookups, so the DOM must already exist."""
    text = index_text()
    m = re.search(r"<script[^>]*src=\"/static/js/app\.js\"[^>]*>", text)
    assert m, "app.js script tag not found"
    assert "defer" in m.group(0) or "type=\"module\"" in m.group(0), (
        "the script tag must be deferred, or top-level $() lookups return null"
    )


# ---------------------------------------------------------------------------
# Theming
# ---------------------------------------------------------------------------

def css_text() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def test_light_and_dark_palettes_define_the_same_variables():
    """A variable missing from one palette silently falls back to the other
    theme's value, which shows up as one unreadable element."""
    css = css_text()
    dark = re.search(r":root\s*\{(.*?)\}", css, re.S).group(1)
    light = re.search(r':root\[data-theme="light"\]\s*\{(.*?)\}', css, re.S).group(1)

    def colour_vars(block):
        # Only colour tokens must exist in both palettes. Structural tokens
        # (type scale, spacing, radii, layout sizes) are theme-independent on
        # purpose: text should not resize or reshape when the theme changes.
        structural = ("--fs-", "--sp-", "--r-", "--font", "--header-", "--footer-", "--sidebar-")
        return {
            n for n in re.findall(r"(--[\w-]+)\s*:", block)
            if not n.startswith(structural)
        }

    missing = colour_vars(dark) - colour_vars(light)
    assert not missing, f"light theme does not override: {sorted(missing)}"


def test_theme_bootstrap_runs_before_the_stylesheet_paints():
    """The inline bootstrap must sit in <head>, or the default theme flashes."""
    text = index_text()
    head = text[: text.index("</head>")]
    assert "rmbg-theme" in head, "theme bootstrap must be in <head>"


def test_no_hardcoded_colours_outside_the_palettes():
    """A literal colour in a rule cannot follow the theme.

    Only the palette blocks may contain colour literals; everything else must
    go through var(--...).
    """
    css = css_text()
    # drop the palette definitions, then look for what is left
    stripped = re.sub(r":root[^{]*\{.*?\}", "", css, flags=re.S)
    stripped = re.sub(r"@media[^{]*\{\s*:root[^{]*\{.*?\}\s*\}", "", stripped, flags=re.S)
    offenders = []
    for i, line in enumerate(stripped.splitlines(), 1):
        if re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", line):
            offenders.append(line.strip()[:90])
    assert not offenders, (
        "colour literals outside the palettes (use var(--...)): " + str(offenders)
    )


def test_theme_choices_are_wired_in_markup_and_script():
    text, js = index_text(), script_body()
    for choice in ("light", "system", "dark"):
        assert f'data-theme-choice="{choice}"' in text, f"no {choice} theme button"
    assert "localStorage" in js, "the theme choice must be persisted"
    assert "removeAttribute" in js, '"system" must clear data-theme to follow the OS'


def test_every_class_in_the_markup_has_a_style_rule():
    """A class used in index.html but absent from the stylesheet is dead
    layout: this is how `.workarea` and `.fixed-top` were once lost in a
    rewrite, which silently collapsed the whole work area.
    """
    css = css_text()
    used = set()
    for attr in re.findall(r'class="([^"]+)"', index_text()):
        used.update(attr.split())

    # Classes only ever applied from JS, or used purely as JS hooks.
    dynamic = {
        "view", "hidden", "sel", "active", "show", "flash", "dragover",
        "disabled", "warn", "err", "ok", "viewed", "is-default", "checker",
    }
    missing = sorted(c for c in used - dynamic if f".{c}" not in css)
    assert not missing, f"classes in the markup with no CSS rule: {missing}"


def test_stylesheet_has_no_dangling_variable_references():
    """var(--x) with no --x defined silently renders as nothing."""
    css = css_text()
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    missing = sorted(used - defined)
    assert not missing, f"var() references undefined tokens: {missing}"


# ---------------------------------------------------------------------------
# Viewer and model comparison
# ---------------------------------------------------------------------------

def test_viewer_offers_all_three_compare_modes():
    text, js = index_text(), script_body()
    for mode in ("result", "split", "side"):
        assert f'data-mode="{mode}"' in text, f"viewer is missing the {mode} mode"
    assert "VIEW_MODES" in js and "openViewer" in js


def test_viewer_releases_images_on_close():
    """A full-size pair decoded in memory should not outlive the viewer."""
    js = script_body()
    body = js[js.index("function closeViewer("):]
    body = body[: body.index("\n}")]
    assert "removeAttribute" in body, "closeViewer must drop the image sources"


def test_viewer_divider_keeps_a_constant_on_screen_size():
    """The divider lives inside the scaled pane; without counter-scaling its
    grip shrinks as you zoom in, which is exactly when it is needed most."""
    css, js = css_text(), script_body()
    assert "--inv-zoom" in css, "the divider must counter-scale"
    assert "--inv-zoom" in js, "app.js must publish the inverse zoom"


def test_compare_keeps_existing_results():
    """Comparison must add results, never replace them -- that is the whole
    difference from Reprocess."""
    js = script_body()
    body = js[js.index("function runCompare("):]
    body = body[: body.index("\n}")]
    assert "jobs.push" in body, "runCompare must add jobs"
    assert "revokeObjectURL" not in body, (
        "runCompare must not free the existing result: comparison adds, it does "
        "not replace"
    )
    assert "transient: true" in body, (
        "a comparison run must not steal the pinned default model"
    )


def test_compare_only_offers_downloaded_models():
    """Ticking a model that is not on disk would start a ~930 MB download from
    a dialog that says nothing about downloading."""
    js = script_body()
    body = js[js.index("function openCompare("):]
    body = body[: body.index("\nfunction closeCompare")]
    assert "DOWNLOADED[key]" in body, "rows must reflect what is on disk"
    assert "disabled" in body


def test_viewer_surface_is_not_selectable():
    """Dragging over the stage used to paint a blue text selection across the
    whole image area, and left it there until you clicked elsewhere."""
    css = css_text()
    stage = css[css.index("  .viewer-stage {"):]
    stage = stage[: stage.index("\n  }")]
    assert "user-select: none" in stage, "the viewer stage must not be selectable"


def test_viewer_images_do_not_capture_the_pointer():
    """The <img> elements sat above the stage, so the browser's native image
    drag swallowed pointerdown and panning silently did nothing."""
    css, js = css_text(), script_body()
    layer = css[css.index("  .v-layer img {"):]
    layer = layer[: layer.index("\n  }")]
    assert "pointer-events: none" in layer, "images must not intercept the pointer"
    assert "user-drag: none" in layer, "native image drag must be off"
    assert 'addEventListener("dragstart"' in js, "dragstart must be suppressed"


def test_split_handle_has_a_usable_hit_area():
    """A 2px grab target is unusable; the visible hairline is drawn by a
    pseudo-element inside a much wider invisible strip."""
    css = css_text()
    handle = css[css.index("  .v-handle {"):]
    handle = handle[: handle.index("\n  }")]
    width = re.search(r"width:\s*calc\((\d+)px", handle)
    assert width and int(width.group(1)) >= 16, (
        "the divider's hit area must be at least 16px wide"
    )
    assert ".v-handle::before" in css, "the visible line must be a pseudo-element"


def test_drag_ends_even_when_the_pointer_leaves_the_stage():
    """Releasing outside the stage used to leave the viewer stuck mid-drag."""
    js = script_body()
    assert 'window.addEventListener("pointerup", endVDrag)' in js, (
        "a pointerup outside the stage must still end the drag"
    )


# ---------------------------------------------------------------------------
# Export: trim and ZIP
# ---------------------------------------------------------------------------

def test_trim_measures_alpha_before_the_backdrop_is_filled():
    """Filling the chosen backdrop first makes every pixel opaque, and the trim
    would then find nothing to cut."""
    js = script_body()
    body = js[js.index("async function renderJobCanvas("):]
    body = body[: body.index("\nfunction exportMime")]
    probe = body.index("probe")
    fill = body.index("fillStyle")
    assert probe < fill, "alpha must be measured before the backdrop is painted"


def test_trim_handles_a_fully_transparent_image():
    """Returning a zero-sized box would produce an empty export."""
    js = script_body()
    body = js[js.index("function alphaBounds("):]
    body = body[: body.index("\n}")]
    assert "return null" in body, "a fully transparent image must return null"


def test_zip_marks_filenames_as_utf8():
    """Without the UTF-8 flag, accented filenames are mojibake on extraction."""
    js = script_body()
    body = js[js.index("function buildZip("):]
    body = body[: body.index("\n}")]
    assert body.count("0x0800") >= 2, (
        "the UTF-8 flag must be set in both the local and central headers"
    )


def test_zip_deduplicates_filenames():
    """Comparing models produces several results from one source image, so the
    generated names collide."""
    js = script_body()
    assert "function uniqueName(" in js
    body = js[js.index("async function downloadAll("):]
    body = body[: body.index("\n}")]
    assert "uniqueName(" in body, "batch export must de-duplicate names"


def test_batch_export_reenables_its_button():
    """The button is disabled while packing; an error must not leave it stuck."""
    js = script_body()
    body = js[js.index("async function downloadAll("):]
    body = body[: body.index("\n}")]
    assert "finally" in body and "btn.disabled = false" in body


def test_jpeg_export_reattaches_exif():
    """canvas.toBlob writes no EXIF, so a JPG download would silently lose the
    camera data the server took care to preserve."""
    js = script_body()
    assert "function withJpegExif(" in js
    body = js[js.index("function withJpegExif("):]
    body = body[: body.index("\n}")]
    # APP1 is the marker that carries EXIF; 0xE1 is its second byte.
    assert "0xE1" in body, "must write an APP1 segment"
    assert "0x45, 0x78, 0x69, 0x66" in body, 'must include the "Exif" signature'
    # Existing APP0/APP1 segments are skipped so they cannot stack up.
    assert "0xE0" in body, "must skip the encoder's own APP0/APP1"


def test_plain_png_export_returns_the_server_bytes():
    """Any canvas round-trip discards metadata; when the export would be an
    exact copy, the server's own blob is handed back instead."""
    js = script_body()
    body = js[js.index("function canUseServerBlob("):]
    body = body[: body.index("\n}")]
    assert 'format === "png"' in body
    assert "!opts.trim" in body
    assert "job.outBlob" in body


def test_export_formats_map_to_the_right_mime_and_extension():
    """The format selector has to reach both toBlob and the download name; a
    mismatch produces a file whose extension lies about its contents."""
    js = script_body()
    mime = js[js.index("function exportMime("):]
    mime = mime[: mime.index("\n}")]
    assert '"image/png"' in mime and '"image/webp"' in mime and '"image/jpeg"' in mime

    ext = js[js.index("function exportExt("):]
    ext = ext[: ext.index("\n")]
    # "jpg" is the extension users expect; the MIME type is image/jpeg.
    assert '"jpg"' in ext

    name = js[js.index("function exportName("):]
    name = name[: name.index("\n}")]
    assert "exportExt(format)" in name, "the filename must use the chosen format"


def test_png_export_is_not_given_a_quality_value():
    """PNG is lossless; passing a quality argument is meaningless and would be
    a sign the format branches got mixed up."""
    js = script_body()
    line = next(l for l in js.splitlines() if "cv.toBlob(r, exportMime(format)" in l)
    assert 'format === "png" ? undefined' in line, (
        "PNG must get undefined quality, lossy formats 0.92"
    )


def test_batch_and_single_downloads_read_the_same_selector():
    """Two code paths reach toBlob; both must honour the same dropdown."""
    js = script_body()
    single = js[js.index("async function exportJob("):]
    single = single[: single.index("\n}")]
    assert "exportOptions()" in single

    batch = js[js.index("async function downloadAll("):]
    batch = batch[: batch.index("\n}")]
    assert '$("dl-format").value' in batch


def test_memory_warning_mirrors_the_server_budget():
    """The warning said "this will be refused" for runs the server accepts,
    because it compared the peak against free RAM only. The server's budget
    also counts the memory the process already holds and reuses."""
    js = script_body()
    budget = js[js.index("function memoryBudgetMb("):]
    budget = budget[: budget.index("\n}")]
    assert "PROCESS_MB" in budget, (
        "the budget must include the memory the process already holds"
    )
    assert "HEADROOM_MB" in budget, "the headroom must come from the server"


def test_memory_estimate_matches_the_server_formula():
    """Two copies of one formula drift. These are the terms that must match
    estimate_peak_mb() in server.py."""
    js = script_body()
    est = js[js.index("function estimatePeakMb("):]
    est = est[: est.index("\n}")]
    assert "Math.max" in est, (
        "ViTMatte must be a max(), not an addition: it runs after the "
        "segmentation network frees its buffers"
    )
    assert "MAX_PROCESS_PX" in est, (
        "megapixels must come from the server's cap, not a hardcoded guess"
    )
    assert "320" in est and "550" in est, "per-megapixel terms must match"


def test_memory_figures_are_refreshed_before_warning():
    """Free memory moves as other applications come and go, so a figure read
    once at load time goes stale."""
    js = script_body()
    assert "async function refreshMemory(" in js
    assert "refreshMemoryWarning()" in js, (
        "changing model or options must re-read the current figures"
    )


def test_backdrop_change_does_not_rebuild_every_card():
    """Rebuilding the list throws away the scroll position: picking a colour on
    the tenth image used to jump the page back to the top."""
    js = script_body()
    assert "function paintCardBackdrop(" in js, (
        "a single-card repaint helper must exist"
    )
    start = js.index("const pickBg = (bg, live) => {")
    body = js[start : js.index("};", start)]
    assert "paintCardBackdrop(job)" in body
    assert "renderResults()" not in body, (
        "changing one card's backdrop must not rebuild the whole list"
    )

    start = js.index("function buildGlobalSwatches(")
    body = js[start : js.index("\n}", start)]
    assert "paintCardBackdrop" in body
    assert "renderResults()" not in body


def test_backdrop_change_does_not_rewrite_the_blobs():
    """A stored record holds the source image and the cut-out - tens of MB.
    Re-putting the whole record to save a colour stalled the page and churned
    the disk on every click."""
    js = script_body()
    assert "async function idbPatch(" in js, "a partial update helper must exist"
    start = js.index("const pickBg = (bg, live) => {")
    body = js[start : js.index("};", start)]
    assert "idbPatch(" in body, "the swatch must patch, not re-put the record"
    assert "persistJob(" not in body, (
        "persistJob writes both blobs; use idbPatch for a colour change"
    )


def test_drop_zone_is_never_squeezed_by_a_long_result_list():
    """A drag is only accepted over the drop zone itself, so the panel holding
    it must not shrink to make room for the cards below. It caps its height and
    scrolls instead, which keeps the advanced options reachable on a short
    screen without the list ever eating the drop target."""
    css = css_text()
    block = css[css.index("  .fixed-top {"):]
    block = block[: block.index("\n  }")]
    assert "flex: 0 0 auto" in block, (
        "the panel must not be shrinkable; a flex-shrink of 1 lets the result "
        "list squeeze the drop zone out of view"
    )
    assert "overflow-y: auto" in block, "it must scroll rather than clip"
    assert "max-height" in block, (
        "without a cap the panel can push the results off-screen"
    )
    assert "min-height: min-content" not in block, (
        "min-content would win over max-height and defeat the cap"
    )


def test_reprocessing_one_image_does_not_rebuild_the_list():
    """renderResults() clears every card and appends them again, so the browser
    loses the scroll position. Doing that for a single job's state change threw
    the reader back to the top of a long session -- on Reprocess, and again on
    every transition as the queue ran."""
    js = script_body()
    assert "function refreshCard(" in js, "a single-card refresh helper must exist"

    start = js.index("function reprocessJob(")
    body = js[start : js.index("\n}", start)]
    assert "refreshCard(job)" in body, "Reprocess must refresh just its own card"
    assert "renderAll()" not in body, (
        "reprocessing one image must not rebuild the whole list"
    )

    start = js.index("async function runJob(")
    body = js[start : js.index("\n}", start)]
    assert "renderAll()" not in body, (
        "a job's state transitions must not rebuild the list"
    )

    start = js.index("async function pump()")
    body = js[start : js.index("\nasync function runJob", start)]
    assert "renderAll()" not in body, (
        "draining the queue must not rebuild the list and undo the per-card work"
    )


def test_custom_colour_input_survives_its_own_events():
    """The native colour picker fires `input` continuously while it is open. If
    a handler rebuilds the swatch row, the live <input> is removed mid-drag and
    the picker dies with it -- clicking a colour appeared to do nothing at all.
    The row must be left standing while the picker streams."""
    js = script_body()
    start = js.index("function buildSwatches(")
    body = js[start : js.index("\n}", start)]
    assert "markActive(" in body, (
        "the highlight must move in place rather than by rebuilding the row"
    )
    assert 'onPick(e.target.value, true)' in body, (
        "streamed values must be flagged so callers skip the rebuild"
    )
    assert 'inp.addEventListener("change"' in body, (
        "the committed value needs a change listener; input alone never settles"
    )

    for caller in ("const pickBg = (bg, live) => {", "buildSwatches($(\"global-bg\")"):
        start = js.index(caller)
        chunk = js[start : start + 600]
        assert "live" in chunk, f"{caller} must honour the live flag"


def test_result_cards_have_no_composited_blur():
    """Each backdrop-filter is a composited layer the browser keeps alive.
    With a label and a zoom button in both halves of every card, a list of ten
    images carried 60 of them and scrolling stuttered."""
    css = css_text()
    for selector in (".cell .label {", ".cell .zoom-btn {"):
        block = css[css.index(selector):]
        block = block[: block.index("\n  }")]
        # strip comments: they explain why the property is absent
        block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
        assert "backdrop-filter:" not in block, (
            f"{selector.strip()} must not create a composited layer"
        )


def test_cards_skip_offscreen_rendering():
    """A long session holds full-resolution images in every card."""
    css = css_text()
    block = css[css.index("  .card {"):]
    block = block[: block.index("\n  }")]
    assert "content-visibility" in block, (
        "cards should skip layout and paint while off-screen"
    )


def test_card_footer_opts_out_of_content_visibility():
    """A native <select> popup is a platform window tied to its element, and it
    is dismissed whenever the browser re-runs the visibility check on the
    subtree that holds it. Every card repaint during a queue run does exactly
    that, so the model list on finished cards would not open by mouse -- while
    the keyboard, which needs no popup, still worked. The footer holding the
    selects must therefore render normally; the image cells above it keep the
    optimisation."""
    css = css_text()
    card = css[css.index("  .card {"):]
    card = card[: card.index("\n  }")]
    assert "content-visibility" in card, "cards still skip work while off-screen"

    foot = css[css.index("  .card-foot {"):]
    foot = foot[: foot.index("}")]
    assert "content-visibility: visible" in foot, (
        "the footer's selects must not sit inside a skipped subtree"
    )
    assert "contain-intrinsic-size: none" in foot, (
        "the placeholder size must be dropped along with the skipping"
    )


def test_cards_display_downscaled_previews():
    """A card shows an image at ~320px but held the 3000px original, so the
    browser rescaled several megapixels on every repaint. Seven results meant
    roughly half a gigabyte of decoded pixels."""
    js = script_body()
    assert "async function makePreviewUrl(" in js, "a preview builder must exist"
    assert "job.outPreview || job.outUrl" in js, (
        "the card must prefer the preview and fall back to the original"
    )
    assert "job.inPreview || job.inUrl" in js


def test_viewer_and_export_keep_full_resolution():
    """Previews are for the card only: zooming and downloading must still get
    every pixel the model produced."""
    js = script_body()
    assert '$("v-out-img").src = job.outUrl' in js, "the viewer must use the original"
    assert "loadImage(job.outUrl)" in js, "export must use the original"
    for line in js.splitlines():
        if "v-out-img" in line or "loadImage(job.out" in line:
            assert "Preview" not in line, f"preview leaked into: {line.strip()[:80]}"


def test_previews_are_freed_with_their_job():
    """Two extra object URLs per result add up over a long session."""
    js = script_body()
    for context in ("function deleteJob(", "function deleteSession(", "function reprocessJob("):
        start = js.index(context)
        body = js[start : js.index("\n}", start)]
        assert "Preview" in body, f"{context.strip()} must release its previews"


def test_images_are_lazy_and_decoded_off_thread():
    js = script_body()
    start = js.index("function makeImg(")
    body = js[start : js.index("\n}", start)]
    assert 'img.loading = "lazy"' in body
    assert 'img.decoding = "async"' in body


def test_split_layers_are_pixel_aligned():
    """In split mode the two halves are stacked, so they must agree exactly.

    A border on .v-out ate a pixel of its content box (box-sizing: border-box
    plus inset: 0), shifting and shrinking the result half. Zooming in made the
    mismatch visible.
    """
    css = css_text()
    for line in css.splitlines():
        if ".viewer.mode-split .v-out" in line:
            assert "border-left:" not in line, (
                "a border on .v-out offsets it from .v-orig; the divider is .v-handle"
            )

    orig = css[css.index("  .v-orig {"):]
    orig = orig[: orig.index("}")]
    assert "position: absolute" in orig, (
        ".v-orig must be absolutely positioned like .v-out"
    )


def test_viewer_images_are_sized_to_their_layer():
    """Letting each image size itself from its own file leaves the two halves
    free to land on different sub-pixel sizes when scaled."""
    css = css_text()
    block = css[css.index("  .v-layer img {"):]
    block = block[: block.index("\n  }")]
    assert "width: 100%" in block and "height: 100%" in block
    assert "object-fit: contain" in block


def test_side_by_side_still_flows():
    """The layers are absolute when stacked; side-by-side puts them back in
    flow, and that override must survive."""
    css = css_text()
    assert ".viewer.mode-side .v-layer { position: relative; inset: auto; }" in css


def test_viewer_pane_is_not_shrunk_by_the_stage():
    """The stage centres its child with flex. layoutViewer() sets the pane to
    the image's exact pixel size, but a flex item shrinks by default: the image
    drifted off centre and the split divider stopped matching the seam."""
    css = css_text()
    block = css[css.index("  .v-pane {"):]
    block = block[: block.index("\n  }")]
    assert "flex: 0 0 auto" in block or "flex-shrink: 0" in block, (
        "the pane must keep the size layoutViewer() gives it"
    )


def test_split_divider_and_clip_share_one_measure():
    """The handle and the clip-path must be positioned from the same box, or
    the visible seam and the thing you drag disagree."""
    js = script_body()
    body = js[js.index("function applySplit("):]
    body = body[: body.index("\n}")]
    assert "clipPath" in body and "pct" in body
    assert 'style.left = pct + "%"' in body, (
        "both must use the same percentage of the pane"
    )


def test_storage_figure_waits_for_the_deletes_to_commit():
    """navigator.storage.estimate() reports what is on disk right now. Firing
    the IndexedDB deletes and reading immediately returned the pre-delete
    total, so the footer kept showing the old figure until a reload."""
    js = script_body()
    assert "async function refreshStorageAfter(" in js
    body = js[js.index("async function refreshStorageAfter("):]
    body = body[: body.index("\n}")]
    assert "await Promise.allSettled" in body, (
        "the estimate must be read after the transactions finish"
    )

    for context in ("function deleteJob(", "function deleteSession("):
        start = js.index(context)
        block = js[start : js.index("\n}", start)]
        assert "refreshStorageAfter(" in block, (
            f"{context.strip()} must refresh the figure once its deletes land"
        )


def test_storage_figure_is_not_hidden_by_an_empty_job_list():
    """Closing every session does not erase what is on disk; hiding the figure
    then would suggest it had."""
    js = script_body()
    start = js.index("async function renderStorage(")
    body = js[start : js.index("\n}", start)]
    assert "!jobs.length" not in body, (
        "the figure reflects disk usage, not how many cards are on screen"
    )


def test_every_write_transaction_handles_abort():
    """A transaction can be aborted without firing onerror -- which is exactly
    what the browser does when it kills a write for lack of disk space. A
    promise settled only on complete/error hangs there instead of reporting."""
    js = script_body()
    for name in ("idbPut", "idbDelete", "idbClear", "idbPatch"):
        start = js.index(f"function {name}(")
        block = js[start : js.index("\n}", start) + 2]
        assert "txDone(" in block, (
            f"{name} must settle through txDone so an abort rejects instead of hanging"
        )
    start = js.index("function txDone(")
    body = js[start : js.index("\n}", start)]
    assert "tx.onabort" in body, "txDone must listen for abort, not just error"


def test_a_blocked_database_is_not_reported_as_a_full_disk():
    """Chrome raises `UnknownError: The user denied permission to access the
    database` when the browser is set to block this page's storage. Telling
    someone to delete older sessions then sends them off doing something that
    cannot possibly help -- nothing about the disk is wrong."""
    js = script_body()
    assert "STORAGE_BLOCKED_ERRORS" in js
    start = js.index("const STORAGE_BLOCKED_ERRORS")
    assert "UnknownError" in js[start : js.index("\n", start)]

    start = js.index("async function classifyStorageError(")
    body = js[start : js.index("\n}", start)]
    assert "QuotaExceededError" in body, "the clean quota error must still count"
    assert "STORAGE_BLOCKED_ERRORS.includes" in body, (
        "a refused database must be told apart from a full one"
    )
    assert "storageUsage()" in body, (
        "an unnamed failure with the quota nearly gone is still a full disk"
    )
    assert '"unknown"' in body, "anything else must be reported honestly, not guessed"


def test_the_three_storage_failures_give_three_different_answers():
    js = script_body()
    start = js.index("async function reportStorageFailure(")
    body = js[start : js.index("\n}\n", start)]
    assert "delete older sessions" in body.lower(), "the full case says what frees space"
    assert "blocking storage" in body.lower(), "the blocked case says the browser refused"
    assert "see the console" in body.lower(), "the unknown case admits it does not know"


def test_a_blocked_database_is_reported_at_startup():
    """Opening the database fails the same way a write does. Swallowing that
    means finding out only after an image has been processed and waited for."""
    js = script_body()
    start = js.index("async function restoreFromIDB(")
    body = js[start : js.index("\n}", start)]
    assert "catch {" not in body, "the failure must not be swallowed silently"
    assert "reportStorageFailure(" in body


def test_opening_the_database_cannot_hang():
    """`indexedDB.open` throws outright in a browser set to block site data, and
    onblocked fires when another tab holds the database against an upgrade.
    Neither settles a promise wired only to success and error."""
    js = script_body()
    start = js.index("function idbOpen(")
    body = js[start : js.index("\n}\n", start)]
    assert "try {" in body, "reading indexedDB can throw, not just fail"
    assert "onblocked" in body, "a blocked open must reject rather than hang"


def test_the_storage_warning_can_fire_again_after_space_is_freed():
    """Otherwise it warns once per page load and stays silent afterwards,
    including after the user acts on it and fills the disk up again."""
    js = script_body()
    start = js.index("async function refreshStorageAfter(")
    body = js[start : js.index("\n}", start)]
    assert "storageWarned = false" in body, (
        "deleting results must re-arm the warning"
    )


def test_a_full_quota_is_flagged_before_the_work_not_after():
    """Learning that a result cannot be kept is much less annoying before
    waiting through processing than after."""
    js = script_body()
    assert "warnIfStorageNearlyFull()" in js
    start = js.index("async function warnIfStorageNearlyFull(")
    body = js[start : js.index("\n}", start)]
    assert "storageUsage()" in body and "toast(" in body
