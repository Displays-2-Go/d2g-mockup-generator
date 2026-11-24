"""Runs one artwork check in its own process (called from app.py), so a huge
or broken file can only ever fail its own request, never the whole app.

usage: python check_worker.py <file> <original filename> <spec json> <out json> <out png>"""
import json
import sys

import artwork_check

if __name__ == "__main__":
    path, name, spec, out_json, out_png = sys.argv[1:6]
    try:
        with open(path, "rb") as f:
            data = f.read()
        rep = artwork_check.check_artwork(data, name, json.loads(spec))
        png = rep.pop("preview_png", None)
        if png:
            with open(out_png, "wb") as f:
                f.write(png)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(rep, f)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
