"""Inputs: retrieved config (geojson zips), country zips, admin level files."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .report import Issues, log

# geojson_{ISO3}_adm{i}*.zip (e.g. geojson_AFG_adm2_simpl35.zip)
GEOJSON_ZIP = re.compile(r"^geojson_([A-Za-z]{3})_adm(\d+).*\.zip$", re.IGNORECASE)


def retrieve_config(folder: Path, issues: Issues) -> dict[str, dict]:
    """Countries and target admin level from the geojson zips in folder.

    Only files at the top level of folder are read. If a country has more
    than one file, the highest admin level is used (flagged); if more than
    one file at that level, the first by name (flagged).

    Returns {iso3: {"level": int, "geojson": Path}} sorted by iso3.
    """
    found: dict[str, list[tuple[int, Path]]] = {}
    for f in sorted(Path(folder).iterdir()):
        parsed = GEOJSON_ZIP.match(f.name) if f.is_file() else None
        if parsed:
            found.setdefault(parsed.group(1).upper(), []).append(
                (int(parsed.group(2)), f)
            )

    config = {}
    for iso3, items in sorted(found.items()):
        level = max(lv for lv, _ in items)
        at_level = [f for lv, f in items if lv == level]
        if len(items) > 1:
            names = ", ".join(f.name for _, f in items)
            log.warning("%s: %d config geojsons (%s); using adm%d, %s",
                        iso3, len(items), names, level, at_level[0].name)
            issues.add(iso3, level, "config_multiple_geojson", "warning",
                       at_level[0].name, f"all files: {names}")
        config[iso3] = {"level": level, "geojson": at_level[0]}
    return config


def find_country_zip(folder: Path, iso3: str, issues: Issues) -> Path | None:
    """Zip in the shp-folder for iso3 (name starting with iso3 + '_' or '-').

    If several, the first by name is used (flagged).
    """
    pattern = re.compile(rf"^{iso3}[_-].*\.zip$", re.IGNORECASE)
    matches = sorted(
        f for f in Path(folder).iterdir() if f.is_file() and pattern.match(f.name)
    )
    if len(matches) > 1:
        names = ", ".join(f.name for f in matches)
        log.warning("%s: %d zips in shp-folder (%s); using %s",
                    iso3, len(matches), names, matches[0].name)
        issues.add(iso3, "", "multiple_zips", "warning", matches[0].name,
                   f"all zips: {names}")
    return matches[0] if matches else None


def shp_members(zip_path: Path) -> list[str]:
    """Paths of the .shp files inside a zip (subfolders included)."""
    with zipfile.ZipFile(zip_path) as zf:
        return [n for n in zf.namelist() if n.lower().endswith(".shp")]


def select_level_file(members: list[str], iso3: str, level: int
                      ) -> tuple[str | None, list[str]]:
    """.shp of an admin level: name with '_admin{i}' or '_adm{i}', no '_em'.

    The level number must not be followed by another digit (adm1 != adm10).
    If several match, names starting with '{iso3}_' go first, then by name;
    the first is used. Returns (selected or None, all candidates).
    """
    pattern = re.compile(rf"_adm(in)?{level}(?!\d)", re.IGNORECASE)
    candidates = [
        m for m in members
        if pattern.search(Path(m).name) and "_em" not in Path(m).name.lower()
    ]
    prefix = f"{iso3.lower()}_"
    candidates.sort(key=lambda m: (not Path(m).name.lower().startswith(prefix), m))
    return (candidates[0] if candidates else None), candidates


def vsizip(zip_path: Path, member: str) -> str:
    """GDAL path of a file inside a zip (read without extracting)."""
    return f"/vsizip/{Path(zip_path).as_posix()}/{member}"


def read_level(zip_path: Path, member: str) -> gpd.GeoDataFrame:
    return gpd.read_file(vsizip(zip_path, member), engine="pyogrio")


def read_reference(zip_path: Path) -> gpd.GeoDataFrame:
    """GeoDataFrame from the (first) geojson inside a config geojson zip."""
    with zipfile.ZipFile(zip_path) as zf:
        member = [n for n in zf.namelist() if n.lower().endswith(".geojson")][0]
        return gpd.read_file(io.BytesIO(zf.read(member)))


def load_global_pcodes(path: Path) -> pd.DataFrame:
    """Global P-codes list: Location (iso3), Admin Level, P-Code columns."""
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    needed = ["Location", "Admin Level", "P-Code"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(
            f"Global P-codes file {path.name} misses columns {missing}; "
            f"columns found: {list(df.columns)}"
        )
    for c in needed:
        df[c] = df[c].str.strip()
    return df


def global_pcode_set(gp: pd.DataFrame, iso3: str, level: int) -> set[str]:
    rows = (gp["Location"].str.upper() == iso3) & (gp["Admin Level"] == str(level))
    return set(gp.loc[rows, "P-Code"].dropna())
