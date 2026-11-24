"""Print-artwork checker: tells a designer (or rep) straight away whether a
file is ready to print for a given finished size, in plain English.

    from artwork_check import check_artwork
    report = check_artwork(file_bytes, filename, spec)

`spec` is a dict: finished width/height in mm, bleed and safe zone in mm,
and the minimum resolution at full size (dpi). See print_specs.json.

Checks, for PDFs: password protection; page count; finished size and shape
(including artwork supplied to scale, e.g. 10% or 50%, which large-format
printers accept); bleed; resolution of every photo at full printed size;
fonts embedded or outlined; RGB images and spot colours; text inside the
safe zone. For image files (JPG, PNG, TIFF...): shape, resolution at full
size, colour mode, and whether the pixels allow for bleed.

Libraries are all permissively licensed (pikepdf MPL-2.0, pdfplumber MIT,
pypdfium2 Apache-2.0/BSD, Pillow HPND), so this can sit on a public website
without copyleft obligations.
"""
import io
from PIL import Image, ImageDraw

PT_MM = 25.4 / 72.0
SCALES = (1.0, 0.5, 0.25, 0.2, 0.1)
PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"


def _check(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _overall(checks):
    s = {c["status"] for c in checks}
    return FAIL if FAIL in s else WARN if WARN in s else PASS


def _fmt_mm(w, h):
    return f"{w:.0f} x {h:.0f}mm"


def _match_scale(w_mm, h_mm, fw, fh):
    """Is (w, h) the finished size (fw, fh) at a standard scale? Returns
    (scale, rotated) or (None, rotated). Tolerance 1.5% or 2mm."""
    for rotated, (a, b) in ((False, (fw, fh)), (True, (fh, fw))):
        for s in SCALES:
            if abs(w_mm - a * s) <= max(2.0 * s, a * s * 0.015) and abs(h_mm - b * s) <= max(2.0 * s, b * s * 0.015):
                return s, rotated
    return None, None


def _aspect_status(w, h, fw, fh):
    ar, sr = w / h, fw / fh
    diff = abs(ar - sr) / sr
    rot = abs(ar - 1 / sr) / (1 / sr)
    return diff, rot


# ------------------------------------------------------------------ PDF ---

def _pdf_boxes(page):
    def box(name):
        b = page.obj.get(name)
        if b is None:
            return None
        x0, y0, x1, y1 = [float(v) for v in b]
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    media = box("/MediaBox")
    crop = box("/CropBox") or media
    trim = box("/TrimBox")
    bleed = box("/BleedBox")
    return media, crop, trim, bleed


def _fonts(pdf):
    """-> (embedded names, not embedded names) across every page, including
    fonts used inside form XObjects."""
    import pikepdf
    emb, missing, seen = set(), set(), set()

    def font_ok(f):
        sub = f.get("/Subtype")
        if sub == "/Type3":
            return True
        if sub == "/Type0":
            try:
                f = f["/DescendantFonts"][0]
            except Exception:
                return False
        fd = f.get("/FontDescriptor")
        return fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))

    def walk(res, depth=0):
        if res is None or depth > 6:
            return
        fonts = res.get("/Font")
        if fonts is not None:
            for _, f in fonts.items():
                try:
                    key = f.objgen if f.is_indirect else id(f)
                except Exception:
                    key = id(f)
                if key in seen:
                    continue
                seen.add(key)
                name = str(f.get("/BaseFont", "unnamed")).lstrip("/")
                name = name.split("+", 1)[-1]
                (emb if font_ok(f) else missing).add(name)
        xo = res.get("/XObject")
        if xo is not None:
            for _, x in xo.items():
                try:
                    if x.get("/Subtype") == "/Form":
                        walk(x.get("/Resources"), depth + 1)
                except Exception:
                    pass

    for p in pdf.pages:
        walk(p.obj.get("/Resources"))
    return emb, missing


def _colour_spaces(pdf):
    """-> (count of RGB images, set of spot colour names)."""
    rgb, spots, seen = 0, set(), set()

    import pikepdf

    def cs_kind(cs):
        try:
            if isinstance(cs, (str, pikepdf.Name)):
                name = str(cs)
                return "rgb" if name in ("/DeviceRGB", "/CalRGB") else "cmyk" if name == "/DeviceCMYK" else "other"
            head = str(cs[0])
            if head == "/ICCBased":
                n = int(cs[1].get("/N", 0))
                return "rgb" if n == 3 else "cmyk" if n == 4 else "other"
            if head in ("/Separation",):
                spots.add(str(cs[1]).lstrip("/"))
                return "spot"
            if head == "/DeviceN":
                for n in cs[1]:
                    nm = str(n).lstrip("/")
                    if nm not in ("Cyan", "Magenta", "Yellow", "Black", "None", "All"):
                        spots.add(nm)
                return "spot"
            if head == "/Indexed":
                return cs_kind(cs[1])
        except Exception:
            pass
        return "other"

    def walk(res, depth=0):
        nonlocal rgb
        if res is None or depth > 6:
            return
        css = res.get("/ColorSpace")
        if css is not None:
            for _, cs in css.items():
                cs_kind(cs)
        xo = res.get("/XObject")
        if xo is None:
            return
        for _, x in xo.items():
            try:
                key = x.objgen
            except Exception:
                key = id(x)
            if key in seen:
                continue
            seen.add(key)
            st = x.get("/Subtype")
            if st == "/Image":
                if cs_kind(x.get("/ColorSpace", "/DeviceGray")) == "rgb":
                    rgb += 1
            elif st == "/Form":
                walk(x.get("/Resources"), depth + 1)

    for p in pdf.pages:
        walk(p.obj.get("/Resources"))
    return rgb, spots


def _render_preview(data, trim_pt, media_pt, safe_pt, bleed_pt):
    """Page 1 rendered small, with the trim line (magenta), bleed edge (cyan,
    dashed look) and safe zone (green) drawn on."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(data)
    page = doc[0]
    pw, ph = page.get_size()
    scale = 900.0 / max(pw, ph)
    im = page.render(scale=scale).to_pil().convert("RGB")
    d = ImageDraw.Draw(im)
    # page.get_size() is the crop box; boxes are in PDF space (origin bottom-left)
    cx0, cy0 = media_pt[0], media_pt[1]
    try:
        cb = page.get_cropbox()
        cx0, cy0 = cb[0], cb[1]
    except Exception:
        pass

    def rect(b, colour, width):
        x0, y0, x1, y1 = b
        X0, X1 = (x0 - cx0) * scale, (x1 - cx0) * scale
        Y0, Y1 = (ph - (y1 - cy0)) * scale, (ph - (y0 - cy0)) * scale
        d.rectangle((X0, Y0, X1, Y1), outline=colour, width=width)

    if bleed_pt:
        rect(bleed_pt, (0, 170, 220), 2)
    if trim_pt:
        rect(trim_pt, (230, 0, 160), 3)
    if safe_pt:
        rect(safe_pt, (0, 160, 60), 2)
    out = io.BytesIO()
    im.save(out, "PNG")
    doc.close()
    return out.getvalue()


def _check_pdf(data, spec):
    import pikepdf
    import pdfplumber
    checks, facts = [], {}
    fw, fh = float(spec["width_mm"]), float(spec["height_mm"])
    bleed_req, safe_req, min_dpi = float(spec["bleed_mm"]), float(spec["safe_mm"]), float(spec["min_dpi"])

    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError:
        return [_check("Password", FAIL, "The PDF is password-protected, so it can't be printed from.",
                       "Save it again without a password or security restrictions.")], facts, None
    except Exception as e:
        return [_check("File", FAIL, f"This doesn't open as a PDF ({e.__class__.__name__}).",
                       "Export the PDF again from the design software.")], facts, None

    n = len(pdf.pages)
    facts["pages"] = n
    if n > 1:
        checks.append(_check("Pages", WARN, f"The PDF has {n} pages. Only page 1 was checked in detail.",
                             "Send one PDF per printed item, or tell us which page is which."))
    else:
        checks.append(_check("Pages", PASS, "One page."))

    page = pdf.pages[0]
    media, crop, trim, bleedbox = _pdf_boxes(page)
    page_box = crop or media
    pw_mm, ph_mm = (page_box[2] - page_box[0]) * PT_MM, (page_box[3] - page_box[1]) * PT_MM
    has_trim = trim is not None and any(abs(a - b) > 0.5 for a, b in zip(trim, page_box))

    # ---- size, scale, bleed
    scale = rotated = None
    bleed_avail_mm = None  # at FULL size
    if has_trim:
        tw, th = (trim[2] - trim[0]) * PT_MM, (trim[3] - trim[1]) * PT_MM
        scale, rotated = _match_scale(tw, th, fw, fh)
        outer = bleedbox or page_box
        sides = [trim[0] - outer[0], trim[1] - outer[1], outer[2] - trim[2], outer[3] - trim[3]]
        bleed_here = max(0.0, min(sides)) * PT_MM
        trim_mm = (tw, th)
    else:
        trim = page_box
        # no trim box: the page is either the finished size, or finished + bleed
        s0, r0 = _match_scale(pw_mm, ph_mm, fw, fh)
        s1, r1 = _match_scale(pw_mm, ph_mm, fw + 2 * bleed_req, fh + 2 * bleed_req) if bleed_req > 0 else (None, None)
        if s1 is not None and (s0 is None or bleed_req > 0):
            scale, rotated = s1, r1
            b = bleed_req * s1 / PT_MM
            trim = (page_box[0] + b, page_box[1] + b, page_box[2] - b, page_box[3] - b)
            bleed_here = bleed_req * s1
        else:
            scale, rotated = s0, r0
            bleed_here = 0.0
        trim_mm = ((trim[2] - trim[0]) * PT_MM, (trim[3] - trim[1]) * PT_MM)

    tw, th = trim_mm
    facts["page_mm"] = [round(pw_mm, 1), round(ph_mm, 1)]
    facts["trim_mm"] = [round(tw, 1), round(th, 1)]
    if scale is None:
        diff, rot = _aspect_status(tw, th, fw, fh)
        if rot < 0.01 and diff > 0.01:
            checks.append(_check("Size and shape", FAIL,
                                 f"The artwork is {_fmt_mm(tw, th)}, turned the other way to the product ({_fmt_mm(fw, fh)}).",
                                 "Rotate the artboard to match the product's orientation."))
            scale = (tw / fh)
        elif diff <= 0.01:
            scale = tw / fw
            checks.append(_check("Size and shape", WARN,
                                 f"Right shape, but supplied at an unusual scale ({scale*100:.0f}% of {_fmt_mm(fw, fh)}).",
                                 "Supply at 100%, or at 50%, 25% or 10% scale, so nothing is resized by guesswork."))
        else:
            scale = tw / fw
            checks.append(_check("Size and shape", FAIL,
                                 f"The artwork is {_fmt_mm(tw, th)}, which is a different shape from the product "
                                 f"({_fmt_mm(fw, fh)}). It would be cropped or stretched.",
                                 f"Set the artboard (trim) to exactly {_fmt_mm(fw, fh)}, or a scaled version of it."))
    else:
        if rotated:
            checks.append(_check("Size and shape", FAIL,
                                 f"The artwork is turned the other way to the product ({_fmt_mm(tw, th)} vs {_fmt_mm(fw, fh)}).",
                                 "Rotate the artboard to match the product's orientation."))
        else:
            pct = "" if scale == 1 else f" (supplied at {scale*100:.0f}% scale, which is fine)"
            checks.append(_check("Size and shape", PASS, f"Finished size {_fmt_mm(fw, fh)}{pct}."))
    facts["scale"] = round(scale, 4)

    bleed_full = bleed_here / scale if scale else 0.0
    bleed_avail_mm = bleed_full
    if bleed_req <= 0:
        checks.append(_check("Bleed", INFO, "This product doesn't need bleed."))
    elif bleed_full + 0.5 >= bleed_req:
        checks.append(_check("Bleed", PASS, f"{bleed_full:.0f}mm of bleed on every side (needs {bleed_req:.0f}mm)."))
    elif bleed_full > 0.5:
        checks.append(_check("Bleed", WARN, f"Only {bleed_full:.0f}mm of bleed (needs {bleed_req:.0f}mm on every side).",
                             f"Extend the background {bleed_req:.0f}mm past the trim on all four sides."))
    else:
        checks.append(_check("Bleed", FAIL, f"No bleed. The background needs to run {bleed_req:.0f}mm past the trim on every side,"
                                            " or a thin white edge can show after cutting.",
                             "Add bleed in the export settings and extend the background to fill it."))
    facts["bleed_mm"] = round(bleed_avail_mm, 1)

    # ---- images: resolution at full size
    low, very_low, worst = [], [], None
    with pdfplumber.open(io.BytesIO(data)) as doc:
        p1 = doc.pages[0]
        page_area = float(p1.width * p1.height)
        for im in p1.images:
            try:
                wpt, hpt = float(im["x1"] - im["x0"]), float(im["bottom"] - im["top"])
                sw, sh = im.get("srcsize") or (0, 0)
            except Exception:
                continue
            if wpt <= 0 or hpt <= 0 or not sw or not sh or (wpt * hpt) < 0.02 * page_area:
                continue
            full_w_in = (wpt * PT_MM / scale) / 25.4
            full_h_in = (hpt * PT_MM / scale) / 25.4
            dpi = min(sw / full_w_in, sh / full_h_in)
            worst = dpi if worst is None else min(worst, dpi)
            if dpi < 0.5 * min_dpi:
                very_low.append(dpi)
            elif dpi < 0.98 * min_dpi:   # 2% leeway for rounding
                low.append(dpi)
        # ---- text in the safe zone (page 1)
        safe_pt = None
        near_edge = 0
        if safe_req > 0:
            m = safe_req * scale / PT_MM
            safe_pt = (trim[0] + m, trim[1] + m, trim[2] - m, trim[3] - m)
            H = float(p1.height)
            ox, oy = float(page_box[0]), float(page_box[1])
            for ch in p1.chars:
                if not ch.get("text", "").strip():
                    continue
                x0, x1 = ch["x0"] + ox, ch["x1"] + ox
                y1, y0 = H - ch["top"] + oy, H - ch["bottom"] + oy
                if x0 < safe_pt[0] or x1 > safe_pt[2] or y0 < safe_pt[1] or y1 > safe_pt[3]:
                    near_edge += 1
    facts["lowest_dpi"] = round(worst) if worst else None
    if worst is None:
        checks.append(_check("Photo resolution", PASS, "No photos found (vector artwork prints sharp at any size)."))
    elif very_low:
        checks.append(_check("Photo resolution", FAIL,
                             f"{len(very_low)} photo(s) are far too small for this size: the lowest is {worst:.0f}dpi "
                             f"at full printed size (needs {min_dpi:.0f}dpi). They will print blurry.",
                             "Replace them with larger originals. Making a small image bigger in software doesn't add detail."))
    elif low:
        checks.append(_check("Photo resolution", WARN,
                             f"{len(low)} photo(s) are a little soft: the lowest is {worst:.0f}dpi at full size "
                             f"(aim for {min_dpi:.0f}dpi). Probably fine from a distance; close up it may look soft.",
                             "Use larger originals if you have them."))
    else:
        checks.append(_check("Photo resolution", PASS, f"Every photo is at least {worst:.0f}dpi at full printed size."))

    # ---- fonts
    emb, missing = _fonts(pdf)
    if missing:
        checks.append(_check("Fonts", FAIL, "These fonts are not embedded, so the text could print in the wrong font: "
                             + ", ".join(sorted(missing)) + ".",
                             "Embed all fonts when exporting the PDF, or convert text to outlines."))
    elif emb:
        checks.append(_check("Fonts", PASS, f"All {len(emb)} font(s) are embedded."))
    else:
        checks.append(_check("Fonts", PASS, "No live text (text is outlined or part of images)."))

    # ---- colour
    rgb, spots = _colour_spaces(pdf)
    if rgb:
        checks.append(_check("Colour", WARN, f"{rgb} image(s) are RGB (screen colours). They will be converted for print, "
                             "and bright greens, blues and oranges can come out duller.",
                             "Convert to CMYK yourself and check the colours, or ask us for a printed proof."))
    else:
        checks.append(_check("Colour", PASS, "No RGB images found."))
    if spots:
        checks.append(_check("Spot colours", INFO, "Spot colours used: " + ", ".join(sorted(spots))
                             + ". Tell us if a colour must match exactly (e.g. a brand Pantone).", ""))

    # ---- safe zone
    if safe_req > 0:
        if near_edge:
            checks.append(_check("Safe zone", WARN, f"Some text sits within {safe_req:.0f}mm of the edge, where it can be "
                                 "cut off, wrap round the frame or be hidden by hardware.",
                                 f"Keep text and logos at least {safe_req:.0f}mm inside the trim."))
        else:
            checks.append(_check("Safe zone", PASS, f"All live text is at least {safe_req:.0f}mm inside the trim."))

    if len(data) > 150 * 1024 * 1024:
        checks.append(_check("File size", INFO, f"The file is {len(data)/1e6:.0f}MB. It will be fine, but slow to send.",
                             "Our guide on making a PDF smaller shows how to shrink it without losing print quality."))

    bleed_outer = None
    if bleed_avail_mm and bleed_avail_mm > 0.5:
        b = bleed_avail_mm * scale / PT_MM
        bleed_outer = (trim[0] - b, trim[1] - b, trim[2] + b, trim[3] + b)
    try:
        preview = _render_preview(data, trim, page_box, safe_pt, bleed_outer)
    except Exception:
        preview = None
    return checks, facts, preview


# ---------------------------------------------------------------- images ---

def _check_image(data, spec):
    checks, facts = [], {}
    fw, fh = float(spec["width_mm"]), float(spec["height_mm"])
    bleed_req, min_dpi = float(spec["bleed_mm"]), float(spec["min_dpi"])
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return [_check("File", FAIL, "This file doesn't open as an image or PDF.",
                       "Send a PDF (best), or a JPG, PNG or TIFF.")], facts, None
    w, h = im.size
    facts.update(pixels=[w, h], mode=im.mode)
    diff_trim, rot_trim = _aspect_status(w, h, fw, fh)
    diff_bleed, rot_bleed = _aspect_status(w, h, fw + 2 * bleed_req, fh + 2 * bleed_req)
    with_bleed = bleed_req > 0 and diff_bleed <= 0.01 and diff_bleed < diff_trim
    total_w = fw + 2 * bleed_req if with_bleed else fw
    if min(diff_trim, diff_bleed) <= 0.01:
        checks.append(_check("Size and shape", PASS, f"Right shape for {_fmt_mm(fw, fh)}."))
    elif min(rot_trim, rot_bleed) <= 0.01:
        checks.append(_check("Size and shape", FAIL, f"The image is turned the other way to the product ({w} x {h} pixels for {_fmt_mm(fw, fh)}).",
                             "Rotate it to match the product."))
    else:
        checks.append(_check("Size and shape", FAIL if min(diff_trim, diff_bleed) > 0.05 else WARN,
                             f"The image ({w} x {h} pixels) is a different shape from the product ({_fmt_mm(fw, fh)}), so it "
                             "would be cropped or have gaps.",
                             f"Crop or extend it to the product's shape, {fw:.0f}:{fh:.0f}."))
    if bleed_req > 0:
        if with_bleed:
            checks.append(_check("Bleed", PASS, f"The pixels include {bleed_req:.0f}mm of bleed on every side."))
        else:
            checks.append(_check("Bleed", WARN, f"An image file has no separate bleed, and this one is the exact finished shape, "
                                 f"so the outer {bleed_req:.0f}mm will be trimmed off.",
                                 "Keep important content away from the edges, or send a PDF with bleed."))
    dpi = w / (total_w / 25.4)
    facts["dpi_at_full_size"] = round(dpi)
    if dpi < 0.5 * min_dpi:
        checks.append(_check("Resolution", FAIL, f"Only {dpi:.0f}dpi at full printed size (needs {min_dpi:.0f}dpi). It will print blurry.",
                             "Send a much larger original, or vector artwork (PDF, AI, EPS)."))
    elif dpi < 0.98 * min_dpi:   # 2% leeway for rounding
        checks.append(_check("Resolution", WARN, f"{dpi:.0f}dpi at full printed size (aim for {min_dpi:.0f}dpi). Fine from a distance; soft close up.",
                             "Use a larger original if you have one."))
    else:
        checks.append(_check("Resolution", PASS, f"{dpi:.0f}dpi at full printed size."))
    if im.mode in ("RGB", "RGBA", "P", "LA"):
        checks.append(_check("Colour", WARN, "The image is RGB (screen colours). It will be converted for print, and bright "
                             "greens, blues and oranges can come out duller.",
                             "Convert to CMYK yourself and check the colours, or ask us for a printed proof."))
    elif im.mode == "CMYK":
        checks.append(_check("Colour", PASS, "CMYK."))
    else:
        checks.append(_check("Colour", INFO, f"Colour mode {im.mode}."))
    checks.append(_check("Text", INFO, "Image files can't be checked for fonts or text near the edge. A PDF is better for "
                         "artwork with text."))
    prev = im.convert("RGB")
    prev.thumbnail((900, 900))
    out = io.BytesIO()
    prev.save(out, "PNG")
    return checks, facts, out.getvalue()


# ------------------------------------------------------------------ main ---

def check_artwork(data, filename, spec):
    """-> {'overall': pass|warn|fail, 'checks': [...], 'facts': {...},
    'preview_png': bytes|None}"""
    is_pdf = data[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    if is_pdf:
        checks, facts, preview = _check_pdf(data, spec)
    else:
        checks, facts, preview = _check_image(data, spec)
    facts["kind"] = "pdf" if is_pdf else "image"
    return {"overall": _overall(checks), "checks": checks, "facts": facts, "preview_png": preview}
