"""Downloads from HDX: COD-AB shapefile zips and the global P-codes list.

Supersedes download_cod_ab.py (wia-scripts root):
  - zips saved flat in one folder (the shp-folder), overwriting files with
    the same name (no ISO3 subfolders)
  - same resource filter: format SHP and name ending in
    "_admin_boundaries.shp.zip" or containing "administrative divisions"
  - additional download of the global P-codes list (first CSV resource of
    https://data.humdata.org/dataset/global-pcodes) into pcodes_dir
    (default: the same folder as the zips)

Used by the flow when run.download_all is true, or on its own:
    python -m cod_ab_geosight.download                 # config countries/folders
    python -m cod_ab_geosight.download --iso3 AFG SOM --out-dir some/folder
    python -m cod_ab_geosight.download --no-pcodes
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import requests

from .report import log

HDX_API = "https://data.humdata.org/api/3/action/package_show"
GLOBAL_PCODES_DATASET = "global-pcodes"
USER_AGENT = "wia-cod-ab-geosight/0.1"


def package_resources(dataset_id: str, session: requests.Session) -> list[dict]:
    """Resources (files) of an HDX dataset, from the CKAN API."""
    r = session.get(HDX_API, params={"id": dataset_id}, timeout=60)
    r.raise_for_status()
    return r.json()["result"]["resources"]


def cod_ab_shp_resources(resources: list[dict]) -> list[dict]:
    """SHP resources of a cod-ab-{iso3} dataset (admin boundaries zip)."""
    return [
        res for res in resources
        if "shp" in (res.get("format") or "").lower()
        and (
            (res.get("name") or "").lower().endswith("_admin_boundaries.shp.zip")
            or "administrative divisions" in (res.get("name") or "").lower()
        )
    ]


def csv_resources(resources: list[dict]) -> list[dict]:
    return [r for r in resources if (r.get("format") or "").lower() == "csv"]


def resource_filename(res: dict, fallback: str) -> str:
    return Path(res["url"].split("?")[0]).name or fallback


def download_file(url: str, dest: Path, session: requests.Session) -> int:
    """Stream url to dest (via a .part file, so a failed download does not
    replace an existing file). Returns bytes written."""
    tmp = dest.with_name(dest.name + ".part")
    with session.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest.stat().st_size


def download_cod_ab(iso3_list: list[str], out_dir: Path,
                    session: requests.Session) -> list[dict]:
    """COD-AB shapefile zip of each country into out_dir (overwrite).

    Returns manifest rows: iso3, file, bytes, status.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for iso3 in iso3_list:
        try:
            resources = cod_ab_shp_resources(
                package_resources(f"cod-ab-{iso3.lower()}", session)
            )
            if not resources:
                log.warning("%s: no SHP resource matching the filter on HDX", iso3)
                rows.append({"iso3": iso3, "file": "", "bytes": "",
                             "status": "no_shp_resource"})
                continue
            if len(resources) > 1:
                log.warning("%s: %d SHP resources on HDX (%s); all downloaded, "
                            "the flow uses the first zip by name",
                            iso3, len(resources),
                            ", ".join(r.get("name", "") for r in resources))
            for res in resources:
                name = resource_filename(res, f"{iso3.lower()}_shp.zip")
                size = download_file(res["url"], out_dir / name, session)
                log.info("%s: downloaded %s (%.1f MB)", iso3, name, size / 1e6)
                rows.append({"iso3": iso3, "file": name, "bytes": size,
                             "status": "ok"})
                time.sleep(0.5)  # be polite to HDX
        except Exception as exc:  # keep going; reported in the manifest
            log.error("%s: download failed: %s", iso3, exc)
            rows.append({"iso3": iso3, "file": "", "bytes": "",
                         "status": f"error: {exc}"})
    return rows


def download_global_pcodes(out_dir: Path, session: requests.Session) -> Path | None:
    """First CSV resource of the global-pcodes dataset into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        csvs = csv_resources(package_resources(GLOBAL_PCODES_DATASET, session))
    except Exception as exc:
        log.error("Global P-codes: HDX query failed: %s", exc)
        return None
    if not csvs:
        log.error("Global P-codes: no CSV resource in dataset %s",
                  GLOBAL_PCODES_DATASET)
        return None
    if len(csvs) > 1:
        log.warning("Global P-codes: %d CSV resources (%s); using the first",
                    len(csvs), ", ".join(r.get("name", "") for r in csvs))
    name = resource_filename(csvs[0], "global_pcodes.csv")
    try:
        size = download_file(csvs[0]["url"], out_dir / name, session)
    except Exception as exc:
        log.error("Global P-codes: download failed: %s", exc)
        return None
    log.info("Global P-codes: downloaded %s (%.1f MB)", name, size / 1e6)
    return out_dir / name


def download_all(iso3_list: list[str], shp_dir: Path,
                 pcodes_dir: Path | None = None,
                 with_pcodes: bool = True) -> tuple[list[dict], Path | None]:
    """COD-AB zips into shp_dir and global P-codes into pcodes_dir
    (default: shp_dir). Returns (manifest rows, path of the P-codes file)."""
    with requests.Session() as session:
        session.headers["User-Agent"] = USER_AGENT
        rows = download_cod_ab(iso3_list, shp_dir, session)
        pcodes = (
            download_global_pcodes(pcodes_dir or shp_dir, session)
            if with_pcodes else None
        )
    return rows, pcodes


def main(argv=None):
    import csv

    from .inputs import retrieve_config
    from .report import Issues, setup_logging
    from .settings import Settings

    parser = argparse.ArgumentParser(
        prog="python -m cod_ab_geosight.download",
        description="Download COD-AB shapefile zips and the global P-codes "
                    "list from HDX (defaults from config.yaml).",
    )
    parser.add_argument("--config", default=None, help="config.yaml path")
    parser.add_argument("--iso3", nargs="*", help="countries (default: config)")
    parser.add_argument("--out-dir", help="folder for the zips "
                        "(default: paths.hdx_shp_zip of config)")
    parser.add_argument("--pcodes-dir", help="folder for the global P-codes "
                        "(default: same as --out-dir if given, else the "
                        "folder of paths.global_pcodes of config)")
    parser.add_argument("--no-pcodes", action="store_true",
                        help="skip the global P-codes download")
    args = parser.parse_args(argv)

    settings = Settings(args.config)
    setup_logging(settings.output / "logs")
    iso3_list = [c.upper() for c in args.iso3] if args.iso3 else list(
        retrieve_config(settings.config_geojsons, Issues())
    )
    out_dir = Path(args.out_dir) if args.out_dir else settings.hdx_shp_zip
    pcodes_dir = (
        Path(args.pcodes_dir) if args.pcodes_dir
        else out_dir if args.out_dir else settings.global_pcodes.parent
    )
    rows, _ = download_all(iso3_list, out_dir, pcodes_dir, not args.no_pcodes)

    manifest = out_dir / "download_manifest.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["iso3", "file", "bytes", "status"])
        writer.writeheader()
        writer.writerows(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    log.info("Done: %d files for %d countries; see %s", ok, len(iso3_list), manifest)


if __name__ == "__main__":
    main()
