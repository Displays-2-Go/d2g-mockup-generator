"""Standalone script that does the actual (memory-hungry, on some products)
rendering as a genuinely separate OS process - invoked via subprocess.run()
from app.py, not Python's multiprocessing (which needed to re-import this
whole app on Linux via its 'spawn' bootstrap and, for reasons not fully
pinned down, hung indefinitely there despite working fine locally - a plain
subprocess of a small standalone script sidesteps that entirely: simpler,
and the standard, well-tested way to isolate risky work in Python).

usage: python render_worker.py <template> <art_path> <out_path>
Exit 0 + writes out_path on success. Exit 1 + message on stderr on failure.
If the OS kills this process outright (out of memory), the caller sees a
non-zero/killed returncode - the app.py side treats that the same as any
other failure and reports it to the rep, without the whole web app going
down with it.
"""
import sys
from PIL import Image
import make_mockup

if __name__ == "__main__":
    template, art_path, out_path = sys.argv[1:4]
    try:
        art = Image.open(art_path)
        result = make_mockup.make_mockup(template, art)
        result.convert("RGB").save(out_path, format="JPEG", quality=92)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
