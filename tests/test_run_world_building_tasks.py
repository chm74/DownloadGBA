import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import Point

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_shp_process_tasks
import run_world_building_tasks as runner
import world_tasks_lib as lib


def build_row(
    continent: str = "Africa",
    country: str = "Algeria",
    city: str = "Algeria",
    download_dir: str = "data/Africa/Algeria/Algeria",
) -> dict:
    return {
        "task_id": f"{continent}|{country}|{city}",
        "continent": continent,
        "country": country,
        "city": city,
        "download_dir": download_dir,
    }


class ExecuteTaskTests(unittest.TestCase):
    def test_execute_task_success_writes_marker_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            store = lib.StateStore(repo_root / "state.db")
            try:
                task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
                task_dir.mkdir(parents=True)
                merge_output = lib.merge_output_path(
                    task_dir,
                    runner.row_to_task(row),
                    "gpkg",
                )

                def fake_run(command, cwd=None, stdout=None, stderr=None, text=None, env=None):
                    merge_output.write_bytes(b"fake gpkg bytes")
                    (task_dir / "grid_manifest.json").write_text(
                        '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                        '"expected_tile_count":2,"grid_ids":["GRID_A","GRID_B"]}',
                        encoding="utf-8",
                    )
                    tile_dir = task_dir / "tiles_national_v1"
                    tile_dir.mkdir(exist_ok=True)
                    (tile_dir / "GRID_A.done.json").write_text(
                        '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                        '"grid_id":"GRID_A","status":"COMPLETE"}',
                        encoding="utf-8",
                    )
                    (tile_dir / "GRID_B.done.json").write_text(
                        '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                        '"grid_id":"GRID_B","status":"EMPTY"}',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0)

                args = argparse.Namespace(python_exe="python", refresh_boundary=False, hash=False, shp_export=False)
                with patch.object(
                    lib,
                    "resolve_boundary",
                    return_value=lib.BoundaryResult(path=repo_root / "boundary.gpkg", source={"type": "test"}),
                ):
                    with patch.object(lib, "count_features", return_value=7):
                        with patch.object(runner.subprocess, "run", side_effect=fake_run):
                            store.sync_manifest([runner.row_to_task(row)])
                            result = runner.execute_task(
                                repo_root,
                                repo_root / "scripts" / "fake_worker.py",
                                args,
                                store,
                                [],
                                lib.DownloadOptions(),
                                row,
                            )

                self.assertEqual(result["status"], lib.STATUS_OK)
                stored = store.get(row["task_id"])
                self.assertEqual(stored["status"], lib.STATUS_OK)
                self.assertEqual(stored["feature_count"], 7)
                self.assertEqual(stored["existing_data_dir"], row["download_dir"])

                marker = lib.read_marker(task_dir)
                self.assertIsNotNone(marker)
                self.assertTrue(lib.marker_is_valid(marker, repo_root, task_dir, merge_output))
                self.assertEqual(marker["data_semantics_version"], "coverage-v2")
                self.assertEqual(marker["grid_algorithm"], "national-grid-v1")
                self.assertEqual(marker["expected_tile_count"], 2)
                self.assertEqual(marker["complete_tile_count"], 1)
                self.assertEqual(marker["empty_tile_count"], 1)
                self.assertEqual(marker["missing_tile_count"], 0)
            finally:
                store.close()

    def test_execute_task_records_failure_when_worker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            store = lib.StateStore(repo_root / "state.db")
            try:
                args = argparse.Namespace(python_exe="python", refresh_boundary=False, hash=False, shp_export=False)

                def fake_run(command, cwd=None, stdout=None, stderr=None, text=None, env=None):
                    return subprocess.CompletedProcess(command, 2)

                with patch.object(
                    lib,
                    "resolve_boundary",
                    return_value=lib.BoundaryResult(path=repo_root / "boundary.gpkg", source={"type": "test"}),
                ):
                    with patch.object(runner.subprocess, "run", side_effect=fake_run):
                        store.sync_manifest([runner.row_to_task(row)])
                        result = runner.execute_task(
                            repo_root,
                            repo_root / "scripts" / "fake_worker.py",
                            args,
                            store,
                            [],
                            lib.DownloadOptions(),
                            row,
                        )

                self.assertEqual(result["status"], lib.STATUS_FAILED)
                stored = store.get(row["task_id"])
                self.assertEqual(stored["status"], lib.STATUS_FAILED)
                self.assertEqual(stored["exit_code"], 2)
                self.assertTrue(stored["last_error"])
            finally:
                store.close()

    def test_execute_task_rejects_incomplete_national_grid_before_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            store = lib.StateStore(repo_root / "state.db")
            try:
                task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
                task_dir.mkdir(parents=True)
                merge_output = lib.merge_output_path(task_dir, runner.row_to_task(row), "gpkg")

                def fake_run(command, cwd=None, stdout=None, stderr=None, text=None, env=None):
                    merge_output.write_bytes(b"fake gpkg bytes")
                    (task_dir / "grid_manifest.json").write_text(
                        '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                        '"expected_tile_count":2,"grid_ids":["GRID_A","GRID_B"]}',
                        encoding="utf-8",
                    )
                    tile_dir = task_dir / "tiles_national_v1"
                    tile_dir.mkdir()
                    (tile_dir / "GRID_A.done.json").write_text(
                        '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                        '"grid_id":"GRID_A","status":"COMPLETE"}',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0)

                args = argparse.Namespace(python_exe="python", refresh_boundary=False, hash=False, shp_export=False)
                with patch.object(
                    lib,
                    "resolve_boundary",
                    return_value=lib.BoundaryResult(path=repo_root / "boundary.gpkg", source={"type": "test"}),
                ):
                    with patch.object(lib, "count_features", return_value=7):
                        with patch.object(runner.subprocess, "run", side_effect=fake_run):
                            store.sync_manifest([runner.row_to_task(row)])
                            result = runner.execute_task(
                                repo_root,
                                repo_root / "scripts" / "fake_worker.py",
                                args,
                                store,
                                [],
                                lib.DownloadOptions(),
                                row,
                            )

                self.assertEqual(result["status"], lib.STATUS_FAILED)
                stored = store.get(row["task_id"])
                self.assertEqual(stored["status"], lib.STATUS_FAILED)
                self.assertIn("missing=1", stored["last_error"])
                self.assertIsNone(lib.read_marker(task_dir))
            finally:
                store.close()


class DryRunTests(unittest.TestCase):
    def test_dry_run_lists_plan_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            manifest = repo_root / "tasks.csv"
            manifest.write_text(
                "continent,country,city,download_dir\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria\n",
                encoding="utf-8",
            )
            state_db = repo_root / "state.db"
            overrides = repo_root / "overrides.csv"
            overrides.write_text("continent,country,city,kind,value,note\n", encoding="utf-8")

            argv = [
                "run_world_building_tasks.py",
                "--tasks",
                str(manifest),
                "--state-db",
                str(state_db),
                "--overrides",
                str(overrides),
                "--repo-root",
                str(repo_root),
                "--dry-run",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(buffer):
                    code = runner.main()

            self.assertEqual(code, 0)
            output = buffer.getvalue()
            self.assertIn("[PLAN]", output)
            self.assertIn("[DRY-RUN]", output)

            task_dir = repo_root / "data" / "Africa" / "Algeria" / "Algeria"
            self.assertFalse((task_dir / lib.MARKER_NAME).exists())
            self.assertFalse(task_dir.exists())

            store = lib.StateStore(state_db)
            try:
                rows = store.all_tasks()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], lib.STATUS_PENDING)
            finally:
                store.close()


class ShpExportTests(unittest.TestCase):
    def test_export_shp_parts_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            merge_output = task_dir / "merge.gpkg"
            gdf = gpd.GeoDataFrame(
                {
                    "GBA_ID": [str(index) for index in range(5)],
                    "Height": [1.0] * 5,
                    "geometry": [Point(index, 0) for index in range(5)],
                },
                geometry="geometry",
                crs=4326,
            )
            gdf.to_file(merge_output, driver="GPKG")

            parts = runner.export_shp_parts(merge_output, task_dir, "single", max_features=1000)

            self.assertEqual(len(parts), 1)
            self.assertTrue(parts[0].name == "single_buildings_height_gba.shp")
            self.assertTrue(parts[0].exists())
            self.assertTrue(merge_output.exists())

    def test_export_shp_parts_splits_large_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            merge_output = task_dir / "merge.gpkg"
            gdf = gpd.GeoDataFrame(
                {
                    "GBA_ID": [str(index) for index in range(5)],
                    "Height": [1.0] * 5,
                    "geometry": [Point(index, 0) for index in range(5)],
                },
                geometry="geometry",
                crs=4326,
            )
            gdf.to_file(merge_output, driver="GPKG")

            parts = runner.export_shp_parts(merge_output, task_dir, "multi", max_features=2)

            self.assertEqual(len(parts), 3)
            names = sorted(part.name for part in parts)
            self.assertEqual(
                names,
                [
                    "multi_buildings_height_gba_part1.shp",
                    "multi_buildings_height_gba_part2.shp",
                    "multi_buildings_height_gba_part3.shp",
                ],
            )


class RunningViewTests(unittest.TestCase):
    def test_prints_running_task_with_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            store = lib.StateStore(repo_root / "state.db")
            try:
                row = build_row()
                store.sync_manifest([runner.row_to_task(row)])
                store.set_status(row["task_id"], lib.STATUS_RUNNING, attempts=1, started_at="2026-01-01T00:00:00")

                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    runner.print_running_tasks(store, repo_root)

                output = buffer.getvalue()
                self.assertIn("[RUNNING]", output)
                self.assertIn(row["task_id"], output)
            finally:
                store.close()

    def test_prints_no_running_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    runner.print_running_tasks(store, Path(tmp))
                self.assertIn("没有正在执行的任务", buffer.getvalue())
            finally:
                store.close()

    def test_prints_national_grid_done_marker_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            task_dir = repo_root / row["download_dir"]
            tile_dir = task_dir / "tiles_national_v1"
            tile_dir.mkdir(parents=True)
            (task_dir / "grid_manifest.json").write_text(
                '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                '"expected_tile_count":3,"grid_ids":["GRID_A","GRID_B","GRID_C"]}',
                encoding="utf-8",
            )
            for grid_id, status in (("GRID_A", "COMPLETE"), ("GRID_B", "EMPTY")):
                (tile_dir / f"{grid_id}.done.json").write_text(
                    '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                    f'"grid_id":"{grid_id}","status":"{status}"}}',
                    encoding="utf-8",
                )
            store = lib.StateStore(repo_root / "state.db")
            try:
                store.sync_manifest([runner.row_to_task(row)])
                store.set_status(row["task_id"], lib.STATUS_RUNNING, attempts=1, started_at="2026-01-01T00:00:00")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    runner.print_running_tasks(store, repo_root)
                self.assertIn("tiles=2/3", buffer.getvalue())
            finally:
                store.close()


    def test_export_shp_parts_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            merge_output = task_dir / "merge.gpkg"
            gdf = gpd.GeoDataFrame(
                {
                    "GBA_ID": [str(index) for index in range(5)],
                    "Height": [1.0] * 5,
                    "geometry": [Point(index, 0) for index in range(5)],
                },
                geometry="geometry",
                crs=4326,
            )
            gdf.to_file(merge_output, driver="GPKG")

            messages: list[str] = []
            parts = runner.export_shp_parts(
                merge_output,
                task_dir,
                "logged",
                max_features=2,
                log=messages.append,
            )

            self.assertEqual(len(parts), 3)
            self.assertTrue(any("开始导出" in message for message in messages))
            self.assertTrue(any("导出完成" in message for message in messages))
            self.assertTrue(any("已写出" in message for message in messages))


class MoveQueueTests(unittest.TestCase):
    def test_move_top_reorders_pending_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            manifest = repo_root / "tasks.csv"
            manifest.write_text(
                "continent,country,city,download_dir\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria\n"
                "Africa,Angola,Angola,data\\Africa\\Angola\\Angola\n",
                encoding="utf-8",
            )
            state_db = repo_root / "state.db"
            argv = [
                "run_world_building_tasks.py",
                "--tasks",
                str(manifest),
                "--state-db",
                str(state_db),
                "--repo-root",
                str(repo_root),
                "--move-top",
                "Angola",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(buffer):
                    code = runner.main()

            self.assertEqual(code, 0)
            self.assertIn("[MOVE-TOP]", buffer.getvalue())

            store = lib.StateStore(state_db)
            try:
                self.assertEqual(store.pending_tasks()[0]["task_id"], "Africa|Angola|Angola")
            finally:
                store.close()


class ProcessQueueRegistrationTests(unittest.TestCase):
    def _prepare_completed_task(self, repo_root: Path, row: dict) -> tuple[Path, Path]:
        manifest = repo_root / "tasks.csv"
        manifest.write_text(
            "continent,country,city,download_dir\n"
            f"{row['continent']},{row['country']},{row['city']},{row['download_dir']}\n",
            encoding="utf-8",
        )
        state_db = repo_root / "state.db"
        store = lib.StateStore(state_db)
        try:
            store.sync_manifest([runner.row_to_task(row)])
            store.set_status(row["task_id"], lib.STATUS_OK, feature_count=7)
            store.set_existing_data(row["task_id"], row["download_dir"])
        finally:
            store.close()
        task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "Algeria_buildings_height_gba.shp").write_bytes(b"fake shp")
        return state_db, manifest

    def test_register_task_in_process_queue_adds_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            state_db, manifest = self._prepare_completed_task(repo_root, row)
            args = argparse.Namespace(
                process_sync=True,
                process_db=str(repo_root / "process.db"),
                state_db=str(state_db),
                tasks=str(manifest),
            )

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                runner.register_task_in_process_queue(repo_root, args, row["task_id"])

            self.assertIn("[PROCESS]", buffer.getvalue())
            store = run_shp_process_tasks.ProcessStore(repo_root / "process.db")
            try:
                pending = store.pending_tasks()
            finally:
                store.close()
            self.assertEqual([item["dataset_key"] for item in pending], [row["task_id"]])

    def test_register_task_in_process_queue_respects_disabled_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            state_db, manifest = self._prepare_completed_task(repo_root, row)
            args = argparse.Namespace(
                process_sync=False,
                process_db=str(repo_root / "process.db"),
                state_db=str(state_db),
                tasks=str(manifest),
            )

            runner.register_task_in_process_queue(repo_root, args, row["task_id"])

            self.assertFalse((repo_root / "process.db").exists())

    def test_execute_task_registers_completed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            store = lib.StateStore(repo_root / "state.db")
            try:
                task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
                task_dir.mkdir(parents=True)
                merge_output = lib.merge_output_path(task_dir, runner.row_to_task(row), "gpkg")

                def fake_run(command, cwd=None, stdout=None, stderr=None, text=None, env=None):
                    merge_output.write_bytes(b"fake gpkg bytes")
                    return subprocess.CompletedProcess(command, 0)

                args = argparse.Namespace(
                    python_exe="python",
                    refresh_boundary=False,
                    hash=False,
                    shp_export=True,
                    shp_max_features=1000000,
                )
                fake_part = task_dir / "Algeria_buildings_height_gba.shp"
                with patch.object(
                    lib,
                    "resolve_boundary",
                    return_value=lib.BoundaryResult(path=repo_root / "boundary.gpkg", source={"type": "test"}),
                ):
                    with patch.object(lib, "count_features", return_value=7):
                        with patch.object(runner.subprocess, "run", side_effect=fake_run):
                            with patch.object(runner, "export_shp_parts", return_value=[fake_part]):
                                with patch.object(runner, "register_task_in_process_queue") as register:
                                    store.sync_manifest([runner.row_to_task(row)])
                                    result = runner.execute_task(
                                        repo_root,
                                        repo_root / "scripts" / "fake_worker.py",
                                        args,
                                        store,
                                        [],
                                        lib.DownloadOptions(),
                                        row,
                                    )

                self.assertEqual(result["status"], lib.STATUS_OK)
                register.assert_called_once_with(repo_root, args, row["task_id"])
            finally:
                store.close()

    def test_execute_task_skips_registration_for_empty_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            row = build_row()
            store = lib.StateStore(repo_root / "state.db")
            try:
                task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
                task_dir.mkdir(parents=True)
                merge_output = lib.merge_output_path(task_dir, runner.row_to_task(row), "gpkg")

                def fake_run(command, cwd=None, stdout=None, stderr=None, text=None, env=None):
                    merge_output.write_bytes(b"fake gpkg bytes")
                    return subprocess.CompletedProcess(command, 0)

                args = argparse.Namespace(
                    python_exe="python",
                    refresh_boundary=False,
                    hash=False,
                    shp_export=True,
                    shp_max_features=1000000,
                )
                with patch.object(
                    lib,
                    "resolve_boundary",
                    return_value=lib.BoundaryResult(path=repo_root / "boundary.gpkg", source={"type": "test"}),
                ):
                    with patch.object(lib, "count_features", return_value=0):
                        with patch.object(runner.subprocess, "run", side_effect=fake_run):
                            with patch.object(runner, "export_shp_parts") as export:
                                with patch.object(runner, "register_task_in_process_queue") as register:
                                    store.sync_manifest([runner.row_to_task(row)])
                                    result = runner.execute_task(
                                        repo_root,
                                        repo_root / "scripts" / "fake_worker.py",
                                        args,
                                        store,
                                        [],
                                        lib.DownloadOptions(),
                                        row,
                                    )

                self.assertEqual(result["status"], lib.STATUS_OK_EMPTY)
                export.assert_not_called()
                register.assert_not_called()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
