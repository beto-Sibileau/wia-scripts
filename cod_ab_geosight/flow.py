"""Harmonization flow: config geojsons -> per admin level outputs for GeoSight.

Steps (per country of the retrieved config):
  1. country zip in the shp-folder (optionally downloaded first)
  2. .shp of each admin level 0..target ('_admin{i}' / '_adm{i}', no '_em')
  3. output fields from the mapped source columns (country skipped if any
     is missing; columns found are printed)
  4. target level: P-codes against the global list (master: global or
     shapefile), both differences reported, Source note on dropped P-codes
  5. checks on the shipped data: nulls, duplicates, invalid geometries,
     hierarchy (target level up to admin 0), P-codes repeated across levels
  6. shape comparison of the target level against the config geojson
Then all countries are compiled per admin level and written (zip with one
shapefile per level, geopackage) with the reports of the run.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from . import checks
from .compare import compare, get_col, reconciliation_lines
from .download import download_all
from .harmonize import harmonize_level, source_text
from .inputs import (
    find_country_zip, global_pcode_set, load_global_pcodes, read_level,
    read_reference, retrieve_config, select_level_file, shp_members,
)
from .report import Issues, close_logging, log, preview, setup_logging
from .settings import OUT_FIELDS, Settings

WGS84 = "EPSG:4326"


def run(config: str | Path | None = None, download_all_override: bool | None = None
        ) -> dict:
    """Run the flow with a config.yaml (default: the one of the package).

    download_all_override: True / False to override run.download_all.
    Returns {"levels": {level: GeoDataFrame}, "summary": DataFrame,
             "issues": DataFrame, "comparison": DataFrame, "log": Path}.
    """
    settings = Settings(config, download_all=download_all_override)
    reports = settings.output / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    # outputs are large: keep them out of git (as in data_out/logs)
    ignore = settings.output / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Ignore everything\n*\n\n# Except this file\n"
                          "!.gitignore\n", encoding="utf-8")
    log_path = setup_logging(settings.output / "logs")
    try:
        return _run(settings, reports, log_path)
    finally:
        close_logging()


def _run(settings: Settings, reports: Path, log_path: Path) -> dict:
    issues = Issues()
    log.info("Config: %s", settings.path)
    log.info("Config geojsons: %s", settings.config_geojsons)
    log.info("Shp-folder: %s", settings.hdx_shp_zip)

    targets = retrieve_config(settings.config_geojsons, issues)
    log.info("Retrieved config: %d countries: %s", len(targets),
             ", ".join(f"{k} adm{v['level']}" for k, v in targets.items()))

    pcodes_path = settings.global_pcodes
    if settings.download_all:
        log.info("download_all: downloading %d countries and global P-codes "
                 "from HDX (access date %s)", len(targets), settings.today)
        rows, downloaded = download_all(
            list(targets), settings.hdx_shp_zip, settings.global_pcodes.parent
        )
        pd.DataFrame(rows).to_csv(reports / "download_manifest.csv", index=False)
        settings.downloaded = {r["iso3"] for r in rows if r["status"] == "ok"}
        ignored = [c for c in settings.has_overridden_dates()
                   if c in settings.downloaded]
        if ignored:
            log.warning("download_all: per-country access_date ignored for "
                        "%s (downloaded today)", ", ".join(ignored))
        failed = [iso3 for iso3 in targets if iso3 not in settings.downloaded]
        if failed:
            log.warning("download_all: no download for %s; the zips already in "
                        "the shp-folder are used (if any) with their configured "
                        "access date", ", ".join(failed))
            for iso3 in failed:
                issues.add(iso3, "", "download_failed", "warning",
                           detail="existing zip used with configured access date")
        if downloaded is not None:
            settings.pcodes_downloaded = True
            if downloaded.name != settings.global_pcodes.name:
                log.warning("Global P-codes downloaded as %s (config: %s); "
                            "using the downloaded file", downloaded.name,
                            settings.global_pcodes.name)
            pcodes_path = downloaded
        else:
            log.warning("Global P-codes not downloaded; using %s with its "
                        "configured access date", pcodes_path.name)

    gp = load_global_pcodes(pcodes_path)
    log.info("Global P-codes: %s (%d rows, accessed %s)", pcodes_path.name,
             len(gp), settings.pcodes_access_date())

    shipped: dict[str, dict[int, gpd.GeoDataFrame]] = {}
    summary, comparison, crosswalk = [], [], []
    for iso3, target in targets.items():
        log.info("=" * 70)
        row = {"iso3": iso3, "target_level": target["level"],
               "config_geojson": target["geojson"].name,
               "pcode_master": settings.pcode_master(iso3)}
        levels = process_country(
            iso3, target, settings, gp, pcodes_path.name, issues, row,
            comparison, crosswalk,
        )
        if levels is not None:
            shipped[iso3] = levels
        row["n_warning"] = issues.count(iso3, "warning")
        row["n_info"] = issues.count(iso3, "info")
        summary.append(row)

    log.info("=" * 70)
    compiled = compile_levels(shipped)

    summary_df = pd.DataFrame(summary)
    counts = sorted(c for c in summary_df.columns if c.startswith("n_adm"))
    for c in counts + ["n_not_ingested"]:
        if c in summary_df:
            summary_df[c] = summary_df[c].astype("Int64")
    first = ["iso3", "target_level", "status", "pcode_master", "zip",
             "access_date", "files", *counts, "n_not_ingested", "not_ingested",
             "shapes_match", "n_warning", "n_info", "config_geojson"]
    summary_df = summary_df[[c for c in first if c in summary_df]
                            + [c for c in summary_df if c not in first]]
    issues_df = issues.frame()
    comparison_df = pd.DataFrame(comparison)
    summary_df.to_csv(reports / "run_summary.csv", index=False)
    issues_df.to_csv(reports / "issues.csv", index=False)
    comparison_df.to_csv(reports / "shape_comparison.csv", index=False)
    (pd.concat(crosswalk, ignore_index=True) if crosswalk else pd.DataFrame()
     ).to_csv(reports / "shape_crosswalk.csv", index=False)

    try:
        outputs = write_outputs(compiled, settings)
    except Exception as exc:
        log.error("Writing outputs failed: %s (is a file open in QGIS / "
                  "being synced?); reports were written", exc)
        outputs = []

    log_final_summary(summary_df, issues_df, compiled, outputs, reports, log_path)
    return {"levels": compiled, "summary": summary_df, "issues": issues_df,
            "comparison": comparison_df, "log": log_path}


# ----------------------------------------------------------------------
def process_country(iso3, target, settings, gp, pcodes_file, issues, row,
                    comparison, crosswalk) -> dict[int, gpd.GeoDataFrame] | None:
    """Harmonized levels 0..target of one country, or None if skipped."""
    level_t = target["level"]
    log.info("%s: target adm%d (config geojson %s)", iso3, level_t,
             target["geojson"].name)

    def skip(reason, check, level="", item="", detail=""):
        log.error("%s: SKIPPED - %s", iso3, reason)
        issues.add(iso3, level, check, "error", item, detail or reason)
        row["status"] = f"skipped: {reason}"
        return None

    zip_path = find_country_zip(settings.hdx_shp_zip, iso3, issues)
    if zip_path is None:
        return skip("no zip in the shp-folder", "missing_zip")
    row["zip"] = zip_path.name
    access_date = settings.access_date(iso3)
    row["access_date"] = access_date
    log.info("%s: zip %s (accessed %s)", iso3, zip_path.name, access_date)

    # .shp per level
    members = shp_members(zip_path)
    files = {}
    for level in range(level_t + 1):
        selected, candidates = select_level_file(members, iso3, level)
        if selected is None:
            return skip(f"no .shp for adm{level} ('_admin{level}' / "
                        f"'_adm{level}', no '_em') in {zip_path.name}",
                        "missing_level_file", level,
                        detail=f".shp in zip: {', '.join(members)}")
        if len(candidates) > 1:
            log.warning("%s adm%d: %d candidate files (%s); using %s", iso3,
                        level, len(candidates), ", ".join(candidates), selected)
            issues.add(iso3, level, "multiple_level_files", "warning", selected,
                       f"candidates: {', '.join(candidates)}")
        files[level] = selected
        log.info("%s adm%d: reading %s", iso3, level, selected)
    row["files"] = "; ".join(f"adm{k}: {v}" for k, v in files.items())

    # harmonized fields per level
    levels, missing_any = {}, False
    for level, member in files.items():
        gdf = read_level(zip_path, member)
        if gdf.crs is None:
            log.warning("%s adm%d: no CRS in %s; assumed %s", iso3, level,
                        member, WGS84)
            issues.add(iso3, level, "crs_missing", "warning", member,
                       f"assumed {WGS84}")
            gdf = gdf.set_crs(WGS84)
        elif gdf.crs.to_epsg() != 4326:
            log.info("%s adm%d: reprojected %s -> %s", iso3, level,
                     gdf.crs.to_string(), WGS84)
            issues.add(iso3, level, "crs_reprojected", "info", member,
                       f"{gdf.crs.to_string()} -> {WGS84}")
            gdf = gdf.to_crs(WGS84)

        mapping, constants = settings.field_spec(iso3, level)
        out, missing, used = harmonize_level(gdf, level, iso3, mapping, constants)
        if missing:
            missing_any = True
            for m in missing:
                hint = (f"candidates: {', '.join(m['candidates'])}"
                        if m["candidates"] else "no candidate column")
                log.error("%s adm%d: column '%s' for %s not in %s (%s)", iso3,
                          level, m["column"], m["field"], member, hint)
                issues.add(iso3, level, "missing_column", "error",
                           f"{m['field']} <- {m['column']}", hint)
            log.error("%s adm%d: columns in %s: %s", iso3, level, member,
                      ", ".join(c for c in gdf.columns if c != "geometry"))
            continue
        if used != settings.field_spec("_", level)[0]:  # not the default columns
            log.info("%s adm%d: field sources %s", iso3, level, used)
        out["Source"] = source_text(iso3, zip_path.name, access_date)
        levels[level] = out
    if missing_any:
        return skip("standard column(s) missing; map them in config.yaml "
                    f"(countries: {iso3}: fields / constants)", "missing_columns")

    # target level: P-codes against the global list
    shp_set = set(levels[level_t]["Pcode"].dropna())
    glob_set = global_pcode_set(gp, iso3, level_t)
    in_shp_only = sorted(shp_set - glob_set)
    in_glob_only = sorted(glob_set - shp_set)
    master = settings.pcode_master(iso3)
    log.info("%s adm%d: P-codes %d in shapefile, %d in global list (master: %s)",
             iso3, level_t, len(shp_set), len(glob_set), master)
    if not glob_set and master == "global":
        return skip(f"no adm{level_t} P-codes for {iso3} in the global list; "
                    "set pcode_master: shapefile for this country to use the "
                    "shapefile P-codes", "no_global_pcodes", level_t)
    if in_shp_only:
        action = "not ingested" if master == "global" else "ingested (shapefile master)"
        log.warning("%s adm%d: %d P-codes in shapefile not in global list, %s: %s",
                    iso3, level_t, len(in_shp_only), action, preview(in_shp_only))
        issues.add_many(iso3, level_t, "pcode_in_shp_not_global", "warning",
                        in_shp_only, action)
    if in_glob_only:
        log.warning("%s adm%d: %d P-codes in global list not in shapefile: %s",
                    iso3, level_t, len(in_glob_only), preview(in_glob_only))
        issues.add_many(iso3, level_t, "pcode_in_global_not_shp", "warning",
                        in_glob_only, "no shape in the HDX file")
    dropped = []
    if master == "global" and in_shp_only:
        tgt = levels[level_t]
        dropped = in_shp_only
        n_rows = int((~tgt["Pcode"].isin(glob_set)).sum())
        levels[level_t] = tgt[tgt["Pcode"].isin(glob_set)].reset_index(drop=True).copy()
        log.info("%s adm%d: %d rows dropped (%d P-codes)", iso3, level_t,
                 n_rows, len(dropped))
        levels[level_t]["Source"] = source_text(
            iso3, zip_path.name, access_date, dropped, pcodes_file,
            settings.pcodes_access_date(),
        )
    row["n_not_ingested"] = len(dropped)
    row["not_ingested"] = ", ".join(dropped)

    run_checks(iso3, levels, issues)
    for level, g in levels.items():
        row[f"n_adm{level}"] = len(g)

    if settings.compare_shapes:
        compare_target(iso3, level_t, target["geojson"], levels[level_t],
                       settings.compare, issues, row, comparison, crosswalk)
    row["status"] = "ok"
    return levels


def run_checks(iso3, levels, issues):
    """Checks on the shipped data (fields written to the outputs)."""
    for level, g in levels.items():
        nulls = checks.null_counts(g, level)
        for field, n in nulls.items():
            log.warning("%s adm%d: %d null/empty values in %s", iso3, level, n, field)
            issues.add(iso3, level, "null_values", "warning", field,
                       f"{n} of {len(g)} rows")
        dups = checks.duplicated_pcodes(g)
        if dups:
            log.warning("%s adm%d: %d duplicated P-codes: %s", iso3, level,
                        len(dups), preview(dups))
            issues.add_many(iso3, level, "duplicated_pcode", "warning", dups,
                            "P-code in more than one row")
        invalid = checks.invalid_geometries(g)
        if invalid:
            log.info("%s adm%d: %d units with invalid/empty geometry (not "
                     "repaired): %s", iso3, level, len(invalid), preview(invalid))
            issues.add_many(iso3, level, "invalid_geometry", "info", invalid,
                            "invalid or empty geometry, not repaired")

    for h in checks.hierarchy(levels):
        k = h["level"]
        if h["missing_parent"]:
            log.warning("%s adm%d: %d parent P-codes not found at adm%d "
                        "(hierarchy broken): %s", iso3, k, len(h["missing_parent"]),
                        k - 1, preview(h["missing_parent"]))
            issues.add_many(iso3, k, "hierarchy_missing_parent", "warning",
                            h["missing_parent"],
                            f"Prnt_Pcode at adm{k} not a P-code at adm{k - 1}")
        if h["childless_parent"]:
            log.info("%s adm%d: %d units not referenced as parent at adm%d: %s",
                     iso3, k - 1, len(h["childless_parent"]), k,
                     preview(h["childless_parent"]))
            issues.add_many(iso3, k - 1, "hierarchy_childless_parent", "info",
                            h["childless_parent"],
                            f"P-code at adm{k - 1} not referenced at adm{k}")
        if not h["missing_parent"] and not h["childless_parent"]:
            log.info("%s adm%d -> adm%d: hierarchy complete", iso3, k, k - 1)

    for a, b, common in checks.repeated_across_levels(levels):
        log.info("%s: %d P-codes used at both adm%d and adm%d: %s", iso3,
                 len(common), a, b, preview(common))
        issues.add_many(iso3, f"{a},{b}", "pcode_repeated_across_levels", "info",
                        common, f"same P-code at adm{a} and adm{b}")


def compare_target(iso3, level, geojson, gdf_hdx, cfg, issues, row,
                   comparison, crosswalk):
    """Compare the harmonized target level with the config geojson."""
    try:
        ref = read_reference(geojson)
        pcode_ref = get_col(ref, f"adm{level}_pcode")
        if pcode_ref is None:
            log.warning("%s: column adm%d_pcode not in %s (columns: %s); "
                        "comparison skipped", iso3, level, geojson.name,
                        list(ref.columns))
            issues.add(iso3, level, "compare_skipped", "warning", geojson.name,
                       f"no adm{level}_pcode column")
            return
        if ref.crs is None:
            ref = ref.set_crs(WGS84)
        result = compare(ref.to_crs(WGS84), gdf_hdx, pcode_ref, "Pcode", cfg)
    except Exception as exc:
        log.error("%s: shape comparison failed: %s", iso3, exc)
        issues.add(iso3, level, "compare_failed", "warning", geojson.name, str(exc))
        return

    log.info("%s adm%d: shape comparison with %s:", iso3, level, geojson.name)
    for line in reconciliation_lines(result, cfg.get("max_rows", 10)):
        log.info("    %s", line)
    row["shapes_match"] = result["one_to_one"]
    if not result["one_to_one"]:
        counts = {r: result[r] for r in
                  ["pcode_changed", "pcode_interchanged", "split", "merge",
                   "complex", "no_counterpart_ref", "no_counterpart_hdx"]
                  if result[r]}
        issues.add(iso3, level, "shapes_differ", "warning", geojson.name,
                   "; ".join(f"{k}: {v}" for k, v in counts.items()))
    comparison.append({
        "iso3": iso3, "level": level, "config_geojson": geojson.name,
        **{k: v for k, v in result.items()
           if k not in ("cw", "shifts", "flagged", "only_ref", "only_hdx")},
        "only_ref": ", ".join(result["only_ref"]),
        "only_hdx": ", ".join(result["only_hdx"]),
    })
    cw = result["cw"]
    cw = cw[cw["relation"] != "same_pcode"]
    shifts = result["shifts"].assign(relation="boundary_shift")
    parts = [p for p in (cw, shifts) if len(p)]
    if parts:
        crosswalk.append(pd.concat(parts, ignore_index=True)
                         .assign(iso3=iso3, level=level))


# ----------------------------------------------------------------------
def compile_levels(shipped: dict[str, dict[int, gpd.GeoDataFrame]]
                   ) -> dict[int, gpd.GeoDataFrame]:
    """All countries per admin level (countries reaching that level)."""
    max_level = max((max(v) for v in shipped.values()), default=-1)
    compiled = {}
    for level in range(max_level + 1):
        parts = [lv[level] for lv in shipped.values() if level in lv]
        if parts:
            g = pd.concat(parts, ignore_index=True)
            g = gpd.GeoDataFrame(g, geometry="geometry", crs=WGS84)
            compiled[level] = g.sort_values(["ISO3", "Pcode"]).reset_index(drop=True)
    return compiled


def write_outputs(compiled: dict[int, gpd.GeoDataFrame], settings: Settings
                  ) -> list[Path]:
    """Zip with one shapefile per level, and the geopackage (optional)."""
    out_dir = settings.output
    out_dir.mkdir(parents=True, exist_ok=True)
    name = settings.output_name
    written = []
    if not compiled:
        log.error("Nothing to write: no country harmonized")
        return written

    zip_path = out_dir / f"{name}_shp.zip"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for level, g in compiled.items():
            g[OUT_FIELDS + ["geometry"]].to_file(
                tmp / f"{name}_admin{level}.shp", engine="pyogrio",
                encoding="UTF-8",
            )
        tmp_zip = tmp / "out.zip"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(tmp.iterdir()):
                if f != tmp_zip:
                    zf.write(f, arcname=f.name)
        shutil.copyfile(tmp_zip, zip_path)
    written.append(zip_path)

    if settings.write_gpkg:
        # written in a temp folder then copied (new file each run, so no
        # stale layers; SQLite is not happy writing on synced/mounted folders)
        gpkg = out_dir / f"{name}.gpkg"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_gpkg = Path(tmp) / gpkg.name
            for level, g in compiled.items():
                g[OUT_FIELDS + ["geometry"]].to_file(
                    tmp_gpkg, layer=f"admin{level}", driver="GPKG",
                    engine="pyogrio", promote_to_multi=True,
                )
            for suffix in ("-journal", "-wal", "-shm"):
                stale = gpkg.with_name(gpkg.name + suffix)
                if stale.exists():
                    try:
                        stale.unlink()
                    except OSError as exc:
                        log.warning("Could not remove %s (%s); delete it before "
                                    "opening the geopackage", stale.name, exc)
            shutil.copyfile(tmp_gpkg, gpkg)
        written.append(gpkg)
    return written


def log_final_summary(summary, issues, compiled, outputs, reports, log_path):
    ok = summary[summary["status"] == "ok"]
    skipped = summary[summary["status"] != "ok"]
    log.info("SUMMARY")
    log.info("Countries harmonized: %d of %d", len(ok), len(summary))
    for _, r in skipped.iterrows():
        log.info("  skipped %s: %s", r["iso3"], r["status"].replace("skipped: ", ""))
    for level, g in compiled.items():
        log.info("adm%d: %d units, %d countries", level, len(g), g["ISO3"].nunique())
    if len(issues):
        counts = issues.groupby(["severity", "check"]).size()
        log.info("Issues by check (rows in issues.csv):")
        for (sev, check), n in counts.items():
            log.info("  %-7s %-30s %d", sev, check, n)
    if "shapes_match" in summary:
        diff = summary.loc[summary["shapes_match"] == False, "iso3"].tolist()  # noqa: E712
        log.info("Target level same as config geojson: %d countries; differ: %s",
                 int((summary["shapes_match"] == True).sum()),  # noqa: E712
                 ", ".join(diff) or "none")
    for p in outputs:
        log.info("Output: %s", p)
    log.info("Reports: %s", reports)
    log.info("Log: %s", log_path)
