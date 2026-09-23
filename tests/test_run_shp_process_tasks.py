import argparse
import contextlib
import io
import json
import sqlite3
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

import run_shp_process_tasks as runner
import world_tasks_lib as lib


def write_tiny_shp(path: Path, features: int = 2, crs: int = 4326) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame(
        {"Height": [3.0 + index for index in range(features)]},
        geometry=[Point(index, index) for index in range(features)],
        crs=crs,
    )
    gdf.to_file(path)
    return path


def build_repo_fixture(repo_root: Path) -> tuple[Path, Path]:
    data_root = repo_root / "data"
    download_db = repo_root / "download_state.db"

    manifest = data_root / "world_building_download_tasks.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "continent,country,city,download_dir,process_name_prefix\n"
        "Asia,China,Guangxi,data\\Asia\\Guangxi,guangxi\n"
        "Europe,San_Marino,San_Marino,data\\Europe\\San_Marino,San_Marino\n"
        "Africa,Algeria,Algeria,data\\Africa\\Algeria,Algeria\n",
        encoding="utf-8",
    )

    guangxi_dir = data_root / "Asia" / "Guangxi"
    write_tiny_shp(guangxi_dir / "guangxi_buildings_height_gba.shp")
    san_marino_dir = data_root / "Europe" / "San_Marino"
    write_tiny_shp(san_marino_dir / "San_Marino_buildings_height_gba.shp")
    algeria_dir = data_root / "Africa" / "Algeria"
    algeria_dir.mkdir(parents=True, exist_ok=True)
    (algeria_dir / "Algeria_buildings_height_gba.gpkg").write_bytes(b"gpkg-only")
    scan_only_dir = data_root / "mongolia_gba_wfs"
    write_tiny_shp(scan_only_dir / "mongolia_buildings_height_gba.shp")

    store = lib.StateStore(download_db)
    try:
        store.sync_manifest(
            [
                lib.Task("Asia", "China", "Guangxi", "data/Asia/Guangxi"),
                lib.Task("Europe", "San_Marino", "San_Marino", "data/Europe/San_Marino"),
                lib.Task("Africa", "Algeria", "Algeria", "data/Africa/Algeria"),
            ]
        )
        store.set_existing_data("Asia|China|Guangxi", "data/Asia/Guangxi")
        store.set_status(
            "Europe|San_Marino|San_Marino",
            lib.STATUS_OK,
            feature_count=2,
            finished_at="2026-01-01T00:00:00",
        )
        store.set_existing_data("Africa|Algeria|Algeria", "data/Africa/Algeria")
    finally:
        store.close()
    return download_db, data_root


def row_to_source(row: dict) -> dict:
    return {
        "dataset_key": row["dataset_key"],
        "task_id": row["task_id"],
        "display_name": row["display_name"],
        "process_name_prefix": row.get("process_name_prefix") or runner.dataset_slug(row["dataset_key"]),
        "source_dir": row["source_dir"],
        "shp_files": json.loads(row["shp_files"] or "[]"),
        "feature_count": row["feature_count"],
    }


class SyncTests(unittest.TestCase):
    def test_sync_registers_pending_scan_and_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"

            result = runner.sync_process_tasks(
                repo_root,
                process_db,
                download_db,
                data_root,
                [],
                [],
            )

            self.assertEqual(result["total"], 4)
            store = runner.ProcessStore(process_db)
            try:
                rows = {row["dataset_key"]: row for row in store.all_tasks()}
                self.assertEqual(rows["Asia|China|Guangxi"]["status"], runner.PROCESS_PENDING)
                self.assertEqual(rows["Asia|China|Guangxi"]["feature_count"], 2)
                self.assertEqual(rows["Asia|China|Guangxi"]["process_name_prefix"], "guangxi")
                self.assertEqual(rows["Europe|San_Marino|San_Marino"]["status"], runner.PROCESS_PENDING)
                self.assertEqual(rows["Europe|San_Marino|San_Marino"]["process_name_prefix"], "San_Marino")
                self.assertEqual(rows["Africa|Algeria|Algeria"]["status"], runner.PROCESS_BLOCKED)
                self.assertEqual(rows["Africa|Algeria|Algeria"]["process_name_prefix"], "Algeria")
                self.assertIn("mongo", rows["EXT|data/mongolia_gba_wfs"]["dataset_key"])
                self.assertEqual(rows["EXT|data/mongolia_gba_wfs"]["status"], runner.PROCESS_PENDING)
                self.assertEqual(rows["EXT|data/mongolia_gba_wfs"]["process_name_prefix"], "mongolia")
                blocked = rows["Africa|Algeria|Algeria"]
                self.assertIn("export-shp-only", blocked["last_error"])
            finally:
                store.close()

    def test_sync_recovers_blocked_when_shp_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])

            algeria_dir = data_root / "Africa" / "Algeria"
            write_tiny_shp(algeria_dir / "Algeria_buildings_height_gba.shp")
            runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])

            store = runner.ProcessStore(process_db)
            try:
                self.assertEqual(store.get("Africa|Algeria|Algeria")["status"], runner.PROCESS_PENDING)
            finally:
                store.close()

    def test_sync_only_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"

            result = runner.sync_process_tasks(
                repo_root,
                process_db,
                download_db,
                data_root,
                [],
                ["Guangxi"],
            )

            self.assertEqual(result["total"], 1)
            store = runner.ProcessStore(process_db)
            try:
                self.assertEqual([row["dataset_key"] for row in store.all_tasks()], ["Asia|China|Guangxi"])
            finally:
                store.close()

    def test_sync_register_extra_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            extra = repo_root / "custom" / "shenzhen_gba_wfs"
            write_tiny_shp(extra / "shenzhen_buildings_height_gba.shp")

            runner.sync_process_tasks(
                repo_root,
                repo_root / "process.db",
                download_db,
                data_root,
                [extra],
                [],
                scan_enabled=False,
            )

            store = runner.ProcessStore(repo_root / "process.db")
            try:
                keys = [row["dataset_key"] for row in store.all_tasks()]
                self.assertIn("EXT|custom/shenzhen_gba_wfs", keys)
            finally:
                store.close()


class PrefixTests(unittest.TestCase):
    def test_load_manifest_prefixes_from_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            prefixes = runner.load_manifest_prefixes(repo_root, "data/world_building_download_tasks.csv")
            self.assertEqual(prefixes["Asia|China|Guangxi"], "guangxi")
            self.assertEqual(prefixes["Europe|San_Marino|San_Marino"], "San_Marino")

    def test_derive_prefix_from_files(self):
        self.assertEqual(
            runner.derive_prefix_from_files(["data/x/guangxi_buildings_height_gba.shp"]),
            "guangxi",
        )
        self.assertEqual(
            runner.derive_prefix_from_files(["data/x/guangxi_buildings_height_gba_part03.shp"]),
            "guangxi",
        )
        self.assertIsNone(runner.derive_prefix_from_files([]))

    def test_resolve_prefix_fallbacks(self):
        prefix_map = {"Asia|China|Guangxi": "guangxi"}
        self.assertEqual(
            runner.resolve_process_name_prefix(
                {"dataset_key": "Asia|China|Guangxi", "task_id": "Asia|China|Guangxi", "shp_files": []},
                prefix_map,
            ),
            "guangxi",
        )
        self.assertEqual(
            runner.resolve_process_name_prefix(
                {
                    "dataset_key": "EXT|data/mongolia_gba_wfs",
                    "task_id": None,
                    "shp_files": ["data/mongolia_gba_wfs/mongolia_buildings_height_gba.shp"],
                },
                prefix_map,
            ),
            "mongolia",
        )


class PrepareInputTests(unittest.TestCase):
    def test_prepare_input_dir_renames_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shp = write_tiny_shp(repo_root / "data" / "src" / "guangxi_buildings_height_gba.shp")
            source = {
                "dataset_key": "Asia|China|Guangxi",
                "process_name_prefix": "guangxi",
                "shp_files": [lib.repo_relative(repo_root, shp)],
            }

            input_dir, renames = runner.prepare_input_dir(repo_root, source, repo_root / "out")

            self.assertEqual(input_dir.name, "guangxi_pipeline_input")
            self.assertTrue((input_dir / "guangxi.shp").exists())
            self.assertTrue((input_dir / "guangxi.dbf").exists())
            self.assertEqual(
                renames,
                [{"source": "data/src/guangxi_buildings_height_gba.shp", "input": "guangxi.shp"}],
            )

    def test_prepare_input_dir_numbers_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            first = write_tiny_shp(repo_root / "data" / "src" / "guangxi_buildings_height_gba_part1.shp")
            second = write_tiny_shp(repo_root / "data" / "src" / "guangxi_buildings_height_gba_part2.shp")
            source = {
                "dataset_key": "Asia|China|Guangxi",
                "process_name_prefix": "guangxi",
                "shp_files": [lib.repo_relative(repo_root, first), lib.repo_relative(repo_root, second)],
            }

            input_dir, renames = runner.prepare_input_dir(repo_root, source, repo_root / "out")

            self.assertTrue((input_dir / "guangxi_part01.shp").exists())
            self.assertTrue((input_dir / "guangxi_part02.shp").exists())
            self.assertEqual([item["input"] for item in renames], ["guangxi_part01.shp", "guangxi_part02.shp"])


class ParseStageTests(unittest.TestCase):
    def test_parse_stage_tracks_tiles_and_stage(self):
        state = {"stage": "PREPARE", "stage_detail": "", "tiles_total": None}
        runner.parse_stage("Starting pipeline: input=/x output=/y", state)
        self.assertEqual(state["stage"], "PREPARE")
        runner.parse_stage("Loaded a.shp: feature_count=200000", state)
        self.assertEqual(state["stage"], "SPLIT")
        runner.parse_stage("Split a.shp into 3 chunk(s)", state)
        self.assertEqual(state["tiles_total"], 3)
        runner.parse_stage("Skipping split for b.shp", state)
        self.assertEqual(state["tiles_total"], 4)
        runner.parse_stage("Running QGIS validity check: a_001_001.shp", state)
        self.assertEqual(state["stage"], "VALIDATE")
        runner.parse_stage("Cleaning valid output: a_001_001_valid.shp", state)
        self.assertEqual(state["stage"], "CLEAN")
        runner.parse_stage("Reprojecting to EPSG:3857: a_001_001_clean.shp", state)
        self.assertEqual(state["stage"], "REPROJECT")
        runner.parse_stage("Completed a.shp; cleaned temporary split/valid data", state)
        self.assertEqual(state["stage"], "VERIFY")


class RunPipelineProcessTests(unittest.TestCase):
    def test_streams_output_and_returns_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / runner.DEFAULT_PIPELINE_DIR).mkdir(parents=True, exist_ok=True)
            log_path = repo_root / "logs" / "stdout.log"
            script = (
                "print('Starting pipeline: input=/x output=/y')\n"
                "print('Split a.shp into 2 chunk(s)')\n"
                "print('Completed a.shp; cleaned temporary split/valid data')\n"
            )
            seen: list[str] = []
            command = [sys.executable, "-u", "-c", script]

            exit_code = runner.run_pipeline_process(repo_root, command, log_path, seen.append)

            self.assertEqual(exit_code, 0)
            self.assertIn("Split a.shp into 2 chunk(s)", log_path.read_text(encoding="utf-8"))
            self.assertTrue(any("chunk(s)" in line for line in seen))


class ExecuteTaskTests(unittest.TestCase):
    def _make_args(self, download_db: Path) -> argparse.Namespace:
        return argparse.Namespace(python_exe="python", download_db=download_db)

    def test_execute_task_success_writes_marker_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            output_root = repo_root / "out_data"
            runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], ["Guangxi"])
            store = runner.ProcessStore(process_db)
            try:
                row = store.get("Asia|China|Guangxi")
                source = row_to_source(row)

                def fake_run(repo_root_arg, command, log_path, on_progress):
                    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(log_path).write_text("Split a.shp into 1 chunk(s)\n", encoding="utf-8")
                    on_progress("Split a.shp into 1 chunk(s)")
                    final_dir = output_root / "guangxi_pipeline" / "final"
                    write_tiny_shp(final_dir / "guangxi_001_001_3857.shp", features=1, crs=3857)
                    self.assertIn(str(Path(repo_root_arg)), str(repo_root_arg))
                    return 0

                with patch.object(runner, "run_pipeline_process", side_effect=fake_run):
                    result = runner.execute_process_task(
                        repo_root,
                        store,
                        source,
                        self._make_args(download_db),
                        output_root,
                    )

                self.assertEqual(result["status"], runner.PROCESS_OK)
                self.assertEqual(result["tiles"], 1)
                stored = store.get("Asia|China|Guangxi")
                self.assertEqual(stored["status"], runner.PROCESS_OK)
                self.assertEqual(stored["attempts"], 1)
                self.assertEqual(stored["tile_count"], 1)
                self.assertEqual(stored["feature_count"], 1)

                marker = output_root / "guangxi_pipeline" / runner.PROCESS_MARKER_NAME
                self.assertTrue(marker.exists())
                payload = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], runner.PROCESS_OK)
                self.assertEqual(payload["tile_count"], 1)
                self.assertEqual(payload["process_name_prefix"], "guangxi")
                self.assertEqual(payload["input_renames"][0]["input"], "guangxi.shp")

                input_dir = output_root / "guangxi_pipeline_input"
                self.assertTrue((input_dir / "guangxi.shp").exists())
                self.assertTrue((input_dir / "guangxi.dbf").exists())

                download_store = lib.StateStore(download_db)
                try:
                    task_row = download_store.get("Asia|China|Guangxi")
                    expected = lib.repo_relative(repo_root, output_root / "guangxi_pipeline")
                    self.assertEqual(task_row["processed_3857_dir"], expected)
                finally:
                    download_store.close()
            finally:
                store.close()

    def test_execute_task_records_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            output_root = repo_root / "out_data"
            runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], ["Guangxi"])
            store = runner.ProcessStore(process_db)
            try:
                row = store.get("Asia|China|Guangxi")
                with patch.object(runner, "run_pipeline_process", return_value=2):
                    result = runner.execute_process_task(
                        repo_root,
                        store,
                        row_to_source(row),
                        self._make_args(download_db),
                        output_root,
                    )
                self.assertEqual(result["status"], runner.PROCESS_FAILED)
                stored = store.get("Asia|China|Guangxi")
                self.assertEqual(stored["status"], runner.PROCESS_FAILED)
                self.assertIn("2", stored["last_error"])
            finally:
                store.close()

    def test_verify_output_rejects_missing_tiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, tiles, features, reason = runner.verify_output(Path(tmp))
            self.assertFalse(ok)
            self.assertEqual(tiles, 0)
            self.assertTrue(reason)


class CliTests(unittest.TestCase):
    def test_run_stops_when_resources_insufficient(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            argv = [
                "run_shp_process_tasks.py",
                "--repo-root",
                str(repo_root),
                "--process-db",
                str(process_db),
                "--download-db",
                str(download_db),
                "--scan-root",
                str(data_root),
                "--output-root",
                str(repo_root / "out_data"),
                "--run",
                "--only",
                "Guangxi",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with patch.object(
                    runner,
                    "wait_for_resources",
                    return_value=(False, "可用内存 1.0GB < 6.0GB"),
                ):
                    with contextlib.redirect_stdout(buffer):
                        code = runner.main()

            self.assertEqual(code, 3)
            self.assertIn("资源不足", buffer.getvalue())
            store = runner.ProcessStore(process_db)
            try:
                self.assertEqual(store.get("Asia|China|Guangxi")["status"], runner.PROCESS_PENDING)
            finally:
                store.close()

    def test_run_dry_run_keeps_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            argv = [
                "run_shp_process_tasks.py",
                "--repo-root",
                str(repo_root),
                "--process-db",
                str(process_db),
                "--download-db",
                str(download_db),
                "--scan-root",
                str(data_root),
                "--output-root",
                str(repo_root / "out_data"),
                "--run",
                "--dry-run",
                "--only",
                "Guangxi",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(buffer):
                    code = runner.main()

            self.assertEqual(code, 0)
            self.assertIn("[PLAN 1/1]", buffer.getvalue())
            self.assertIn("prefix=guangxi", buffer.getvalue())
            self.assertFalse((repo_root / "out_data").exists())
            store = runner.ProcessStore(process_db)
            try:
                self.assertEqual(store.get("Asia|China|Guangxi")["status"], runner.PROCESS_PENDING)
            finally:
                store.close()

    def test_reset_running_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            download_db, data_root = build_repo_fixture(repo_root)
            process_db = repo_root / "process.db"
            runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
            store = runner.ProcessStore(process_db)
            try:
                store.set_status("Asia|China|Guangxi", runner.PROCESS_RUNNING, attempts=1, started_at="2026-01-01T00:00:00")
                self.assertEqual(runner.reset_running_tasks(store), 1)
                row = store.get("Asia|China|Guangxi")
                self.assertEqual(row["status"], runner.PROCESS_PENDING)
                self.assertIsNone(row["started_at"])
            finally:
                store.close()


class QueueOrderTests(unittest.TestCase):
    def _sync_fixture(self, repo_root: Path) -> Path:
        download_db, data_root = build_repo_fixture(repo_root)
        process_db = repo_root / "process.db"
        runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
        return process_db

    def test_schema_migration_adds_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(str(db_path))
            connection.execute(
                "CREATE TABLE process_tasks ("
                "dataset_key TEXT PRIMARY KEY, source_dir TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO process_tasks (dataset_key, source_dir, status, updated_at) "
                "VALUES ('Asia|China|Guangxi', 'data/Asia/Guangxi', 'PENDING', '2026-01-01T00:00:00')"
            )
            connection.commit()
            connection.close()

            store = runner.ProcessStore(db_path)
            try:
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(process_tasks)")}
                self.assertIn("queue_order", columns)
            finally:
                store.close()

    def test_upsert_assigns_incrementing_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                rows = store.all_tasks()
                self.assertEqual([row["queue_order"] for row in rows], [1, 2, 3, 4])

                download_db = repo_root / "download_state.db"
                data_root = repo_root / "data"
                runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
                self.assertEqual([row["queue_order"] for row in store.all_tasks()], [1, 2, 3, 4])
            finally:
                store.close()

    def test_move_up_down_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                keys = [row["dataset_key"] for row in store.pending_tasks()]
                self.assertEqual(keys, ["Asia|China|Guangxi", "EXT|data/mongolia_gba_wfs", "Europe|San_Marino|San_Marino"])

                self.assertEqual(store.move_task(keys[0], "up"), keys)

                moved_down = store.move_task(keys[0], "down")
                self.assertEqual(moved_down[:2], [keys[1], keys[0]])

                moved_top = store.move_task(keys[0], "top")
                self.assertEqual(moved_top[0], keys[0])

                last_top = store.move_task(keys[-1], "top")
                self.assertEqual(last_top[0], keys[-1])

                self.assertEqual([row["queue_order"] for row in store.pending_tasks()], [1, 2, 3])

                with self.assertRaises(KeyError):
                    store.move_task("Asia|China|Nope", "up")
                with self.assertRaises(ValueError):
                    store.move_task(keys[0], "sideways")
            finally:
                store.close()

    def test_reorder_pending_puts_keys_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                ordered = store.reorder_pending(["EXT|data/mongolia_gba_wfs"])
                self.assertEqual(ordered[0], "EXT|data/mongolia_gba_wfs")
                self.assertEqual(len(ordered), 3)
                with self.assertRaises(KeyError):
                    store.reorder_pending(["nope"])
            finally:
                store.close()

    def test_select_tasks_respects_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                store.reorder_pending(["EXT|data/mongolia_gba_wfs", "Europe|San_Marino|San_Marino"])
                tasks = runner.select_tasks(store, [], False, False, 0)
                self.assertEqual(
                    [row["dataset_key"] for row in tasks],
                    ["EXT|data/mongolia_gba_wfs", "Europe|San_Marino|San_Marino", "Asia|China|Guangxi"],
                )
            finally:
                store.close()

    def test_cli_move_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            argv = [
                "run_shp_process_tasks.py",
                "--repo-root",
                str(repo_root),
                "--process-db",
                str(process_db),
                "--move-top",
                "mongo",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(buffer):
                    code = runner.main()

            self.assertEqual(code, 0)
            self.assertIn("[QUEUE 01]", buffer.getvalue())
            store = runner.ProcessStore(process_db)
            try:
                self.assertEqual(store.pending_tasks()[0]["dataset_key"], "EXT|data/mongolia_gba_wfs")
            finally:
                store.close()

    def test_cli_move_unknown_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            argv = [
                "run_shp_process_tasks.py",
                "--repo-root",
                str(repo_root),
                "--process-db",
                str(process_db),
                "--move-top",
                "zzz-not-exist",
            ]
            buffer = io.StringIO()
            with patch.object(sys, "argv", argv):
                with contextlib.redirect_stdout(buffer):
                    code = runner.main()

            self.assertEqual(code, 1)
            self.assertIn("[ERROR]", buffer.getvalue())


class ProcessRequeueTests(unittest.TestCase):
    def _sync_fixture(self, repo_root: Path) -> Path:
        download_db, data_root = build_repo_fixture(repo_root)
        process_db = repo_root / "process.db"
        runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
        return process_db

    def test_requeue_failed_task_tail_and_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                keys = [row["dataset_key"] for row in store.pending_tasks()]
                store.set_status(
                    keys[0],
                    runner.PROCESS_FAILED,
                    attempts=2,
                    last_error="boom",
                    started_at="2026-01-01T00:00:00",
                    finished_at="2026-01-01T00:10:00",
                    duration_seconds=600,
                )

                queue = store.requeue_task(keys[0], "tail")
                self.assertEqual(queue[-1], keys[0])
                row = store.get(keys[0])
                self.assertEqual(row["status"], runner.PROCESS_PENDING)
                self.assertIsNone(row["last_error"])
                self.assertIsNone(row["started_at"])
                self.assertIsNone(row["finished_at"])
                self.assertIsNone(row["duration_seconds"])

                store.set_status(keys[1], runner.PROCESS_FAILED, last_error="boom")
                queue = store.requeue_task(keys[1], "top")
                self.assertEqual(queue[0], keys[1])
                self.assertEqual(store.pending_tasks()[0]["dataset_key"], keys[1])

                events = [
                    row["event"]
                    for row in store.conn.execute(
                        "SELECT event FROM process_events WHERE dataset_key = ? ORDER BY event_id DESC",
                        (keys[0],),
                    )
                ]
                self.assertIn("PROCESS_REQUEUE", events)
            finally:
                store.close()

    def test_requeue_rejects_non_failed_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                key = store.pending_tasks()[0]["dataset_key"]
                with self.assertRaises(ValueError):
                    store.requeue_task(key, "tail")
                with self.assertRaises(KeyError):
                    store.requeue_task("Asia|China|Nope", "tail")

                store.set_status(key, runner.PROCESS_FAILED, last_error="boom")
                with self.assertRaises(ValueError):
                    store.requeue_task(key, "sideways")
                self.assertEqual(store.get(key)["status"], runner.PROCESS_FAILED)

                queue = runner.requeue_task(process_db, key, "top")
                self.assertEqual(queue[0], key)
                self.assertEqual(store.get(key)["status"], runner.PROCESS_PENDING)
            finally:
                store.close()


class NoteTests(unittest.TestCase):
    def _sync_fixture(self, repo_root: Path) -> Path:
        download_db, data_root = build_repo_fixture(repo_root)
        process_db = repo_root / "process.db"
        runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
        return process_db

    def test_schema_migration_adds_note_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(str(db_path))
            connection.execute(
                "CREATE TABLE process_tasks ("
                "dataset_key TEXT PRIMARY KEY, source_dir TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL)"
            )
            connection.commit()
            connection.close()

            store = runner.ProcessStore(db_path)
            try:
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(process_tasks)")}
                self.assertIn("note", columns)
            finally:
                store.close()

    def test_set_note_writes_and_trims(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                saved = store.set_note("Asia|China|Guangxi", "  待确认边界  ")
                self.assertEqual(saved, "待确认边界")
                self.assertEqual(store.get("Asia|China|Guangxi")["note"], "待确认边界")
            finally:
                store.close()

    def test_sync_and_reset_keep_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = self._sync_fixture(repo_root)
            store = runner.ProcessStore(process_db)
            try:
                store.set_note("Asia|China|Guangxi", "备注不能丢")

                download_db = repo_root / "download_state.db"
                data_root = repo_root / "data"
                runner.sync_process_tasks(repo_root, process_db, download_db, data_root, [], [])
                self.assertEqual(store.get("Asia|China|Guangxi")["note"], "备注不能丢")

                store.reset_task("Asia|China|Guangxi", "测试重置")
                self.assertEqual(store.get("Asia|China|Guangxi")["note"], "备注不能丢")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
