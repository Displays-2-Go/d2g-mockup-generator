"""Standalone web app for the D2G mockup engine. One page: pick a product
range and variant, upload the client's artwork (screenshot/logo/photo),
get back a finished mockup JPG to download and attach to an email.

Run locally:   python app.py            (http://localhost:8000)
Deploy: build with the included Dockerfile on Render/Railway/etc - no
other setup needed beyond pointing TEMPLATE_DIR (in make_mockup.py) at
wherever the 45 .psd templates are stored on the host (a mounted disk,
or baked into the image if size allows).
"""
import io
import os
import subprocess
import sys
import tempfile
from flask import Flask, request, render_template_string, send_file
from PIL import Image
import make_mockup

TOO_COMPLEX_MSG = (
    "This product's scene is too complex to generate right now. "
    "Try a different size/variant, or ask a rep to put the mockup together manually."
)


def render_in_subprocess(template, art_bytes, timeout=170):
    """Runs the actual (memory-hungry, on some products) render as a genuinely
    separate OS process (render_worker.py via plain subprocess.run - NOT
    Python's multiprocessing, which needed to re-import this whole app on
    Linux via its 'spawn' bootstrap and hung indefinitely there for reasons
    not fully pinned down, despite working fine in local testing; a plain
    subprocess of a small standalone script is the simpler, standard,
    well-tested way to isolate risky work and sidesteps that entirely).

    If a product's scene is too complex for the host's memory, the
    container's own real memory limit kills this child - but the PARENT
    process (this whole web app) survives regardless, and can tell the rep
    it didn't work, rather than the whole tool going down for everyone
    using it at that moment. Added 31/08 after finding some of the 45
    Squiggles templates are too complex to reliably fit in the host's
    memory, and Phil ruled out paying for more - the responsible fallback
    is to fail one request cleanly, not let it take the whole service down.

    Returns (jpeg_bytes, None) on success, or (None, error_message)."""
    with tempfile.TemporaryDirectory() as tmp:
        art_path = os.path.join(tmp, "art")
        out_path = os.path.join(tmp, "out.jpg")
        with open(art_path, "wb") as f:
            f.write(art_bytes)
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), "render_worker.py"),
                 template, art_path, out_path],
                timeout=timeout, capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired:
            return None, TOO_COMPLEX_MSG + " (it was taking too long)"
        if proc.returncode != 0 or not os.path.exists(out_path):
            # non-zero exit with no useful stderr (or killed outright, e.g.
            # by the OS for using too much memory) - treat the same way
            msg = (proc.stderr or "").strip()
            if msg:
                return None, f"Couldn't generate that mockup: {msg}"
            return None, TOO_COMPLEX_MSG
        with open(out_path, "rb") as f:
            return f.read(), None

app = Flask(__name__)

PAGE = """
<!doctype html><html><head><title>D2G Mockup Generator</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px}
h1{font-size:1.3rem}
label,.fieldlabel{display:block;margin-top:16px;font-weight:600}
select,input[type=file]{width:100%;max-width:420px;padding:8px;margin-top:4px}
button,.btn{margin-top:20px;padding:10px 20px;font-size:1rem;cursor:pointer;display:inline-block;
  text-decoration:none;color:inherit;border:1px solid #888;border-radius:4px;background:#f4f4f4}
.err{color:#b00020;margin-top:12px}
.ratio-hint{margin-top:6px;font-weight:400;color:#555;font-size:0.95rem;display:flex;align-items:center;gap:10px;min-height:44px}
.ratio-diagram{flex:none;width:44px;height:44px;display:flex;align-items:center;justify-content:center}
.ratio-diagram div{background:#ccc;border:1px solid #888}
.dropzone{margin-top:4px;max-width:420px;border:2px dashed #999;border-radius:6px;padding:20px;
  text-align:center;color:#555;font-weight:400;cursor:pointer;background:#fafafa}
.dropzone.drag{border-color:#2a7;background:#eefaf3;color:#1a6}
.dropzone input[type=file]{display:none}
.dropzone .preview{max-width:100%;max-height:160px;margin-top:10px;display:none}
.dropzone .fname{margin-top:8px;font-size:0.9rem;color:#333;word-break:break-all}
.result img{max-width:100%;border:1px solid #ddd;margin-top:16px}
.result{margin-top:8px}
</style></head><body>
<h1>D2G Mockup Generator</h1>
<p>Pick a product, upload the client's artwork (a website screenshot, logo, or product photo), and generate the mockup.</p>
<form method="post" action="/generate" enctype="multipart/form-data">
  <label>Product range
    <select name="range" required>
      {% for r in ranges %}<option value="{{r}}">{{r}}</option>{% endfor %}
    </select>
  </label>
  <label>Specific product
    <select name="template" required>
      {% for r, variants in ranges.items() %}
        {% for v in variants %}
          {% set key = v if v.startswith('new:') else r ~ '/' ~ v %}
          {% set info = ratios.get(key, {}) %}
          <option value="{{ key }}" data-range="{{r}}" data-ratio-label="{{ info.get('label', '') }}" data-ratio-num="{{ info.get('ratio', '') }}">{{ v[4:] if v.startswith('new:') else v.rsplit('.', 1)[0] }}</option>
        {% endfor %}
      {% endfor %}
    </select>
  </label>
  <p id="ratioHint" class="ratio-hint">
    <span class="ratio-diagram" id="ratioDiagram"></span>
    <span id="ratioText"></span>
  </p>
  <div class="fieldlabel">Client artwork</div>
  <div id="dropzone" class="dropzone">
    <div>Click to browse, drag a file here, or paste (Ctrl+V) an image</div>
    <div class="fname" id="fname"></div>
    <img class="preview" id="preview" alt="">
    <input type="file" name="art" id="artInput" accept="image/*" required>
  </div>
  <button type="submit">Generate mockup</button>
</form>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if image_data_uri %}
<div class="result">
  <p><a class="btn" href="{{ image_data_uri }}" download="{{ download_name }}">Download {{ download_name }}</a></p>
  <img src="{{ image_data_uri }}" alt="Generated mockup">
</div>
{% endif %}
<script>
// filter the "specific product" dropdown by the chosen range
const rangeSel = document.querySelector('select[name=range]');
const tplSel = document.querySelector('select[name=template]');
const ratioText = document.getElementById('ratioText');
const ratioDiagram = document.getElementById('ratioDiagram');
const allOpts = Array.from(tplSel.options);
function updateHint(){
  const opt = tplSel.options[tplSel.selectedIndex];
  const label = opt ? opt.dataset.ratioLabel : '';
  const ratio = opt ? parseFloat(opt.dataset.ratioNum) : NaN;
  ratioText.textContent = label || '';
  ratioDiagram.innerHTML = '';
  if (!isNaN(ratio) && ratio > 0){
    const box = document.createElement('div');
    const MAX = 40;
    if (ratio >= 1){ box.style.width = MAX + 'px'; box.style.height = (MAX / ratio) + 'px'; }
    else { box.style.height = MAX + 'px'; box.style.width = (MAX * ratio) + 'px'; }
    ratioDiagram.appendChild(box);
  }
}
function refresh(){
  const r = rangeSel.value;
  const wanted = tplSel.dataset.selected;
  tplSel.innerHTML = '';
  allOpts.filter(o => o.dataset.range === r).forEach(o => tplSel.add(o));
  if (wanted) tplSel.value = wanted;
  updateHint();
}
rangeSel.addEventListener('change', refresh);
tplSel.addEventListener('change', updateHint);
refresh();

// drag & drop / paste-from-clipboard for the artwork file
const dropzone = document.getElementById('dropzone');
const artInput = document.getElementById('artInput');
const fname = document.getElementById('fname');
const preview = document.getElementById('preview');

function showFile(file){
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  artInput.files = dt.files;
  fname.textContent = file.name || 'Pasted image';
  const reader = new FileReader();
  reader.onload = e => { preview.src = e.target.result; preview.style.display = 'block'; };
  reader.readAsDataURL(file);
}

dropzone.addEventListener('click', () => artInput.click());
artInput.addEventListener('change', () => { if (artInput.files[0]) showFile(artInput.files[0]); });

['dragenter','dragover'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.add('drag'); }));
['dragleave','drop'].forEach(evt =>
  dropzone.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.remove('drag'); }));
dropzone.addEventListener('drop', e => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) showFile(file);
});

document.addEventListener('paste', e => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items){
    if (item.type.startsWith('image/')){
      const file = item.getAsFile();
      if (file) { showFile(file); e.preventDefault(); }
      break;
    }
  }
});
</script>
</body></html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, ranges=make_mockup.list_templates(),
                                   ratios=make_mockup.ratio_labels(), error=None,
                                   image_data_uri=None)


@app.route("/generate", methods=["POST"])
def generate():
    import base64
    template = request.form.get("template")
    art_file = request.files.get("art")
    ranges = make_mockup.list_templates()
    ratios = make_mockup.ratio_labels()
    if not template or not art_file:
        return render_template_string(PAGE, ranges=ranges, ratios=ratios, image_data_uri=None,
                                       error="Pick a product and upload an image."), 400
    jpeg_bytes, err = render_in_subprocess(template, art_file.stream.read())
    if err:
        return render_template_string(PAGE, ranges=ranges, ratios=ratios, image_data_uri=None,
                                       error=err), 500
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    if template.startswith("new:"):
        variant_name = template[len("new:"):]
    else:
        variant_name = template.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    out_name = variant_name + " - mockup.jpg"
    return render_template_string(
        PAGE, ranges=ranges, ratios=ratios, error=None,
        image_data_uri=f"data:image/jpeg;base64,{b64}", download_name=out_name)


if __name__ == "__main__":
    # Flask's built-in server is single-request-at-a-time by default, which
    # would make one rep's upload queue behind another's - waitress is a
    # small, dependency-light production server that handles several D2G
    # reps hitting this at once without needing a full deploy stack.
    from waitress import serve
    import os
    # Render (and most host platforms) assign the port via $PORT at runtime
    # rather than always using 8000 - fall back to 8000 for local/PC use.
    port = int(os.environ.get("PORT", 8000))
    print("D2G Mockup Generator running - on this PC's network, browse to:")
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        print(f"  http://{s.getsockname()[0]}:{port}")
        s.close()
    except Exception:
        pass
    serve(app, host="0.0.0.0", port=port)
