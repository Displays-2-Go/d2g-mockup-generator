"""Template packs: a one-off, offline conversion of each Squiggles .psd into a
small folder of ready-to-use pieces, so the live app never opens a .psd.

Why: opening a 3.2GB set of .psd files with psd_tools at request time is what
made some products too heavy for the host. Everything render3 actually reads
from a template - the stored preview, each layer's own pixels at its own
bounding box, its effective mask (own + every ancestor group mask), its
blend settings, the smart-object placement/warp numbers, and for each
editable artwork slot the placeholder box/surround/stencil and the stock
artwork - is fixed per template. So it is worked out ONCE here, with the
exact same engine helpers the live path uses, and saved losslessly (PNG).

At request time engine.open_template() returns a PackedPSD that answers the
same questions render3 asks of a psd_tools PSDImage, reading only the small
pieces it needs. The render code itself is unchanged, and verify() proves
pack renders match .psd renders pixel for pixel.

Run on the PC that holds the .psd files (one command, then copy the output
folder to the host):

    python template_pack.py bake  "<folder of range sub-folders>"  packs
    python template_pack.py verify "<same folder>"  packs  some_test_logo.png

`bake` mirrors the range/file.psd layout as range/file.psd.pack/ folders.
`verify` renders every template both ways with the test image and reports
the largest pixel difference and the peak memory of each way.
"""
import io
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PACK_SUFFIX = ".pack"
FORMAT_VERSION = 1


# --------------------------------------------------------------- reading ---

class _Placed:
    """Stands in for psd_tools' PLACED_LAYER2 descriptor: only the fields
    engine.assets() and warp.warp_grid() read (uuid, transform, warp)."""
    def __init__(self, d):
        self.uuid = bytes.fromhex(d["uuid"]) if d.get("uuid") else None
        self.transform = d["transform"]
        w = d.get("warp")
        if w is None:
            self.warp = None
        else:
            env = {b"meshPoints": {b"Hrzn": w["H"], b"Vrtc": w["V"]}}
            if w.get("qx") is not None:
                env[b"quiltSliceX"] = {b"quiltSliceX": w["qx"]}
            if w.get("qy") is not None:
                env[b"quiltSliceY"] = {b"quiltSliceY": w["qy"]}
            self.warp = {b"customEnvelopeWarp": env}


class _Blocks:
    def __init__(self, placed):
        self._placed = placed

    def get_data(self, tag):
        from psd_tools.constants import Tag
        if tag == Tag.PLACED_LAYER2 and self._placed is not None:
            return self._placed
        return None


class PackedLayer:
    def __init__(self, pack, d):
        from psd_tools.constants import BlendMode
        self._pack = pack
        self._d = d
        self.name = d["name"]
        self.kind = d["kind"]
        self.visible = d["visible"]
        self.blend_mode = BlendMode[d["blend_mode"]]
        self.opacity = d["opacity"]
        self.fill_opacity = d["fill_opacity"]
        self.bbox = tuple(d["bbox"]) if d["bbox"] else None
        self.tagged_blocks = _Blocks(_Placed(d["placed"]) if d.get("placed") else None)

    def is_group(self):
        return False

    def patch(self):
        """(uint8 RGBA array, x0, y0) - exactly what engine.layer_patch saw at
        bake time - or None."""
        p = self._d.get("patch")
        if not p:
            return None
        arr = np.asarray(Image.open(self._pack.dir / p["file"]).convert("RGBA"))
        return arr, p["x0"], p["y0"]

    def emask(self):
        """Effective mask (own + ancestor groups) as float32 HxW, or None."""
        m = self._d.get("emask")
        if not m:
            return None
        return np.asarray(Image.open(self._pack.dir / m).convert("L")).astype(np.float32) / 255.0


class PackedPSB:
    """Stands in for an editable smart object's embedded .psb bytes."""
    def __init__(self, pack, key, d):
        self._pack = pack
        self.key = key
        self.width, self.height = d["size"]
        self._d = d

    def _img(self, name, mode):
        f = self._d.get(name)
        return Image.open(self._pack.dir / f).convert(mode) if f else None

    def placeholder_info(self):
        ph = self._d.get("placeholder")
        if ph is None:
            return None
        return {"psb_size": (self.width, self.height), "box": tuple(ph["box"]),
                "design_layer": ph["design_layer"],
                "surround": self._img("surround", "RGBA"),
                "stencil": self._img("stencil", "L")}

    def stock(self):
        return self._img("stock", "RGBA")


class PackedPSD:
    def __init__(self, pack_dir):
        self.dir = Path(pack_dir)
        meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        if meta.get("format") != FORMAT_VERSION:
            raise ValueError(f"{pack_dir}: pack format {meta.get('format')}, expected {FORMAT_VERSION} - re-run bake")
        self.width, self.height = meta["size"]
        self.packed_leaves = [PackedLayer(self, d) for d in meta["layers"]]
        self._assets = meta["assets"]
        self._psb = {k: PackedPSB(self, k, d) for k, d in meta["assets"].items()}

    def preview(self):
        return Image.open(self.dir / "preview.png").convert("RGB")

    def packed_assets(self):
        """Same shape as engine.assets(psd)."""
        out = {}
        for k, d in self._assets.items():
            out[bytes.fromhex(k)] = {
                "layers": [(i, self.packed_leaves[i]) for i in d["layers"]],
                "editable": d["editable"],
                "psb": self._psb[k] if d.get("has_psb") else None,
            }
        return out


def is_pack(path):
    return str(path).endswith(PACK_SUFFIX) and Path(path).is_dir()


# --------------------------------------------------------------- baking ----

def _warp_data(pl):
    w = pl.warp
    mesh = w.get(b"customEnvelopeWarp") if w else None
    if mesh is None:
        return None
    mp = mesh[b"meshPoints"]
    out = {"H": [float(v) for v in mp[b"Hrzn"]], "V": [float(v) for v in mp[b"Vrtc"]], "qx": None, "qy": None}
    try:
        out["qx"] = [float(v) for v in mesh[b"quiltSliceX"][b"quiltSliceX"]]
        out["qy"] = [float(v) for v in mesh[b"quiltSliceY"][b"quiltSliceY"]]
    except Exception:
        pass
    return out


def bake(psd_path, out_dir):
    """Convert one .psd into a pack folder. Uses the engine's own helpers so
    the saved pieces are exactly what the live .psd path computes."""
    import engine
    from psd_tools import PSDImage
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    psd = PSDImage.open(str(psd_path))
    W, H = psd.width, psd.height
    psd.composite().convert("RGB").save(out / "preview.png")
    L = engine.leaves(psd)
    emask = engine.eff_mask_factory(psd, W, H)
    A = engine.assets(psd)
    uuid_of = {}
    for u, g in A.items():
        for i, _ in g["layers"]:
            uuid_of[i] = u
    layers = []
    for i, l in enumerate(L):
        d = {"name": l.name, "kind": l.kind, "visible": bool(l.visible),
             "blend_mode": l.blend_mode.name, "opacity": int(l.opacity),
             "fill_opacity": int(getattr(l, "fill_opacity", 255)), "bbox": None,
             "patch": None, "emask": None, "placed": None}
        try:
            bx = l.bbox
            d["bbox"] = [int(v) for v in bx] if bx else None
        except Exception:
            pass
        r = engine.layer_patch(l, W, H)
        if r is not None:
            patch, x0, y0 = r
            f = f"L{i:03d}.png"
            Image.fromarray(np.round(patch * 255).astype(np.uint8), "RGBA").save(out / f)
            d["patch"] = {"file": f, "x0": int(x0), "y0": int(y0)}
        m = emask(l)
        if m is not None:
            f = f"M{i:03d}.png"
            Image.fromarray(np.round(m * 255).astype(np.uint8), "L").save(out / f)
            d["emask"] = f
        pl = engine.placed(l)
        if pl is not None and l.kind == "smartobject":
            u = uuid_of.get(i)
            d["placed"] = {"uuid": u.hex() if isinstance(u, bytes) else None,
                           "transform": [float(v) for v in pl.transform],
                           "warp": _warp_data(pl)}
        layers.append(d)
    assets = {}
    for u, g in A.items():
        key = u.hex()
        a = {"layers": [i for i, _ in g["layers"]], "editable": bool(g["editable"]),
             "has_psb": g["psb"] is not None, "size": [0, 0]}
        if g["psb"] is not None and g["editable"]:
            inner = PSDImage.open(io.BytesIO(g["psb"]))
            a["size"] = [inner.width, inner.height]
            info = engine.placeholder_box(g["psb"])
            if info is not None:
                ph = {"box": list(info["box"]), "design_layer": info["design_layer"]}
                if info["surround"] is not None:
                    info["surround"].save(out / f"S_{key[:16]}.png"); a["surround"] = f"S_{key[:16]}.png"
                if info["stencil"] is not None:
                    info["stencil"].save(out / f"T_{key[:16]}.png"); a["stencil"] = f"T_{key[:16]}.png"
                a["placeholder"] = ph
            try:
                inner.composite(force=True).convert("RGBA").save(out / f"O_{key[:16]}.png")
                a["stock"] = f"O_{key[:16]}.png"
            except Exception:
                pass
        assets[key] = a
    meta = {"format": FORMAT_VERSION, "source": Path(psd_path).name, "size": [W, H],
            "layers": layers, "assets": assets}
    (out / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return out


def bake_all(template_dir, packs_dir):
    template_dir, packs_dir = Path(template_dir), Path(packs_dir)
    done, failed = 0, []
    for psd in sorted(template_dir.glob("*/*.psd")):
        rel = psd.relative_to(template_dir)
        dest = packs_dir / (str(rel) + PACK_SUFFIX)
        try:
            bake(psd, dest)
            done += 1
            size = sum(f.stat().st_size for f in dest.iterdir()) / 1e6
            print(f"ok    {rel}  ({psd.stat().st_size/1e6:.0f}MB -> {size:.0f}MB)")
        except Exception as e:
            failed.append((rel, e))
            print(f"FAIL  {rel}: {e}")
    print(f"\n{done} baked, {len(failed)} failed")
    return failed


# --------------------------------------------------------------- verify ----

_CHILD = r"""
import sys, resource, numpy as np
from PIL import Image
sys.path.insert(0, sys.argv[1])
import make_mockup
img = make_mockup.render_template_path(sys.argv[2], Image.open(sys.argv[3]))
np.save(sys.argv[4], np.asarray(img.convert('RGB')))
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
"""


def _render_child(template_path, art_path, out_npy):
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(_CHILD)
    p = subprocess.run([sys.executable, f.name, str(Path(__file__).parent), str(template_path),
                        str(art_path), out_npy], capture_output=True, text=True)
    os.unlink(f.name)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip().splitlines()[-1] if p.stderr.strip() else f"exit {p.returncode}")
    kb = int(p.stdout.strip().splitlines()[-1])
    return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024


def verify(template_dir, packs_dir, art_path):
    """Render each template from the .psd and from its pack (separate
    processes, so peak memory is measured honestly) and compare."""
    import tempfile
    template_dir, packs_dir = Path(template_dir), Path(packs_dir)
    worst = 0
    with tempfile.TemporaryDirectory() as tmp:
        for psd in sorted(template_dir.glob("*/*.psd")):
            rel = psd.relative_to(template_dir)
            pack = packs_dir / (str(rel) + PACK_SUFFIX)
            if not pack.is_dir():
                print(f"MISSING pack for {rel}"); continue
            try:
                m1 = _render_child(psd, art_path, os.path.join(tmp, "a.npy"))
                m2 = _render_child(pack, art_path, os.path.join(tmp, "b.npy"))
                a = np.load(os.path.join(tmp, "a.npy")).astype(int)
                b = np.load(os.path.join(tmp, "b.npy")).astype(int)
                diff = int(np.abs(a - b).max()) if a.shape == b.shape else 999
                worst = max(worst, diff)
                print(f"{'same' if diff == 0 else 'DIFF'}  max pixel diff {diff:3d}   peak memory "
                      f"{m1:6.0f}MB (.psd) -> {m2:6.0f}MB (pack)   {rel}")
            except Exception as e:
                print(f"FAIL  {rel}: {e}")
    print("\nAll identical." if worst == 0 else f"\nLargest difference: {worst} (0-255 scale)")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "bake":
        bake_all(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 5 and sys.argv[1] == "verify":
        verify(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)
        raise SystemExit(1)
