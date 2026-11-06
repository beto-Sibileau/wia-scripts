# -*- coding: utf-8 -*-
"""
export_hazard_rasters_gee.py
============================
Exports hazard layers as GeoTIFFs clipped to a country, for the pixel maps
drawn in postprocess_exposure_wavg.ipynb.

Companion to pop_w_exposure_gee.py, which sends the aggregated CSVs. This
script does no aggregation and no thresholding: it exports the layer values
on the layer's own grid, so the pixels are the ones behind the indicators.

Run in Colab, like pop_w_exposure_gee.py.

Workflow
--------
  1. Edit ADMIN_ASSETS and EXPORTS in section 2.
  2. Run the script and wait for the tasks to finish.
  3. Download the TIFFs from the Drive folder DRIVE_FOLDER to
     data_in/Raster_Datasets/GEE_hazards/

Output
------
  One float32 cloud-optimised GeoTIFF per entry, named
  {ISO3}_{hazard_name}.tif, plus a JSON run log in the same Drive folder.
  Pixels outside the country or without data in the layer are written as
  NODATA_VALUE. Inside the country this means:
    - river flood: no modelled flooding (nodata means zero hazard)
    - SPEI: no observation

Notes
-----
  - The export uses the layer's own CRS and transform, so pixels are not
    resampled. Resampling for display happens later, in post-processing.
  - The export extent is the bounding box of the admin asset. Any subnational
    level works, since only the outer limits and shape of the country are used.
  - Clipping keeps a pixel when its centre falls inside the country. These
    rasters are for display only and do not enter the indicators.
"""

from google.colab import drive
drive.mount('/content/drive')

import json
import os
import time
from datetime import datetime, timezone

import ee

# ── Initialize ───────────────────────────────────────────────────────────
ee.Authenticate()
ee.Initialize(project='unicef-ccri')


# =========================================================================
# 1. HAZARD LAYERS (same assets and loading as pop_w_exposure_gee.py)
# =========================================================================
HAZARD_LAYERS = {
    "river_flood_100yr_jrc_2024": {
        "asset_id": "projects/unicef-ccri/assets/hazards/river_flood_r100",
        "is_collection": True,
    },
    "drought_spei_terraclimate_1958-2025": {
        "asset_id": "projects/unicef-ccri/assets/droughts/spei12_TerraClimate_1958-2025",
        "is_collection": False,
    },
}


# =========================================================================
# 2. EXPORT LIST: add a line to export another country or layer
# =========================================================================
# Subnational boundaries of each country, as used in pop_w_exposure_gee.py
ADMIN_ASSETS = {
    "SSD": "",  # paste the asset ID, e.g. projects/unicef-ccri/assets/misc_boundaries/...
    "YEM": "",  # paste the asset ID
}

EXPORTS = [
    {"iso3": "SSD", "hazard": "river_flood_100yr_jrc_2024"},
    {"iso3": "YEM", "hazard": "drought_spei_terraclimate_1958-2025"},
]

DRIVE_FOLDER = "hazard_rasters_export"
NODATA_VALUE = -9999


# =========================================================================
# 3. HELPERS
# =========================================================================
def load_hazard(cfg, region):
    """Return (image, native projection info), first band only.

    A mosaic loses the native projection of its tiles, so for a collection the
    projection is read from the first tile over the country.
    """
    if cfg["is_collection"]:
        collection = ee.ImageCollection(cfg["asset_id"])
        img = collection.mosaic().select(0)
        projection = collection.filterBounds(region).first().select(0).projection()
    else:
        img = ee.Image(cfg["asset_id"]).select(0)
        projection = img.projection()
    return img, projection.getInfo()


def check_tile_grids(cfg, region):
    """For a collection, print how many tiles cover the country and which grids
    they use. A single entry means all tiles share one CRS and pixel size, as
    assumed in load_hazard. It does not compare where each tile's grid starts."""
    if not cfg["is_collection"]:
        return None

    def tag(image):
        projection = ee.Image(image).select(0).projection()
        return ee.Feature(None, {
            "grid": ee.String(projection.crs()).cat(" | ").cat(
                ee.Number(projection.nominalScale()).format("%.3f")
            ),
        })

    tiles = ee.ImageCollection(cfg["asset_id"]).filterBounds(region)
    grids = ee.FeatureCollection(tiles.map(tag)).aggregate_histogram("grid").getInfo()
    print(f"    tiles by grid (crs | pixel size in m): {grids}")
    return grids


def start_export(entry):
    """Submit one export task and return its record for tracking and logging."""
    iso3, hazard = entry["iso3"], entry["hazard"]
    if not ADMIN_ASSETS.get(iso3):
        raise ValueError(f"Add the admin asset of {iso3} to ADMIN_ASSETS.")

    cfg = HAZARD_LAYERS[hazard]
    admin_fc = ee.FeatureCollection(ADMIN_ASSETS[iso3])
    region = admin_fc.geometry().bounds()
    img, proj = load_hazard(cfg, region)
    file_stem = f"{iso3}_{hazard}"

    print(f"  {file_stem}  crs={proj['crs']}  pixel={abs(proj['transform'][0])}")
    tile_grids = check_tile_grids(cfg, region)

    task = ee.batch.Export.image.toDrive(
        image=img.toFloat().clipToCollection(admin_fc),
        description=file_stem,
        folder=DRIVE_FOLDER,
        fileNamePrefix=file_stem,
        region=region,
        crs=proj["crs"],
        crsTransform=proj["transform"],
        maxPixels=1e13,
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True, "noData": NODATA_VALUE},
    )
    task.start()

    return {
        "file": f"{file_stem}.tif",
        "iso3": iso3,
        "hazard": hazard,
        "asset_id": cfg["asset_id"],
        "admin_asset_id": ADMIN_ASSETS[iso3],
        "bounding_box": region.getInfo(),
        "crs": proj["crs"],
        "crs_transform": proj["transform"],
        "tile_grids": tile_grids,
        "task": task,
    }


# =========================================================================
# 4. SUBMIT
# =========================================================================
print(f"── Submitting {len(EXPORTS)} export task(s) ──")
tasks = [start_export(entry) for entry in EXPORTS]


# =========================================================================
# 5. WAIT FOR COMPLETION
# =========================================================================
# (Run this cell separately if you prefer to check back later.)
FINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED")

while True:
    states = [t["task"].status()["state"] for t in tasks]
    n_done = sum(s in FINAL_STATES for s in states)
    print(f"  {n_done}/{len(tasks)} done: {states}")
    if n_done == len(tasks):
        break
    time.sleep(30)


# =========================================================================
# 6. RUN LOG
# =========================================================================
records = []
for t in tasks:
    status = t["task"].status()
    record = {k: v for k, v in t.items() if k != "task"}
    record["state"] = status["state"]
    record["error"] = status.get("error_message")
    records.append(record)

failed = [r["file"] for r in records if r["state"] != "COMPLETED"]
print(f"⚠️  Not completed: {failed}" if failed else "✅  All exports completed.")

run_log = {
    "pipeline": "export_hazard_rasters_gee",
    "run_timestamp": datetime.now(timezone.utc).isoformat(),
    "drive_folder": DRIVE_FOLDER,
    "nodata_value": NODATA_VALUE,
    "exports": records,
}
log_dir = os.path.join("/content/drive/MyDrive", DRIVE_FOLDER)
os.makedirs(log_dir, exist_ok=True)
log_name = f"export_log_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
with open(os.path.join(log_dir, log_name), "w") as f:
    json.dump(run_log, f, indent=2)
print(f"Run log: {log_name}")
