import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import world_tasks_lib as lib


def make_task(
    continent: str = "Africa",
    country: str = "Algeria",
    city: str = "Algeria",
    download_dir: str = "data/Africa/Algeria/Algeria",
) -> lib.Task:
    return lib.Task(
        continent=continent,
        country=country,
        city=city,
        download_dir=download_dir,
        source_row=2,
    )


class ManifestTests(unittest.TestCase):
    def test_read_manifest_gbk(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "tasks.csv"
            content = (
                "continent,country,city,download_dir\n"
                "亚洲,中国,广东省,data\\亚洲\\中国\\广东省\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria\n"
            )
            manifest.write_bytes(content.encode("gbk"))

            tasks = lib.read_manifest(manifest)

            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0].task_id, "亚洲|中国|广东省")
            self.assertEqual(tasks[0].download_dir, "data/亚洲/中国/广东省")
            self.assertEqual(tasks[1].task_id, "Africa|Algeria|Algeria")

    def test_read_manifest_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "tasks.csv"
            content = (
                "continent,country,city,download_dir\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria\n"
            )
            manifest.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                lib.read_manifest(manifest)

    def test_read_manifest_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "tasks.csv"
            content = "continent,country,city,download_dir\nAfrica,Algeria,Algeria,data/../../etc\n"
            manifest.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                lib.read_manifest(manifest)


class QueryTests(unittest.TestCase):
    def test_country_level_candidates(self):
        task = make_task(country="United_States_of_America", city="United_States_of_America")
        candidates = lib.query_candidates(task)
        self.assertEqual(candidates[0], {"country": "United States"})
        self.assertEqual(candidates[1], "United States")

    def test_state_level_candidates(self):
        task = make_task(country="United_States_of_America", city="New_York")
        candidates = lib.query_candidates(task)
        self.assertEqual(candidates[0], {"state": "New York", "country": "United States"})
        self.assertEqual(candidates[1], {"city": "New York", "country": "United States"})
        self.assertEqual(candidates[2], "New York, United States")

    def test_country_alias(self):
        self.assertEqual(lib.normalize_country_for_query("Turkmenistann"), "Turkmenistan")
        self.assertEqual(lib.normalize_country_for_query("中国"), "China")
        self.assertEqual(lib.normalize_country_for_query("Congo(DRC)"), "Democratic Republic of the Congo")


class OverrideTests(unittest.TestCase):
    def test_load_and_match_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.csv"
            path.write_text(
                "continent,country,city,kind,value,note\n"
                'Asia,Indonesia,Indonesia_east,bbox,"127,-11,141,2",测试\n',
                encoding="utf-8",
            )
            overrides = lib.load_overrides(path)
            task = make_task(continent="Asia", country="Indonesia", city="Indonesia_east")
            override = lib.match_override(task, overrides)
            self.assertIsNotNone(override)
            self.assertEqual(override.kind, "bbox")
            self.assertEqual(lib.parse_bbox(override.value), (127.0, -11.0, 141.0, 2.0))

    def test_parse_bbox_rejects_invalid(self):
        with self.assertRaises(ValueError):
            lib.parse_bbox("1,2,3")

    def test_resolve_boundary_skip_override(self):
        task = make_task()
        override = lib.RangeOverride("", "", "", "skip", "", "人工跳过")
        result = lib.resolve_boundary(task, [override], Path("data"), Path("."), refresh=True)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "人工跳过")

    def test_resolve_boundary_bbox_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            override = lib.RangeOverride("", "", "", "bbox", "1,2,3,4", "")
            with patch.object(lib, "save_bbox_boundary") as save_bbox:
                save_bbox.return_value = task_dir / lib.BOUNDARY_NAME
                result = lib.resolve_boundary(task_dir=task_dir, task=make_task(), overrides=[override], repo_root=Path(tmp))
            self.assertFalse(result.skipped)
            self.assertEqual(result.source["type"], "override_bbox")
            save_bbox.assert_called_once()


class PathAndCommandTests(unittest.TestCase):
    def test_generate_slug(self):
        self.assertEqual(lib.generate_slug("New York"), "New_York")
        self.assertEqual(lib.generate_slug("广东省"), "广东省")
        self.assertEqual(lib.generate_slug("Congo(DRC)"), "Congo(DRC)")
        self.assertEqual(lib.generate_slug("???"), "task")

    def test_resolve_task_dir_rejects_outside_repo(self):
        with self.assertRaises(ValueError):
            lib.resolve_task_dir(Path.cwd(), "../outside")

    def test_build_worker_command_adaptive(self):
        options = lib.DownloadOptions()
        command = lib.build_worker_command(
            Path("scripts/download_gba_lod1_wfs_adaptive.py"),
            "python",
            Path("data/place_boundary.gpkg"),
            Path("data/Africa/Algeria/Algeria"),
            Path("data/Africa/Algeria/Algeria/Algeria_buildings_height_gba.gpkg"),
            options,
        )
        joined = " ".join(command)
        self.assertIn("-u", command)
        self.assertIn("--boundary", command)
        self.assertIn("--split-threshold", command)
        self.assertIn("--min-grid-size", command)
        self.assertIn("20000", joined)

    def test_build_worker_command_standard_has_no_adaptive_flags(self):
        options = lib.DownloadOptions(engine="standard")
        command = lib.build_worker_command(
            Path("scripts/download_gba_lod1_wfs.py"),
            "python",
            Path("boundary.gpkg"),
            Path("out"),
            Path("out/merge.gpkg"),
            options,
        )
        self.assertNotIn("--split-threshold", command)

    def test_build_worker_command_includes_tile_workers(self):
        options = lib.DownloadOptions(tile_workers=2)
        command = lib.build_worker_command(
            Path("scripts/download_gba_lod1_wfs_adaptive.py"),
            "python",
            Path("boundary.gpkg"),
            Path("out"),
            Path("out/merge.gpkg"),
            options,
        )
        self.assertIn("--tile-workers", command)
        self.assertIn("2", command)

    def test_build_worker_command_omits_tile_workers_when_single(self):
        options = lib.DownloadOptions()
        command = lib.build_worker_command(
            Path("scripts/download_gba_lod1_wfs_adaptive.py"),
            "python",
            Path("boundary.gpkg"),
            Path("out"),
            Path("out/merge.gpkg"),
            options,
        )
        self.assertNotIn("--tile-workers", command)


class LogTextTests(unittest.TestCase):
    def test_read_log_text_decodes_gbk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_bytes("[WFS-RATE] 429 限流，等待 5 秒后重试同一请求（第 1 次）\n".encode("gbk"))

            text = lib.read_log_text(path)

            self.assertIn("限流", text)
            self.assertIn("秒后重试同一请求", text)

    def test_read_log_text_handles_mixed_encodings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_bytes("=== 广东省 start ===\n".encode("utf-8") + "[WFS-RATE] 限流\n".encode("gbk"))

            lines = lib.read_log_text(path).splitlines()

            self.assertEqual(lines[0], "=== 广东省 start ===")
            self.assertEqual(lines[1], "[WFS-RATE] 限流")

    def test_tail_lines_returns_gbk_last_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.err.log"
            path.write_bytes("第一行\n最后一行：失败\n".encode("gbk"))

            self.assertEqual(lib.tail_lines(path, limit=1), "最后一行：失败")

    def test_child_process_env_forces_utf8_stdio(self):
        env = lib.child_process_env()

        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_has_merged_raw_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertFalse(lib.has_merged_raw_data(directory))
            (directory / "GBA_0001.gpkg").write_bytes(b"chunk")
            self.assertFalse(lib.has_merged_raw_data(directory))
            (directory / "algeria_buildings_height_gba.gpkg").write_bytes(b"merged")
            self.assertTrue(lib.has_merged_raw_data(directory))
            self.assertFalse(lib.has_merged_raw_data(directory / "missing"))


class StateStoreTests(unittest.TestCase):
    def test_sync_select_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [make_task(), make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola")]
                result = store.sync_manifest(tasks)
                self.assertEqual(result["added"], 2)

                rows = store.all_tasks()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["status"], lib.STATUS_PENDING)

                store.set_status(tasks[0].task_id, lib.STATUS_OK, feature_count=10, merge_output="x.gpkg")
                counts = store.counts()
                self.assertEqual(counts.get(lib.STATUS_OK), 1)

                selected = lib.select_tasks(
                    store.all_tasks(),
                    countries=["Algeria"],
                    statuses=[lib.STATUS_OK],
                )
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["task_id"], tasks[0].task_id)

                store.reset_task(tasks[0].task_id)
                self.assertEqual(store.get(tasks[0].task_id)["status"], lib.STATUS_PENDING)
            finally:
                store.close()

    def test_sync_resets_success_when_download_dir_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                task = make_task()
                store.sync_manifest([task])
                store.set_status(task.task_id, lib.STATUS_OK, feature_count=1)
                moved = make_task(download_dir="data/Africa/Algeria/new_Algeria")
                store.sync_manifest([moved])
                self.assertEqual(store.get(task.task_id)["status"], lib.STATUS_PENDING)
            finally:
                store.close()

    def test_set_existing_data_and_schema_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(tasks)")}
                self.assertIn("existing_data_dir", columns)
                self.assertIn("processed_3857_dir", columns)

                task = make_task()
                store.sync_manifest([task])
                store.set_existing_data(task.task_id, "data/legacy/Algeria")
                self.assertEqual(store.get(task.task_id)["existing_data_dir"], "data/legacy/Algeria")
                store.set_processed_3857(task.task_id, "data/processed/Algeria_3857")
                self.assertEqual(store.get(task.task_id)["processed_3857_dir"], "data/processed/Algeria_3857")
            finally:
                store.close()


class MarkerTests(unittest.TestCase):
    def test_marker_roundtrip_and_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            task_dir = repo_root / "data" / "Africa" / "Algeria" / "Algeria"
            task_dir.mkdir(parents=True)
            merge_output = task_dir / "Algeria_buildings_height_gba.gpkg"
            merge_output.write_bytes(b"fake gpkg content")

            marker = {
                "status": lib.STATUS_OK,
                "download_dir": "data/Africa/Algeria/Algeria",
                "merge_output": "data/Africa/Algeria/Algeria/Algeria_buildings_height_gba.gpkg",
                "merge_size_bytes": merge_output.stat().st_size,
            }
            lib.write_marker(task_dir, marker)

            loaded = lib.read_marker(task_dir)
            self.assertTrue(lib.marker_is_valid(loaded, repo_root, task_dir, merge_output))

            merge_output.write_bytes(b"changed content size")
            self.assertFalse(lib.marker_is_valid(loaded, repo_root, task_dir, merge_output))

    def test_marker_invalid_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            task_dir = repo_root / "task"
            task_dir.mkdir()
            merge_output = task_dir / "merge.gpkg"
            merge_output.write_bytes(b"x")
            self.assertFalse(lib.marker_is_valid(None, repo_root, task_dir, merge_output))


class NationalGridProgressTests(unittest.TestCase):
    def test_progress_counts_only_expected_valid_completion_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "grid_manifest.json").write_text(
                json.dumps(
                    {
                        "grid_algorithm": "national-grid-v1",
                        "data_semantics_version": "coverage-v2",
                        "grid_ids": ["GRID_A", "GRID_B", "GRID_C"],
                        "expected_tile_count": 3,
                    }
                ),
                encoding="utf-8",
            )
            tile_dir = task_dir / "tiles_national_v1"
            tile_dir.mkdir()
            (tile_dir / "GRID_A.done.json").write_text(
                json.dumps(
                    {
                        "grid_algorithm": "national-grid-v1",
                        "data_semantics_version": "coverage-v2",
                        "grid_id": "GRID_A",
                        "status": "COMPLETE",
                    }
                ),
                encoding="utf-8",
            )
            (tile_dir / "GRID_B.done.json").write_text(
                json.dumps(
                    {
                        "grid_algorithm": "national-grid-v1",
                        "data_semantics_version": "coverage-v2",
                        "grid_id": "GRID_B",
                        "status": "EMPTY",
                    }
                ),
                encoding="utf-8",
            )
            (tile_dir / "GRID_X.done.json").write_text(
                json.dumps(
                    {
                        "grid_algorithm": "national-grid-v1",
                        "data_semantics_version": "coverage-v2",
                        "grid_id": "GRID_X",
                        "status": "COMPLETE",
                    }
                ),
                encoding="utf-8",
            )

            progress = lib.read_national_grid_progress(task_dir)

            self.assertEqual(
                progress,
                {
                    "grid_algorithm": "national-grid-v1",
                    "data_semantics_version": "coverage-v2",
                    "expected": 3,
                    "complete": 1,
                    "empty": 1,
                    "done": 2,
                    "missing": 1,
                },
            )

    def test_progress_returns_none_for_legacy_task_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(lib.read_national_grid_progress(Path(tmp)))


class LocalBoundaryTests(unittest.TestCase):
    def _fake_loader(self):
        admin0 = gpd.GeoDataFrame(
            [
                {"NAME": "San Marino", "ADMIN": "San Marino", "SOVEREIGNT": "San Marino", "NAME_ZH": "圣马力诺", "geometry": box(0, 0, 1, 1)},
                {"NAME": "China", "ADMIN": "China", "SOVEREIGNT": "China", "NAME_ZH": "中国", "geometry": box(1, 1, 2, 2)},
                {"NAME": "United States of America", "ADMIN": "United States of America", "SOVEREIGNT": "United States of America", "NAME_ZH": "美国", "geometry": box(3, 3, 4, 4)},
                {"NAME": "Dem. Rep. Congo", "ADMIN": "Democratic Republic of the Congo", "SOVEREIGNT": "Democratic Republic of the Congo", "NAME_ZH": "刚果民主共和国", "geometry": box(2, 2, 3, 3)},
            ],
            geometry="geometry",
            crs=4326,
        )
        admin1 = gpd.GeoDataFrame(
            [
                {"admin": "China", "name": "Guangdong", "name_en": "Guangdong", "name_zh": "广东省", "geometry": box(1.1, 1.1, 1.2, 1.2)},
                {"admin": "United States of America", "name": "New York", "name_en": "New York", "name_zh": "纽约州", "geometry": box(1.3, 1.3, 1.4, 1.4)},
            ],
            geometry="geometry",
            crs=4326,
        )

        def fake_load(boundaries_dir, layer):
            return admin0 if layer == "admin0" else admin1

        return fake_load

    def test_match_country_level(self):
        with patch.object(lib, "load_admin_boundary", side_effect=self._fake_loader()):
            task = make_task(continent="Europe", country="San_Marino", city="San_Marino")
            match = lib.match_local_boundary(task, Path("data/boundaries"))
            self.assertEqual(match["level"], "admin0")
            self.assertEqual(match["matched_name"], "San Marino")

    def test_match_admin1_with_chinese_names(self):
        with patch.object(lib, "load_admin_boundary", side_effect=self._fake_loader()):
            task = make_task(continent="亚洲", country="中国", city="广东省")
            match = lib.match_local_boundary(task, Path("data/boundaries"))
            self.assertEqual(match["level"], "admin1")
            self.assertEqual(match["matched_name"], "Guangdong")

    def test_match_admin1_with_underscored_english_names(self):
        with patch.object(lib, "load_admin_boundary", side_effect=self._fake_loader()):
            task = make_task(continent="America", country="United_States_of_America", city="New_York")
            match = lib.match_local_boundary(task, Path("data/boundaries"))
            self.assertEqual(match["level"], "admin1")
            self.assertEqual(match["matched_name"], "New York")

    def test_alias_country_match(self):
        with patch.object(lib, "load_admin_boundary", side_effect=self._fake_loader()):
            task = make_task(continent="Africa", country="Congo(DRC)", city="Congo(DRC)")
            match = lib.match_local_boundary(task, Path("data/boundaries"))
            self.assertEqual(match["level"], "admin0")
            self.assertEqual(match["matched_name"], "Dem. Rep. Congo")

    def test_resolve_boundary_prefers_local_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            task = make_task(continent="Europe", country="San_Marino", city="San_Marino")
            with patch.object(lib, "load_admin_boundary", side_effect=self._fake_loader()):
                with patch.object(lib, "save_geometry_boundary") as save_local:
                    with patch.object(lib, "save_geocode_boundary") as geocode:
                        save_local.return_value = task_dir / lib.BOUNDARY_NAME
                        result = lib.resolve_boundary(
                            task,
                            [],
                            task_dir,
                            Path(tmp),
                            boundaries_dir=Path(tmp),
                        )
            self.assertEqual(result.source["type"], "local_boundary")
            save_local.assert_called_once()
            geocode.assert_not_called()


    def test_queue_order_migration_backfills_legacy_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(str(db_path))
            connection.executescript(
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, continent TEXT NOT NULL, country TEXT NOT NULL,
                    city TEXT NOT NULL, download_dir TEXT NOT NULL,
                    manifest_order INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, feature_count INTEGER,
                    merge_output TEXT, boundary_source TEXT, existing_data_dir TEXT,
                    processed_3857_dir TEXT, last_error TEXT, started_at TEXT,
                    finished_at TEXT, duration_seconds INTEGER, updated_at TEXT NOT NULL
                );
                INSERT INTO tasks (task_id, continent, country, city, download_dir, manifest_order, updated_at)
                VALUES ('Africa|Algeria|Algeria','Africa','Algeria','Algeria','data/x',0,'2026-01-01T00:00:00'),
                       ('Africa|Angola|Angola','Africa','Angola','Angola','data/y',1,'2026-01-01T00:00:00');
                """
            )
            connection.commit()
            connection.close()

            store = lib.StateStore(db_path)
            try:
                rows = store.all_tasks()
                self.assertEqual([row["queue_order"] for row in rows], [1, 2])
            finally:
                store.close()

    def test_move_task_up_down_top_and_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [
                    make_task(country="Algeria", city="Algeria"),
                    make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola"),
                    make_task(country="Benin", city="Benin", download_dir="data/Africa/Benin/Benin"),
                ]
                store.sync_manifest(tasks)
                self.assertEqual([row["queue_order"] for row in store.all_tasks()], [1, 2, 3])

                queue = store.move_task(tasks[2].task_id, "top")
                self.assertEqual(queue[0], tasks[2].task_id)
                self.assertEqual(store.pending_tasks()[0]["task_id"], tasks[2].task_id)

                queue = store.move_task(tasks[2].task_id, "down")
                self.assertEqual(queue[0], tasks[0].task_id)
                self.assertEqual(queue[1], tasks[2].task_id)

                queue = store.move_task(tasks[0].task_id, "up")
                self.assertEqual(queue[0], tasks[0].task_id)

                with self.assertRaises(KeyError):
                    store.move_task("Africa|Nowhere|Nowhere", "up")

                store.set_status(tasks[1].task_id, lib.STATUS_OK, feature_count=1)
                with self.assertRaises(KeyError):
                    store.move_task(tasks[1].task_id, "up")
            finally:
                store.close()

    def test_requeue_failed_task_tail_and_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [
                    make_task(country="Algeria", city="Algeria"),
                    make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola"),
                    make_task(country="Benin", city="Benin", download_dir="data/Africa/Benin/Benin"),
                ]
                store.sync_manifest(tasks)
                store.set_status(
                    tasks[1].task_id,
                    lib.STATUS_FAILED,
                    attempts=3,
                    exit_code=1,
                    last_error="boom",
                    started_at="2026-01-01T00:00:00",
                    finished_at="2026-01-01T00:10:00",
                    duration_seconds=600,
                )

                queue = store.requeue_task(tasks[1].task_id, "tail")
                self.assertEqual(queue[-1], tasks[1].task_id)
                row = store.get(tasks[1].task_id)
                self.assertEqual(row["status"], lib.STATUS_PENDING)
                self.assertEqual(row["attempts"], 0)
                self.assertIsNone(row["exit_code"])
                self.assertIsNone(row["last_error"])
                self.assertIsNone(row["duration_seconds"])

                store.set_status(tasks[2].task_id, lib.STATUS_FAILED, last_error="boom")
                queue = store.requeue_task(tasks[2].task_id, "top")
                self.assertEqual(queue[0], tasks[2].task_id)
                self.assertEqual(store.pending_tasks()[0]["task_id"], tasks[2].task_id)

                events = [event["event"] for event in store.recent_events(tasks[1].task_id)]
                self.assertIn("REQUEUE", events)
            finally:
                store.close()

    def test_requeue_completed_tasks_to_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [
                    make_task(country="Algeria", city="Algeria"),
                    make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola"),
                    make_task(country="Benin", city="Benin", download_dir="data/Africa/Benin/Benin"),
                ]
                store.sync_manifest(tasks)

                for status, task in (
                    (lib.STATUS_OK, tasks[1]),
                    (lib.STATUS_OK_EMPTY, tasks[2]),
                ):
                    store.set_status(
                        task.task_id,
                        status,
                        attempts=2,
                        feature_count=12,
                        finished_at="2026-01-01T00:10:00",
                        duration_seconds=600,
                    )
                    queue = store.requeue_task(task.task_id, "top")
                    self.assertEqual(queue[0], task.task_id)
                    row = store.get(task.task_id)
                    self.assertEqual(row["status"], lib.STATUS_PENDING)
                    self.assertEqual(row["attempts"], 0)
                    self.assertIsNone(row["feature_count"])
                    self.assertIsNone(row["finished_at"])
                    self.assertIsNone(row["duration_seconds"])
            finally:
                store.close()

    def test_requeue_pending_task_to_top_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [
                    make_task(country="Algeria", city="Algeria"),
                    make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola"),
                ]
                store.sync_manifest(tasks)

                queue = store.requeue_task(tasks[1].task_id, "top")

                self.assertEqual(queue[0], tasks[1].task_id)
                self.assertEqual(store.get(tasks[1].task_id)["status"], lib.STATUS_PENDING)
            finally:
                store.close()

    def test_requeue_rejects_running_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                task = make_task()
                store.sync_manifest([task])
                store.set_status(task.task_id, lib.STATUS_RUNNING)
                with self.assertRaises(ValueError):
                    store.requeue_task(task.task_id, "top")
                with self.assertRaises(KeyError):
                    store.requeue_task("Africa|Nowhere|Nowhere", "tail")

                store.set_status(task.task_id, lib.STATUS_FAILED, last_error="boom")
                with self.assertRaises(ValueError):
                    store.requeue_task(task.task_id, "sideways")
                self.assertEqual(store.get(task.task_id)["status"], lib.STATUS_FAILED)
            finally:
                store.close()

    def test_sync_manifest_preserves_queue_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lib.StateStore(Path(tmp) / "state.db")
            try:
                tasks = [
                    make_task(country="Algeria", city="Algeria"),
                    make_task(country="Angola", city="Angola", download_dir="data/Africa/Angola/Angola"),
                ]
                store.sync_manifest(tasks)
                store.move_task(tasks[1].task_id, "top")
                store.sync_manifest(tasks)
                self.assertEqual(store.pending_tasks()[0]["task_id"], tasks[1].task_id)
                self.assertEqual(store.get(tasks[0].task_id)["queue_order"], 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
