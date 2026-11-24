FROM python:3.11-slim

# opencv needs these system libs even in "headless" mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# PREFERRED: template packs (see template_pack.py). Run once on the PC with
# the .psd files:  python template_pack.py bake "<psd folder>" packs
# then copy the packs folder here (or mount it) and set TEMPLATE_PACKS_DIR.
# Renders then never open a .psd. If the packs are small enough, uncomment:
# COPY packs /app/packs
# ENV TEMPLATE_PACKS_DIR=/app/packs
#
# FALLBACK: the 45 .psd templates are NOT baked into this image (~3.2GB).
# Mount them at /app/templates as a persistent disk/volume on whatever host
# runs this, or download them at container start from wherever they're
# stored (OneDrive/S3/etc) - either way, make_mockup.TEMPLATE_DIR must end
# up pointing at a folder containing the 45 .psd files before first use.

EXPOSE 8000
CMD ["python", "app.py"]
