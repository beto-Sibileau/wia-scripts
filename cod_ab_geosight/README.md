# cod_ab_geosight

Harmonized administrative boundaries of the WIA countries for UNICEF GeoSight,
compiled from the per-country OCHA COD-AB shapefiles on HDX: one layer per
admin level (admin 0 down to the level at which WIA is published), all
countries together, same attribute fields at every level.

The run is driven by the zipped reference geojsons used for WIA
(`geojson_{ISO3}_adm{i}*.zip`): they say which countries and down to which
level. The HDX shapefiles are what gets shipped, and the target level is
compared against the geojson so that differences between the two versions
are visible before uploading.

Why not the global COD-AB geodatabase (`cod-ab-global`): it does not always
match the per-country releases yet, and it has no admin 0 layer.

## Quick start

From the `wia-scripts` folder:

```bash
pip install -r cod_ab_geosight/requirements.txt   # pyyaml, scipy on top of the usual stack
python -m cod_ab_geosight                          # uses cod_ab_geosight/config.yaml
python -m cod_ab_geosight --download-all           # download from HDX first
python -m cod_ab_geosight --config other.yaml
```

or in a notebook in `wia-scripts`:

```python
from cod_ab_geosight import run
result = run()                      # or run("cod_ab_geosight/config.yaml")
result["summary"]                   # one row per country
result["issues"]                    # one row per finding
result["levels"][2]                 # GeoDataFrame of admin 2 (all countries)
```

Only the HDX download step needs internet. A run over the 32 countries
takes about two minutes.

## Inputs (paths in `config.yaml`)

| Input | What it is |
|---|---|
| `config_geojsons` | Folder with `geojson_{ISO3}_adm{i}*.zip` (top level only, subfolders ignored). One country and target level per file; if a country has several, the highest level is used (flagged). Dropping or replacing a file here is how you change the run. |
| `hdx_shp_zip` | The "shp-folder": one COD-AB zip per country, flat, name starting with the iso3 (`afg_admin_boundaries.shp.zip`, `col-administrative-divisions-shapefiles.zip`). Zips of countries not in the config are ignored. |
| `global_pcodes` | HDX `global-pcodes` CSV (`Location`, `Admin Level`, `P-Code` columns). |
| `output` | Output folder (created; a `.gitignore` keeps it out of git). |

## Two ways to run

- `download_all: false` (default): uses the zips already in the shp-folder.
  A config country without a zip is flagged and skipped. Access dates in
  `Source` are `default_access_date`, or a country's `access_date`.
- `download_all: true`: first downloads the COD-AB zip of every config
  country into the shp-folder (same file name is overwritten) and the
  global P-codes CSV next to `global_pcodes`. Access dates become today for
  what was downloaded; per-country `access_date` is then ignored. If a
  download fails, the zip already there is used with its configured date
  (flagged).

`python -m cod_ab_geosight.download` does only the download step
(`--iso3`, `--out-dir`, `--pcodes-dir`, `--no-pcodes`). It replaces
`download_cod_ab.py` at the repo root: same resource filter, but zips land
flat in one folder and it also fetches the global P-codes (first CSV
resource of the dataset; a warning lists them if there are several).

## What happens per country

1. Zip: the first by name starting with `{iso3}_` / `{iso3}-` (more than one
   is flagged). The file used is logged.
2. Level files, admin 0 to target: `.shp` names containing `_admin{i}` or
   `_adm{i}` (not followed by another digit) and not `_em` (edge-matched).
   If several match, names starting with `{iso3}_` go first (e.g. MDG
   `mgd_op_adm1_old_names_pcodes.shp` is not used); flagged. If a level has
   no file, the whole country is skipped (no half hierarchy).
3. Fields: each output field is read from its source column (exact name,
   see `fields` in config). If any is missing, the country is skipped and
   the log lists the missing column, candidate columns and all columns, so
   you can map them in config (below). Shapes are reprojected to EPSG:4326
   if needed; geometries are not simplified nor repaired.
4. Target level only, P-codes against the global list: P-codes in the
   shapefile and not in the list, and the other way round, are always
   reported. With `pcode_master: global` (default) only P-codes of the list
   are shipped (drops e.g. "Conflict" / "Unspecified" shapes) and `Source`
   gets a note of what was not ingested. With `shapefile` all are shipped.
5. Checks on what is shipped: null/empty values per field, duplicated
   P-codes, invalid geometries, P-codes reused across levels, and the
   hierarchy from the target level up to admin 0 (parents referenced but
   missing = hierarchy broken, warning; units never referenced as parent =
   info).
6. Shape comparison of the target level against the config geojson (same
   method as `compare_simplified_*.ipynb`).

## Outputs

- `<name>_shp.zip`: `<name>_admin0.shp` ... `<name>_admin{max}.shp` together.
- `<name>.gpkg`: layers `admin0` ... with the same attributes.
- `reports/run_summary.csv`: one row per country (status, files used, units
  per level, P-codes not ingested, shapes match).
- `reports/issues.csv`: one row per finding (`severity`: error = country
  skipped, warning = shipped but check it, info).
- `reports/shape_comparison.csv`, `reports/shape_crosswalk.csv`: comparison
  per country and the units that do not match 1 to 1 with the same P-code.
- `reports/download_manifest.csv` (download runs), `logs/run_<time>.log`.

| Field | Content |
|---|---|
| `Pcode` | `adm{i}_pcode` |
| `Prnt_Pcode` | `adm{i-1}_pcode`, blank at admin 0 |
| `Name` | `adm{i}_name` |
| `Language` | `lang` |
| `ISO3` | from the config geojson |
| `Version` | `version` |
| `Valid_on` | `valid_on` as `YYYY-MM-DD` |
| `Source` | `OCHA COD-AB <HDX dataset url> (<zip>), accessed <date>`, plus at the target level, if P-codes were dropped: `Not ingested (not in <global-pcodes url> (<csv>), accessed <date>): <P-codes>`. Kept within 254 characters (shapefile limit): if the list does not fit, only the count, and the list stays in the reports. |

Field names are 10 characters at most (shapefile limit), hence `Prnt_Pcode`.

## Per-country exceptions (config, no code changes)

Only what differs from the defaults; `{i}` = level, `{p}` = parent level.

```yaml
countries:
  CMR:
    levels:
      2:
        fields: {Name: adm2_ref_n}       # one level only
  COL:
    fields:                              # all levels
      Pcode: "ADM{i}_PCODE"
      Prnt_Pcode: "ADM{p}_PCODE"
      Name: "ADM{i}_ES"
      Valid_on: validOn
    constants: {Language: es, Version: v01}   # only if the column is missing
  SOM:
    pcode_master: shapefile
    access_date: "2025-05-13"            # zip put in the shp-folder by hand
```

Output field names never clash with HDX column names, so pointing a field
to another column is enough (no rename / drop needed).

## Reading the shape comparison

Per country, against the config geojson (`ref`) and the harmonized target
level (`hdx`):

- `same_pcode`: same unit and P-code (the expected case; IoU below 1 comes
  from the geojson simplification).
- `pcode_changed` / `pcode_interchanged`: same shape, P-code changed or
  swapped between versions.
- `split` / `merge`: one geojson unit is several harmonized units, or the
  reverse.
- `complex`: different subdivision (n to m).
- `no_counterpart_ref` / `no_counterpart_hdx`: units only on one side (e.g.
  discarded during ingestion).
- boundary shifts: minor overlaps between neighbours, not structural.

When a country differs, either bring a new config geojson aligned with the
HDX version (WIA then follows the current COD-AB), or put in the
shp-folder the zip version matching the geojson and set its `access_date`.

## What stays your responsibility

- One zip per country in the shp-folder. A download that comes back under a
  new HDX file name leaves the old zip there too: the first by name is used
  and flagged, so remove the one you do not want.
- A zip you put by hand needs its `access_date` in config, or `Source` will
  carry `default_access_date`.
- The config geojson and the shp-folder zip are meant to be the same COD-AB
  version; the comparison tells you when they are not, it does not decide.
- `global_pcodes.csv` version: replace the file (or use `download_all`) and
  keep `global_pcodes_access_date` in line.
- Invalid geometries are reported, not fixed; check them if GeoSight
  complains.
