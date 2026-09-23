from pathlib import Path

import geopandas as gpd

from shp_pipeline_qgis.reproject import reproject_shapefile
from shp_pipeline_qgis.test_support import build_rectangles


def test_reproject_shapefile_writes_epsg_3857(tmp_path: Path):
    source = tmp_path / "source.shp"
    build_rectangles(count=3, axis="x", crs="EPSG:4326").to_file(source)

    output = reproject_shapefile(source, tmp_path / "final")
    result = gpd.read_file(output)

    assert result.crs.to_epsg() == 3857
    assert len(result) == 3
