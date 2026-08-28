"""New-template engine for products with no Squiggles PSD (real product photo
+ hand-marked quad + fabric mask, instead of a Photoshop smart object).

Same math as engine.render3 for the flat (non-curved) case: fit client art to
the panel's TRUE aspect via engine.safe_fit, perspective-warp it into the
photographed quad, then composite only inside the panel's real silhouette
mask (never a synthetic rounded-rect - the mask comes from the photo itself,
so real rounded corners / camera angle are exact, not approximated).

A template is a dict: {photo, quad: [(x,y)*4 TL,TR,BR,BL], mask, true_w, true_h}
`true_w`/`true_h` = the real product's width/height in any consistent unit
(metres) - only the RATIO matters, used to fit client art before warping.
"""
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import engine

NEW_TEMPLATES_DIR = Path(__file__).parent / "templates_new"


def list_new_templates():
    manifest_path = NEW_TEMPLATES_DIR / "manifest.json"
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_template(key):
    """key: one of list_new_templates()'s keys. Rebuilds the fabric mask from
    the stored photo each time (cheap) rather than caching it on disk."""
    manifest = list_new_templates()
    t = manifest[key]
    photo = Image.open(NEW_TEMPLATES_DIR / t["photo"]).convert("RGB")
    if t.get("kind") == "curved":
        return {"photo": photo, "top_x": t["top_x"], "top_y": t["top_y"],
                "fabric_height": t["fabric_height"], "left": t["left"], "right": t["right"],
                "true_w": t["true_w"], "true_h": t["true_h"]}
    mask = build_fabric_mask(photo)
    return {"photo": photo, "quad": t["quad"], "mask": mask,
            "true_w": t["true_w"], "true_h": t["true_h"]}


def make_mockup_from_new_template(key, client_art):
    manifest = list_new_templates()
    t = manifest[key]
    template = load_template(key)
    if t.get("kind") == "curved":
        return make_mockup_from_curved_photo(template, client_art)
    return make_mockup_from_photo(template, client_art)


def make_mockup_from_photo(template, client_art):
    photo = template["photo"]  # PIL.Image RGB
    quad = np.array(template["quad"], dtype=np.float32)  # TL,TR,BR,BL
    mask = template["mask"]  # numpy uint8, same size as photo, 0/255
    true_w, true_h = template["true_w"], template["true_h"]

    client_art = client_art.convert("RGBA")
    # fit client art to the panel's true aspect first (cover, or contain+blur
    # if that would crop too much) - never warp a raw, wrong-aspect image.
    PANEL_RES = 1200
    if true_w >= true_h:
        pw, ph = PANEL_RES, int(round(PANEL_RES * true_h / true_w))
    else:
        ph, pw = PANEL_RES, int(round(PANEL_RES * true_w / true_h))
    fitted = engine.safe_fit(client_art, pw, ph).convert("RGB")
    fitted_np = np.array(fitted)

    src_pts = np.array([[0, 0], [pw, 0], [pw, ph], [0, ph]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(src_pts, quad)

    W, H = photo.size
    # BORDER_REPLICATE: the hand-derived mask can overshoot the warped
    # source by a pixel or two at the edges (contour vs quad aren't
    # perfectly identical) - replicate-edge avoids a black fringe there
    # instead of the default black fill.
    warped = cv2.warpPerspective(
        fitted_np, M, (W, H), flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE)

    photo_np = np.array(photo.convert("RGB"))
    m = (mask.astype(np.float32) / 255.0)[:, :, None]
    out = (warped.astype(np.float32) * m + photo_np.astype(np.float32) * (1 - m))
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def make_mockup_from_curved_photo(template, client_art):
    """Curved SuperWall case: the fabric's top/bottom edges follow a real
    photographed wave curve (not a flat quad), but a curved SuperWall's
    frame keeps a constant fabric HEIGHT across its width - only the
    top/bottom edge POSITION shifts (verified against 3 independent real
    photos, 25/08: two independent height estimates from the same photo's
    unoccluded left/right regions agreed to ~2.5%). So the warp only needs
    to vary vertically per column; horizontally a frontal photo maps ~1:1.

    template extra keys vs the flat case: top_x/top_y (dense sampled top
    edge curve, pixel coords), fabric_height (px), left/right (px, the
    fabric's horizontal extent) - no `quad`, no contour-based `mask`
    (the counter/other objects in front of some curved photos would
    contaminate a contour-based mask; this band is built directly from
    the measured curve instead)."""
    photo = template["photo"]
    top_x = np.array(template["top_x"], dtype=np.float32)
    top_y = np.array(template["top_y"], dtype=np.float32)
    height = float(template["fabric_height"])
    left, right = float(template["left"]), float(template["right"])
    true_w, true_h = template["true_w"], template["true_h"]

    client_art = client_art.convert("RGBA")
    PANEL_RES = 1200
    pw, ph = PANEL_RES, int(round(PANEL_RES * true_h / true_w))
    fitted = engine.safe_fit(client_art, pw, ph).convert("RGB")
    fitted_np = np.array(fitted)

    W, H = photo.size
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    top_interp = np.interp(np.arange(W), top_x, top_y).astype(np.float32)
    top_map = np.broadcast_to(top_interp, (H, W))
    col_frac = np.clip((xs - left) / (right - left), 0, 1)
    row_frac = np.clip((ys - top_map) / height, 0, 1)
    src_x = col_frac * (pw - 1)
    src_y = row_frac * (ph - 1)
    warped = cv2.remap(fitted_np, src_x, src_y, interpolation=cv2.INTER_LANCZOS4,
                        borderMode=cv2.BORDER_REPLICATE)

    band = (xs >= left) & (xs <= right) & (ys >= top_map) & (ys <= top_map + height)
    mask = cv2.GaussianBlur((band.astype(np.uint8) * 255), (5, 5), 0).astype(np.float32) / 255.0

    photo_np = np.array(photo.convert("RGB")).astype(np.float32)
    out = warped.astype(np.float32) * mask[:, :, None] + photo_np * (1 - mask[:, :, None])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def build_fabric_mask(photo):
    """Derive the panel's real silhouette from a clean white-background studio
    photo: saturated-colour OR clearly-dark pixels, excluding near-white bg
    and light-grey legs/shadow. Same heuristic used to locate the reference
    quads - kept here so new templates can be built the same way."""
    img = np.array(photo.convert("RGB"))[:, :, ::-1].copy()  # RGB->BGR for cv2
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    not_white = gray < 235
    colorful = (hsv[:, :, 1] > 25) | (gray < 200)
    mask = (not_white & colorful).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(contours, key=cv2.contourArea)
    clean = np.zeros_like(mask)
    cv2.drawContours(clean, [c], -1, 255, -1)
    clean = cv2.dilate(clean, np.ones((3, 3), np.uint8))  # cover to the true edge, never leave old art peeking through
    clean = cv2.GaussianBlur(clean, (5, 5), 0)
    return clean
