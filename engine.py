"""General D2G mockup engine. Understands the template structure:
  - artwork assets are smart objects grouped by shared .psb uuid
  - an asset is user-editable if any instance is named '...CLICK...'
  - EVERY visible instance of that uuid (front panel, reflection, side panel)
    must be re-rendered, each with its own transform quad + envelope warp + mask
"""
import io, numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import BlendMode, Tag
from warp import render_art, render_art_fast, warp_grid

ADJUST = {'huesaturation','brightnesscontrast','levels','exposure','curves',
          'colorbalance','gradientmap','selectivecolor','photofilter','channelmixer'}

def leaves(node):
    out=[]
    def w(ls):
        for l in ls:
            if l.is_group(): w(l)
            else: out.append(l)
    w(node); return out

def placed(l):
    try: return l.tagged_blocks.get_data(Tag.PLACED_LAYER2)
    except Exception: return None

def assets(psd):
    """-> {uuid: {'layers':[(idx,layer)], 'editable':bool, 'psb':bytes|None}}"""
    L = leaves(psd); groups={}
    for i,l in enumerate(L):
        if l.kind!='smartobject': continue
        pl = placed(l)
        if pl is None: continue
        u = bytes(pl.uuid) if pl.uuid else f'idx{i}'.encode()
        g = groups.setdefault(u, {'layers':[], 'editable':False, 'psb':None})
        g['layers'].append((i,l))
        if 'CLICK' in l.name.upper(): g['editable']=True
        if g['psb'] is None:
            try: g['psb'] = l.smart_object.data
            except Exception: pass
    return groups

_M = {
    BlendMode.NORMAL:       lambda b,s: s,
    BlendMode.MULTIPLY:     lambda b,s: b*s,
    BlendMode.SCREEN:       lambda b,s: b+s-b*s,
    BlendMode.LINEAR_DODGE: lambda b,s: b+s,
    BlendMode.LINEAR_BURN:  lambda b,s: b+s-1.0,
    BlendMode.LINEAR_LIGHT: lambda b,s: b+2.0*s-1.0,
    BlendMode.VIVID_LIGHT:  lambda b,s: np.where(s<=0.5, 1-np.minimum(1,(1-b)/np.maximum(2*s,1e-6)), np.minimum(1,b/np.maximum(2*(1-s),1e-6))),
    BlendMode.OVERLAY:      lambda b,s: np.where(b<=0.5, 2*b*s, 1-2*(1-b)*(1-s)),
    BlendMode.HARD_LIGHT:   lambda b,s: np.where(s<=0.5, 2*b*s, 1-2*(1-b)*(1-s)),
    BlendMode.SOFT_LIGHT:   lambda b,s: np.where(s<=0.5, b-(1-2*s)*b*(1-b),
                              b+(2*s-1)*(np.where(b<=0.25,((16*b-12)*b+4)*b,np.sqrt(np.maximum(b,0)))-b)),
    BlendMode.DARKEN:       lambda b,s: np.minimum(b,s),
    BlendMode.LIGHTEN:      lambda b,s: np.maximum(b,s),
    BlendMode.COLOR_BURN:   lambda b,s: 1-np.minimum(1,(1-b)/np.maximum(s,1e-6)),
    BlendMode.COLOR_DODGE:  lambda b,s: np.minimum(1,b/np.maximum(1-s,1e-6)),
}

def blend_rgba(base, rgba, mode, opacity=255, fill=255):
    src = rgba[...,:3]
    a = rgba[...,3]*(opacity/255.0)*(fill/255.0)
    fn = _M.get(mode, _M[BlendMode.NORMAL])
    return base*(1-a[...,None]) + np.clip(fn(base,src),0,1)*a[...,None]

def layer_rgba(l, W, H):
    im = l.composite(viewport=(0,0,W,H), apply_icc=False)
    if im is None: return None
    return np.asarray(im.convert('RGBA')).astype(np.float32)/255.0

def mask_array(l, W, H):
    m = l.mask
    if m is None: return None
    mi = m.topil()
    if mi is None: return None
    full = Image.new('L',(W,H), int(getattr(m,'background_color',0)))
    full.paste(mi.convert('L'), (int(m.left), int(m.top)))
    return np.asarray(full).astype(np.float32)/255.0

def eff_mask_factory(psd, W, H):
    """mask lookup that INCLUDES ancestor group masks - the pop-up counter
    hides the model's legs with a mask on her GROUP, and flattening to leaves
    lost it (white ghost legs carved into the new counter art)."""
    anc = {}
    def _walk(node, stack):
        for ch in node:
            if ch.is_group():
                _walk(ch, stack + [ch])
            else:
                anc[id(ch)] = list(stack)
    _walk(psd, [])
    cache = {}
    def emask(l):
        k = id(l)
        if k in cache: return cache[k]
        m = mask_array(l, W, H)
        for g in anc.get(k, []):
            gm = mask_array(g, W, H)
            if gm is not None:
                m = gm if m is None else np.minimum(m, gm)
        cache[k] = m
        return m
    return emask

def cover(art, tw, th):
    aw,ah = art.size; tr=tw/max(th,1); ar=aw/max(ah,1)
    if ar>tr:
        nw=max(int(ah*tr),1); return art.crop(((aw-nw)//2,0,(aw-nw)//2+nw,ah))
    nh=max(int(aw/tr),1); return art.crop((0,0,aw,nh))

def safe_fit(art, tw, th, max_crop_frac=0.15, vis_box=None):
    """cover(), but never crops away more than max_crop_frac of the source on
    its long axis. A website screenshot's headline usually runs edge-to-edge;
    plain cover-cropping it onto a narrow tall slot (blade stand panel,
    aspect ~0.4 vs a landscape hero) chops letters off both sides (confirmed
    on Blade Stand 900x2.25m: 'NO ADDED' rendered as 'O ADDEI'). Past the
    threshold, CONTAIN-fit instead: the full source stays visible, scaled to
    fit, on a blurred cover-cropped backdrop of the same source (so the
    letterboxed margin reads as out-of-focus print, not a flat colour bar).

    `vis_box` = (vx, vy, vw, vh): the caller's placeholder box can legitimately
    overshoot the visible canvas as print bleed (straight superwall: box
    height 3000 vs a 2000px-tall canvas). Judging AND fitting against the
    inflated box let the blurred letterbox band land inside the visible
    frame ('NO ADDED' doubled/ghosted above the sharp headline). vis_box is
    the on-canvas window in box-local coordinates: the crop decision and the
    CONTAIN scale/position both use it, so the sharp foreground is centred on
    what's actually seen and any leftover letterbox band is pushed toward the
    bleed margins outside it. Backdrop still renders at the full (tw, th) so
    bleed stays continuous. None = the whole output is the visible window."""
    if vis_box is None:
        vx, vy, vw, vh = 0, 0, tw, th
    else:
        vx, vy, vw, vh = vis_box
    aw,ah = art.size; tr=vw/max(vh,1); ar=aw/max(ah,1)
    if ar > tr:
        crop_frac = 1.0 - (ah*tr)/aw
    else:
        crop_frac = 1.0 - (aw/tr)/ah
    if crop_frac <= max_crop_frac:
        return cover(art, tw, th).resize((tw, th), Image.LANCZOS)
    # Backdrop must read as an unfocused colour wash, not a second, dimmer
    # copy of the source: a moderate Gaussian blur (sigma ~60px on a 3000px
    # panel) still left bold white headline text legible as a ghost above the
    # sharp copy (straight superwall). Downsample-then-upsample instead - at
    # ~1/60th resolution no letterform survives, whatever the blur radius.
    covered = cover(art, tw, th).resize((tw, th), Image.LANCZOS).convert('RGBA')
    bg_small = covered.resize((max(3, tw//200), max(3, th//200)), Image.LANCZOS)
    canvas = bg_small.resize((tw, th), Image.LANCZOS)
    scale = min(vw/aw, vh/ah)
    nw = max(int(round(aw*scale)), 1); nh = max(int(round(ah*scale)), 1)
    fg = art.resize((nw, nh), Image.LANCZOS)
    canvas.paste(fg, (vx + (vw-nw)//2, vy + (vh-nh)//2), fg.convert('RGBA'))
    return canvas

def render(psd_path, art, out_path=None, mesh_n=200, crop=True, only_uuid=None):
    """art: PIL image, or dict {uuid: PIL image}, or None (= use each asset's own psb)."""
    psd = PSDImage.open(psd_path)
    W,H = psd.width, psd.height
    L = leaves(psd)
    A = assets(psd)
    targets=[]
    for u,g in A.items():
        if not g['editable']: continue
        if only_uuid and u!=only_uuid: continue
        for i,l in g['layers']:
            if l.visible: targets.append((i,l,u))
    if not targets: raise ValueError('no editable artwork slots')
    targets.sort()
    top = max(i for i,_,_ in targets)

    saved = {id(l): l.visible for l in L}
    for i,l,_ in targets: l.visible=False
    for i,l in enumerate(L):
        if i>top: l.visible=False
    base = np.asarray(psd.composite(force=True).convert('RGB')).astype(np.float32)/255.0
    for l in L: l.visible = saved[id(l)]

    art_cache={}
    for i,l,u in targets:
        if isinstance(art, dict): src = art.get(u)
        elif art is not None:     src = art
        else:
            if u not in art_cache:
                inner = PSDImage.open(io.BytesIO(A[u]['psb']))
                art_cache[u] = inner.composite(force=True).convert('RGBA')
            src = art_cache[u]
        if src is None: continue
        src = src.convert('RGBA')
        if crop:
            DX,DY = warp_grid(l, 24)
            pw=max(int(np.hypot(DX[0,-1]-DX[0,0],DY[0,-1]-DY[0,0])),1)
            ph=max(int(np.hypot(DX[-1,0]-DX[0,0],DY[-1,0]-DY[0,0])),1)
            src = cover(src, pw, ph)
        rgba = np.asarray(render_art(l, src, (W,H), n=mesh_n)).astype(np.float32)/255.0
        mk = mask_array(l, W, H)
        if mk is not None: rgba[...,3]*=mk
        base = blend_rgba(base, rgba, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))

    for i,l in enumerate(L):
        if i<=top or not saved[id(l)]: continue
        if l.kind in ADJUST:  # adjustment above artwork - approximate via full-canvas render
            continue
        rgba = layer_rgba(l, W, H)
        if rgba is None: continue
        base = blend_rgba(base, rgba, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))

    img = Image.fromarray((np.clip(base,0,1)*255).astype(np.uint8))
    if out_path: img.save(out_path, quality=95, subsampling=0)
    return img

def reference(psd_path):
    return PSDImage.open(psd_path).composite().convert('RGB')


# ---------------------------------------------------------------------------
# Own compositor. psd_tools' stack renderer silently drops layers on several of
# these templates (blade stands lose the whole background photo), and touching
# any layer's .visible switches psd.composite() from returning the file's stored
# preview to using that broken renderer. Individual layer.composite() calls are
# reliable, so we stack them ourselves. Valid because all 197 groups in the set
# are PASS_THROUGH.
# ---------------------------------------------------------------------------

def stored_preview(psd_path):
    """The flattened image Photoshop saved in the file - true ground truth."""
    return PSDImage.open(psd_path).composite().convert('RGB')

def flat_composite(psd, subs=None, skip=(), W=None, H=None):
    """subs: {id(layer): RGBA float array} to substitute for that layer."""
    W = W or psd.width; H = H or psd.height
    L = leaves(psd)
    base = np.ones((H,W,3), np.float32)
    for l in L:
        if not l.visible or id(l) in skip: continue
        if subs and id(l) in subs:
            rgba = subs[id(l)]
        elif l.kind in ADJUST:
            continue                      # handled separately; rare (14 in the set)
        else:
            rgba = layer_rgba(l, W, H)
            if rgba is None: continue
        base = blend_rgba(base, rgba, l.blend_mode, l.opacity,
                          getattr(l,'fill_opacity',255))
    return base


def render2(psd_path, art, out_path=None, mesh_n=160, crop=True):
    """Render a mockup using our own compositor.
    art: PIL image | {uuid: PIL image} | None (None = each asset's own embedded art,
    which is how we self-validate against the file's stored preview)."""
    psd = PSDImage.open(psd_path)
    W,H = psd.width, psd.height
    A = assets(psd)
    subs={}; cache={}
    for u,g in A.items():
        if not g['editable']: continue
        for _,l in g['layers']:
            if not l.visible: continue
            if isinstance(art, dict): src = art.get(u)
            elif art is not None:     src = art
            else:
                if u not in cache:
                    try:
                        cache[u] = PSDImage.open(io.BytesIO(g['psb'])).composite(force=True).convert('RGBA')
                    except Exception:
                        cache[u] = None
                src = cache[u]
            if src is None: continue
            src = src.convert('RGBA')
            if crop:
                DX,DY = warp_grid(l, 24)
                pw=max(int(np.hypot(DX[0,-1]-DX[0,0],DY[0,-1]-DY[0,0])),1)
                ph=max(int(np.hypot(DX[-1,0]-DX[0,0],DY[-1,0]-DY[0,0])),1)
                src = cover(src, pw, ph)
            rgba = np.asarray(render_art_fast(l, src, (W,H), n=mesh_n)).astype(np.float32)/255.0
            mk = mask_array(l, W, H)
            if mk is not None: rgba[...,3]*=mk
            subs[id(l)] = rgba
    base = flat_composite_fast(psd, subs=subs, W=W, H=H)
    img = Image.fromarray((np.clip(base,0,1)*255).astype(np.uint8))
    if out_path: img.save(out_path, quality=95, subsampling=0)
    return img


# --- fast path: composite each layer at its own bbox, not the full canvas ----

def layer_patch(l, W, H):
    """-> (rgba float array, x0, y0) rendered at the layer's own bbox."""
    try:
        bx = l.bbox
    except Exception:
        bx = None
    if not bx or bx[2]<=bx[0] or bx[3]<=bx[1]:
        return None
    x0,y0,x1,y1 = [int(v) for v in bx]
    cx0,cy0 = max(x0,0), max(y0,0)
    cx1,cy1 = min(x1,W), min(y1,H)
    if cx1<=cx0 or cy1<=cy0: return None
    im = l.composite(viewport=(cx0,cy0,cx1,cy1), apply_icc=False)
    if im is None: return None
    return np.asarray(im.convert('RGBA')).astype(np.float32)/255.0, cx0, cy0

def blend_patch(base, patch, x0, y0, mode, opacity=255, fill=255):
    h,w = patch.shape[:2]
    sub = base[y0:y0+h, x0:x0+w]
    if sub.shape[:2] != (h,w):
        h,w = sub.shape[:2]; patch = patch[:h,:w]
    src = patch[...,:3]
    a = patch[...,3]*(opacity/255.0)*(fill/255.0)
    fn = _M.get(mode, _M[BlendMode.NORMAL])
    base[y0:y0+h, x0:x0+w] = sub*(1-a[...,None]) + np.clip(fn(sub,src),0,1)*a[...,None]
    return base

def flat_composite_fast(psd, subs=None, W=None, H=None):
    W = W or psd.width; H = H or psd.height
    base = np.ones((H,W,3), np.float32)
    for l in leaves(psd):
        if not l.visible: continue
        if subs and id(l) in subs:
            rgba = subs[id(l)]
            base = blend_patch(base, rgba, 0, 0, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))
            continue
        if l.kind in ADJUST: continue
        r = layer_patch(l, W, H)
        if r is None: continue
        patch,x0,y0 = r
        base = blend_patch(base, patch, x0, y0, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))
    return base


def _extend_fill(rgba, target, max_frac=0.35, max_depth=None):
    """Extend artwork to cover `target` alpha (0..1): fill uncovered target
    pixels with the nearest covered pixel's colour and raise alpha. Guarded -
    if the gap is a large share of the footprint the target is untrustworthy
    (e.g. a substrate layer whose broken mask covers half the scene): skip.
    `max_depth`: an alternative pass condition for a target we already trust
    completely (the layer's OWN stored Photoshop raster) - a THIN rim gap
    (nowhere more than max_depth px from covered artwork) is safe to fill
    even when its total area exceeds max_frac (pop-up counter: our analytic
    warp undershoots Photoshop's on a curved return face by a locally-solid
    but globally-small-fraction band, so the ratio gate never fired and the
    template's OLD purple design showed through); a large DISCONNECTED gap
    (cart: skirt over the wheels) has pixels far from any covered edge and
    still correctly fails this test."""
    import cv2
    srcm = rgba[...,3] >= 0.5
    gap = (target > 0.5) & ~srcm
    if not gap.any() or not srcm.any(): return False
    inv = (~srcm).astype(np.uint8)
    dist, lbl = cv2.distanceTransformWithLabels(inv, cv2.DIST_L2, 5,
                    labelType=cv2.DIST_LABEL_PIXEL)
    ok_ratio = gap.sum()/max(srcm.sum(),1) < max_frac
    ok_depth = max_depth is not None and float(dist[gap].max()) <= max_depth
    if not (ok_ratio or ok_depth): return False
    zpos = np.argwhere(inv==0)
    zlab = lbl[inv==0]
    lut = np.zeros((int(zlab.max())+1, 2), np.int32)
    lut[zlab] = zpos
    gy, gx = np.nonzero(gap)
    near = lut[lbl[gy, gx]]
    rgba[gy, gx, :3] = rgba[near[:,0], near[:,1], :3]
    rgba[...,3] = np.maximum(rgba[...,3], target)
    return True


def render3(psd_path, art, out_path=None, mesh_n=64, crop=True):
    """Use the file's own stored preview as the base - it is a correct full render
    by Photoshop itself - and only replace the artwork region. Sidesteps every
    background/gradient/canvas layer that psd_tools renders wrongly, because we
    never re-render them: they are already correct in the preview."""
    psd = PSDImage.open(psd_path)
    W,H = psd.width, psd.height
    base = np.asarray(stored_preview(psd_path).convert('RGB')).astype(np.float32)/255.0
    if base.shape[0]!=H or base.shape[1]!=W:
        base = np.asarray(Image.fromarray((base*255).astype(np.uint8)).resize((W,H))).astype(np.float32)/255.0

    L = leaves(psd); A = assets(psd)
    emask = eff_mask_factory(psd, W, H)
    targets=[]
    for u,g in A.items():
        if not g['editable']: continue
        for i,l in g['layers']:
            if l.visible: targets.append((i,l,u))
    if not targets: raise ValueError('no editable artwork slots')
    targets.sort()
    top = max(i for i,_,_ in targets)

    def canvas_alpha(idx, pl_alpha):
        """Alpha of the print SUBSTRATE under a panel: the first visible opaque
        NORMAL pixel/smart-object layer below it. The template's fabric shape
        (wavy hems, rounded corners) lives THERE, not in the artwork layer -
        the artwork warp deliberately overhangs it, invisible only because the
        stock design is white in the overhang. Clip our paste to it."""
        for j in range(idx-1, -1, -1):
            lj = L[j]
            if not lj.visible or lj.kind not in ('pixel','smartobject'): continue
            if lj.blend_mode != BlendMode.NORMAL or lj.opacity != 255: continue
            if getattr(lj,'fill_opacity',255) != 255: continue
            r = layer_patch(lj, W, H)
            if r is None: continue
            patch,x0,y0 = r
            a = np.zeros((H,W), np.float32)
            a[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch[...,3]
            mkj = emask(lj)   # psd_tools drops layer masks in patch
            if mkj is not None: a *= mkj # renders - apply explicitly (rounded
            # must actually sit under the panel, not be a distant background
            inter = (a*pl_alpha).sum() / max(pl_alpha.sum(), 1.0)
            if inter > 0.5:
                return a
        return None

    def form_sil(idx, art_alpha):
        """Product SILHOUETTE from a baked FORM layer: an opaque OVERLAY pixel
        layer above the panel (the 3D render's own lighting of the product).
        Verified against the stored preview: on the curved superwall its full
        alpha outline hugs the visible fabric everywhere - rounded top corners,
        sloped top roll, sides AND hem - within ~3px, while the artwork raster
        overruns all of those edges (hidden in stock designs only because the
        design is pale). Returns a float HxW alpha (keeps the bake's own
        antialiased edge - a binary mask staircased the sloped top), or None."""
        sil = None
        for j in range(idx+1, len(L)):
            lj = L[j]
            if not lj.visible or lj.kind != 'pixel': continue
            if lj.blend_mode != BlendMode.OVERLAY: continue
            if lj.opacity < 200 or getattr(lj,'fill_opacity',255) < 200: continue
            r = layer_patch(lj, W, H)
            if r is None: continue
            patch,x0,y0 = r
            a = np.zeros((H,W), np.float32)
            a[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch[...,3]
            mkj = emask(lj)
            if mkj is not None: a *= mkj
            m = a > 0.25
            if not (m & (art_alpha >= 0.5)).any(): continue
            # FORM bakes (a 3D render's own lighting) have a knife-hard bottom
            # edge - measured 1-2px across every template - while light GLOWS
            # ('Floor lights', 'Background darkening') trail off over 25-450px.
            # Only a hard edge traces the product silhouette; glows never do.
            m1 = a > 0.10; m2 = a > 0.75
            c = m1.any(axis=0) & m2.any(axis=0)
            if c.sum() < 50: continue
            b1 = (H-1) - np.argmax(m1[::-1,:], axis=0)
            b2 = (H-1) - np.argmax(m2[::-1,:], axis=0)
            if np.median((b1-b2)[c]) > 5: continue
            # A LONG straight contact run down the layer's own raster bound
            # means the bake stops at its crop there (4x3: 715 rows dead
            # straight at the patch edge while the fabric curves on 19px
            # further) - or the fabric edge genuinely sits on the bound (3x2
            # left). Either way the artwork's own warped edge is the truth just
            # past it: carry the border column's profile outward 60px and let
            # the art's edge stand. Rows past the profile (below the foot
            # roundover) stay clipped - that kept the 3x2's hanging-tail fix.
            for left in (True, False):
                bx = x0 if left else x0 + patch.shape[1] - 1
                if not (0 <= bx < W): continue
                bc = m[:, bx]
                # cropped bake: long straight contact AND full-strength alpha
                # right at the bound (a natural edge is antialiased there -
                # measured 0.99 on cropped borders vs 0.38-0.64 on real edges)
                if bc.sum() > 300 and float(a[bc, bx].mean()) > 0.9:
                    prof = a[:, bx:bx+1]
                    sl = slice(max(bx-60, 0), bx) if left else slice(bx+1, min(bx+61, W))
                    n = sl.stop - sl.start
                    # taper the carried profile so the print FADES across the
                    # band: the lighting bake ends at the crop, so a full-
                    # strength band renders as a flat unlit tab - fading lets
                    # the preview's own baked rolled-edge shading show through
                    t = (1.0 - np.arange(1, n+1, dtype=np.float32)/28.0).clip(0, 1)
                    if left: t = t[::-1]
                    a[:, sl] = np.maximum(a[:, sl], prof * t[None, :])
            sil = a if sil is None else np.maximum(sil, a)
        return sil

    def edge_ring(idx, art_alpha):
        """Union of thin hard EDGE BAKES ('Edge hilite'/'Edge shade' strips
        that hug the fabric edge on blade stands). Thin = small area relative
        to bounding box. Returns bool mask or None."""
        ring = None
        ab = art_alpha >= 0.5
        for j, lj in enumerate(L):
            if not lj.visible or lj.kind != 'pixel': continue
            if lj.opacity < 75: continue
            r = layer_patch(lj, W, H)
            if r is None: continue
            patch, x0, y0 = r
            ph_, pw_ = patch.shape[:2]
            a = patch[..., 3]
            m = a > 0.15
            area = int(m.sum())
            if area < 500 or area > 0.12 * ph_ * pw_: continue   # not a thin strip
            full = np.zeros((H, W), bool)
            full[y0:y0+ph_, x0:x0+pw_] = m
            mkj = emask(lj)
            if mkj is not None: full &= (mkj > 0.5)
            # must hug the artwork: nearly all strip pixels on/inside it
            if not full.any(): continue
            inside = float((full & ab).sum()) / float(full.sum())
            if inside < 0.85: continue
            # ...and live in a narrow band along its BOUNDARY - a sparse
            # full-panel texture bake also passes the thin-strip test
            # (brandframe weave: huge bbox, low fill) but spreads through the
            # interior, and it wrongly rounded an approved square frame
            import cv2
            band = ab & ~(cv2.erode(ab.astype(np.uint8), np.ones((51,51), np.uint8)) > 0)
            if float((full & band).sum()) / float(full.sum()) < 0.7: continue
            ring = full if ring is None else (ring | full)
        return ring

    # Occluders: opaque NORMAL layers ABOVE the artwork (people, hardware, caps).
    # Mask the artwork out from under them BEFORE pasting, so their preview
    # pixels are never touched - repainting them afterwards loses adjustment-
    # layer grading (seam through the model), and the preview is already right.
    # Collected PER LAYER (with RGB) so occlusion can be evaluated relative to
    # each instance's own z-index: the 6x3 booth's chair sits above the
    # backdrop but below the topmost artwork instance, and a global above-top
    # mask missed it (chair got painted over).
    tset = set(ti for ti,_,_ in targets)
    occ_layers = []   # (idx, alpha HxW, rgb HxW3)
    for i,l in enumerate(L):
        if i in tset or not l.visible or l.kind in ADJUST: continue
        if l.kind not in ('pixel','smartobject'): continue
        if l.blend_mode != BlendMode.NORMAL or l.opacity != 255: continue
        if getattr(l,'fill_opacity',255) != 255: continue
        r = layer_patch(l, W, H)
        if r is None: continue
        patch,x0,y0 = r
        a = np.zeros((H,W), np.float32)
        a[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch[...,3]
        rgb = np.zeros((H,W,3), np.float32)
        rgb[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch[...,:3]
        mk = emask(l)
        if mk is not None: a *= mk
        occ_layers.append((i, a, rgb))
    occl = np.zeros((H,W), np.float32)
    for j,a,_ in occ_layers:
        if j > top: occl = np.maximum(occl, a)

    coverage = np.zeros((H,W), np.float32)   # panels: lighting re-pass runs here
    _old_cache = {}
    _bake_cut = {}   # layer idx -> mask of PRINT-BAKE parts to drop everywhere
    _refl_top = {}   # per-uuid: per-column first row of the floor reflection

    def old_warp(u, l, mk, pw, ph):
        """The template's own stock art warped through this instance."""
        if u not in _old_cache:
            try:
                _old_cache[u] = PSDImage.open(io.BytesIO(A[u]['psb'])).composite(force=True).convert('RGBA')
            except Exception:
                _old_cache[u] = None
        old_src = _old_cache.get(u)
        if old_src is None: return None
        osrc = cover(old_src.convert('RGBA'), pw, ph) if crop else old_src.convert('RGBA')
        orgba = np.asarray(render_art_fast(l, osrc, (W,H), n=mesh_n)).astype(np.float32)/255.0
        if mk is not None: orgba[...,3] *= mk
        return orgba
    for i,l,u in targets:
        src = art.get(u) if isinstance(art, dict) else art
        if src is None: continue
        src = src.convert('RGBA')
        DX,DY = warp_grid(l, 24)
        pw=max(int(np.hypot(DX[0,-1]-DX[0,0],DY[0,-1]-DY[0,0])),1)
        ph=max(int(np.hypot(DX[-1,0]-DX[0,0],DY[-1,0]-DY[0,0])),1)
        if crop:
            src = cover(src, pw, ph)
        rgba = np.asarray(render_art_fast(l, src, (W,H), n=mesh_n)).astype(np.float32)/255.0
        mk = emask(l)
        if mk is not None: rgba[...,3]*=mk
        # Photoshop's TRUE footprint for this instance is the layer's stored
        # raster (its baked render of the old art). Our analytic warp matches it
        # to within a fringe, but stops short at bulged corners/rolls (curved
        # superwall pillow corners were left bare). Extend our artwork's edge
        # pixels into that fringe so the silhouette matches the original.
        rp = layer_patch(l, W, H)
        if rp is not None:
            patch,x0,y0 = rp
            ta = np.zeros((H,W), np.float32)
            ta[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch[...,3]
            if mk is not None: ta *= mk
            # fringe-only: the stored raster is trusted for bulged EDGES, but a
            # large gap means it covers regions our warp genuinely excludes
            # (cart: white skirt over the wheels - extending smeared them)
            _extend_fill(rgba, ta, max_frac=0.08, max_depth=70)
        # Occlusion. Secondaries keep the global above-top mask (plus the
        # hardware mask in their branch). PANELS use their OWN z-index - the
        # booth chair sits above the backdrop but below the topmost instance -
        # minus PRINT-BAKE components: canvas layers sometimes duplicate part
        # of the stock print (pop-up counter supermarket bakes the design's
        # model photo into a layer above the artwork). Matching the warped OLD
        # art identifies them; real foreground objects (the chair) don't match.
        _solidity = l.opacity * getattr(l,'fill_opacity',255) / 255.0
        _orgba = None
        occ = occl
        if _solidity >= 200:
            import cv2
            occ = np.zeros((H,W), np.float32)
            for _j,_a2,_ in occ_layers:
                if _j > i: occ = np.maximum(occ, _a2)
            _orgba = old_warp(u, l, mk, pw, ph)
            if _orgba is not None:
                oa = _orgba[...,3] > 0.5
                orgb = _orgba[...,:3]
                for _j,_a2,_rgb2 in occ_layers:
                    if _j <= i: continue
                    if not ((_a2 > 0.5) & oa).any(): continue
                    ncomp, lab = cv2.connectedComponents((_a2 > 0.5).astype(np.uint8))
                    for c in range(1, ncomp):
                        comp = lab == c
                        n = int(comp.sum())
                        if n < 400: continue
                        if float((comp & oa).sum())/n < 0.85: continue
                        sel = comp & oa
                        if float(np.abs(_rgb2[sel] - orgb[sel]).mean()) < 0.10:
                            occ[comp] = 0.0
                            _bake_cut.setdefault(_j, np.zeros((H,W), bool))[comp] = True
        rgba[...,3] *= (1.0-occ)   # keep people/hardware in front untouched
        # PANEL vs SECONDARY. Across the whole template set, printed panels are
        # near-solid (opacity >= 230: NORMAL 255, MULTIPLY 255/fill 230, LINEAR_BURN
        # 230-240) while reflections/shadows are faint (opacity <= 128). Blending a
        # near-solid panel over the preview lets the OLD artwork ghost through, so
        # panels are simulated against a WHITE print substrate and pasted opaquely.
        # Secondary instances keep their true blend over the preview; the old
        # artwork's reflection is already baked in so the two stack slightly -
        # visually minor. (An exact old->new reflection swap was tried and REJECTED:
        # sub-pixel warp misalignment leaves the old art ghosting on the floor.)
        # `coverage` (where the lighting re-pass runs) grows only for panels:
        # preview pixels under secondaries are already lit.
        solidity = l.opacity * getattr(l,'fill_opacity',255) / 255.0
        if solidity >= 200:
            ca = canvas_alpha(i, rgba[...,3])
            import os as _os
            _dbg = _os.environ.get('DBG_CLIP')
            if _dbg: print('DBG_CLIP', l.name, 'ca_found=', ca is not None)
            if ca is not None:
                # The substrate layer is the print's real shape (fabric pillow
                # incl. rolled top and rounded corners). EXTEND the artwork to
                # fill it - the stock designs leave those zones white, but a
                # real print covers the whole fabric (Phil's 4x3 reference) -
                # then CLIP to it (wavy hems, hardware notches). Both guarded:
                # extension skips if the gap share is implausibly large, and
                # clipping skips if it would remove a large share (the artwork
                # legitimately extends past that layer, e.g. pop-up wall).
                rgba = rgba.copy()
                # Decide the hem/notch TRIM on the analytic footprint BEFORE
                # extending - extension inflates the footprint (roll, corners)
                # and pushed `removed` past the gate, silently disabling the
                # trim (glass ran straight through the curved hem, Phil round 3).
                removed = 1.0 - float((rgba[...,3]*ca).sum())/max(float(rgba[...,3].sum()),1.0)
                if _dbg: print('DBG_CLIP', l.name, 'removed=%.4f'%removed, 'clip=', removed<0.15)
                # the instance's own layer mask is a HARD print boundary (cart:
                # mask stops the band above the wheels) - never extend past it
                ca_t = ca*mk if mk is not None else ca
                # ...and never extend BELOW the artwork's own bottom silhouette:
                # the substrate raster bakes in its own floor reflection as solid
                # pixels (below-hem zone reads as 'fabric'), and filling down
                # there runs the print straight through the curved hem. The warp's
                # bottom edge IS the hem - extension may only go up/sideways.
                srcm0 = rgba[...,3] >= 0.5
                if srcm0.any():
                    colhit = srcm0.any(axis=0)
                    bot = np.full(W, -1.0)
                    bot[colhit] = (H-1) - np.argmax(srcm0[::-1,:], axis=0)[colhit]
                    xs = np.arange(W)
                    bot = np.interp(xs, xs[colhit], bot[colhit])
                    below = np.arange(H)[:,None] > (bot[None,:] + 10)
                    ca_t = ca_t * (~below)
                extended = _extend_fill(rgba, ca_t)
                if removed < 0.15:
                    rgba[...,3] *= np.maximum(ca, 0)
                if extended:
                    rgba[...,3] *= (1.0-occ)    # extension must not re-cover occluders
            # Clip the print to the product's TRUE silhouette where a baked form
            # layer traces it (curved superwall: the artwork raster overruns the
            # visible fabric on EVERY edge - square corners over the rounded
            # top, 20-130px past the hem - and nothing else in the file cuts
            # it). A bottom-only hem line was tried first and rejected: the top
            # roll and corners stayed square. Gates, all load-bearing:
            #  - the floor reflection must confirm a >=30px overhang below the
            #    silhouette's bottom in some column (pop-up counter: a hard
            #    lighting edge sits mid-panel above a legitimate visible edge -
            #    clipping there combed the wall);
            #  - the silhouette must cover >=60% of the artwork (else it is a
            #    partial shading patch, not the product);
            #  - the clip may remove at most 40% of the artwork's alpha.
            _sil_used = False
            Fs = form_sil(i, rgba[...,3])
            if Fs is not None:
                import cv2
                sil = Fs > 0.25
                am = rgba[...,3] >= 0.5
                if am.any():
                    colhit = am.any(axis=0)
                    bots = np.where(colhit, (H-1) - np.argmax(am[::-1,:], axis=0), -1).astype(np.float32)
                    scol = sil.any(axis=0)
                    sbot = np.where(scol, (H-1) - np.argmax(sil[::-1,:], axis=0), -1).astype(np.float32)
                    rt0 = _refl_top.get(u)
                    inter = float((sil & am).sum()) / max(float(am.sum()), 1.0)
                    if rt0 is not None and inter >= 0.6:
                        rtf = rt0.astype(np.float32)
                        over = bots - sbot
                        core = colhit & scol & (rtf < H) & (bots - rtf >= 30) & (over > 2) & (over <= 200)
                        if core.any():
                            # 2px-generous GRAYSCALE dilation keeps the bake's
                            # antialiased edge profile (binary dilate + blur
                            # staircased the sloped top edge at zoom)
                            fade = np.clip(cv2.dilate(Fs, np.ones((5,5), np.uint8)), 0, 1)
                            # fill interior holes (the bake can be empty where
                            # no lighting lands mid-panel)
                            m8 = sil.astype(np.uint8)
                            ff = m8.copy()
                            ffm = np.zeros((H+2, W+2), np.uint8)
                            cv2.floodFill(ff, ffm, (0,0), 1)
                            fade[(ff == 0) & ~sil] = 1.0
                            cut = 1.0 - float((rgba[...,3]*fade).sum())/max(float(rgba[...,3].sum()),1.0)
                            if cut < 0.4:
                                rgba[...,3] *= fade
                                _sil_used = True
            # ---- blade-stand family: no form bake exists, and nothing in the
            # file clips the print (the Canvas substrate is an opaque full
            # plate). Two template truths substitute:
            if not _sil_used:
                import cv2
                # (a) ROUNDED CORNERS live in thin hard 'Edge hilite/shade'
                # bakes hugging the fabric edge. Fit the corner radius by
                # morphological OPENING: the largest disk whose opened footprint
                # still contains (>=99.5%) every ring pixel. Opening preserves
                # every straight edge and concave curve exactly - it only
                # rounds convex corners - and the fit must be CONSTRAINED
                # (containment must fail at the next radius up) so a ring that
                # never limits R can never trigger a 60px carve-up.
                ring = edge_ring(i, rgba[...,3])
                if ring is not None:
                    fp = (rgba[...,3] >= 0.5).astype(np.uint8)
                    rtot = float(ring.sum())
                    best = None
                    prev_ok = True
                    for R in (8, 16, 24, 32, 40, 50, 60):
                        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*R+1, 2*R+1))
                        op = cv2.morphologyEx(fp, cv2.MORPH_OPEN, k)
                        # 2px tolerance: ring pixels sit ON the edge, and the
                        # opening nibbles a couple of px off slightly wavy
                        # straight runs - only a true corner cut (tens of px)
                        # should fail containment
                        opd = cv2.dilate(op, np.ones((5,5), np.uint8))
                        cont = float((ring & (opd > 0)).sum()) / max(rtot, 1.0)
                        if cont >= 0.995: best = (R, op)
                        else: prev_ok = False; break
                    if best is not None and not prev_ok:
                        # the fit is self-guarding: a straight strip running
                        # into a SQUARE corner fails containment at the very
                        # first radius (best=None), so a constrained fit means
                        # the ring genuinely arcs at the corners
                        R, op = best
                        fade = cv2.GaussianBlur(op.astype(np.float32), (0, 0), 1.0)
                        cut = 1.0 - float((rgba[...,3]*fade).sum())/max(float(rgba[...,3].sum()),1.0)
                        if cut < 0.05:
                            rgba[...,3] *= np.clip(fade, 0, 1)
                # (A base-plate bottom clip was tried here and REMOVED: the
                # panel raster's own bottom IS the slot line - clipping at the
                # plate's sloped back edge cut visible fabric and exposed the
                # stock design on the 2.25m blade. The below-panel wash the
                # inspectors flagged was the REFLECTION instance painting over
                # the plate; the secondaries' hardware mask fixes that.)
            lit_px = blend_rgba(np.ones_like(base), rgba, l.blend_mode,
                                l.opacity, getattr(l,'fill_opacity',255))
            a = rgba[...,3:4]
            # Substrate shading: the print surface is not always flat white (the
            # vendor tray band curves through light and shade, blade stand canvas
            # has weave). preview = shading x (old art on white), so estimate
            # shading = preview / (old art on white), heavily BLURRED so no old-art
            # edges survive (unblurred ratio ghosts the old design), then re-apply
            # it to the new panel.
            orgba = _orgba if _orgba is not None else old_warp(u, l, mk, pw, ph)
            if orgba is not None:
                import cv2
                old_px = blend_rgba(np.ones_like(base), orgba, l.blend_mode,
                                    l.opacity, getattr(l,'fill_opacity',255))
                # Read shading ONLY where the old art is near-white (elsewhere the
                # ratio is the old DESIGN, and even blurred it ghosts as smudges).
                # Weighted blur fills the gaps smoothly; where there is no white
                # support nearby, fall back to no correction (shade=1).
                bright = old_px.mean(axis=2)
                wgt = np.clip((bright-0.55)/0.25, 0, 1) * rgba[...,3]
                # Exclude a HALO around dark old-art content, not just its
                # core: a logo's soft drop-shadow/anti-aliased edge sits at
                # bright 0.55-0.8 and still partially passes the ramp above,
                # baking the logo's own silhouette in as fake "shading" (blade
                # stand plinth: ghost blob under a dark D2G logo). The core
                # dark region has no such gradual falloff elsewhere (weave/
                # curve shading), so dilating only costs coverage near real
                # printed content, which is exactly what should be excluded.
                dark_core = (bright < 0.6).astype(np.uint8)
                if dark_core.any():
                    halo = cv2.dilate(dark_core, np.ones((31,31), np.uint8)) > 0
                    wgt = wgt * (~halo)
                ratio = np.clip(base / np.maximum(old_px, 0.25), 0.4, 1.4)
                sig = max(8, int(0.03*np.sqrt(pw*ph)))
                num = cv2.GaussianBlur(ratio*wgt[...,None], (0,0), sig)
                den = cv2.GaussianBlur(wgt, (0,0), sig)
                shade = num/np.maximum(den[...,None], 1e-3)
                conf = np.clip(den*8, 0, 1)[...,None]
                shade = 1.0 + (np.clip(shade,0.4,1.4)-1.0)*conf
                lit_px = np.clip(lit_px*shade, 0, 1)
            # (A reflection-top-anchored fade-to-white lived here; REMOVED. It
            # sat 30-60px above the true hem and read as a smudge - rejected
            # twice on the curved superwall - and its per-column >=30px gate
            # comb-toothed the pop-up counter's sloped bottom edge. The form-
            # layer hem clip above replaces it.)
            base = base*(1-a) + lit_px*a
            coverage = np.maximum(coverage, rgba[...,3])
        else:
            # HARDWARE masking for secondaries: reflections/shadows must not
            # paint over solid hardware (the blade stand's base plate) - the
            # template lets them overlap it (red wedge on the white plate with
            # bold art) because its own stock art is pale there. Mask by every
            # COMPACT opaque-NORMAL plate regardless of z; scene-sized layers
            # (floor, walls) are exactly what reflections belong on, so they
            # are excluded by the size gate.
            osec = np.zeros((H,W), np.float32)
            for j in range(len(L)):
                if j == i: continue
                lj = L[j]
                if not lj.visible or lj.kind not in ('pixel','smartobject'): continue
                if any(j == ti for ti,_,_ in targets): continue
                if lj.blend_mode != BlendMode.NORMAL or lj.opacity != 255: continue
                if getattr(lj,'fill_opacity',255) != 255: continue
                r2 = layer_patch(lj, W, H)
                if r2 is None: continue
                patch2,x2,y2 = r2
                a2 = np.zeros((H,W), np.float32)
                a2[y2:y2+patch2.shape[0], x2:x2+patch2.shape[1]] = patch2[...,3]
                mk2 = emask(lj)
                if mk2 is not None: a2 *= mk2
                # scene-sized plates (backgrounds) would erase the reflection
                # outright - only compact hardware occludes
                if float((a2>0.5).sum()) > 0.25*W*H: continue
                osec = np.maximum(osec, a2)
            if osec.max() > 0:
                rgba = rgba.copy()
                rgba[...,3] *= (1.0-osec)
            # floor reflections mark where the product MEETS THE FLOOR: record
            # the footprint's top edge per column - the panel fade uses it as
            # the hem line (the artwork raster genuinely overhangs the visible
            # fabric, hidden in stock designs only because they are pale there)
            fp = rgba[...,3] > 0.03
            if fp.any():
                colhit = fp.any(axis=0)
                first = np.where(colhit, np.argmax(fp, axis=0), H)
                prev_t = _refl_top.get(u)
                _refl_top[u] = np.minimum(prev_t, first) if prev_t is not None else first
            base = blend_rgba(base, rgba, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))

    # Re-apply the lighting/shading above the artwork, only where panel pixels
    # were replaced. Occluded regions (people, hardware) were never painted, so
    # their preview pixels - including adjustment-layer grading psd_tools can't
    # reproduce - stay exactly as Photoshop rendered them.
    if coverage.max() > 0:
        lit = base.copy()
        for i,l in enumerate(L):
            if i<=top or not l.visible or l.kind in ADJUST: continue
            r = layer_patch(l, W, H)
            if r is None: continue
            patch,x0,y0 = r
            mk_l = emask(l)
            if i in _bake_cut or mk_l is not None:
                # apply layer+group masks (the model's legs are hidden by a
                # mask on her GROUP - unmasked re-stamping walked her legs
                # over the freshly painted counter) and drop print-bake parts
                full = np.zeros((H,W,4), np.float32)
                full[y0:y0+patch.shape[0], x0:x0+patch.shape[1]] = patch
                if mk_l is not None:
                    full[...,3] *= mk_l
                if i in _bake_cut:
                    full[...,3] *= (~_bake_cut[i]).astype(np.float32)
                lit = blend_rgba(lit, full, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))
            else:
                lit = blend_patch(lit, patch, x0, y0, l.blend_mode, l.opacity, getattr(l,'fill_opacity',255))
        c = coverage[...,None]
        base = base*(1-c) + lit*c

    img = Image.fromarray((np.clip(base,0,1)*255).astype(np.uint8))
    if out_path: img.save(out_path, quality=95, subsampling=0)
    return img


def render4(psd_path, art, out_path=None, mesh_n=64, crop=True):
    """Best of both: keep the file's stored preview (Photoshop-correct) everywhere,
    but inside the artwork's own footprint rebuild the pixels from the real layer
    stack with true blend modes. Fixes render3's ghosting: a translucent artwork
    layer (e.g. MULTIPLY fill 230) composited as 'replace' let 10% of the OLD art
    bleed through the preview. Here the region under the artwork is re-rendered
    from the below layers (reliable individually), so nothing old shows through."""
    psd = PSDImage.open(psd_path)
    W,H = psd.width, psd.height
    preview = np.asarray(stored_preview(psd_path).convert('RGB')).astype(np.float32)/255.0
    if preview.shape[0]!=H or preview.shape[1]!=W:
        preview = np.asarray(Image.fromarray((preview*255).astype(np.uint8)).resize((W,H))).astype(np.float32)/255.0

    A = assets(psd)
    subs={}; coverage = np.zeros((H,W), np.float32)
    for u,g in A.items():
        if not g['editable']: continue
        src0 = art.get(u) if isinstance(art, dict) else art
        if src0 is None: continue
        for _,l in g['layers']:
            if not l.visible: continue
            src = src0.convert('RGBA')
            if crop:
                DX,DY = warp_grid(l, 24)
                pw=max(int(np.hypot(DX[0,-1]-DX[0,0],DY[0,-1]-DY[0,0])),1)
                ph=max(int(np.hypot(DX[-1,0]-DX[0,0],DY[-1,0]-DY[0,0])),1)
                src = cover(src, pw, ph)
            rgba = np.asarray(render_art_fast(l, src, (W,H), n=mesh_n)).astype(np.float32)/255.0
            mk = emask(l)
            if mk is not None: rgba[...,3]*=mk
            subs[id(l)] = rgba
            coverage = np.maximum(coverage, rgba[...,3]*(l.opacity/255.0))
    if not subs: raise ValueError('no editable artwork slots matched')

    rebuilt = flat_composite_fast(psd, subs=subs, W=W, H=H)
    c = np.clip(coverage,0,1)[...,None]
    base = preview*(1-c) + rebuilt*c
    img = Image.fromarray((np.clip(base,0,1)*255).astype(np.uint8))
    if out_path: img.save(out_path, quality=95, subsampling=0)
    return img


def placeholder_box(psb_bytes):
    """The 'your design here' area: bbox of the artwork currently sitting inside
    the smart object, plus the .psb rendered WITHOUT it (the surround to keep),
    plus the ORIGINAL composite's alpha = the printable region stencil (curved
    hems, notches around hardware - NOT a plain rectangle)."""
    inner = PSDImage.open(io.BytesIO(psb_bytes))
    W,H = inner.width, inner.height
    lys=[]
    def w(ls):
        for l in ls:
            if l.is_group(): w(l)
            else: lys.append(l)
    w(inner)
    vis=[l for l in lys if l.visible and l.kind in ('pixel','smartobject')]
    if not vis: return None
    try:
        full = inner.composite(force=True).convert('RGBA')
        stencil = full.getchannel('A') if full.size==(W,H) else None
    except Exception:
        stencil = None
    # the design layer = topmost visible pixel/SO layer with real area
    design = vis[-1]
    bb = design.bbox
    if not bb or bb[2]<=bb[0] or bb[3]<=bb[1]: return None
    saved={id(l):l.visible for l in lys}
    design.visible=False
    try:
        surround = inner.composite(force=True).convert('RGBA')
    except Exception:
        surround = None
    for l in lys: l.visible=saved[id(l)]
    return {'psb_size':(W,H), 'box':tuple(int(v) for v in bb),
            'design_layer':design.name, 'surround':surround, 'stencil':stencil}

def compose_into_placeholder(psb_bytes, new_art):
    """Emulate the Photoshop step: drop the client's artwork into the smart
    object. If the design is a single layer neatly filling the canvas, paste
    into its box and keep the surround. If the STOCK DESIGN is itself several
    stacked layers (pop-up counter: background + bottles + model photo), or
    the 'design layer' overshoots the canvas (6x3 booth: box starts 239px
    above it, beheading the headline), the whole content IS the print - so
    replace all of it, full-canvas."""
    info = placeholder_box(psb_bytes)
    if info is None: return None
    W,H = info['psb_size']; x0,y0,x1,y1 = info['box']
    overshoot = max(0, -x0, -y0, x1-W, y1-H) / float(max(W,H))
    # does hiding the 'design layer' still leave a visible design? (model
    # photos, bottle shots stacked in the psb) - then the whole content is
    # the print
    busy = 0.0
    if info['surround'] is not None:
        s = np.asarray(info['surround'].convert('RGBA')).astype(np.float32)
        vis = s[...,3] > 128
        rgb = s[...,:3]
        nonwhite = vis & ((rgb.max(axis=2)-rgb.min(axis=2) > 24) | (rgb.mean(axis=2) < 200))
        busy = float(nonwhite.sum()) / float(W*H)
    # BOTH signals must fire: overshoot alone is normal design bleed (straight
    # superwall: 21% and renders correctly via the box), residual content
    # alone sits harmlessly UNDER a canvas-covering paste (blade stands).
    # Together they mean the box placement is broken AND the stock design
    # leaks through (6x3 booth, pop-up counter supermarket, retail demo).
    if overshoot > 0.12 and busy > 0.05:
        x0,y0,x1,y1 = 0,0,W,H
        base = Image.new('RGBA',(W,H),(255,255,255,255))
    else:
        base = info['surround'] if info['surround'] is not None else Image.new('RGBA',(W,H),(255,255,255,255))
        if base.size != (W,H): base = base.resize((W,H))
    bw,bh = x1-x0, y1-y0
    # Fit against the ON-CANVAS portion of the box, not its raw size - a box
    # legitimately overshooting into print bleed (straight superwall: 3000-
    # tall box on a 2000px-tall canvas) is not a narrow slot, and sizing off
    # the inflated box wrongly triggered CONTAIN, landing its blurred
    # letterbox band inside the visible frame.
    vx = max(0, -x0); vy = max(0, -y0)
    vw = min(x1, W) - max(x0, 0); vh = min(y1, H) - max(y0, 0)
    vis_box = (vx, vy, vw, vh) if vw > 0 and vh > 0 else None
    art = safe_fit(new_art.convert('RGBA'), bw, bh, vis_box=vis_box)
    out = base.copy()
    out.paste(art, (x0,y0), art)
    # Clip to the ORIGINAL printable region: the old design's alpha carries the
    # product's real contour (curved fabric hems, cut-outs around the roller
    # cassette). A full rectangle spills past the hem and over hardware.
    if info.get('stencil') is not None:
        a = np.minimum(np.asarray(out.getchannel('A')), np.asarray(info['stencil']))
        out.putalpha(Image.fromarray(a))
    return out
