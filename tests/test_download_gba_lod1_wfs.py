import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "download_gba_lod1_wfs.py"
    spec = importlib.util.spec_from_file_location("download_gba_lod1_wfs", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DownloadGbaLod1WfsMainTests(unittest.TestCase):
    def test_place_mode_does_not_require_boundary_argument(self):
        module = load_module()
        argv = [
            "download_gba_lod1_wfs.py",
            "--place",
            "Erdenet, Bayan-Ondor, Orkhon, Mongolia",
            "--output-dir",
            "data/erdenet_gba_wfs",
            "--grid-size",
            "0.02",
            "--page-size",
            "5000",
            "--tile-format",
            "gpkg",
            "--merge-output",
            "data/erdenet_gba_wfs/erdenet_buildings_height_gba.shp",
        ]

        with patch.object(sys, "argv", argv):
            with patch.object(module, "run_place_mode") as run_place_mode:
                module.main()

        run_place_mode.assert_called_once_with(
            "Erdenet, Bayan-Ondor, Orkhon, Mongolia",
            Path("data/erdenet_gba_wfs"),
            0.02,
            5000,
            0,
            "gpkg",
            Path("data/erdenet_gba_wfs/erdenet_buildings_height_gba.shp"),
            tile_workers=1,
        )


class BoundaryModeTests(unittest.TestCase):
    def test_boundary_mode_saves_tiles_and_merges(self):
        module = load_module()

        def fake_fetch(minx, miny, maxx, maxy, page_size=0, max_pages=0):
            geometry = box(minx, miny, maxx, maxy)
            return gpd.GeoDataFrame(
                [{"GBA_ID": "1", "Height": 10.0, "geometry": geometry}],
                geometry="geometry",
                crs=4326,
            )

        boundary = gpd.GeoDataFrame([{"geometry": box(0, 0, 1, 1)}], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", side_effect=fake_fetch):
                with patch.object(module, "merge_outputs") as merge:
                    module.run_boundary_mode(
                        boundary,
                        output_dir,
                        1.0,
                        5000,
                        0,
                        "gpkg",
                        output_dir / "merged.gpkg",
                    )

            tile = output_dir / "tiles_national_v1" / "GBA_100_I0180_J0090.gpkg"
            self.assertTrue((output_dir / "gba_wfs_grid.gpkg").exists())
            self.assertTrue(tile.exists())
            merge.assert_called_once_with([tile], output_dir / "merged.gpkg")

    def test_boundary_selects_a_full_canonical_cell_without_filtering_features(self):
        module = load_module()
        fetched_bounds = []
        boundary = gpd.GeoDataFrame(
            [{"geometry": box(0.1, 0.1, 0.2, 0.2)}],
            geometry="geometry",
            crs=4326,
        )

        def fake_fetch(minx, miny, maxx, maxy, page_size=0, max_pages=0):
            fetched_bounds.append((minx, miny, maxx, maxy))
            return gpd.GeoDataFrame(
                [
                    {"GBA_ID": "inside", "geometry": box(0.12, 0.12, 0.13, 0.13)},
                    {"GBA_ID": "outside", "geometry": box(0.4, 0.4, 0.41, 0.41)},
                ],
                geometry="geometry",
                crs=4326,
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", side_effect=fake_fetch):
                module.run_boundary_mode(
                    boundary,
                    output_dir,
                    0.5,
                    5000,
                    0,
                    "gpkg",
                    None,
                )

            tile = output_dir / "tiles_national_v1" / "GBA_050_I0360_J0180.gpkg"
            result = gpd.read_file(tile)
            self.assertEqual(fetched_bounds, [(0.0, 0.0, 0.5, 0.5)])
            self.assertEqual(set(result["GBA_ID"]), {"inside", "outside"})

    def test_completed_tile_is_reused_only_with_a_valid_completion_marker(self):
        module = load_module()
        boundary = gpd.GeoDataFrame([{"geometry": box(0.1, 0.1, 0.2, 0.2)}], geometry="geometry", crs=4326)

        def fake_fetch(minx, miny, maxx, maxy, page_size=0, max_pages=0):
            return gpd.GeoDataFrame(
                [{"GBA_ID": "1", "geometry": box(0.12, 0.12, 0.13, 0.13)}],
                geometry="geometry",
                crs=4326,
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", side_effect=fake_fetch) as fetch:
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)
                self.assertEqual(fetch.call_count, 1)

            marker = output_dir / "tiles_national_v1" / "GBA_050_I0360_J0180.done.json"
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "COMPLETE")
            self.assertEqual(metadata["data_semantics_version"], "coverage-v2")
            self.assertEqual(metadata["feature_count"], 1)

            with patch.object(module, "fetch_bbox", side_effect=AssertionError("completed tile was downloaded again")):
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

    def test_orphan_tile_without_completion_marker_is_redownloaded(self):
        module = load_module()
        boundary = gpd.GeoDataFrame([{"geometry": box(0.1, 0.1, 0.2, 0.2)}], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            tile_dir = output_dir / "tiles_national_v1"
            tile_dir.mkdir()
            orphan = tile_dir / "GBA_050_I0360_J0180.gpkg"
            gpd.GeoDataFrame(
                [{"GBA_ID": "stale", "geometry": box(0.12, 0.12, 0.13, 0.13)}],
                geometry="geometry",
                crs=4326,
            ).to_file(orphan, driver="GPKG")

            fresh = gpd.GeoDataFrame(
                [{"GBA_ID": "fresh", "geometry": box(0.14, 0.14, 0.15, 0.15)}],
                geometry="geometry",
                crs=4326,
            )
            with patch.object(module, "fetch_bbox", return_value=fresh) as fetch:
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(gpd.read_file(orphan)["GBA_ID"].tolist(), ["fresh"])

    def test_tile_with_mismatched_checksum_is_redownloaded(self):
        module = load_module()
        boundary = gpd.GeoDataFrame([{"geometry": box(0.1, 0.1, 0.2, 0.2)}], geometry="geometry", crs=4326)

        def frame(value, offset):
            return gpd.GeoDataFrame(
                [{"GBA_ID": value, "geometry": box(offset, offset, offset + 0.01, offset + 0.01)}],
                geometry="geometry",
                crs=4326,
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", return_value=frame("first", 0.12)):
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

            tile_dir = output_dir / "tiles_national_v1"
            tile = tile_dir / "GBA_050_I0360_J0180.gpkg"
            marker = tile_dir / "GBA_050_I0360_J0180.done.json"
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            metadata["files"][tile.name]["sha256"] = "0" * 64
            marker.write_text(json.dumps(metadata), encoding="utf-8")

            with patch.object(module, "fetch_bbox", return_value=frame("fresh", 0.14)) as fetch:
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(gpd.read_file(tile)["GBA_ID"].tolist(), ["fresh"])

    def test_empty_tile_marker_prevents_repeated_downloads(self):
        module = load_module()
        boundary = gpd.GeoDataFrame([{"geometry": box(0.1, 0.1, 0.2, 0.2)}], geometry="geometry", crs=4326)
        empty = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", return_value=empty) as fetch:
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)
                self.assertEqual(fetch.call_count, 1)

            marker = output_dir / "tiles_national_v1" / "GBA_050_I0360_J0180.done.json"
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "EMPTY")
            self.assertEqual(metadata["feature_count"], 0)

            with patch.object(module, "fetch_bbox", side_effect=AssertionError("empty tile was downloaded again")):
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

    def test_grid_manifest_lists_only_canonical_expected_tiles(self):
        module = load_module()
        boundary = gpd.GeoDataFrame([{"geometry": box(0.1, 0.1, 0.2, 0.2)}], geometry="geometry", crs=4326)
        empty = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", return_value=empty):
                module.run_boundary_mode(boundary, output_dir, 0.5, 5000, 0, "gpkg", None)

            manifest = json.loads((output_dir / "grid_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["grid_algorithm"], "national-grid-v1")
            self.assertEqual(manifest["data_semantics_version"], "coverage-v2")
            self.assertEqual(manifest["expected_tile_count"], 1)
            self.assertEqual(manifest["grid_ids"], ["GBA_050_I0360_J0180"])


class TileWorkerConcurrencyTests(unittest.TestCase):
    def test_tile_workers_run_concurrently(self):
        import threading

        module = load_module()
        barrier = threading.Barrier(2, timeout=10)

        def fake_fetch(minx, miny, maxx, maxy, page_size=0, max_pages=0):
            barrier.wait()
            return gpd.GeoDataFrame(
                [{"GBA_ID": "1", "Height": 10.0, "geometry": box(minx, miny, maxx, maxy)}],
                geometry="geometry",
                crs=4326,
            )

        boundary = gpd.GeoDataFrame([{"geometry": box(0, 0, 2, 1)}], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", side_effect=fake_fetch):
                with patch.object(module, "merge_outputs"):
                    module.run_boundary_mode(
                        boundary,
                        output_dir,
                        1.0,
                        5000,
                        0,
                        "gpkg",
                        None,
                        tile_workers=2,
                    )

            tile_dir = output_dir / "tiles_national_v1"
            self.assertTrue((tile_dir / "GBA_100_I0180_J0090.gpkg").exists())
            self.assertTrue((tile_dir / "GBA_100_I0181_J0090.gpkg").exists())

    def test_tile_failure_raises_and_keeps_finished_tiles(self):
        import threading

        module = load_module()
        call_counter = {"count": 0}
        counter_lock = threading.Lock()

        def fake_fetch(minx, miny, maxx, maxy, page_size=0, max_pages=0):
            with counter_lock:
                call_counter["count"] += 1
                index = call_counter["count"]
            if index == 2:
                raise RuntimeError("simulated tile failure")
            return gpd.GeoDataFrame(
                [{"GBA_ID": "1", "geometry": box(minx, miny, maxx, maxy)}],
                geometry="geometry",
                crs=4326,
            )

        boundary = gpd.GeoDataFrame([{"geometry": box(0, 0, 2, 1)}], geometry="geometry", crs=4326)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch.object(module, "fetch_bbox", side_effect=fake_fetch):
                with patch.object(module, "merge_outputs") as merge:
                    with self.assertRaises(RuntimeError):
                        module.run_boundary_mode(
                            boundary,
                            output_dir,
                            1.0,
                            5000,
                            0,
                            "gpkg",
                            Path("out/merged.gpkg"),
                            tile_workers=1,
                        )
            merge.assert_not_called()
            self.assertTrue(
                (output_dir / "tiles_national_v1" / "GBA_100_I0180_J0090.gpkg").exists()
            )


class MergeOutputsTests(unittest.TestCase):
    def test_merge_outputs_logs_progress_and_dedups(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tiles = []
            for index in range(3):
                x = float(index)
                gdf = gpd.GeoDataFrame(
                    [{"GBA_ID": str(index), "geometry": box(x, 0, x + 1, 1)}],
                    geometry="geometry",
                    crs=4326,
                )
                path = base / f"GBA_{index + 1:04d}.gpkg"
                gdf.to_file(path, driver="GPKG")
                tiles.append(path)

            duplicate = base / "GBA_0004.gpkg"
            gpd.GeoDataFrame(
                [{"GBA_ID": "2", "geometry": box(2, 0, 3, 1)}],
                geometry="geometry",
                crs=4326,
            ).to_file(duplicate, driver="GPKG")
            tiles.append(duplicate)

            output = base / "merged.gpkg"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                module.merge_outputs(tiles, output)

            text = buffer.getvalue()
            self.assertIn("[MERGE] 开始合并", text)
            self.assertIn("[MERGE] 已读取", text)
            self.assertIn("[MERGE] 去重完成", text)
            self.assertIn("[MERGE] 合并完成", text)

            merged = gpd.read_file(output)
            self.assertEqual(len(merged), 3)

    def test_merge_outputs_handles_empty_tile_list(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "empty.gpkg"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                module.merge_outputs([], output)
            self.assertTrue(output.exists())
            self.assertIn("[MERGE] 无分块", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
