import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "download_gba_lod1_wfs_adaptive.py"
    spec = importlib.util.spec_from_file_location("download_gba_lod1_wfs_adaptive", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def boundary_gdf():
    return gpd.GeoDataFrame([{"geometry": box(0, 0, 1, 1)}], geometry="geometry", crs=4326)


class DownloadGbaLod1WfsAdaptiveTests(unittest.TestCase):
    def test_place_mode_forwards_new_adaptive_arguments(self):
        module = load_module()
        argv = [
            "download_gba_lod1_wfs_adaptive.py",
            "--place",
            "Mongolia",
            "--output-dir",
            "data/mongolia_gba_wfs",
            "--grid-size",
            "0.5",
            "--page-size",
            "5000",
            "--tile-format",
            "gpkg",
            "--merge-output",
            "data/mongolia_gba_wfs/mongolia_buildings_height_gba.shp",
            "--split-threshold",
            "20000",
            "--failure-split-retries",
            "5",
            "--min-grid-size",
            "0.05",
            "--probe-page-size",
            "1",
        ]

        with patch.object(sys, "argv", argv):
            with patch.object(module, "run_place_mode") as run_place_mode:
                module.main()

        run_place_mode.assert_called_once_with(
            "Mongolia",
            Path("data/mongolia_gba_wfs"),
            0.5,
            5000,
            0,
            "gpkg",
            Path("data/mongolia_gba_wfs/mongolia_buildings_height_gba.shp"),
            20000,
            5,
            0.05,
            1,
            tile_workers=1,
        )

    def test_boundary_mode_delegates_to_shared_standard_worker(self):
        module = load_module()

        with patch.object(module, "standard_run_boundary_mode") as standard_run:
            module.run_boundary_mode_adaptive(
                boundary_gdf(),
                Path("out"),
                1.0,
                5000,
                0,
                "gpkg",
                None,
                split_threshold=10,
                failure_split_retries=5,
                min_grid_size=0.25,
                probe_page_size=1,
                boundary_source="test",
                source_mode="boundary",
            )

        standard_run.assert_called_once()
        args, kwargs = standard_run.call_args
        self.assertEqual(tuple(args[0].total_bounds), (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(args[1], Path("out"))
        self.assertEqual(kwargs.get("boundary_source"), "test")
        self.assertEqual(kwargs.get("source_mode"), "boundary")

    def test_bbox_mode_delegates_to_standard_worker(self):
        module = load_module()
        argv = [
            "download_gba_lod1_wfs_adaptive.py",
            "--bbox",
            "1,2,3,4",
            "--output",
            "out/test.gpkg",
        ]
        with patch.object(sys, "argv", argv):
            with patch.object(module, "standard_run_bbox_mode") as standard_run:
                module.main()

        standard_run.assert_called_once()
        args, _ = standard_run.call_args
        self.assertEqual(args[0], "1,2,3,4")
        self.assertEqual(args[1], Path("out/test.gpkg"))


if __name__ == "__main__":
    unittest.main()
