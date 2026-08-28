"""Public entry point for the D2G mockup engine.

    from make_mockup import make_mockup
    img = make_mockup("blade stands/Blade Stand 900mm x 2.25m rounded corners white base.psd", client_art)
    img.save("out.jpg", quality=92)

`client_art` is a PIL.Image (any mode/size - a website screenshot, logo, or
product photo). Every customisable slot on the template (front panel, header,
plinth, side wrap - whichever the product has) gets this same image, each
shaped to its own slot: cropped to fit if it's a close aspect match, or a
centred fit-inside with a softened backdrop if it isn't (so headline text
never gets chopped off the sides on a narrow slot).

Dependencies: psd_tools, opencv-python (cv2), numpy, Pillow - `pip install
psd-tools opencv-python numpy Pillow`.

Template files: reads directly from the Squiggles OneDrive share's real
folder layout - 7 sub-folders by product range (blade stands, brandframe,
dem tables, premier pull up banner, superwall, vendor trays, wheeled
sampling cart), each holding its own .psd files, no renaming needed. Point
TEMPLATE_DIR below at that folder (on Phil's PC: Shared Documents -
Squiggles\\Photos\\editable photoshop files under his D2G OneDrive).
"""
import io
import json
import os
from pathlib import Path
from PIL import Image
import engine
import photo_template

# Local dev (Phil's PC) defaults to the OneDrive share; a deployed container
# overrides this via the TEMPLATE_DIR env var (see Dockerfile) to point at
# wherever the 45 .psd files were copied on that host instead.
TEMPLATE_DIR = Path(os.environ.get(
    "TEMPLATE_DIR",
    r"C:\Users\PhilHine\OneDrive - Displays 2 Go\Shared Documents - Squiggles\Photos\editable photoshop files"))

# Sizes with no Squiggles PSD, built from a real product photo instead (see
# photo_template.py). Kept in their own pseudo-range so the picker still
# groups by product but never confuses the two mechanisms.
NEW_SIZES_RANGE = "superwall (new sizes, no Squiggles file)"


def list_templates():
    """Product range (sub-folder name) -> list of .psd filenames in it. Build
    a two-step picker from this: range first, then specific size/colour
    variant. Template identifier used elsewhere is "range/filename.psd" for
    the Squiggles files, or "new:<key>" for a photo_template.py size."""
    out = {}
    if TEMPLATE_DIR.is_dir():
        for range_dir in sorted(p for p in TEMPLATE_DIR.iterdir() if p.is_dir()):
            variants = sorted(p.name for p in range_dir.glob("*.psd"))
            if variants:
                out[range_dir.name] = variants
    new_keys = sorted(photo_template.list_new_templates().keys())
    if new_keys:
        out[NEW_SIZES_RANGE] = ["new:" + k for k in new_keys]
    return out


RATIO_CACHE_PATH = Path(__file__).parent / "templates_new" / "ratio_cache.json"


def _label_ratio(w, h):
    """Plain-language shape description + exact ratio, for the picker UI -
    so a rep can tell up front whether their logo file is a good fit
    instead of finding out from a blurry-padded result."""
    r = w / h
    if r > 3:
        shape = "very wide banner"
    elif r > 1.7:
        shape = "wide landscape"
    elif r > 1.15:
        shape = "landscape"
    elif r >= 0.87:
        shape = "roughly square"
    elif r >= 0.6:
        shape = "portrait"
    else:
        shape = "tall narrow"
    # always width:height, in that order, never flipped for portrait shapes -
    # small whole-number ratio where there's a clean one (e.g. 4:3), else one
    # decimal place against a height of 1 (e.g. 0.83:1).
    from fractions import Fraction
    frac = Fraction(w, h).limit_denominator(24) if float(w).is_integer() and float(h).is_integer() \
        else Fraction(r).limit_denominator(24)
    if frac.numerator <= 24 and frac.denominator <= 24:
        ratio_txt = f"{frac.numerator} : {frac.denominator}"
    else:
        ratio_txt = f"{r:.2f} : 1"
    return f"Ideal sizing/ratio for pasted image: {shape} ({ratio_txt})", r


def _scan_ratio(template_key):
    """Measure a template's true printable-area shape from its own file -
    the largest editable slot's real pixel size for a Squiggles PSD, or the
    stored true_w/true_h for a photo_template.py size."""
    if template_key.startswith("new:"):
        manifest = photo_template.list_new_templates()
        t = manifest[template_key[len("new:"):]]
        return t["true_w"], t["true_h"]
    from psd_tools import PSDImage
    psd_path = TEMPLATE_DIR / template_key
    psd = PSDImage.open(str(psd_path))
    A = engine.assets(psd)
    best, best_area = None, 0
    for uuid, g in A.items():
        if not g["editable"] or g["psb"] is None:
            continue
        inner = PSDImage.open(io.BytesIO(g["psb"]))
        area = inner.width * inner.height
        if area > best_area:
            best_area, best = area, (inner.width, inner.height)
    return best


def ratio_labels():
    """{template_key: {"label": "Ideal sizing/ratio...", "ratio": w/h}} for
    every template, cached to disk after the first scan (the OneDrive PSDs
    are online-only placeholders - avoid re-touching all 45 on every app
    restart). `ratio` (a plain width/height float) is for drawing the small
    shape diagram in the UI - the label text alone can't be parsed for that."""
    if RATIO_CACHE_PATH.is_file():
        try:
            return json.loads(RATIO_CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    labels = {}
    for range_name, variants in list_templates().items():
        for v in variants:
            key = v if v.startswith("new:") else f"{range_name}/{v}"
            try:
                wh = _scan_ratio(key)
                if wh:
                    label, ratio = _label_ratio(*wh)
                    labels[key] = {"label": label, "ratio": ratio}
            except Exception:
                continue
    try:
        RATIO_CACHE_PATH.parent.mkdir(exist_ok=True)
        RATIO_CACHE_PATH.write_text(json.dumps(labels, indent=1), encoding="utf-8")
    except OSError:
        pass
    return labels


def make_mockup(template_key, client_art):
    """template_key: "range/filename.psd" - range is the sub-folder name,
    filename is one of list_templates()[range]. Or "new:<key>" for a
    photo-based template (see photo_template.py / templates_new/manifest.json).
    client_art: PIL.Image, or raw image bytes (jpg/png/whatever PIL reads).
    Returns a PIL.Image ready to .save() as jpg."""
    if isinstance(client_art, (bytes, bytearray)):
        client_art = Image.open(io.BytesIO(client_art))
    client_art = client_art.convert("RGBA")

    if template_key.startswith("new:"):
        return photo_template.make_mockup_from_new_template(template_key[len("new:"):], client_art)

    psd_path = TEMPLATE_DIR / template_key
    from psd_tools import PSDImage
    psd = PSDImage.open(str(psd_path))
    A = engine.assets(psd)

    artmap = {}
    for uuid, g in A.items():
        if not g["editable"] or g["psb"] is None:
            continue
        composed = engine.compose_into_placeholder(g["psb"], client_art)
        if composed is None:
            inner = PSDImage.open(io.BytesIO(g["psb"]))
            composed = engine.safe_fit(client_art, inner.width, inner.height)
        artmap[uuid] = composed

    return engine.render3(str(psd_path), artmap, mesh_n=64, crop=False)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        print("usage: python make_mockup.py <range/template.psd> <client_art.jpg> <out.jpg>")
        raise SystemExit(1)
    tpl, art_path, out_path = sys.argv[1:4]
    result = make_mockup(tpl, Image.open(art_path))
    result.convert("RGB").save(out_path, quality=92)
    print("wrote", out_path)
