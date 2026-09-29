"""Batch-run (extract + postprocess) notebooks for a list of countries.

Each notebook is executed via papermill with `country` injected as a
parameter (see the cell tagged "parameters" in each notebook). No executed
notebook is kept: papermill needs an output path, so each run is written to a
scratch file that is deleted right after, whether the run succeeded or
failed. The durable trace of a run is what the notebooks already write to
disk today: TIFFs, xlsx/PNG/HTML outputs, and the run-log JSON files under
data_out/logs/.

Usage: python run_extract_post_batch.py
"""

import tempfile
from pathlib import Path
from dataclasses import dataclass

import papermill as pm
from papermill.exceptions import PapermillExecutionError


@dataclass(frozen=True)
class RunConfig:
    country: str
    admin_level: int


# List of tasks. Duplicates of 'country' are perfectly fine here.
CONFIGS = [
    RunConfig("Afghanistan", 2),
    RunConfig("Burkina Faso", 2),
    RunConfig("Central African Republic", 2),
    RunConfig("Congo, The Democratic Republic of the", 2),
    RunConfig("Colombia", 2),
    RunConfig("Haiti", 1),
    RunConfig("Haiti", 2),
    RunConfig("Lebanon", 2),
    RunConfig("Lebanon", 3),
    RunConfig("Madagascar", 2),
    RunConfig("Mali", 1),
    RunConfig("Mali", 2),
    RunConfig("Myanmar", 2),
    RunConfig("Myanmar", 3),
    RunConfig("Mozambique", 2),
    RunConfig("State of Palestine", 2),
    RunConfig("Sudan", 2),
    RunConfig("Somalia", 2),
    RunConfig("South Sudan", 2),
    RunConfig("Ukraine", 2),
    RunConfig("Venezuela", 2),
    RunConfig("Yemen", 2),
]

NOTEBOOK_EXT = "SPEI_impact.ipynb"
NOTEBOOK_POST = "postprocess_SPEI.ipynb"

# minimum admin level per country, fine for small lists O(n²)
# allows extractions using the lowest admin level in country shapes load
mins = {
    country: min(c.admin_level for c in CONFIGS if c.country == country)
    for country in {c.country for c in CONFIGS}
}


def run_notebook(phase: str, notebook: str, country: str, admin_level: int) -> bool:
    """Execute one notebook with papermill, print the outcome, return success.

    All START / DONE / FAILED reporting lives here, so both phases share the
    same error handling. Returns True if the run succeeded, False otherwise.
    """
    label = f"{country}, L{admin_level}"
    print(f"[START {phase}] {label}")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            scratch_path = Path(tmp_dir) / f"executed_{notebook}"
            pm.execute_notebook(
                notebook,
                str(scratch_path),
                parameters={"country": country, "admin_level": admin_level},
                progress_bar=False,
            )
    except PapermillExecutionError as e:
        print(
            f"[FAILED {phase}] {label} — {notebook}, "
            f"cell #{e.cell_index}: {e.ename}: {e.evalue}"
        )
        return False
    except Exception as e:
        print(f"[FAILED {phase}] {label} — {notebook}, {type(e).__name__}: {e}")
        return False

    print(f"[DONE {phase}] {label}")
    return True


def main() -> None:
    # Defined up-front so Phase 2 also works when extraction is switched off
    # (NOTEBOOK_EXT empty/None): no country is then considered failed.
    failed_countries = set()

    # ==========================================
    # PHASE 1: Extraction (Once per unique country)
    # notebook load admin_level shapes but extraction is raster-based
    # ==========================================
    if NOTEBOOK_EXT:
        print("--- PHASE 1: Running Extraction Notebooks ---")

        # the mins keys become the unique countries in CONFIGS
        for country in sorted(mins.keys()):
            if not run_notebook("EXTRACTION", NOTEBOOK_EXT, country, mins[country]):
                failed_countries.add(country)

    # ==========================================
    # PHASE 2: Postprocess (Run for every country/admin_level combo)
    # ==========================================
    print("\n--- PHASE 2: Running Postprocess Notebooks ---")

    for config in CONFIGS:
        # Skip postprocessing if country extraction failed
        if config.country in failed_countries:
            print(
                f"[SKIP POST] {config.country}, L{config.admin_level} — extraction failed"
            )
            continue

        run_notebook("POST", NOTEBOOK_POST, config.country, config.admin_level)


if __name__ == "__main__":
    main()
