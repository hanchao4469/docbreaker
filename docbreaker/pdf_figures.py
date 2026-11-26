"""Export actual diagram pixels and a complete source-page reference."""
import re
from html import escape
from pathlib import Path

from PIL import Image, ImageStat

FIGURE_NAME = re.compile(r"figure-[0-9]+-[1-3]\.jpg")
MAX_FIGURES = 24
MAX_BYTES = 600_000


def save_figures(image: Path, figures, number: int, remaining: int, text_regions=()):
    saved = []
    with Image.open(image) as page:
        for index, figure in enumerate(figures[:3], 1):
            if len(saved) >= remaining:
                break
            if not isinstance(figure, dict) or not isinstance(figure.get("steps"), list):
                continue
            coords = [figure.get(k) for k in ("left", "top", "right", "bottom")]
            if any(type(v) is not int or not 0 <= v <= 1000 for v in coords):
                continue
            x, y, r, b = coords
            if r - x < 35 or b - y < 35:
                continue
            steps = list(dict.fromkeys(
                escape(re.sub(r"[\[\]`\\*]", "", " ".join(v.split())[:400]))
                for v in figure["steps"][:8] if isinstance(v, str) and v.strip()))
            steps = [v for v in steps if v]
            if not steps:
                continue
            # Model bounds can cut off a diagram label. Expand using actual
            # PDF coordinates of labels quoted in its steps, without inventing pixels.
            description = re.sub(r"[\W_]+", "", " ".join(steps).casefold())
            for region in text_regions:
                label = re.sub(r"[\W_]+", "", region["text"].casefold())
                minimum = 2 if any(ord(c) > 127 for c in label) else 3
                if len(label) >= minimum and label in description:
                    left, top, right, bottom = region["bounds"]
                    x, y = min(x, left*1000), min(y, top*1000)
                    r, b = max(r, right*1000), max(b, bottom*1000)
            # A small margin keeps line endings and edge labels in the original crop.
            box = (max(0, x - 12) * page.width // 1000, max(0, y - 12) * page.height // 1000,
                   min(1000, r + 12) * page.width // 1000, min(1000, b + 12) * page.height // 1000)
            box = tuple(int(value) for value in box)
            name = f"figure-{number}-{index}.jpg"
            path = image.parent / name
            with page.crop(box) as crop:
                # Coordinates are model output. A white or uniform patch is not
                # useful; retain the source page when localization misses.
                with crop.convert("RGB") as rgb:
                    if max(ImageStat.Stat(rgb).stddev) < 4:
                        with page.convert("RGB") as full_page:
                            if max(ImageStat.Stat(full_page).stddev) < 4:
                                continue
                            _write_jpeg(full_page, path)
                    else:
                        _write_jpeg(rgb, path)
            if not path.exists():
                continue
            saved.append({"name": name, "page": number, "steps": steps})
        if saved:
            # Model coordinates are advisory. Keep a complete original page as
            # the final reference so clipped non-text parts remain available too.
            with page.convert("RGB") as full_page:
                reference = image.parent / saved[-1]["name"]
                _write_jpeg(full_page, reference)
                if not reference.exists():
                    raise ValueError("Could not retain the complete source-page image")
                saved[-1]["source_page"] = True
    return saved


def _write_jpeg(image, path):
    image.thumbnail((1800, 1800))
    for quality in (86, 72, 58):
        image.save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= MAX_BYTES:
            return
    path.unlink()
