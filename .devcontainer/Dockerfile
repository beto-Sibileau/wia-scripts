FROM mcr.microsoft.com/devcontainers/python:3.12

# ── Node.js + mapshaper ──────────────────────────────────────────────
ARG NODE_MAJOR=18
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs npm; \
    node -v; \
    npm -v; \
    npm install -g mapshaper; \
    mapshaper -v; \
    rm -rf /var/lib/apt/lists/*

# ── Chromium for Kaleido (Plotly static image export) ────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libxss1 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

# ── GDAL / GEOS / PROJ system libraries ─────────────────────────────
# Required by odc-stac and rioxarray for COG writing, /vsicurl/ remote
# reads, and reprojection from Equi7Grid and other non-standard CRSs.
# Also benefits rasterio and Fiona when pip wheels don't bundle the
# specific GDAL feature needed (e.g. HTTP/2 support, specific drivers).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python tooling ──────────────────────────────────────────────────
RUN python -m pip install -U pip setuptools wheel

# ── GDAL environment variables for optimised remote reads ────────────
# These only affect HTTP-based raster access (/vsicurl/, /vsis3/, etc.)
# and are inert for local file operations, so safe to always have set.
ENV GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=5000000 \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff" \
    CPL_VSIL_CURL_CACHE_SIZE=200000000
	
# ── Dask memory management defaults ─────────────────────────────────
# Conservative thresholds for a 32 GB laptop running Docker.
# Only activate when a dask.distributed Client is started.
ENV DASK_DISTRIBUTED__WORKER__MEMORY__TARGET=0.70 \
    DASK_DISTRIBUTED__WORKER__MEMORY__SPILL=0.80 \
    DASK_DISTRIBUTED__WORKER__MEMORY__PAUSE=0.90 \
    DASK_DISTRIBUTED__WORKER__MEMORY__TERMINATE=0.95 \
    DASK_ARRAY__CHUNK_SIZE="128MiB"
