"""Command line: python -m cod_ab_geosight [--config X] [--download-all]"""

import argparse

from .flow import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m cod_ab_geosight",
        description="Harmonize HDX COD-AB shapefiles per admin level for "
                    "GeoSight, driven by the config geojsons.",
    )
    parser.add_argument("--config", default=None,
                        help="config.yaml path (default: the package one)")
    parser.add_argument("--download-all", action="store_true",
                        help="download from HDX first (overrides "
                             "run.download_all of the config)")
    args = parser.parse_args(argv)
    run(args.config, download_all_override=True if args.download_all else None)


if __name__ == "__main__":
    main()
