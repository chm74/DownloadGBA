import argparse
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_shp_process_tasks
import status_server
import world_tasks_lib as lib


def make_task(continent, country, city, download_dir):
    return lib.Task(
        continent=continent,
        country=country,
        city=city,
        download_dir=download_dir,
    )


def build_repo_fixture(repo_root: Path) -> Path:
    state_db = repo_root / "state.db"
    store = lib.StateStore(state_db)
    try:
        tasks = [
            make_task("Africa", "Algeria", "Algeria", "data/Africa/Algeria/Algeria"),
            make_task("Africa", "Angola", "Angola", "data/Africa/Angola/Angola"),
            make_task("Africa", "Benin", "Benin", "data/Africa/Benin/Benin"),
            make_task("Europe", "San_Marino", "San_Marino", "data/Europe/San_Marino/San_Marino"),
        ]
        store.sync_manifest(tasks)

        task_dir = repo_root / "data" / "Africa" / "Algeria" / "Algeria"
        task_dir.mkdir(parents=True, exist_ok=True)
        grid = gpd.GeoDataFrame(
            {"GRID_ID": ["A", "B", "C"]},
            geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(0, 1, 1, 2)],
            crs=4326,
        )
        grid.to_file(task_dir / "gba_wfs_grid.gpkg", driver="GPKG")
        (task_dir / "GBA_0001.gpkg").write_bytes(b"tile-one")
        (task_dir / "GBA_0002.gpkg").write_bytes(b"tile-two")
        (task_dir / "run.log").write_text("line-1\nSaved 10 features\nline-3\n", encoding="utf-8")

        store.set_status(
            "Africa|Algeria|Algeria",
            lib.STATUS_RUNNING,
            attempts=2,
            started_at="2026-01-01T00:00:00",
        )
        store.set_status(
            "Europe|San_Marino|San_Marino",
            lib.STATUS_OK,
            feature_count=7347,
            duration_seconds=34,
            finished_at="2026-01-01T01:00:00",
        )
        store.set_existing_data("Africa|Angola|Angola", "data/legacy/Angola")
        store.set_processed_3857("Africa|Benin|Benin", "data/processed/Benin_3857")
    finally:
        store.close()
    return state_db


def build_process_fixture(repo_root: Path) -> Path:
    process_db = repo_root / "process.db"
    store = run_shp_process_tasks.ProcessStore(process_db)
    try:
        sources = [
            {
                "dataset_key": "Asia|China|Guangxi",
                "task_id": "Asia|China|Guangxi",
                "display_name": "Guangxi",
                "source_dir": "data/Asia/Guangxi",
                "shp_files": ["data/Asia/Guangxi/guangxi_buildings_height_gba.shp"],
                "feature_count": 5129083,
            },
            {
                "dataset_key": "Europe|San_Marino|San_Marino",
                "task_id": "Europe|San_Marino|San_Marino",
                "display_name": "San_Marino",
                "source_dir": "data/Europe/San_Marino",
                "shp_files": ["data/Europe/San_Marino/San_Marino_buildings_height_gba.shp"],
                "feature_count": 7347,
            },
            {
                "dataset_key": "Africa|Benin|Benin",
                "task_id": "Africa|Benin|Benin",
                "display_name": "Benin",
                "source_dir": "data/Africa/Benin",
                "shp_files": ["data/Africa/Benin/Benin_buildings_height_gba.shp"],
                "feature_count": 100,
            },
            {
                "dataset_key": "Africa|Algeria|Algeria",
                "task_id": "Africa|Algeria|Algeria",
                "display_name": "Algeria",
                "source_dir": "data/Africa/Algeria",
                "shp_files": [],
                "feature_count": None,
                "status": run_shp_process_tasks.PROCESS_BLOCKED,
                "last_error": "只有合并 gpkg，缺少交付 SHP",
            },
            {
                "dataset_key": "Asia|China|Shanghai",
                "task_id": "Asia|China|Shanghai",
                "display_name": "Shanghai",
                "process_name_prefix": "shanghai",
                "source_dir": "data/Asia/Shanghai",
                "shp_files": ["data/Asia/Shanghai/shanghai.shp"],
                "feature_count": 1567600,
            },
            {
                "dataset_key": "Asia|China|Yunnan",
                "task_id": "Asia|China|Yunnan",
                "display_name": "Yunnan",
                "process_name_prefix": "yunnan",
                "source_dir": "data/Asia/Yunnan",
                "shp_files": ["data/Asia/Yunnan/yunnan.shp"],
                "feature_count": 8712955,
            },
        ]
        for source in sources:
            store.upsert_source(source)
        store.set_status(
            "Asia|China|Guangxi",
            run_shp_process_tasks.PROCESS_RUNNING,
            attempts=1,
            started_at="2026-01-01T00:00:00",
            stage="VALIDATE",
            stage_detail="Running QGIS validity check: guangxi_001_001.shp",
            tiles_done=2,
            tiles_total=5,
            last_log="Split a.shp into 5 chunk(s)\nRunning QGIS validity check: guangxi_001_001.shp",
        )
        store.set_status(
            "Europe|San_Marino|San_Marino",
            run_shp_process_tasks.PROCESS_OK,
            tile_count=1,
            feature_count=7347,
            duration_seconds=42,
            finished_at="2026-01-01T02:00:00",
            output_dir="Tools/Oneshp_pipline_qgis/out_data/San_Marino_pipeline",
        )
        store.set_status(
            "Africa|Benin|Benin",
            run_shp_process_tasks.PROCESS_FAILED,
            attempts=2,
            last_error="处理进程退出码 1；boom",
            finished_at="2026-01-01T03:00:00",
        )
        store.set_note("Asia|China|Shanghai", "测试备注")
    finally:
        store.close()
    return process_db


class FormatTests(unittest.TestCase):
    def test_format_continent_english_and_chinese(self):
        self.assertEqual(status_server.format_continent("Africa"), "Africa(非洲)")
        self.assertEqual(status_server.format_continent("America"), "America(美洲)")
        self.assertEqual(status_server.format_continent("亚洲"), "Asia(亚洲)")
        self.assertEqual(status_server.format_continent("Unknown"), "Unknown")

    def test_format_bilingual(self):
        self.assertEqual(status_server.format_bilingual("Angola", "安哥拉"), "Angola(安哥拉)")
        self.assertEqual(status_server.format_bilingual("中国", "中国"), "中国")
        self.assertEqual(status_server.format_bilingual("中国", "中华人民共和国"), "中国")
        self.assertEqual(status_server.format_bilingual("Angola", ""), "Angola")
        self.assertEqual(status_server.format_bilingual("Angola", "nan"), "Angola")

    def test_chinese_names_country_and_admin1(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundaries = Path(tmp)
            (boundaries / lib.ADMIN0_GPKG).write_bytes(b"stub")
            admin0_row = {
                "NAME_ZH": "美国",
                "NAME": "United States of America",
                "ADMIN": "United States of America",
                "SOVEREIGNT": "United States of America",
                "NAME_LONG": "United States of America",
            }
            admin1_row = {"name_zh": "德克萨斯州"}
            status_server._chinese_names.cache_clear()
            with patch.object(lib, "find_admin0_row", return_value=admin0_row):
                with patch.object(lib, "find_admin1_row", return_value=admin1_row):
                    country_zh, city_zh = status_server._chinese_names(
                        "America", "United_States_of_America", "Texas", str(boundaries)
                    )
            self.assertEqual(country_zh, "美国")
            self.assertEqual(city_zh, "德克萨斯州")


class LogTailTests(unittest.TestCase):
    def test_tail_lines_decodes_legacy_gbk_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.log"
            log_path.write_bytes("第一行\n[WFS-RATE] 429 限流，等待 5 秒后重试\n".encode("gbk"))

            tail = status_server._tail_lines(log_path, 1)

            self.assertEqual(tail, ["[WFS-RATE] 429 限流，等待 5 秒后重试"])


class SnapshotTests(unittest.TestCase):
    def test_national_grid_progress_uses_completion_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            tile_dir = task_dir / "tiles_national_v1"
            tile_dir.mkdir()
            (task_dir / "grid_manifest.json").write_text(
                '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                '"expected_tile_count":3,"grid_ids":["GRID_A","GRID_B","GRID_C"]}',
                encoding="utf-8",
            )
            for grid_id, state in (("GRID_A", "COMPLETE"), ("GRID_B", "EMPTY")):
                (tile_dir / f"{grid_id}.done.json").write_text(
                    '{"grid_algorithm":"national-grid-v1","data_semantics_version":"coverage-v2",'
                    f'"grid_id":"{grid_id}","status":"{state}"}}',
                    encoding="utf-8",
                )

            self.assertEqual(status_server._count_tiles(task_dir), 2)
            self.assertEqual(status_server._grid_total(task_dir), 3)

    def test_build_snapshot_reports_running_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            boundaries = repo_root / "data" / "boundaries"
            boundaries.mkdir(parents=True, exist_ok=True)
            (boundaries / lib.ADMIN0_GPKG).write_bytes(b"stub")
            admin0_row = {
                "NAME_ZH": "安哥拉",
                "NAME": "Angola",
                "ADMIN": "Angola",
                "SOVEREIGNT": "Angola",
                "NAME_LONG": "Angola",
            }
            status_server._chinese_names.cache_clear()

            with patch.object(lib, "find_admin0_row", return_value=admin0_row):
                snapshot = status_server.build_snapshot(
                    repo_root,
                    state_db,
                    queue_limit=15,
                    recent_limit=10,
                    boundaries_dir=boundaries,
                )

            self.assertIsNone(snapshot["error"])
            self.assertEqual(snapshot["total"], 4)
            self.assertEqual(snapshot["counts"].get(lib.STATUS_RUNNING), 1)
            self.assertEqual(snapshot["counts"].get(lib.STATUS_PENDING), 2)
            self.assertEqual(snapshot["batch_state"], "running")

            running = snapshot["running"]
            self.assertEqual(len(running), 1)
            entry = running[0]
            self.assertEqual(entry["task_id"], "Africa|Algeria|Algeria")
            self.assertEqual(entry["attempts"], 2)
            self.assertEqual(entry["tiles_done"], 2)
            self.assertEqual(entry["tiles_total"], 3)
            self.assertAlmostEqual(entry["progress"], 2 / 3, places=3)
            self.assertIn("line-3", entry["last_log"][-1])
            self.assertGreater(entry["output_size_bytes"], 0)

            queue = snapshot["queue"]
            self.assertEqual([item["task_id"] for item in queue], ["Africa|Angola|Angola", "Africa|Benin|Benin"])
            self.assertEqual(queue[0]["order"], 1)
            self.assertEqual(queue[0]["continent_display"], "Africa(非洲)")
            self.assertEqual(queue[0]["country_display"], "Angola(安哥拉)")
            self.assertEqual(queue[0]["city_display"], "Angola(安哥拉)")
            self.assertEqual(snapshot["queue_total"], 2)
            self.assertEqual(snapshot["queue_offset"], 0)

            paged = status_server.build_snapshot(
                repo_root,
                state_db,
                queue_limit=1,
                queue_offset=1,
                boundaries_dir=boundaries,
            )
            self.assertEqual(paged["queue_total"], 2)
            self.assertEqual([item["task_id"] for item in paged["queue"]], ["Africa|Benin|Benin"])

            recent = snapshot["recent_done"]
            self.assertEqual(recent[0]["task_id"], "Europe|San_Marino|San_Marino")
            self.assertEqual(recent[0]["feature_count"], 7347)
            self.assertIsNone(recent[0]["existing_data_dir"])

    def test_build_snapshot_marks_worker_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            task_dir = repo_root / "data" / "Africa" / "Algeria" / "Algeria"

            active = status_server.build_snapshot(
                repo_root,
                state_db,
                worker_command_lines=[
                    f'python -u download_gba_lod1_wfs_adaptive.py --output-dir "{task_dir}"'
                ],
            )
            self.assertTrue(active["running"][0]["worker_active"])

            inactive = status_server.build_snapshot(
                repo_root,
                state_db,
                worker_command_lines=["python -u download_gba_lod1_wfs_adaptive.py --output-dir \"D:\\other\""],
            )
            self.assertFalse(inactive["running"][0]["worker_active"])

    def test_build_snapshot_recent_done_reports_raw_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            task_dir = repo_root / "data" / "Europe" / "San_Marino" / "San_Marino"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "san_marino_buildings_height_gba.shp").write_bytes(b"shp")
            status_server.RAW_DATA_CACHE.clear()

            snapshot = status_server.build_snapshot(repo_root, state_db)

            recent = snapshot["recent_done"]
            self.assertEqual(recent[0]["task_id"], "Europe|San_Marino|San_Marino")
            self.assertEqual(recent[0]["existing_data_dir"], "data/Europe/San_Marino/San_Marino")

    def test_build_snapshot_queue_search_matches_english_and_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            translations = {"Angola": "安哥拉", "Benin": "贝宁"}

            def fake_chinese_names(continent, country, city, boundaries_value):
                return "", translations.get(city, "")

            with patch.object(status_server, "_chinese_names", side_effect=fake_chinese_names):
                english = status_server.build_snapshot(repo_root, state_db, queue_search="benin")
                chinese = status_server.build_snapshot(repo_root, state_db, queue_search="安哥拉")
                upper = status_server.build_snapshot(repo_root, state_db, queue_search="ANGOLA")

            self.assertEqual([item["task_id"] for item in english["queue"]], ["Africa|Benin|Benin"])
            self.assertEqual(english["queue_total"], 1)
            self.assertEqual(english["queue_total_all"], 2)
            self.assertEqual([item["task_id"] for item in chinese["queue"]], ["Africa|Angola|Angola"])
            self.assertEqual([item["task_id"] for item in upper["queue"]], ["Africa|Angola|Angola"])

    def test_build_snapshot_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = status_server.build_snapshot(Path(tmp), Path(tmp) / "missing.db")
            self.assertIsNotNone(snapshot["error"])
            self.assertEqual(snapshot["total"], 0)

    def test_build_processing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)

            snapshot = status_server.build_processing_snapshot(repo_root, process_db, recent_limit=10)

            self.assertIsNone(snapshot["error"])
            self.assertEqual(snapshot["total"], 6)
            self.assertEqual(snapshot["counts"].get(run_shp_process_tasks.PROCESS_RUNNING), 1)
            self.assertEqual(snapshot["batch_state"], "running")

            running = snapshot["running"]
            self.assertEqual(len(running), 1)
            entry = running[0]
            self.assertEqual(entry["dataset_key"], "Asia|China|Guangxi")
            self.assertEqual(entry["stage"], "VALIDATE")
            self.assertEqual(entry["stage_label"], "QGIS 校验")
            self.assertEqual(entry["tiles_done"], 2)
            self.assertEqual(entry["tiles_total"], 5)
            self.assertAlmostEqual(entry["progress"], 2 / 5, places=3)
            self.assertTrue(any("QGIS" in line for line in entry["last_log"]))

            queue = snapshot["queue"]
            self.assertEqual([item["dataset_key"] for item in queue], ["Asia|China|Shanghai", "Asia|China|Yunnan"])
            self.assertEqual([item["order"] for item in queue], [1, 2])
            self.assertEqual(queue[0]["process_name_prefix"], "shanghai")
            self.assertEqual(queue[0]["note"], "测试备注")
            self.assertEqual(queue[1]["note"], "")
            self.assertEqual(snapshot["queue_total"], 2)

            recent = snapshot["recent_done"]
            self.assertEqual([item["dataset_key"] for item in recent], ["Europe|San_Marino|San_Marino"])
            self.assertEqual(recent[0]["tile_count"], 1)
            self.assertEqual(recent[0]["feature_count"], 7347)

            self.assertEqual([item["dataset_key"] for item in snapshot["failed"]], ["Africa|Benin|Benin"])
            self.assertIn("退出码", snapshot["failed"][0]["last_error"])

            self.assertEqual([item["dataset_key"] for item in snapshot["blocked"]], ["Africa|Algeria|Algeria"])
            self.assertIn("gpkg", snapshot["blocked"][0]["reason"])

    def test_build_processing_snapshot_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            snapshot = status_server.build_processing_snapshot(repo_root, repo_root / "missing.db")
            self.assertIsNotNone(snapshot["error"])
            self.assertEqual(snapshot["total"], 0)

    def test_render_page_replaces_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            page.write_text("<script>const REFRESH_MS = %%REFRESH_MS%%;</script>", encoding="utf-8")
            rendered = status_server.render_page(page, 15)
            self.assertIn(b"15000", rendered)

    def test_http_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            process_db = build_process_fixture(repo_root)
            page = repo_root / "page.html"
            page.write_text("<html>%%REFRESH_MS%%</html>", encoding="utf-8")

            args = argparse.Namespace(
                host="127.0.0.1",
                port=0,
                state_db=str(state_db),
                process_db=str(process_db),
                page=str(page),
                boundaries_dir=str(repo_root / "data" / "boundaries"),
                python_exe="python",
                action_token="",
                queue_limit=15,
                recent_limit=10,
                log_stale_minutes=10,
                log_tail_lines=2,
                refresh_seconds=15,
            )
            server = status_server.create_server(args, repo_root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["counts"].get(lib.STATUS_PENDING), 2)
                self.assertEqual(payload["processing"]["counts"].get(run_shp_process_tasks.PROCESS_RUNNING), 1)
                self.assertEqual(payload["processing"]["total"], 6)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                    body = response.read().decode("utf-8")
                self.assertIn("15000", body)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status?queue_offset=1&queue_limit=1",
                    timeout=10,
                ) as response:
                    paged = json.loads(response.read().decode("utf-8"))
                self.assertEqual(len(paged["queue"]), 1)
                self.assertEqual(paged["queue_total"], 2)
                self.assertEqual(paged["queue_offset"], 1)
                self.assertEqual(paged["queue_limit"], 1)

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status?queue_search=benin",
                    timeout=10,
                ) as response:
                    searched = json.loads(response.read().decode("utf-8"))
                self.assertEqual([item["task_id"] for item in searched["queue"]], ["Africa|Benin|Benin"])
                self.assertEqual(searched["queue_total"], 1)
                self.assertEqual(searched["queue_total_all"], 2)
                self.assertEqual(searched["queue_search"], "benin")
            finally:
                server.shutdown()
                server.server_close()


class DbUpdateTests(unittest.TestCase):
    def test_build_db_update_snapshot_merges_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            marks_file = repo_root / "data" / "db_update_status.json"

            snapshot = status_server.build_db_update_snapshot(repo_root, process_db, marks_file)

            self.assertIsNone(snapshot["error"])
            self.assertEqual([row["dataset_key"] for row in snapshot["rows"]], ["Europe|San_Marino|San_Marino"])
            self.assertFalse(snapshot["rows"][0]["updated"])
            self.assertEqual(snapshot["counts"], {"total": 1, "updated": 0, "pending": 1})

            saved = status_server.save_db_update_marks(
                marks_file,
                [
                    {
                        "dataset_key": "Europe|San_Marino|San_Marino",
                        "updated": True,
                        "updated_at": "2026-09-17T10:00:00",
                        "note": "已更新 world_building",
                    }
                ],
            )
            self.assertEqual(saved, 1)

            updated = status_server.build_db_update_snapshot(repo_root, process_db, marks_file)
            row = updated["rows"][0]
            self.assertTrue(row["updated"])
            self.assertEqual(row["updated_at"], "2026-09-17T10:00:00")
            self.assertEqual(row["note"], "已更新 world_building")
            self.assertEqual(updated["counts"], {"total": 1, "updated": 1, "pending": 0})

    def test_save_db_update_marks_rejects_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks_file = Path(tmp) / "marks.json"
            with self.assertRaises(ValueError):
                status_server.save_db_update_marks(marks_file, [{"updated": True}])

    def test_build_db_update_snapshot_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            snapshot = status_server.build_db_update_snapshot(
                repo_root, repo_root / "missing.db", repo_root / "marks.json"
            )
            self.assertIsNotNone(snapshot["error"])
            self.assertEqual(snapshot["rows"], [])


class ControlTests(unittest.TestCase):
    def test_process_inventory_decodes_utf8_chinese_command_lines(self):
        payload = (
            '[{"ProcessId":48840,"CommandLine":"python -u run_world_building_tasks.py '
            '--only 亚洲|中国|福建省","Created":123}]'
        ).encode("utf-8")

        def fake_run(command, capture_output=None, text=None, timeout=None):
            if text:
                raise UnicodeDecodeError("gbk", payload, 95, 96, "simulated locale mismatch")
            return type("Result", (), {"stdout": payload, "returncode": 0})()

        with patch.object(status_server.subprocess, "run", side_effect=fake_run):
            processes = status_server._list_python_processes()

        self.assertEqual(processes[0]["ProcessId"], 48840)
        self.assertIn("亚洲|中国|福建省", processes[0]["CommandLine"])

    def test_process_inventory_forces_powershell_utf8_for_detached_server(self):
        captured = {}

        def fake_run(command, capture_output=None, timeout=None):
            captured["command"] = command
            return type("Result", (), {"stdout": b"[]", "returncode": 0})()

        with patch.object(status_server.subprocess, "run", side_effect=fake_run):
            self.assertEqual(status_server._list_python_processes(), [])

        powershell_script = captured["command"][-1]
        self.assertIn("[Console]::OutputEncoding", powershell_script)
        self.assertIn("UTF8Encoding", powershell_script)

    def test_detect_batch_picks_newest_runner(self):
        processes = [
            {"ProcessId": 10, "CommandLine": "python -u run_world_building_tasks.py --tile-workers 2", "Created": 100},
            {"ProcessId": 20, "CommandLine": "python -u run_world_building_tasks.py --tile-workers 3", "Created": 200},
            {"ProcessId": 30, "CommandLine": "python download_gba_lod1_wfs_adaptive.py", "Created": 300},
        ]
        with patch.object(status_server, "_list_python_processes", return_value=processes):
            detected = status_server.detect_batch()
        self.assertEqual(detected["runner"]["pid"], 20)
        self.assertEqual(detected["tile_workers"], 3)
        self.assertEqual(detected["runner_count"], 2)
        self.assertEqual([item["pid"] for item in detected["workers"]], [30])

    def test_normalize_tile_workers(self):
        self.assertEqual(status_server.normalize_tile_workers(3), 3)
        with self.assertRaises(ValueError):
            status_server.normalize_tile_workers(0)
        with self.assertRaises(ValueError):
            status_server.normalize_tile_workers(7)
        with self.assertRaises(ValueError):
            status_server.normalize_tile_workers("abc")

    def test_build_restart_command_preserves_filters(self):
        base = (
            r"python.exe -u E:\LoD1\scripts\run_world_building_tasks.py --sleep-seconds 2 "
            r"--task-attempts 2 --tile-workers 2 --continent Africa --retry-failed"
        )
        command = status_server.build_restart_command("python", Path(r"E:\LoD1"), 3, base)
        self.assertIn("-u", command)
        self.assertEqual(command[command.index("--tile-workers") + 1], "3")
        self.assertEqual(command[command.index("--continent") + 1], "Africa")
        self.assertEqual(command[command.index("--sleep-seconds") + 1], "2")
        self.assertIn("--retry-failed", command)

    def test_build_restart_command_defaults(self):
        command = status_server.build_restart_command("python", Path("E:/LoD1"), 2, None)
        self.assertEqual(command[command.index("--tile-workers") + 1], "2")
        self.assertEqual(command[command.index("--sleep-seconds") + 1], "2")
        self.assertEqual(command[command.index("--task-attempts") + 1], "2")

    def _make_server(self, repo_root: Path, action_token: str = ""):
        page = repo_root / "page.html"
        page.write_text("<html>%%REFRESH_MS%%</html>", encoding="utf-8")
        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_db=str(repo_root / "state.db"),
            process_db=str(repo_root / "process.db"),
            page=str(page),
            boundaries_dir=str(repo_root / "data" / "boundaries"),
            python_exe="python",
            action_token=action_token,
            queue_limit=15,
            recent_limit=10,
            log_stale_minutes=10,
            log_tail_lines=2,
            refresh_seconds=15,
        )
        server = status_server.create_server(args, repo_root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server

    def test_restart_endpoint_calls_restart_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server,
                    "restart_batch",
                    return_value={"ok": True, "tile_workers": 3, "runner_pid": 123, "stopped_pids": [1]},
                ) as restart:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/restart",
                        data=json.dumps({"tile_workers": 3}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                restart.assert_called_once()
                self.assertEqual(restart.call_args[0][1], 3)
            finally:
                server.shutdown()
                server.server_close()

    def test_restart_endpoint_rejects_bad_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/restart",
                    data=json.dumps({"tile_workers": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_restart_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/restart",
                    data=json.dumps({"tile_workers": 2}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()


    def test_stop_batch_action_stops_runner_before_workers(self):
        calls: list[list[int]] = []
        detected = {
            "runner": {"pid": 100, "command_line": "python -u run_world_building_tasks.py --tile-workers 2"},
            "runners": [{"pid": 100, "command_line": "python -u run_world_building_tasks.py --tile-workers 2"}],
            "runner_count": 1,
            "workers": [{"pid": 200, "command_line": "python download_gba_lod1_wfs_adaptive.py"}],
            "tile_workers": 2,
        }
        detected_after = {
            "runner": None,
            "runners": [],
            "runner_count": 0,
            "workers": [{"pid": 201, "command_line": "python download_gba_lod1_wfs_adaptive.py"}],
            "tile_workers": None,
        }
        config = {"repo_root": Path("."), "python_exe": "python", "action_token": ""}
        with patch.object(status_server, "detect_batch", side_effect=[detected, detected_after]):
            with patch.object(
                status_server,
                "stop_batch",
                side_effect=lambda pids: calls.append(list(pids)) or list(pids),
            ):
                with patch.object(status_server, "_wait_for_exit"):
                    result = status_server.stop_batch_action(config)

        self.assertEqual(calls[0], [100])
        self.assertEqual(calls[1], [200, 201])
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped_pids"], [100, 200, 201])

    def test_stop_endpoint_calls_stop_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server,
                    "stop_batch_action",
                    return_value={"ok": True, "stopped_pids": [1, 2], "runner_pid": 1},
                ) as stop_action:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/stop",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["stopped_pids"], [1, 2])
                stop_action.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()

    def test_stop_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_reorder_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/reorder",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "direction": "up"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["queue"][:2], ["Asia|China|Yunnan", "Asia|China|Shanghai"])

                store = run_shp_process_tasks.ProcessStore(process_db)
                try:
                    self.assertEqual(
                        [row["dataset_key"] for row in store.pending_tasks()],
                        ["Asia|China|Yunnan", "Asia|China|Shanghai"],
                    )
                finally:
                    store.close()

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(
                    [item["dataset_key"] for item in snapshot["processing"]["queue"]],
                    ["Asia|China|Yunnan", "Asia|China|Shanghai"],
                )
                self.assertEqual([item["order"] for item in snapshot["processing"]["queue"]], [1, 2])
            finally:
                server.shutdown()
                server.server_close()

    def test_process_reorder_endpoint_rejects_bad_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/reorder",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "direction": "sideways"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_reorder_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/reorder",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "direction": "up"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_reorder_endpoint_moves_pending_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/reorder",
                    data=json.dumps({"task_id": "Africa|Benin|Benin", "direction": "top"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["queue"][0], "Africa|Benin|Benin")

                store = lib.StateStore(repo_root / "state.db")
                try:
                    self.assertEqual(store.pending_tasks()[0]["task_id"], "Africa|Benin|Benin")
                finally:
                    store.close()

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(snapshot["queue"][0]["task_id"], "Africa|Benin|Benin")
                self.assertEqual(snapshot["queue"][0]["order"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_reorder_endpoint_rejects_bad_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/reorder",
                    data=json.dumps({"task_id": "Africa|Benin|Benin", "direction": "sideways"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_reorder_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/reorder",
                    data=json.dumps({"task_id": "Africa|Benin|Benin", "direction": "top"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_adds_failed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            store = lib.StateStore(state_db)
            try:
                store.set_status("Africa|Angola|Angola", lib.STATUS_FAILED, attempts=3, last_error="boom")
            finally:
                store.close()
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/requeue",
                    data=json.dumps({"task_id": "Africa|Angola|Angola", "position": "tail"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["position"], "tail")
                self.assertEqual(payload["queue"][-1], "Africa|Angola|Angola")
                self.assertEqual(payload["pending_total"], len(payload["queue"]))

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertIn("Africa|Angola|Angola", [item["task_id"] for item in snapshot["queue"]])
                self.assertEqual(snapshot["failed"], [])

                store = lib.StateStore(state_db)
                try:
                    row = store.get("Africa|Angola|Angola")
                    self.assertEqual(row["status"], lib.STATUS_PENDING)
                    self.assertEqual(row["attempts"], 0)
                    self.assertIsNone(row["last_error"])
                finally:
                    store.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_moves_failed_task_to_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            store = lib.StateStore(state_db)
            try:
                store.set_status("Africa|Angola|Angola", lib.STATUS_FAILED, last_error="boom")
            finally:
                store.close()
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/requeue",
                    data=json.dumps({"task_id": "Africa|Angola|Angola", "position": "top"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["queue"][0], "Africa|Angola|Angola")

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(snapshot["queue"][0]["task_id"], "Africa|Angola|Angola")
                self.assertEqual(snapshot["queue"][0]["order"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_moves_completed_task_to_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/requeue",
                    data=json.dumps(
                        {"task_id": "Europe|San_Marino|San_Marino", "position": "top"}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["queue"][0], "Europe|San_Marino|San_Marino")

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(snapshot["queue"][0]["task_id"], "Europe|San_Marino|San_Marino")
                self.assertNotIn(
                    "Europe|San_Marino|San_Marino",
                    [item["task_id"] for item in snapshot["recent_done"]],
                )

                store = lib.StateStore(state_db)
                try:
                    row = store.get("Europe|San_Marino|San_Marino")
                    self.assertEqual(row["status"], lib.STATUS_PENDING)
                    self.assertIsNone(row["feature_count"])
                finally:
                    store.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_moves_pending_task_to_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/requeue",
                    data=json.dumps({"task_id": "Africa|Benin|Benin", "position": "top"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["queue"][0], "Africa|Benin|Benin")

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(snapshot["queue"][0]["task_id"], "Africa|Benin|Benin")
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_rejects_running_and_bad_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                for body in (
                    {"task_id": "Africa|Algeria|Algeria", "position": "tail"},
                    {"task_id": "Africa|Angola|Angola", "position": "sideways"},
                    {"task_id": "", "position": "tail"},
                ):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/queue/requeue",
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(request, timeout=10)
                    self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/queue/requeue",
                    data=json.dumps({"task_id": "Africa|Angola|Angola", "position": "tail"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_db_updated_endpoint_saves_and_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_process_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/db_updated",
                    data=json.dumps(
                        {
                            "rows": [
                                {
                                    "dataset_key": "Europe|San_Marino|San_Marino",
                                    "updated": True,
                                    "updated_at": "2026-09-17T11:00:00",
                                    "note": "manual",
                                }
                            ]
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["saved"], 1)

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/db_updated", timeout=10) as response:
                    snapshot = json.loads(response.read().decode("utf-8"))
                self.assertEqual(snapshot["counts"]["updated"], 1)
                self.assertEqual(snapshot["rows"][0]["note"], "manual")
            finally:
                server.shutdown()
                server.server_close()

    def test_db_updated_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/db_updated",
                    data=json.dumps({"rows": []}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_db_updated_endpoint_rejects_bad_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/db_updated",
                    data=json.dumps({"rows": [{"updated": True}]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_detect_process_batch(self):
        processes = [
            {
                "ProcessId": 11,
                "CommandLine": 'python -u E:\\LoD1\\scripts\\run_shp_process_tasks.py --run --limit 3 --wait-resources',
                "Created": 300,
            },
            {
                "ProcessId": 12,
                "CommandLine": 'python -u E:\\LoD1\\Tools\\Oneshp_pipline_qgis\\pipeline.py in out',
                "Created": 400,
            },
            {
                "ProcessId": 13,
                "CommandLine": 'python -u E:\\LoD1\\scripts\\run_world_building_tasks.py --tile-workers 2',
                "Created": 500,
            },
        ]
        with patch.object(status_server, "_list_python_processes", return_value=processes):
            detected = status_server.detect_process_batch()
        self.assertEqual(detected["runner"]["pid"], 11)
        self.assertEqual(detected["limit"], 3)
        self.assertTrue(detected["wait_resources"])
        self.assertEqual([item["pid"] for item in detected["workers"]], [12])

    def test_build_process_start_command(self):
        command = status_server.build_process_start_command("python", Path("E:/LoD1"), 0, True)
        self.assertEqual(command[0], "python")
        self.assertTrue(any("run_shp_process_tasks.py" in part for part in command))
        self.assertNotIn("--limit", command)
        self.assertIn("--wait-resources", command)

        limited = status_server.build_process_start_command("python", Path("E:/LoD1"), 3, False)
        self.assertEqual(limited[limited.index("--limit") + 1], "3")
        self.assertNotIn("--wait-resources", limited)

    def test_process_control_snapshot_reports_current_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            config = {"repo_root": repo_root, "process_db": str(process_db), "action_token": ""}
            empty_detected = {
                "runner": None,
                "runners": [],
                "runner_count": 0,
                "workers": [],
                "limit": 0,
                "wait_resources": False,
            }
            with patch.object(status_server, "detect_process_batch", return_value=empty_detected):
                snapshot = status_server.build_process_control_snapshot(config)
            self.assertFalse(snapshot["running"])
            self.assertIsNone(snapshot["runner_pid"])
            self.assertEqual(snapshot["current_task"]["dataset_key"], "Asia|China|Guangxi")
            self.assertEqual(snapshot["current_task"]["stage_label"], "QGIS 校验")

    def test_process_start_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            empty_detected = {
                "runner": None,
                "runners": [],
                "runner_count": 0,
                "workers": [],
                "limit": 0,
                "wait_resources": False,
            }
            try:
                with patch.object(status_server, "detect_process_batch", return_value=empty_detected):
                    with patch.object(
                        status_server,
                        "start_process_batch",
                        return_value={"pid": 4242, "command": []},
                    ) as start:
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/process/start",
                            data=json.dumps({"limit": 2, "wait_resources": True}).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=10) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["runner_pid"], 4242)
                self.assertEqual(payload["limit"], 2)
                start.assert_called_once()
                command = start.call_args[0][1]
                self.assertEqual(command[command.index("--limit") + 1], "2")
                self.assertIn("--wait-resources", command)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_start_endpoint_rejects_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            running_detected = {
                "runner": {"pid": 99, "command_line": "run_shp_process_tasks.py", "created": 1},
                "runners": [{"pid": 99, "command_line": "run_shp_process_tasks.py", "created": 1}],
                "runner_count": 1,
                "workers": [],
                "limit": 0,
                "wait_resources": False,
            }
            try:
                with patch.object(status_server, "detect_process_batch", return_value=running_detected):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/process/start",
                        data=json.dumps({"limit": 0}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 409)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_stop_endpoint_resets_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server,
                    "_stop_all_process_queue_processes",
                    return_value=({"runner": None, "runners": [], "workers": []}, [7]),
                ):
                    with patch.object(
                        status_server.process_queue,
                        "reset_running_in_db",
                        return_value=1,
                    ) as reset:
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{port}/api/process/stop",
                            data=b"{}",
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(request, timeout=10) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["stopped_pids"], [7])
                self.assertEqual(payload["reset_tasks"], 1)
                reset.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()

    def test_process_sync_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server,
                    "sync_process_queue",
                    return_value={"total": 3, "added": 1, "updated": 2},
                ) as sync:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/process/sync",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["total"], 3)
                self.assertEqual(payload["added"], 1)
                self.assertEqual(payload["updated"], 2)
                sync.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()

    def test_sync_process_queue_registers_completed_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = repo_root / "data" / "world_building_download_tasks_state.db"
            store = lib.StateStore(state_db)
            try:
                task = make_task("Africa", "Algeria", "Algeria", "data/Africa/Algeria/Algeria")
                store.sync_manifest([task])
                store.set_status(task.task_id, lib.STATUS_OK, feature_count=3)
                store.set_existing_data(task.task_id, task.download_dir)
            finally:
                store.close()
            task_dir = repo_root / "data" / "Africa" / "Algeria" / "Algeria"
            task_dir.mkdir(parents=True, exist_ok=True)
            gdf = gpd.GeoDataFrame(
                {"GBA_ID": ["1", "2", "3"], "Height": [1.0, 2.0, 3.0]},
                geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(0, 1, 1, 2)],
                crs=4326,
            )
            gdf.to_file(task_dir / "Algeria_buildings_height_gba.shp")

            process_db = repo_root / "process.db"
            result = status_server.sync_process_queue({"repo_root": repo_root, "process_db": str(process_db)})

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["updated"], 0)
            pstore = run_shp_process_tasks.ProcessStore(process_db)
            try:
                pending = pstore.pending_tasks()
            finally:
                pstore.close()
            self.assertEqual([row["dataset_key"] for row in pending], ["Africa|Algeria|Algeria"])

    def test_process_start_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/start",
                    data=json.dumps({"limit": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_note_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/note",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "note": " 补充说明 "}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["note"], "补充说明")

                store = run_shp_process_tasks.ProcessStore(process_db)
                try:
                    self.assertEqual(store.get("Asia|China|Yunnan")["note"], "补充说明")
                finally:
                    store.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_process_note_endpoint_rejects_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_process_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/note",
                    data=json.dumps({"dataset_key": "Asia|China|Nope", "note": "x"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_note_endpoint_rejects_long_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_process_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/note",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "note": "x" * 201}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_note_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/note",
                    data=json.dumps({"dataset_key": "Asia|China|Yunnan", "note": "x"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()

    def test_restart_batch_stops_runner_before_workers(self):
        calls: list[list[int]] = []
        detected = {
            "runner": {"pid": 100, "command_line": "python -u run_world_building_tasks.py --tile-workers 2"},
            "workers": [{"pid": 200, "command_line": "python download_gba_lod1_wfs_adaptive.py"}],
            "tile_workers": 2,
        }
        detected_after = {
            "runner": None,
            "workers": [{"pid": 201, "command_line": "python download_gba_lod1_wfs_adaptive.py"}],
            "tile_workers": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {"repo_root": repo_root, "python_exe": "python", "action_token": ""}
            with patch.object(status_server, "detect_batch", side_effect=[detected, detected_after]):
                with patch.object(
                    status_server,
                    "stop_batch",
                    side_effect=lambda pids: calls.append(list(pids)) or list(pids),
                ):
                    with patch.object(status_server, "_wait_for_exit"):
                        with patch.object(
                            status_server,
                            "start_batch",
                            return_value={"pid": 999, "command": []},
                        ):
                            result = status_server.restart_batch(config, 3)

        self.assertEqual(calls[0], [100])
        self.assertEqual(calls[1], [200, 201])
        self.assertEqual(result["runner_pid"], 999)
        self.assertEqual(result["tile_workers"], 3)


if __name__ == "__main__":
    unittest.main()
