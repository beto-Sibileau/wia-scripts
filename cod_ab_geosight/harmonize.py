"""Harmonized attributes of an admin level and the Source text."""

from __future__ import annotations

import re

import geopandas as gpd
import pandas as pd

from .settings import HDX_DATASET, MAPPED_FIELDS, OUT_FIELDS, SHP_TEXT_LIMIT

# words to suggest candidate source columns when a mapped column is missing
HINT_TOKENS = {
    "Pcode": ["pcode", "pcod"],
    "Prnt_Pcode": ["pcode", "pcod"],
    "Name": ["name", "_en", "_fr", "_es", "_pt", "_ar", "ref"],
    "Language": ["lang"],
    "Version": ["vers"],
    "Valid_on": ["valid", "date"],
}


def as_text(s: pd.Series) -> pd.Series:
    """Values as stripped text; null / empty become None."""
    if pd.api.types.is_datetime64_any_dtype(s):
        out = s.dt.strftime("%Y-%m-%d")
    elif pd.api.types.is_float_dtype(s) and s.dropna().map(float.is_integer).all():
        out = s.astype("Int64").astype("string")  # 12.0 -> "12"
    else:
        out = s.astype("string")
    out = out.str.strip()
    return pd.Series(
        [None if pd.isna(v) or v == "" else v for v in out],
        index=s.index, dtype=object,
    )


def as_date_text(s: pd.Series) -> pd.Series:
    """Dates as YYYY-MM-DD text (date part of datetime-like text kept)."""
    out = as_text(s)
    return out.map(
        lambda v: v[:10] if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v)
        else v
    )


def harmonize_level(gdf: gpd.GeoDataFrame, level: int, iso3: str,
                    mapping: dict, constants: dict
                    ) -> tuple[gpd.GeoDataFrame | None, list[dict], dict]:
    """Output fields of one admin level from its HDX shapefile.

    Each field takes the mapped source column; if missing, the constant of
    config (Language / Version / Valid_on only). Prnt_Pcode is blank at
    admin 0. Source is filled later (empty here).

    Returns (harmonized GeoDataFrame or None, missing fields, sources used)
    where each missing field is {"field", "column", "candidates"} and
    sources used is {field: column name or "constant '<value>'"}.
    """
    data, missing, used = {}, [], {}
    for field in MAPPED_FIELDS:
        if field == "Prnt_Pcode" and level == 0:
            data[field] = pd.Series("", index=gdf.index, dtype=object)
            continue
        column = mapping[field]
        if column in gdf.columns:
            values = gdf[column]
            used[field] = column
        elif field in constants:
            values = pd.Series(constants[field], index=gdf.index)
            used[field] = f"constant '{constants[field]}'"
        else:
            missing.append({
                "field": field, "column": column,
                "candidates": column_candidates(gdf.columns, field, level),
            })
            continue
        data[field] = as_date_text(values) if field == "Valid_on" else as_text(values)

    if missing:
        return None, missing, used

    out = pd.DataFrame(data, index=gdf.index)
    out["ISO3"] = iso3
    out["Source"] = ""
    out = gpd.GeoDataFrame(out, geometry=gdf.geometry, crs=gdf.crs)
    return out[OUT_FIELDS + ["geometry"]].reset_index(drop=True), [], used


def column_candidates(columns, field: str, level: int) -> list[str]:
    """Columns that may hold a missing field (hint for config)."""
    tokens = HINT_TOKENS.get(field, [])
    level_tag = str(level - 1 if field == "Prnt_Pcode" else level)
    out = []
    for c in columns:
        low = c.lower()
        if c == "geometry" or not any(t in low for t in tokens):
            continue
        if field in ("Pcode", "Prnt_Pcode", "Name") and level_tag not in low:
            continue
        out.append(c)
    return out


def source_text(iso3: str, zip_name: str, access_date: str,
                dropped: list[str] | None = None,
                pcodes_file: str = "", pcodes_date: str = "",
                limit: int = SHP_TEXT_LIMIT) -> str:
    """Source field: HDX dataset, resource and access date of the country.

    With dropped P-codes (target level, not in the global list), a note is
    added with the global P-codes reference and the list of P-codes; if it
    does not fit in limit characters (shapefile), a shorter legend is used
    and the full list stays in the run reports.
    """
    main = (f"OCHA COD-AB {HDX_DATASET}/cod-ab-{iso3.lower()} ({zip_name}), "
            f"accessed {access_date}")
    if not dropped:
        return main[:limit]
    n = len(dropped)
    ref = (f"{HDX_DATASET}/global-pcodes ({pcodes_file}), "
           f"accessed {pcodes_date}")
    options = [
        f"{main}. Not ingested (not in {ref}): {', '.join(dropped)}",
        f"{main}. {n} P-codes not ingested (not in {ref}), see run report",
        f"{main}. {n} P-codes not ingested (not in global P-codes), see run report",
    ]
    for text in options:
        if len(text) <= limit:
            return text
    return options[-1][:limit]
