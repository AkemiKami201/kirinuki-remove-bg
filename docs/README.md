# Documentation assets

Images used by the main `README.md`. Documentation only - none of this ships in
the npm package.

## Layout

| Folder | What belongs there |
|---|---|
| `screenshots/` | The interface in use: main view, viewer modes, models page |
| `comparisons/` | One photo through several models, so the difference is visible |
| `examples/` | Standalone before/after pairs, if you want them outside the app chrome |
| `banner.webp` | The logo shown at the top of the README |
| `brand/` | Full-size logo sources. Not used by the app; kept so the icons can be regenerated |

## Format

**WEBP at quality 88, 1600px wide.** These are fetched every time someone opens
the README, and the screenshots were 6.9 MB as PNG against 0.8 MB as WEBP with
no visible difference on text. To add a new one:

```bash
python - <<'PY'
from PIL import Image
im = Image.open("new-shot.png").convert("RGB")
w, h = im.size
if w > 1600:
    im = im.resize((1600, round(h * 1600 / w)), Image.Resampling.LANCZOS)
im.save("docs/screenshots/new-shot.webp", format="WEBP", quality=88, method=6)
PY
```

Keep PNG only when transparency itself is the subject.

## Naming

Lowercase and descriptive, since the filename doubles as a hint when an image
fails to load:

```
screenshots/viewer-split-stator.webp
comparisons/hair-isnet-vs-birefnet-massive.webp
```

## Check before committing

```bash
find docs -type f -size +500k     # anything heavy that slipped through
```
