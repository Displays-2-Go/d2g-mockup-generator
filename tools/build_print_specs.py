"""Build print_specs.json (the artwork checker's product list) and a review
spreadsheet from specs extracted out of D2G's "Artwork specs" SharePoint
folder (JSON files, one array of records each - see the field list below).

    python tools/build_print_specs.py OUT_DIR extracted1.json extracted2.json ...

Writes OUT_DIR/print_specs.json and OUT_DIR/Artwork specs review.xlsx.

Company-wide rules come from "Artwork File Specifications - PLEASE READ.pdf":
10mm bleed on all edges unless a product template says otherwise; 150ppi at
100% recommended, 75ppi the lowest acceptable. A product's own template
always overrides those.

Record fields: product_range, product, panel, trim_w_mm, trim_h_mm,
bleed_left_mm/right/top/bottom, doc_w_mm, doc_h_mm, width_adjustable,
safe_zone_mm, safe_zone_notes, min_ppi, other_rules, source_file,
source_path, source_uri, confidence.
"""
import json
import sys
from pathlib import Path

DEFAULT_BLEED, BEST_PPI, LOWEST_PPI = 10, 150, 75


def num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def build(records):
    products, review, seen = [], [], set()
    for r in records:
        if not isinstance(r, dict) or "_summary" in r:
            continue
        name = (r.get("product") or "").strip()
        if r.get("panel"):
            name += f" ({r['panel']})"
        tw, th = num(r.get("trim_w_mm")), num(r.get("trim_h_mm"))
        adj = bool(r.get("width_adjustable"))
        sides = {k: num(r.get(f"bleed_{k}_mm")) for k in ("left", "right", "top", "bottom")}
        dw, dh = num(r.get("doc_w_mm")), num(r.get("doc_h_mm"))
        flags = []
        # fill a missing trim from document size minus bleed, when both are known
        if tw is None and dw is not None and sides["left"] is not None and sides["right"] is not None:
            tw = dw - sides["left"] - sides["right"]; flags.append("trim width worked out from document size")
        if th is None and dh is not None and sides["top"] is not None and sides["bottom"] is not None:
            th = dh - sides["top"] - sides["bottom"]; flags.append("trim height worked out from document size")
        # a missing bleed is implied by the document size when the guide gives one
        # (e.g. Big Mural: document 100mm wider than trim -> 50mm each side)
        if dw is not None and tw is not None and not adj and sides["left"] is None and sides["right"] is None:
            sides["left"] = sides["right"] = round((dw - tw) / 2, 1)
            flags.append("left/right bleed worked out from document size")
        if dh is not None and th is not None and sides["top"] is None and sides["bottom"] is None:
            sides["top"] = sides["bottom"] = round((dh - th) / 2, 1)
            flags.append("top/bottom bleed worked out from document size")
        if all(v is None for v in sides.values()):
            sides = {k: DEFAULT_BLEED for k in sides}; flags.append("no bleed in guide: company default 10mm used")
        else:
            for k, v in sides.items():
                if v is None:
                    sides[k] = DEFAULT_BLEED; flags.append(f"{k} bleed not stated: 10mm used")
        if any(v < 0 for v in sides.values()):
            flags.append("CHECK: document is smaller than the trim")
        # consistency check: trim + bleed should equal the stated document size
        if dw and tw and not adj and abs(tw + sides["left"] + sides["right"] - dw) > 1:
            flags.append(f"CHECK: trim {tw:.0f} + bleed {sides['left']+sides['right']:.0f} != document width {dw:.0f}")
        if dh and th and abs(th + sides["top"] + sides["bottom"] - dh) > 1:
            flags.append(f"CHECK: trim {th:.0f} + bleed {sides['top']+sides['bottom']:.0f} != document height {dh:.0f}")
        ppi = num(r.get("min_ppi"))
        best = ppi or BEST_PPI
        lowest = min(LOWEST_PPI, ppi) if ppi else LOWEST_PPI
        usable = th is not None and (tw is not None or adj) and (tw or 0) >= 0
        if (r.get("confidence") or "").lower() == "low":
            # D2G writes sizes width first, so an unlabelled "A x B" is read as
            # width A, height B; still worth a glance in the review sheet
            flags.append("read with less certainty (width-first rule applied) - please glance at it")
        row = {"Range": r.get("product_range"), "Product": name, "Trim width mm": tw if not adj else "to suit",
               "Trim height mm": th, "Bleed L": sides["left"], "Bleed R": sides["right"], "Bleed T": sides["top"],
               "Bleed B": sides["bottom"], "Document w x h (guide)": f"{dw or ''} x {dh or ''}".strip(" x"),
               "Safe zone mm": num(r.get("safe_zone_mm")), "Safe zone notes": r.get("safe_zone_notes"),
               "Best ppi": best, "Lowest ppi": lowest, "Other rules": r.get("other_rules"),
               "In checker": "Yes" if usable else "No - missing size", "Flags": "; ".join(flags),
               "Source file": r.get("source_file"), "Folder": r.get("source_path")}
        review.append(row)
        if not usable:
            continue
        if any(f.startswith("CHECK") for f in flags):
            # the guide contradicts itself: the checker could fail correct artwork,
            # so hold it back until someone fixes the guide or confirms the numbers
            row["In checker"] = "Held back - guide's numbers don't add up"
            continue
        key = (name.lower(), tw, th, tuple(sides.values()))
        if key in seen:
            row["In checker"] = "Duplicate (same numbers as another guide)"
            continue
        seen.add(key)
        p = {"name": name, "range": r.get("product_range"), "width_mm": 0 if adj else tw, "height_mm": th,
             "width_adjustable": adj, **{f"bleed_{k}_mm": v for k, v in sides.items()},
             "safe_mm": num(r.get("safe_zone_mm")) or 0, "min_dpi": best, "lowest_dpi": lowest,
             "source": f"{r.get('source_path')}/{r.get('source_file')}"}
        if r.get("safe_zone_notes"):
            p["notes"] = r["safe_zone_notes"]
        products.append(p)
    # same product name with different numbers -> label both so people can tell them apart
    from collections import Counter
    dup = Counter(p["name"].lower() for p in products)
    for p in products:
        if dup[p["name"].lower()] > 1:
            size = "width to suit" if p["width_adjustable"] else f"{p['width_mm']:.0f} x {p['height_mm']:.0f}mm"
            p["name"] += f" - {size}"
    products.sort(key=lambda p: ((p.get("range") or "").lower(), p["name"].lower()))
    review.sort(key=lambda r: ((r["Range"] or "").lower(), (r["Product"] or "").lower()))
    return products, review


def main():
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    records = []
    for f in sys.argv[2:]:
        records += json.loads(Path(f).read_text(encoding="utf-8"))
    products, review = build(records)
    specs = {
        "_read_me": "Artwork checker settings. Built by tools/build_print_specs.py from the 'Artwork specs' "
                    "SharePoint folder. 'defaults' come from 'Artwork File Specifications - PLEASE READ.pdf'. "
                    "Set 'confirmed' to true once D2G has checked the review spreadsheet.",
        "confirmed": False,
        "defaults": {"bleed_mm": DEFAULT_BLEED, "safe_mm": 0, "min_dpi": BEST_PPI, "lowest_dpi": LOWEST_PPI},
        "products": products}
    (out / "print_specs.json").write_text(json.dumps(specs, indent=1), encoding="utf-8")
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    wb = Workbook(); ws = wb.active; ws.title = "Products"
    cols = list(review[0].keys())
    ws.append(cols)
    for r in review:
        ws.append([r[c] for c in cols])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2F4F6F")
    widths = {"Range": 22, "Product": 42, "Safe zone notes": 40, "Other rules": 40, "Flags": 50, "Source file": 40, "Folder": 40}
    from openpyxl.utils import get_column_letter
    for i, c in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 12)
    for row in ws.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "C2"; ws.auto_filter.ref = ws.dimensions
    # every problem / unreadable file the readers reported, for fixing the guides themselves
    ws2 = wb.create_sheet("Guide problems")
    ws2.append(["Folder group", "Kind", "Detail"])
    for f in sys.argv[2:]:
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            if isinstance(r, dict) and "_summary" in r:
                sm = r["_summary"]
                grp = ", ".join(str(x) for x in (sm.get("folders_read") or [])[:4])
                for kind, key in (("Problem in a guide", "problems"), ("Template not read (.ai/.psd, or no text)", "templates_not_read")):
                    for p in sm.get(key) or []:
                        ws2.append([grp, kind, p if isinstance(p, str) else json.dumps(p)])
    ws2.column_dimensions["A"].width = 40; ws2.column_dimensions["B"].width = 30; ws2.column_dimensions["C"].width = 120
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="2F4F6F")
    for row in ws2.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws2.freeze_panes = "A2"
    wb.save(out / "Artwork specs review.xlsx")
    print(f"{len(review)} guide records -> {len(products)} products in the checker; "
          f"{sum(1 for r in review if r['In checker'].startswith('Held'))} held back (guide doesn't add up), "
          f"{sum(1 for r in review if r['In checker'].startswith('No'))} missing a size")


if __name__ == "__main__":
    main()
