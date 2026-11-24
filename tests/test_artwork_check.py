"""Tests for artwork_check.py. Builds small PDFs/images on the fly.
Run:  python -m pytest tests -q"""
import io, os, sys
import pytest
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from artwork_check import check_artwork

pytest.importorskip("reportlab")
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

MM = 72 / 25.4
SPEC = {"width_mm": 850, "height_mm": 2000, "bleed_mm": 10, "safe_mm": 50, "min_dpi": 100}
TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def pdf(w_mm, h_mm, trim=None, img_px=None, img_box=None, text=None, font="Helvetica", cmyk_img=False):
    buf = io.BytesIO()
    kw = {}
    if font != "Helvetica":
        pdfmetrics.registerFont(TTFont("DejaVu", TTF))
        kw["initialFontName"] = "DejaVu"   # else reportlab adds an unused Helvetica
    c = canvas.Canvas(buf, pagesize=(w_mm * MM, h_mm * MM), **kw)
    if trim:
        x0, y0, x1, y1 = [v * MM for v in trim]
        c.setTrimBox((x0, y0, x1, y1))
    if img_px:
        im = Image.new("CMYK" if cmyk_img else "RGB", img_px, (0, 120, 200, 0) if cmyk_img else (0, 120, 200))
        bx = img_box or (0, 0, w_mm, h_mm)
        c.drawImage(ImageReader(im), bx[0] * MM, bx[1] * MM, (bx[2] - bx[0]) * MM, (bx[3] - bx[1]) * MM)
    if text:
        c.setFont(font, 40)
        x, y, s = text
        c.drawString(x * MM, y * MM, s)
    c.showPage(); c.save()
    return buf.getvalue()


def status(rep, name):
    return next(c["status"] for c in rep["checks"] if c["name"] == name)


def test_good_pdf_with_bleed_box():
    # 870 x 2020 page with an 850 x 2000 trim box, vector only, embedded font well inside
    d = pdf(870, 2020, trim=(10, 10, 860, 2010), text=(200, 1000, "Hello"), font="DejaVu")
    r = check_artwork(d, "a.pdf", SPEC)
    assert status(r, "Size and shape") == "pass"
    assert status(r, "Bleed") == "pass"
    assert status(r, "Fonts") == "pass"
    assert status(r, "Safe zone") == "pass"
    assert r["overall"] == "pass", r["checks"]
    assert r["preview_png"][:4] == b"\x89PNG"


def test_bleed_inferred_without_trim_box():
    r = check_artwork(pdf(870, 2020), "a.pdf", SPEC)
    assert status(r, "Size and shape") == "pass" and status(r, "Bleed") == "pass"


def test_no_bleed():
    r = check_artwork(pdf(850, 2000), "a.pdf", SPEC)
    assert status(r, "Size and shape") == "pass"
    assert status(r, "Bleed") == "fail"


def test_ten_percent_scale_is_fine():
    r = check_artwork(pdf(87, 202, trim=(1, 1, 86, 201)), "a.pdf", SPEC)
    assert status(r, "Size and shape") == "pass"
    assert r["facts"]["scale"] == 0.1
    assert status(r, "Bleed") == "pass"


def test_low_res_photo_and_rgb():
    # 850mm wide at full size = 33.5in; 1000px -> ~30dpi: fail; RGB -> warn
    r = check_artwork(pdf(870, 2020, trim=(10, 10, 860, 2010), img_px=(1000, 2353)), "a.pdf", SPEC)
    assert status(r, "Photo resolution") == "fail"
    assert 25 < r["facts"]["lowest_dpi"] < 35
    assert status(r, "Colour") == "warn"


def test_good_photo_cmyk_at_scale():
    # at 10% scale an 870px-wide image over 87mm = ~100dpi at full size
    r = check_artwork(pdf(87, 202, trim=(1, 1, 86, 201), img_px=(3500, 8100), cmyk_img=True), "a.pdf", SPEC)
    assert status(r, "Photo resolution") == "pass"
    assert status(r, "Colour") == "pass"


def test_unembedded_font_and_text_near_edge():
    r = check_artwork(pdf(870, 2020, trim=(10, 10, 860, 2010), text=(20, 1000, "EDGE")), "a.pdf", SPEC)
    assert status(r, "Fonts") == "fail"
    assert status(r, "Safe zone") == "warn"


def test_wrong_shape_and_rotated():
    assert status(check_artwork(pdf(1000, 1000), "a.pdf", SPEC), "Size and shape") == "fail"
    rot = check_artwork(pdf(2000, 850), "a.pdf", SPEC)
    assert status(rot, "Size and shape") == "fail"
    assert "turned the other way" in next(c["detail"] for c in rot["checks"] if c["name"] == "Size and shape")


def test_not_a_pdf():
    r = check_artwork(b"%PDF-1.4 garbage", "x.pdf", SPEC)
    assert r["overall"] == "fail"


def _img(size, mode="RGB"):
    b = io.BytesIO()
    Image.new(mode, size).save(b, "TIFF" if mode == "CMYK" else "PNG")
    return b.getvalue()


def test_image_files():
    good = check_artwork(_img((3425, 7953), "CMYK"), "a.tif", SPEC)   # 870x2020mm @ 100dpi
    assert status(good, "Size and shape") == "pass" and status(good, "Bleed") == "pass"
    assert status(good, "Resolution") == "pass" and status(good, "Colour") == "pass"
    small = check_artwork(_img((425, 1000)), "a.png", SPEC)
    assert status(small, "Resolution") == "fail" and status(small, "Bleed") == "warn"
    assert status(small, "Colour") == "warn"
