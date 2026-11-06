"""COD-AB harmonization for GeoSight (WIA countries).

    from cod_ab_geosight import run
    result = run()                      # cod_ab_geosight/config.yaml
    result = run("path/to/config.yaml")

See README.md.
"""

from .flow import run

__all__ = ["run"]
