"""Shape comparison: config geojson (reference) vs harmonized target level.

Same method as compare_simplified_geojson.ipynb / compare_simplified_vs_gdb
.ipynb, with "s" (simplified) renamed "ref" (config geojson used for WIA)
and "o" (original / reference) renamed "hdx" (harmonized HDX shapes):

  1. units dissolved by P-code, valid geometries, equal area CRS
  2. overlaps between units of both files (>= edge_min_frac of the smaller)
  3. overlaps below boundary_shift_frac (both units) = boundary shifts
  4. connected overlaps classified as relations:
     same_pcode / pcode_changed / pcode_interchanged  (1 to 1)
     split    1 ref unit is n hdx units
     merge    n ref units are 1 hdx unit
     complex  n to m (different subdivision)
     no_counterpart_ref / no_counterpart_hdx (no overlap in the other file,
     e.g. discarded during ingestion)
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

REL_ORDER = [
    "same_pcode", "pcode_changed", "pcode_interchanged", "split", "merge",
    "complex", "no_counterpart_ref", "no_counterpart_hdx",
]
ONE_TO_ONE = ["same_pcode", "pcode_changed", "pcode_interchanged"]


def get_col(gdf, name):
    """Column in gdf matching name case insensitive (None if missing)."""
    found = [c for c in gdf.columns if c.lower() == name.lower()]
    return found[0] if found else None


def prepare(gdf, pcode_col, equal_area_crs):
    """Valid geometries dissolved by pcode, in equal area CRS."""
    gdf = gdf[[pcode_col, "geometry"]].copy()
    gdf[pcode_col] = gdf[pcode_col].astype(str).str.strip()
    gdf["geometry"] = gdf.make_valid()
    return gdf.dissolve(by=pcode_col).to_crs(equal_area_crs)


def overlap_table(gdf_ref, gdf_hdx, pcode_ref, pcode_hdx, cfg):
    """Overlaps between units of both files (equal area CRS).

    Keeps pairs where the intersection is at least edge_min_frac of the
    smaller unit. Returns edges and the pcodes of each file.
    """
    geo_ref = prepare(gdf_ref, pcode_ref, cfg["equal_area_crs"]).geometry
    geo_hdx = prepare(gdf_hdx, pcode_hdx, cfg["equal_area_crs"]).geometry

    idx_r, idx_h = geo_hdx.sindex.query(geo_ref.values, predicate="intersects")
    inter = geo_ref.iloc[idx_r].intersection(
        geo_hdx.iloc[idx_h], align=False
    ).area.to_numpy()
    area_r = geo_ref.area.to_numpy()[idx_r]
    area_h = geo_hdx.area.to_numpy()[idx_h]

    edges = pd.DataFrame({
        "pcode_ref": geo_ref.index[idx_r],
        "pcode_hdx": geo_hdx.index[idx_h],
        "iou": inter / (area_r + area_h - inter),
        "frac_ref": inter / area_r,
        "frac_hdx": inter / area_h,
        "area_ratio": area_r / area_h,
    })
    edges = edges[
        edges[["frac_ref", "frac_hdx"]].max(axis=1) >= cfg["edge_min_frac"]
    ].reset_index(drop=True)
    return edges, list(geo_ref.index), list(geo_hdx.index)


def classify_relations(edges, pcodes_ref, pcodes_hdx, cfg):
    """Crosswalk of units across files from connected overlaps.

    Overlaps below boundary_shift_frac (both fractions) are not used for
    relations; returned as a separate table of boundary shifts.
    """
    shift = edges[["frac_ref", "frac_hdx"]].max(axis=1) < cfg["boundary_shift_frac"]
    shifts = edges[shift].reset_index(drop=True)
    edges = edges[~shift].reset_index(drop=True)

    node_r = {p: i for i, p in enumerate(pcodes_ref)}
    node_h = {p: len(pcodes_ref) + i for i, p in enumerate(pcodes_hdx)}
    n_nodes = len(pcodes_ref) + len(pcodes_hdx)
    rows = edges["pcode_ref"].map(node_r).to_numpy(dtype=int)
    cols = edges["pcode_hdx"].map(node_h).to_numpy(dtype=int)
    _, labels = connected_components(
        coo_matrix((np.ones(len(edges)), (rows, cols)), shape=(n_nodes, n_nodes)),
        directed=False,
    )

    cw = edges.assign(component=labels[rows])
    n_r = cw.groupby("component")["pcode_ref"].transform("nunique")
    n_h = cw.groupby("component")["pcode_hdx"].transform("nunique")
    cw["relation"] = np.select(
        [(n_r == 1) & (n_h == 1), n_r == 1, n_h == 1],
        ["one_to_one", "split", "merge"],
        default="complex",
    )

    # one to one: same pcode, changed, or interchanged (pcodes reused)
    one = cw["relation"] == "one_to_one"
    changed = one & (cw["pcode_ref"] != cw["pcode_hdx"])
    cw.loc[one, "relation"] = "same_pcode"
    cw.loc[changed, "relation"] = "pcode_changed"
    interchanged = (
        changed
        & cw["pcode_ref"].isin(cw.loc[changed, "pcode_hdx"])
        & cw["pcode_hdx"].isin(cw.loc[changed, "pcode_ref"])
    )
    cw.loc[interchanged, "relation"] = "pcode_interchanged"

    # units without overlap in the other file
    lone_r = sorted(set(pcodes_ref) - set(cw["pcode_ref"]))
    lone_h = sorted(set(pcodes_hdx) - set(cw["pcode_hdx"]))
    lone = pd.concat([
        pd.DataFrame({"pcode_ref": lone_r, "relation": "no_counterpart_ref"}),
        pd.DataFrame({"pcode_hdx": lone_h, "relation": "no_counterpart_hdx"}),
    ], ignore_index=True)
    lone["component"] = labels.max() + 1 + np.arange(len(lone))

    cw = pd.concat([cw, lone], ignore_index=True).sort_values(
        ["relation", "component"]
    ).reset_index(drop=True)
    return cw, shifts


def compare(gdf_ref, gdf_hdx, pcode_ref, pcode_hdx, cfg):
    """Compare reference shapes against harmonized shapes (same country).

    Returns metrics, crosswalk (cw) and boundary shifts of the comparison.
    """
    set_r = set(gdf_ref[pcode_ref].astype(str).str.strip())
    set_h = set(gdf_hdx[pcode_hdx].astype(str).str.strip())

    edges, all_r, all_h = overlap_table(gdf_ref, gdf_hdx, pcode_ref, pcode_hdx, cfg)
    cw, shifts = classify_relations(edges, all_r, all_h, cfg)
    rel_counts = cw.groupby("relation")["component"].nunique()

    # shape similarity for 1 to 1 matches (same shape, regardless of pcode)
    res = cw[cw["relation"].isin(ONE_TO_ONE)]
    flagged = res[
        (res["iou"] < cfg["iou_threshold"])
        | ((res["area_ratio"] - 1).abs() > cfg["area_ratio_tol"])
    ]
    return {
        "n_ref": len(set_r),
        "n_hdx": len(set_h),
        "pcodes_equal": set_r == set_h,
        "only_ref": sorted(set_r - set_h),
        "only_hdx": sorted(set_h - set_r),
        **{r: int(rel_counts.get(r, 0)) for r in REL_ORDER},
        "n_boundary_shift": len(shifts),
        "median_iou": res["iou"].median() if len(res) else np.nan,
        "min_iou": res["iou"].min() if len(res) else np.nan,
        "n_flagged": len(flagged),
        "flagged": flagged,
        "one_to_one": len(cw) > 0 and bool((cw["relation"] == "same_pcode").all()),
        "cw": cw,
        "shifts": shifts,
    }


def reconciliation_lines(result, max_rows=10) -> list[str]:
    """Explanation of the differences found, as lines for the log."""
    cw, shifts = result["cw"], result["shifts"]
    if result["one_to_one"]:
        lines = [f"Same units and P-codes in both files ({result['n_ref']}, "
                 f"all 1 to 1 with the same P-code)"]
    else:
        lines = [
            f"P-codes: {result['n_ref']} in config geojson, {result['n_hdx']} "
            f"harmonized; only in geojson: {len(result['only_ref'])}, only "
            f"harmonized: {len(result['only_hdx'])}"
        ]
    res = cw[cw["relation"].isin(ONE_TO_ONE)]
    if len(res):
        lines.append(
            f"1 to 1 IoU: median {result['median_iou']:.4f}, min "
            f"{result['min_iou']:.4f}; flagged (IoU or area): {result['n_flagged']}"
        )
    cases = [
        ("pcode_changed", "Same shape, different P-code (geojson P-code vs "
         "harmonized P-code): recode one side with this mapping",
         ["pcode_ref", "pcode_hdx", "iou"]),
        ("pcode_interchanged", "Same shapes, P-codes interchanged between "
         "units: recode one side with this mapping",
         ["pcode_ref", "pcode_hdx", "iou"]),
        ("split", "Split: one geojson unit is several harmonized units "
         "(frac_hdx ~ share of pcode_hdx in pcode_ref)",
         ["component", "pcode_ref", "pcode_hdx", "frac_ref", "frac_hdx"]),
        ("merge", "Merge: several geojson units are one harmonized unit "
         "(frac_ref ~ share of pcode_ref in pcode_hdx)",
         ["component", "pcode_ref", "pcode_hdx", "frac_ref", "frac_hdx"]),
        ("complex", "Different subdivision (n to m): no clean crosswalk, "
         "manual review (area fractions as allocation weights)",
         ["component", "pcode_ref", "pcode_hdx", "frac_ref", "frac_hdx"]),
        ("no_counterpart_ref", "Units only in the config geojson (no overlap "
         "in harmonized, e.g. discarded during ingestion)", ["pcode_ref"]),
        ("no_counterpart_hdx", "Units only in harmonized (no overlap in the "
         "config geojson)", ["pcode_hdx"]),
        ("shifts", "Boundary shifts between neighbours (not structural)",
         ["pcode_ref", "pcode_hdx", "frac_ref", "frac_hdx"]),
    ]
    for relation, text, cols in cases:
        sub = shifts if relation == "shifts" else cw[cw["relation"] == relation]
        if sub.empty:
            continue
        if relation == "shifts" and result["one_to_one"]:
            lines.append(f"{text}: {len(sub)} (details in shape_crosswalk.csv)")
            continue
        lines.append(f"{text} [{len(sub)} rows]")
        table = sub[cols].round(3).head(max_rows).to_string(index=False)
        lines.extend("    " + t for t in table.splitlines())
        if len(sub) > max_rows:
            lines.append(f"    ... {len(sub) - max_rows} more rows (shape_crosswalk.csv)")
    if len(result["flagged"]):
        lines.append("1 to 1 matches flagged (IoU < threshold or area difference):")
        table = result["flagged"].sort_values("iou")[
            ["pcode_ref", "pcode_hdx", "iou", "area_ratio"]
        ].round(4).head(max_rows).to_string(index=False)
        lines.extend("    " + t for t in table.splitlines())
    return lines
