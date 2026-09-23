from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

from shp_pipeline_qgis.validate_with_qgis import validate_shapefile_with_qgis


def test_validate_shapefile_with_qgis_keeps_only_valid_features(tmp_path: Path):
    source = tmp_path / "invalid_mix.shp"
    gdf = gpd.GeoDataFrame(
        [
            {"feature_id": 1, "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])},
            {"feature_id": 2, "geometry": Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])},
        ],
        geometry="geometry",
        crs="EPSG:4326",
    )
    gdf.to_file(source)

    result = validate_shapefile_with_qgis(source, tmp_path / "valid")
    valid = gpd.read_file(result.output_path)

    assert len(valid) == 1
    assert valid["feature_id"].tolist() == [1]
