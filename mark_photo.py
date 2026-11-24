"""Add a new product size from a real photo, without a Photoshop file and
without hand-marking corners.

    python mark_photo.py PHOTO --key sw_straight_3x2.25 --width 3 --height 2.25 \
        --source "displays2go.com.au gallery, <client> install photo"

What it does:
  1. finds the printed panel's outline with the same fabric-mask rule the
     live renderer uses (photo_template.build_fabric_mask), then fits its four
     corners automatically (checked against the four hand-marked photos
     already in templates_new/: within 1-2px on 800px photos);
  2. saves CHECK_<key>.jpg next to the photo: the found outline and corners
     drawn on it, beside a test mockup - look at it before going live;
  3. with --save, copies the photo into templates_new/ and adds the entry to
     manifest.json, so the size appears in the app's picker.

Works for flat panels on a clean background (like the existing straight
SuperWall and Big Mural photos). Curved walls and busy scenes still need the
curved-template method or hand marking: the tool says so rather than guess.
"""
import argparse
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import photo_template

TPL = photo_template.NEW_TEMPLATES_DIR


def order_corners(p):
    """-> TL, TR, BR, BL."""
    p = np.asarray(p, np.float32)
    s = p.sum(1)
    d = np.diff(p, axis=1).ravel()
    return np.array([p[s.argmin()], p[d.argmin()], p[s.argmax()], p[d.argmax()]], np.float32)


def find_quad(mask):
    """Four corners of the largest panel outline in a 0-255 mask.
    Returns (quad, fit_error_px, note). fit_error = mean distance from the
    outline to the fitted quad's edges: big values mean the panel is not a
    clean four-sided shape (curved, occluded, or the background is busy)."""
    m = (mask > 127).astype(np.uint8)
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return None, None, "no panel found (is the background clean and light?)"
    c = max(cs, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.03 * m.size:
        return None, None, "panel found is too small: the outline rule probably missed it"
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    quad = None
    for eps in np.linspace(0.005, 0.1, 60):
        ap = cv2.approxPolyDP(hull, eps * peri, True)
        if len(ap) == 4:
            quad = order_corners(ap.reshape(4, 2))
            break
    if quad is None:
        return None, None, "could not reduce the outline to four corners"
    poly = np.zeros_like(m)
    cv2.fillConvexPoly(poly, quad.astype(np.int32), 1)
    # outline-to-quad mismatch, as an average band width in pixels
    mismatch = float(np.logical_xor(poly > 0, m > 0).sum()) / max(peri, 1.0)
    note = "ok"
    if mismatch > 6:
        note = ("outline is not a clean four-sided panel (curved, something in front of it, "
                "or a busy background) - check the preview carefully or mark it by hand")
    return quad, round(mismatch, 2), note


def test_art(w=1600, h=1000):
    """A test image with edges and a grid, so any misplacement is obvious."""
    im = Image.new("RGB", (w, h), (30, 90, 160))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 100):
        d.line((x, 0, x, h), fill=(255, 255, 255), width=2)
    for y in range(0, h, 100):
        d.line((0, y, w, y), fill=(255, 255, 255), width=2)
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 210, 0), width=24)
    d.ellipse((w // 2 - 200, h // 2 - 200, w // 2 + 200, h // 2 + 200), fill=(220, 40, 50))
    return im


def check_image(photo, quad, true_w, true_h):
    tpl = {"photo": photo, "quad": quad.tolist(), "mask": photo_template.build_fabric_mask(photo),
           "true_w": true_w, "true_h": true_h}
    mock = photo_template.make_mockup_from_photo(tpl, test_art())
    left = photo.convert("RGB").copy()
    d = ImageDraw.Draw(left)
    pts = [tuple(map(float, p)) for p in quad]
    d.line(pts + [pts[0]], fill=(255, 0, 255), width=3)
    for i, (x, y) in enumerate(pts):
        d.ellipse((x - 7, y - 7, x + 7, y + 7), outline=(255, 0, 255), width=3)
        d.text((x + 9, y + 4), "TL TR BR BL".split()[i], fill=(255, 0, 255))
    out = Image.new("RGB", (left.width * 2 + 10, left.height), (255, 255, 255))
    out.paste(left, (0, 0))
    out.paste(mock, (left.width + 10, 0))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo")
    ap.add_argument("--key", required=True, help="template name, e.g. sw_straight_3x2.25")
    ap.add_argument("--width", type=float, required=True, help="real printed width in metres")
    ap.add_argument("--height", type=float, required=True, help="real printed height in metres")
    ap.add_argument("--source", default="", help="where the photo came from (kept in the manifest)")
    ap.add_argument("--save", action="store_true", help="add it to templates_new/ and the manifest")
    a = ap.parse_args(argv)

    photo = Image.open(a.photo).convert("RGB")
    mask = photo_template.build_fabric_mask(photo)
    quad, err, note = find_quad(mask)
    if quad is None:
        print("Could not mark this photo:", note)
        return 2
    check = check_image(photo, quad, a.width, a.height)
    check_path = Path(a.photo).with_name(f"CHECK_{a.key}.jpg")
    check.save(check_path, quality=90)
    print(f"corners (TL, TR, BR, BL): {quad.round(0).astype(int).tolist()}")
    print(f"outline fit: {err}px  ({note})")
    print(f"check image: {check_path}  <- look at this before using the template")
    if not a.save:
        print("Not saved (add --save once the check image looks right).")
        return 0
    man_path = TPL / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8")) if man_path.is_file() else {}
    if a.key in man:
        print(f"'{a.key}' already exists in the manifest - pick another --key")
        return 3
    dest = TPL / (a.key + Path(a.photo).suffix.lower())
    shutil.copyfile(a.photo, dest)
    man[a.key] = {"photo": dest.name, "quad": quad.round(0).astype(int).tolist(),
                  "true_w": a.width, "true_h": a.height, "source": a.source,
                  "marked": "automatic (mark_photo.py)", "outline_fit_px": err}
    man_path.write_text(json.dumps(man, indent=1), encoding="utf-8")
    # the picker caches shape labels (rebuilding it would re-touch all 45
    # OneDrive .psd files), so add just this size's label to the cache
    cache = TPL / "ratio_cache.json"
    if cache.is_file():
        import make_mockup
        labels = json.loads(cache.read_text(encoding="utf-8"))
        label, ratio = make_mockup._label_ratio(a.width, a.height)
        labels["new:" + a.key] = {"label": label, "ratio": ratio}
        cache.write_text(json.dumps(labels, indent=1), encoding="utf-8")
    print(f"saved: {dest.name} added to templates_new/manifest.json as '{a.key}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
