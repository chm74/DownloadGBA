from pathlib import Path

import geopandas as gpd

from shp_pipeline_qgis.clean import clean_shapefile
from shp_pipeline_qgis.test_support import build_rectangles


def test_clean_shapefile_removes_non_positive_heights(tmp_path: Path):
    source = tmp_path / "source.shp"
    gdf = build_rectangles(count=4, axis="x")
    gdf["HEIGHT"] = [12.0, 0.0, -3.0, None]
    gdf.to_file(source)

    result = clean_shapefile(source, tmp_path / "clean")
    cleaned = gpd.read_file(result.output_path)

    assert result.input_count == 4
    assert result.output_count == 2
    assert result.removed_empty_count == 0
    assert result.removed_zero_height_count == 2
    assert result.removed_duplicate_count == 0
    assert cleaned["HEIGHT"].tolist()[0] == 12.0
    assert cleaned["HEIGHT"].isna().tolist()[1] is True
