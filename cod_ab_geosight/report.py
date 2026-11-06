"""Logging (console + run log file) and the issues table of a run."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger("cod_ab_geosight")


def setup_logging(log_dir: Path) -> Path:
    """Log INFO and above to the console and to logs/run_<timestamp>.log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run_{datetime.now():%Y%m%dT%H%M%S}.log"
    log.setLevel(logging.INFO)
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(file_handler)
    log.addHandler(console)
    log.propagate = False
    return path


def close_logging():
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


class Issues:
    """Findings of the checks, one row per item (reports/issues.csv).

    severity:
      error   - country skipped (nothing shipped for it)
      warning - shipped, but worth a look before uploading to GeoSight
      info    - informative only
    """

    columns = ["iso3", "level", "check", "severity", "item", "detail"]

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, iso3, level, check, severity, item="", detail=""):
        self.rows.append({
            "iso3": iso3, "level": level, "check": check,
            "severity": severity, "item": item, "detail": detail,
        })

    def add_many(self, iso3, level, check, severity, items, detail=""):
        for item in items:
            self.add(iso3, level, check, severity, item, detail)

    def count(self, iso3, severity) -> int:
        return sum(
            1 for r in self.rows if r["iso3"] == iso3 and r["severity"] == severity
        )

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)


def preview(items, n=10) -> str:
    """First n items joined, with the count of the rest."""
    items = list(items)
    text = ", ".join(map(str, items[:n]))
    return text + (f" ... (+{len(items) - n} more)" if len(items) > n else "")
