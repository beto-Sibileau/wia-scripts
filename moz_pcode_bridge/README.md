# moz_pcode_bridge

Bridge of Mozambique adm2 P-codes from the version used by the MSNA 2026
(HDX `cod-ab-moz` downloaded on 27 April 2026, 159 units, valid on
2019-06-07) to the current OCHA COD-AB on HDX (161 units, `v02`, valid on
2025-01-01), so that MSNA indicators can be joined to WIA computed on the
new P-codes.

```bash
python moz_pcode_bridge/build_bridge.py                       # area weights
python moz_pcode_bridge/build_bridge.py --piece-pop pop.csv   # population weights
```

Inputs (defaults, change with `--old-zip`, `--new-zip`, `--reach`, `--out`):
`data_in/geo_sight/cod_shapes/moz_v2025_v2026/` (April zip, REACH
`bridges_pcodes.xlsx`) and `hdx_shp_zip/moz_admin_boundaries.shp.zip`.
Outputs go to `data_out/geo_sight/moz_pcode_bridge/`.

## Four mappings

| Mapping | How | File / column |
|---|---|---|
| REACH | as shared (label based) | `comparison_old_view.csv: reach_new` |
| name | exact (case / accent insensitive), then fuzzy (similarity >= 0.80, "Cidade de" ignored), within the same province (provinces matched by shape), one to one | `name_new`, `name_match` |
| shape | best 1-to-1 assignment maximizing the total IoU (full resolution shapes) | `shape_new`, `shape_iou` |
| hybrid | per **new** unit: how to obtain its MSNA value | `bridge_hybrid.csv`, `bridge_hybrid_long.csv` |

## Hybrid categories (per new unit)

s = share of the new unit covered by an old unit, c = share of that old unit
inside the new unit. Overlaps below 15% of both units are boundary shifts
and ignored (as in `cod_ab_geosight`). "Same unit" = name match or REACH pair.

| Category | Rule | MSNA value |
|---|---|---|
| `equal` | one old unit, IoU >= 0.90, area within 10% | copy |
| `close` | s >= 0.80 and c >= 0.80 (within 20% both ways) | copy |
| `shrunk_copy` | s >= 0.80, 0.50 <= c < 0.80, same unit: lost territory to a neighbour | copy |
| `split_copy` | s >= 0.80 otherwise: one old unit split into several new ones | copy (imputed to each part) |
| `merge_weighted` | several old units, all wholly inside (c >= 0.80) | weighted mean |
| `expanded` | several old units, one is the same unit and lies wholly inside (anchor): gained territory from a neighbour | copy the anchor; population-weighted mean if `--piece-pop` |
| `complex_weighted` | parts of several old units, no anchor | weighted mean, low confidence |

Weights (`weight` in the long file) are area shares unless population per
piece is given. Area shares are a poor proxy where towns and rural areas mix
(e.g. Cidade de Lichinga would take 40% from the Lago Niassa polygon), hence
the anchor copy for `expanded` units by default.

## Population weights

`pieces.gpkg` holds every old x new intersection (`old_pcode`, `new_pcode`,
`km2`). Sum a population raster (e.g. WorldPop) per piece, save a csv
`old_pcode,new_pcode,pop`, and run with `--piece-pop`: `merge_weighted`,
`complex_weighted` and `expanded` then use population shares.

When an old unit has no MSNA value (e.g. the Lago Niassa polygon), drop its
rows in the long file and renormalize the weights of that new unit.

## Outputs

- `summary.md`: counts, agreement of the mappings, the non-trivial units
- `bridge_hybrid.csv`: one row per new unit (category, method, confidence,
  sources with weights, anchor, REACH old P-code, comparison with REACH)
- `bridge_hybrid_long.csv`: one row per new unit x old source (weights,
  shares, IoU), the table to join MSNA results with
- `comparison_old_view.csv`: one row per old unit: REACH / name / shape
  mappings, agreement, how the hybrid uses it
- `overlaps.csv`: all old x new overlaps; `pieces.gpkg`: their geometries
- `bridge_map.html`: new units colored by category, old boundaries as a
  layer (legend), hover shows the sources (needs internet for plotly.js)

## Label bridge (the mapping used for the MSNA)

```bash
python moz_pcode_bridge/label_bridge.py [--config other.yaml]
```

Matches adm2 names v1 -> v2 within the matching province (exact, then
fuzzy above `fuzzy_threshold`, default 0.90), after the manual decisions of
`label_bridge.yaml` (pairs with reason `name_fuzzy` / `shape_analysis`,
v1 units `not_in_v2`, notes). Two steps: run, read
`label_bridge_review.csv` (units without a match and their closest
candidate), add the decisions to the config, run again.

Outputs: `label_bridge_v1_v2.csv` (columns `adm1_pcode_v1` ...
`adm2_name_v2`, `match_reason`, `note`), `label_bridge_review.csv`,
`label_bridge_report.txt` (coverage, pairs vs REACH, name differences
REACH vs shapefiles).
