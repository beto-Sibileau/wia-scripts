"""Checks on the harmonized (shipped) data of a country."""

from __future__ import annotations

import geopandas as gpd

from .settings import OUT_FIELDS


def null_counts(gdf: gpd.GeoDataFrame, level: int) -> dict[str, int]:
    """Null / empty values per shipped field (Prnt_Pcode blank at admin 0)."""
    counts = {}
    for field in OUT_FIELDS:
        if field == "Prnt_Pcode" and level == 0:
            continue
        values = gdf[field]
        n = int(values.isna().sum() + (values.astype(str).str.strip() == "").sum())
        if n:
            counts[field] = n
    return counts


def duplicated_pcodes(gdf: gpd.GeoDataFrame) -> list[str]:
    """P-codes appearing in more than one row (one unit per P-code expected)."""
    pcodes = gdf["Pcode"].dropna()
    return sorted(pcodes[pcodes.duplicated()].unique())


def invalid_geometries(gdf: gpd.GeoDataFrame) -> list[str]:
    """P-codes of rows with invalid or empty geometry (not repaired)."""
    bad = ~gdf.geometry.is_valid | gdf.geometry.is_empty | gdf.geometry.isna()
    return sorted(gdf.loc[bad, "Pcode"].astype(str).unique())


def hierarchy(levels: dict[int, gpd.GeoDataFrame]) -> list[dict]:
    """Parent references between consecutive levels, from target up to admin 0.

    For each level k >= 1, the parents referenced (unique Prnt_Pcode) are
    compared with the P-codes of level k - 1:
      missing_parent:  referenced at k but not a unit of k - 1 (breaks the
                       hierarchy in GeoSight)
      childless_parent: unit of k - 1 not referenced at k (informative)
    """
    out = []
    for k in sorted(levels, reverse=True):
        if k == 0 or (k - 1) not in levels:
            continue
        referenced = set(levels[k]["Prnt_Pcode"].dropna()) - {""}
        parents = set(levels[k - 1]["Pcode"].dropna())
        out.append({
            "level": k,
            "missing_parent": sorted(referenced - parents),
            "childless_parent": sorted(parents - referenced),
        })
    return out


def repeated_across_levels(levels: dict[int, gpd.GeoDataFrame]
                           ) -> list[tuple[int, int, list[str]]]:
    """P-codes used at two different levels of the same country."""
    sets = {k: set(g["Pcode"].dropna()) for k, g in levels.items()}
    out = []
    keys = sorted(sets)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            common = sorted(sets[a] & sets[b])
            if common:
                out.append((a, b, common))
    return out
