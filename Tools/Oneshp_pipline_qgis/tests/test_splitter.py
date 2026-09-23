from shapely.geometry import box
import geopandas as gpd

from shp_pipeline_qgis.splitter import assign_boundary_to_current_chunk, build_split_plan
from shp_pipeline_qgis.test_support import build_rectangles


def test_build_split_plan_returns_single_chunk_when_count_not_above_threshold():
    gdf = build_rectangles(count=100000, axis="x")
    plan = build_split_plan(gdf)
    assert [chunk.size for chunk in plan.chunks] == [100000]


def test_build_split_plan_splits_large_dataset_into_contiguous_chunks():
    gdf = build_rectangles(count=120000, axis="x")
    plan = build_split_plan(gdf)
    assert [chunk.size for chunk in plan.chunks] == [50000, 50000, 20000]


def test_build_split_plan_merges_tail_smaller_than_minimum():
    gdf = build_rectangles(count=108000, axis="x")
    plan = build_split_plan(gdf)
    assert [chunk.size for chunk in plan.chunks] == [50000, 58000]


def test_boundary_feature_stays_in_current_chunk():
    gdf = gpd.GeoDataFrame(
        [
            {"feature_id": 1, "geometry": box(0, 0, 1, 1)},
            {"feature_id": 2, "geometry": box(0.9, 0, 2.0, 1)},
        ],
        geometry="geometry",
        crs="EPSG:4326",
    )
    assert assign_boundary_to_current_chunk(gdf, split_value=1.0, axis="x") == [0, 1]
