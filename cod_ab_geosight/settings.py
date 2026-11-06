"""Run settings from config.yaml: paths, dates, per-country exceptions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PACKAGE_DIR / "config.yaml"

HDX_DATASET = "https://data.humdata.org/dataset"

# fields of the harmonized output, in order (all <= 10 characters: DBF limit)
OUT_FIELDS = [
    "Pcode", "Prnt_Pcode", "Name", "Language", "ISO3", "Version", "Valid_on",
    "Source",
]
# fields read from a source column of the HDX shapefile
MAPPED_FIELDS = ["Pcode", "Prnt_Pcode", "Name", "Language", "Version", "Valid_on"]
# fields that may take a constant from config when the column is missing
CONSTANT_FIELDS = {"Language", "Version", "Valid_on"}
# max length of a text field in a shapefile (DBF)
SHP_TEXT_LIMIT = 254


class Settings:
    """Settings of one run, read from a config.yaml file.

    Paths in the file are relative to the folder of the file.
    """

    def __init__(self, path: str | Path | None = None,
                 download_all: bool | None = None):
        self.path = Path(path or DEFAULT_CONFIG).resolve()
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        base = self.path.parent

        paths = raw.get("paths") or {}
        self.config_geojsons = (base / paths["config_geojsons"]).resolve()
        self.hdx_shp_zip = (base / paths["hdx_shp_zip"]).resolve()
        self.global_pcodes = (base / paths["global_pcodes"]).resolve()
        self.output = (base / paths["output"]).resolve()

        run = raw.get("run") or {}
        self.download_all = bool(
            run.get("download_all", False) if download_all is None
            else download_all
        )
        self.default_access_date = str(
            run.get("default_access_date") or date.today().isoformat()
        )
        self.global_pcodes_access_date = str(
            run.get("global_pcodes_access_date") or self.default_access_date
        )
        self.default_pcode_master = run.get("pcode_master", "global")
        self.compare_shapes = bool(run.get("compare_shapes", True))

        out = raw.get("output") or {}
        self.output_name = out.get("name", "wia_cod_ab")
        self.write_gpkg = bool(out.get("write_gpkg", True))

        self.compare = {
            "iou_threshold": 0.90,
            "area_ratio_tol": 0.10,
            "edge_min_frac": 0.05,
            "boundary_shift_frac": 0.15,
            "equal_area_crs": "EPSG:6933",
            "max_rows": 10,
            **(raw.get("compare") or {}),
        }

        self.fields = {**{f: None for f in MAPPED_FIELDS}, **(raw.get("fields") or {})}
        self.countries = {
            str(k).upper(): (v or {}) for k, v in (raw.get("countries") or {}).items()
        }
        self.today = date.today().isoformat()
        # filled by the flow in download_all mode (what was actually downloaded)
        self.downloaded: set[str] = set()
        self.pcodes_downloaded = False
        self._validate()

    # ------------------------------------------------------------------
    def _validate(self):
        """Fail early on typos in config (field names, master values)."""
        missing = [f for f, v in self.fields.items() if not v]
        if missing:
            raise ValueError(f"config 'fields' without source column: {missing}")
        self._check_master(self.default_pcode_master, "run.pcode_master")
        for iso3, c in self.countries.items():
            if "pcode_master" in c:
                self._check_master(c["pcode_master"], f"countries.{iso3}")
            blocks = [(f"countries.{iso3}", c)] + [
                (f"countries.{iso3}.levels.{lv}", spec or {})
                for lv, spec in (c.get("levels") or {}).items()
            ]
            for where, block in blocks:
                bad = set(block.get("fields") or {}) - set(MAPPED_FIELDS)
                if bad:
                    raise ValueError(
                        f"{where}.fields: unknown output field(s) {sorted(bad)}; "
                        f"valid: {MAPPED_FIELDS}"
                    )
                bad = set(block.get("constants") or {}) - CONSTANT_FIELDS
                if bad:
                    raise ValueError(
                        f"{where}.constants: not allowed for {sorted(bad)}; "
                        f"valid: {sorted(CONSTANT_FIELDS)}"
                    )

    @staticmethod
    def _check_master(value, where):
        if value not in ("global", "shapefile"):
            raise ValueError(f"{where}: pcode_master must be 'global' or "
                             f"'shapefile', got {value!r}")

    # ------------------------------------------------------------------
    def access_date(self, iso3: str) -> str:
        """Access date of the country zip, for the Source field.

        Today if downloaded in this run; else the country access_date or
        default_access_date (also when a download_all download failed and
        the zip already in the shp-folder is used).
        """
        if self.download_all and iso3 in self.downloaded:
            return self.today
        return str(self.countries.get(iso3, {}).get("access_date")
                   or self.default_access_date)

    def pcodes_access_date(self) -> str:
        """Access date of the global P-codes file, for the Source field."""
        if self.download_all and self.pcodes_downloaded:
            return self.today
        return self.global_pcodes_access_date

    def pcode_master(self, iso3: str) -> str:
        return self.countries.get(iso3, {}).get(
            "pcode_master", self.default_pcode_master
        )

    def field_spec(self, iso3: str, level: int) -> tuple[dict, dict]:
        """(source column per output field, constants) of a country level.

        Precedence: level settings > country settings > default fields.
        {i} / {p} in column names become the level / parent level.
        """
        country = self.countries.get(iso3, {})
        levels = country.get("levels") or {}
        lv = levels.get(level) or levels.get(str(level)) or {}
        mapping = {
            **self.fields,
            **(country.get("fields") or {}),
            **(lv.get("fields") or {}),
        }
        constants = {
            **(country.get("constants") or {}),
            **(lv.get("constants") or {}),
        }
        mapping = {
            k: str(v).format(i=level, p=level - 1) for k, v in mapping.items()
        }
        if level == 0:
            mapping.pop("Prnt_Pcode", None)
        return mapping, {k: str(v) for k, v in constants.items()}

    def has_overridden_dates(self) -> list[str]:
        """Countries with an access_date override (ignored in download_all)."""
        return [k for k, v in self.countries.items() if v.get("access_date")]
