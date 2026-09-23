import geopandas as gpd

from shp_pipeline_qgis.test_support import build_rectangles


def test_build_rectangles_creates_requested_count():
    gdf = build_rectangles(count=7, axis="x", crs="EPSG:4326")
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert len(gdf) == 7
    assert gdf.crs.to_string() == "EPSG:4326"
