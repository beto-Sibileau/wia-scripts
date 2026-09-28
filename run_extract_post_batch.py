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

# minimum admin level per country, fine for small lists O(n²)
# allows extractions using the lowest admin level in country shapes load
mins = {
    country: min(c.admin_level for c in CONFIGS if c.country == country)
    for country in {c.country for c in CONFIGS}
}

NOTEBOOK_EXT = "SPEI_impact.ipynb"
NOTEBOOK_POST = "postprocess_SPEI.ipynb"


def run_notebook(notebook: str, country: str, admin_level: int) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        scratch_path = Path(tmp_dir) / f"executed_{notebook}"
        pm.execute_notebook(
            notebook,
            str(scratch_path),
            parameters={"country": country, "admin_level": admin_level},
            progress_bar=False,
        )


def main() -> None:
    # ==========================================
    # PHASE 1: Extraction (Once per unique country)
    # notebook load admin_level shapes but extraction is raster-based
    # ==========================================
    if NOTEBOOK_EXT:
        print("--- PHASE 1: Running Extraction Notebooks ---")

        failed_countries = set()
        # the mins keys become the unique countries in CONFIGS
        for country in sorted(mins.keys()):
            print(f"[START EXTRACTION] {country}")
            try:
                run_notebook(NOTEBOOK_EXT, country, mins[country])
            except PapermillExecutionError as e:
                print(
                    f"[FAILED EXTRACTION] {country} — {NOTEBOOK_EXT}, "
                    f"cell #{e.cell_index}: {e.ename}: {e.evalue}"
                )
                failed_countries.add(country)
            except Exception as e:
                print(
                    f"[FAILED EXTRACTION] {country} — {NOTEBOOK_EXT}, "
                    f"{type(e).__name__}: {e}"
                )
                failed_countries.add(country)
            else:
                print(f"[DONE EXTRACTION] {country} — {NOTEBOOK_EXT}")

    # ==========================================
    # PHASE 2: Postprocess (Run for every country/admin_level combo)
    # ==========================================
    print("\n--- PHASE 2: Running Postprocess Notebooks ---")
    for config in CONFIGS:
        # Skip postprocessing if country extraction failed
        if config.country in failed_countries:
            print(f"[SKIP POST]   {config.country} Extraction failed")
            continue

        print(f"[START POST]  {config.country}, L{config.admin_level}")
        try:
            run_notebook(NOTEBOOK_POST, config.country, config.admin_level)
            print(f"[DONE POST]   {config.country}, L{config.admin_level}")
        except PapermillExecutionError as e:
            print(
                f"[FAILED POST] {config.country}, L{config.admin_level}, "
                f"cell #{e.cell_index}: {e.ename}: {e.evalue}"
            )
        except Exception as e:
            print(
                f"[FAILED POST] {config.country}, L{config.admin_level}, "
                f"{type(e).__name__}: {e}"
            )


if __name__ == "__main__":
    main()
