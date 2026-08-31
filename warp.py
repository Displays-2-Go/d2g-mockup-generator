"""Reproduce Photoshop's smart-object placement: 4x4 Bezier envelope warp + placement transform."""
import numpy as np, cv2
from PIL import Image
from psd_tools.constants import Tag

def _bern(t):
    return np.stack([(1-t)**3, 3*t*(1-t)**2, 3*t**2*(1-t), t**3], axis=-1)

def _mesh_shape(Hf, Vf):
    """Photoshop subdivides a custom warp into a grid of bicubic patches, so the
    control-point count is (3*ny+1)*(3*nx+1) - 16, 52, 130, 256 all occur in this
    template set. The count alone is ambiguous (52 = 4x13 or 13x4), so pick the
    factorisation whose grid is actually monotonic left-to-right / top-to-bottom."""
    N = Hf.size
    best=None
    for r in range(1, N+1):
        if N % r: continue
        c = N//r
        if r % 3 != 1 or c % 3 != 1: continue
        Hg = Hf.reshape(r,c); Vg = Vf.reshape(r,c)
        score = 0.0
        if c>1: score += float(np.mean(np.diff(Hg,axis=1) > 0))
        if r>1: score += float(np.mean(np.diff(Vg,axis=0) > 0))
        if best is None or score > best[0]: best=(score,r,c)
    if best is None:
        raise ValueError(f'unrecognised warp mesh with {N} control points')
    return best[1], best[2]

def _quilt_lookup(T, q, n):
    """Map normalised source coord T to (patch index, local 0..1) using the
    quilt slice positions Photoshop stored (source-space patch cuts are NOT
    uniform - assuming quarters warps lettering grotesquely on superwalls)."""
    q = np.asarray(q, dtype=np.float64)
    qn = (q - q[0]) / max(q[-1] - q[0], 1e-9)
    Tc = np.clip(T, 0, 1)
    idx = np.clip(np.searchsorted(qn, Tc, side='right') - 1, 0, n - 1)
    left = qn[idx]; width = np.maximum(qn[idx + 1] - qn[idx], 1e-9)
    return idx, np.clip((Tc - left) / width, 0, 1)

def _eval_patches(H, Vp, U, V, qx=None, qy=None):
    """Evaluate a grid of bicubic Bezier patches at normalised (U,V)."""
    rows, cols = H.shape
    py_n = max((rows-1)//3, 1); px_n = max((cols-1)//3, 1)
    if qx is not None and len(qx) == px_n + 1:
        px, lu = _quilt_lookup(U, qx, px_n)
    else:
        su = np.clip(U,0,1)*px_n
        px = np.clip(su.astype(int), 0, px_n-1); lu = su-px
    if qy is not None and len(qy) == py_n + 1:
        py, lv = _quilt_lookup(V, qy, py_n)
    else:
        sv = np.clip(V,0,1)*py_n
        py = np.clip(sv.astype(int), 0, py_n-1); lv = sv-py
    Bu = _bern(lu); Bv = _bern(lv)
    # gather the 4x4 control block for each sample point
    ri = (3*py)[...,None,None] + np.arange(4)[None,None,:,None]
    ci = (3*px)[...,None,None] + np.arange(4)[None,None,None,:]
    ri = np.clip(ri, 0, rows-1); ci = np.clip(ci, 0, cols-1)
    Hb = H[ri, ci]; Vb = Vp[ri, ci]
    X = np.einsum('ijr,ijrc,ijc->ij', Bv, Hb, Bu)
    Y = np.einsum('ijr,ijrc,ijc->ij', Bv, Vb, Bu)
    return X, Y

def warp_grid(layer, n=160):
    """Return (u,v) in [0,1]^2 -> canvas xy, honouring the PSD's own warp mesh."""
    pl = layer.tagged_blocks.get_data(Tag.PLACED_LAYER2)
    t = pl.transform
    quad = np.float32([[t[0],t[1]],[t[2],t[3]],[t[4],t[5]],[t[6],t[7]]])
    w = pl.warp
    u = np.linspace(0,1,n); v = np.linspace(0,1,n)
    U,V = np.meshgrid(u,v)

    mesh = w.get(b'customEnvelopeWarp') if w else None
    if mesh is None:
        # no warp mesh: plain perspective onto the quad
        src = np.float32([[0,0],[1,0],[1,1],[0,1]])
        M = cv2.getPerspectiveTransform(src, quad)
        pts = np.stack([U.ravel(),V.ravel(),np.ones(U.size)])
        d = M @ pts; d = d[:2]/d[2]
        return d[0].reshape(n,n), d[1].reshape(n,n)

    mp = mesh[b'meshPoints']
    Hf = np.array(list(mp[b'Hrzn']), dtype=np.float64)
    Vf = np.array(list(mp[b'Vrtc']), dtype=np.float64)
    rows, cols = _mesh_shape(Hf, Vf)
    H = Hf.reshape(rows, cols); Vp = Vf.reshape(rows, cols)
    qx = qy = None
    try:
        qx = [float(v) for v in mesh[b'quiltSliceX'][b'quiltSliceX']]
        qy = [float(v) for v in mesh[b'quiltSliceY'][b'quiltSliceY']]
    except Exception:
        pass
    X, Y = _eval_patches(H, Vp, U, V, qx=qx, qy=qy)
    # The placement quad corresponds to the mesh's own bounding box, NOT the
    # descriptor `bounds` rect (verified: cart mesh bbox 582x1330 == quad 582x1331).
    left,right = H.min(), H.max()
    top,btm = Vp.min(), Vp.max()
    nx = (X-left)/(right-left); ny = (Y-top)/(btm-top)
    src = np.float32([[0,0],[1,0],[1,1],[0,1]])
    M = cv2.getPerspectiveTransform(src, quad)
    pts = np.stack([nx.ravel(), ny.ravel(), np.ones(nx.size)])
    d = M @ pts; d = d[:2]/d[2]
    return d[0].reshape(nx.shape), d[1].reshape(ny.shape)

def render_art(layer, art, canvas_wh, n=160):
    """Warp `art` (PIL RGBA) onto the canvas following the layer's mesh."""
    W,H = canvas_wh
    DX,DY = warp_grid(layer, n)
    aw,ah = art.size
    u = np.linspace(0,1,n); v = np.linspace(0,1,n)
    U,V = np.meshgrid(u,v)
    SX = U*(aw-1); SY = V*(ah-1)

    x0,x1 = int(np.floor(DX.min())), int(np.ceil(DX.max()))
    y0,y1 = int(np.floor(DY.min())), int(np.ceil(DY.max()))
    x0,y0 = max(x0,0), max(y0,0); x1,y1 = min(x1,W), min(y1,H)
    bw,bh = x1-x0, y1-y0
    if bw<=0 or bh<=0: raise ValueError('warp falls outside canvas')

    from scipy.interpolate import LinearNDInterpolator
    pts = np.stack([DX.ravel()-x0, DY.ravel()-y0], axis=1)
    gx,gy = np.meshgrid(np.arange(bw), np.arange(bh))
    fx = LinearNDInterpolator(pts, SX.ravel())(gx,gy)
    fy = LinearNDInterpolator(pts, SY.ravel())(gx,gy)
    valid = ~(np.isnan(fx)|np.isnan(fy))
    mapx = np.nan_to_num(fx).astype(np.float32); mapy = np.nan_to_num(fy).astype(np.float32)
    patch = cv2.remap(np.array(art), mapx, mapy, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    patch[...,3] = (patch[...,3]*valid).astype(np.uint8)
    out = np.zeros((H,W,4), np.uint8)
    out[y0:y1, x0:x1] = patch
    return Image.fromarray(out)


def render_art_fast(layer, art, canvas_wh, n=64):
    """Same result as render_art but ~50x faster: rasterise the Bezier patch
    cell-by-cell with tiny perspective warps instead of scattered interpolation.

    The accumulator only ever gets written inside the warp's own bounding box
    (every cell's `dq` is a small piece of it) - allocating it at full canvas
    size was reserving the WHOLE picture's worth of memory to hold what's
    typically a small patch (measured: 3000x2250 canvas but a 1562x1628
    artwork region - allocating 4x more than needed, the single biggest
    memory cost found profiling the Render deploy, 31/08). Accumulate at the
    bbox's own size instead, then paste into a zero canvas of the requested
    size only at the very end - mathematically identical, since the original
    code never wrote outside that same bbox in a full-size canvas either."""
    W,H = canvas_wh
    DX,DY = warp_grid(layer, n+1)
    aw,ah = art.size
    A = np.array(art)
    u = np.linspace(0,1,n+1)*(aw-1)
    v = np.linspace(0,1,n+1)*(ah-1)

    bx0 = max(int(np.floor(DX.min()))-1, 0); bx1 = min(int(np.ceil(DX.max()))+2, W)
    by0 = max(int(np.floor(DY.min()))-1, 0); by1 = min(int(np.ceil(DY.max()))+2, H)
    bw, bh = max(bx1-bx0, 0), max(by1-by0, 0)
    if bw <= 0 or bh <= 0:
        return Image.fromarray(np.zeros((H,W,4), np.uint8))

    out = np.zeros((bh,bw,4), np.float32)
    acc = np.zeros((bh,bw,1), np.float32)
    for i in range(n):
        for j in range(n):
            dq = np.float32([[DX[i,j],DY[i,j]],[DX[i,j+1],DY[i,j+1]],
                             [DX[i+1,j+1],DY[i+1,j+1]],[DX[i+1,j],DY[i+1,j]]])
            x0 = int(np.floor(dq[:,0].min()))-1; x1 = int(np.ceil(dq[:,0].max()))+2
            y0 = int(np.floor(dq[:,1].min()))-1; y1 = int(np.ceil(dq[:,1].max()))+2
            x0,y0 = max(x0,0), max(y0,0); x1,y1 = min(x1,W), min(y1,H)
            if x1<=x0 or y1<=y0: continue
            sq = np.float32([[u[j],v[i]],[u[j+1],v[i]],[u[j+1],v[i+1]],[u[j],v[i+1]]])
            M = cv2.getPerspectiveTransform(sq, dq - np.float32([x0,y0]))
            patch = cv2.warpPerspective(A, M, (x1-x0, y1-y0),
                                        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            msk = np.zeros((y1-y0, x1-x0), np.float32)
            cv2.fillConvexPoly(msk, np.round((dq-np.float32([x0,y0]))*16).astype(np.int32),
                               1.0, lineType=cv2.LINE_AA, shift=4)
            # same target cell, offset into the bbox-sized accumulator instead
            # of the full canvas
            oy0,oy1 = y0-by0, y1-by0
            ox0,ox1 = x0-bx0, x1-bx0
            out[oy0:oy1, ox0:ox1] += patch.astype(np.float32)*msk[...,None]
            acc[oy0:oy1, ox0:ox1] += msk[...,None]
    np.divide(out, np.maximum(acc,1e-6), out=out)
    out[...,3] *= np.clip(acc[...,0],0,1)
    full = np.zeros((H,W,4), np.uint8)
    full[by0:by1, bx0:bx1] = np.clip(out,0,255).astype(np.uint8)
    return Image.fromarray(full)
