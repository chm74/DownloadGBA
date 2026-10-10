import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
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
            self.assertEqual([item["global_order"] for item in queue], [1, 2])
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
            self.assertEqual(paged["queue"][0]["order"], 2)
            self.assertEqual(paged["queue"][0]["global_order"], 2)

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
            self.assertEqual(english["queue"][0]["order"], 1)
            self.assertEqual(english["queue"][0]["global_order"], 2)
            self.assertEqual([item["task_id"] for item in chinese["queue"]], ["Africa|Angola|Angola"])
            self.assertEqual(chinese["queue"][0]["order"], 1)
            self.assertEqual(chinese["queue"][0]["global_order"], 1)
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

    def test_processing_recent_done_pages_keep_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                for index in range(11):
                    key = f"Recent|Area|City{index:02d}"
                    store.upsert_source({
                        "dataset_key": key,
                        "task_id": key,
                        "display_name": key,
                        "source_dir": f"data/recent/{index:02d}",
                        "shp_files": [f"data/recent/{index:02d}.shp"],
                        "feature_count": index,
                    })
                    store.set_status(
                        key,
                        run_shp_process_tasks.PROCESS_OK,
                        finished_at=f"2026-01-02T00:00:{index:02d}",
                    )
            finally:
                store.close()

            first = status_server.build_processing_snapshot(repo_root, process_db)
            second = status_server.build_processing_snapshot(repo_root, process_db, recent_offset=10)
            self.assertEqual(first["recent_total"], 12)
            self.assertEqual(first["recent_limit"], 10)
            self.assertEqual(first["recent_offset"], 0)
            self.assertEqual(
                [item["dataset_key"] for item in first["recent_done"]],
                [f"Recent|Area|City{index:02d}" for index in range(10, 0, -1)],
            )
            self.assertEqual(second["recent_total"], 12)
            self.assertEqual(second["recent_offset"], 10)
            self.assertEqual(
                [item["dataset_key"] for item in second["recent_done"]],
                ["Recent|Area|City00", "Europe|San_Marino|San_Marino"],
            )

    def test_processing_recent_done_zero_limit_uses_one_record_per_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            snapshot = status_server.build_processing_snapshot(repo_root, process_db, recent_limit=0)
            self.assertEqual(snapshot["recent_total"], 1)
            self.assertEqual(snapshot["recent_limit"], 1)
            self.assertEqual(
                [item["dataset_key"] for item in snapshot["recent_done"]],
                ["Europe|San_Marino|San_Marino"],
            )

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
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.set_status(
                    "Asia|China|Shanghai",
                    run_shp_process_tasks.PROCESS_OK,
                    finished_at="2026-01-02T00:00:00",
                )
            finally:
                store.close()
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

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status?process_recent_offset=1",
                    timeout=10,
                ) as response:
                    recent_page = json.loads(response.read().decode("utf-8"))["processing"]
                self.assertEqual(recent_page["recent_total"], 2)
                self.assertEqual(recent_page["recent_offset"], 1)
                self.assertEqual(
                    [item["dataset_key"] for item in recent_page["recent_done"]],
                    ["Europe|San_Marino|San_Marino"],
                )

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

    def test_reset_and_delete_db_update_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks_file = Path(tmp) / "marks.json"
            key = "Europe|San_Marino|San_Marino"
            status_server.save_db_update_marks(
                marks_file,
                [{"dataset_key": key, "updated": True, "updated_at": "2026-01-01T00:00:00", "note": "已入库"}],
            )

            self.assertFalse(status_server.reset_db_update_mark(marks_file, "Missing|Key|Key"))
            self.assertTrue(status_server.reset_db_update_mark(marks_file, key))
            marks = status_server.load_db_update_marks(marks_file)
            self.assertIn(key, marks)
            self.assertFalse(marks[key]["updated"])
            self.assertEqual(marks[key]["updated_at"], "")
            self.assertEqual(marks[key]["note"], "")

            self.assertFalse(status_server.delete_db_update_mark(marks_file, "Missing|Key|Key"))
            self.assertTrue(status_server.delete_db_update_mark(marks_file, key))
            self.assertNotIn(key, status_server.load_db_update_marks(marks_file))

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
                    return_value={"ok": True, "tile_workers": 2, "runner_pid": 123, "stopped_pids": [1]},
                ) as restart:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/restart",
                        data=json.dumps({}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                restart.assert_called_once()
                self.assertEqual(restart.call_args[0][1], 2)
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
                    data=json.dumps({}).encode("utf-8"),
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

    def test_stop_current_task_action_stops_processes_then_requeues_to_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            config = {"repo_root": repo_root, "state_db": state_db}
            stopped = {
                "runner": {"pid": 100},
                "workers": [{"pid": 200}],
            }
            no_processes = {"runner": None, "runners": [], "workers": []}

            with patch.object(
                status_server,
                "_stop_all_batch_processes",
                return_value=(stopped, [100, 200]),
            ):
                with patch.object(status_server, "detect_batch", return_value=no_processes):
                    result = status_server.stop_current_task_action(
                        config,
                        "Africa|Algeria|Algeria",
                    )

            self.assertTrue(result["ok"])
            self.assertEqual(result["task_id"], "Africa|Algeria|Algeria")
            self.assertEqual(result["position"], "tail")
            self.assertEqual(result["stopped_pids"], [100, 200])
            self.assertEqual(result["queue"][-1], "Africa|Algeria|Algeria")
            store = lib.StateStore(state_db)
            try:
                self.assertEqual(store.get("Africa|Algeria|Algeria")["status"], lib.STATUS_PENDING)
            finally:
                store.close()

    def test_stop_current_task_action_keeps_running_state_when_processes_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db = build_repo_fixture(repo_root)
            config = {"repo_root": repo_root, "state_db": state_db}
            still_running = {"runner": {"pid": 100}, "workers": []}

            with patch.object(
                status_server,
                "_stop_all_batch_processes",
                return_value=({"runner": {"pid": 100}, "workers": []}, []),
            ):
                with patch.object(status_server, "detect_batch", return_value=still_running):
                    with self.assertRaises(RuntimeError):
                        status_server.stop_current_task_action(config, "Africa|Algeria|Algeria")

            store = lib.StateStore(state_db)
            try:
                self.assertEqual(store.get("Africa|Algeria|Algeria")["status"], lib.STATUS_RUNNING)
            finally:
                store.close()

    def test_stop_current_task_endpoint_calls_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                result = {
                    "ok": True,
                    "task_id": "Africa|Algeria|Algeria",
                    "position": "tail",
                    "stopped_pids": [1, 2],
                    "queue": ["Africa|Angola|Angola", "Africa|Algeria|Algeria"],
                    "pending_total": 2,
                }
                with patch.object(status_server, "stop_current_task_action", return_value=result) as action:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/queue/stop-current",
                        data=json.dumps({"task_id": "Africa|Algeria|Algeria"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["position"], "tail")
                action.assert_called_once_with(server.config, "Africa|Algeria|Algeria")
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

    def test_completed_process_requeue_endpoint_requires_confirmation_and_updates_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_db = build_repo_fixture(root)
            process_db = build_process_fixture(root)
            key = "Europe|San_Marino|San_Marino"
            output_dir = root / "Tools/Oneshp_pipline_qgis/out_data/San_Marino_pipeline"
            output_dir.mkdir(parents=True)
            (output_dir / "result.shp").write_bytes(b"output")
            marks_file = root / "data" / "db_update_status.json"
            status_server.save_db_update_marks(
                marks_file,
                [{"dataset_key": key, "updated": True, "updated_at": "2026-01-01T00:00:00", "note": "已入库"}],
            )
            server = self._make_server(root)
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/api/process/requeue-completed"
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/status") as response:
                    recent = json.loads(response.read().decode("utf-8"))["processing"]["recent_done"]
                self.assertEqual(recent[0]["input_dir"],
                                 "Tools/Oneshp_pipline_qgis/out_data/San_Marino_pipeline_input")
                def post(payload):
                    request = urllib.request.Request(
                        url, data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    return urllib.request.urlopen(request, timeout=10)
                with self.assertRaises(urllib.error.HTTPError) as context:
                    post({"dataset_key": key})
                self.assertEqual(context.exception.code, 400)
                self.assertTrue(output_dir.exists())
                with self.assertRaises(urllib.error.HTTPError) as context:
                    post({"dataset_key": key, "confirm_delete": True})
                self.assertEqual(context.exception.code, 400)
                with post({"dataset_key": key, "confirm_delete": True,
                           "expected_output_dir": "Tools/Oneshp_pipline_qgis/out_data/San_Marino_pipeline",
                           "expected_input_dir": "Tools/Oneshp_pipline_qgis/out_data/San_Marino_pipeline_input"}) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertTrue(result["ok"])
                self.assertEqual(result["queue"][0], key)
                self.assertFalse(output_dir.exists())
                marks = status_server.load_db_update_marks(marks_file)
                self.assertIn(key, marks)
                self.assertFalse(marks[key]["updated"])
                self.assertEqual(marks[key]["updated_at"], "")
                self.assertEqual(marks[key]["note"], "")
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/status") as response:
                    snapshot = json.loads(response.read().decode("utf-8"))["processing"]
                self.assertNotIn(key, [row["dataset_key"] for row in snapshot["recent_done"]])
                self.assertEqual(snapshot["queue"][0]["dataset_key"], key)
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

    def _make_completed_download(self, repo_root: Path) -> Path:
        task_dir = repo_root / "data" / "Europe" / "San_Marino" / "San_Marino"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "GBA_0001.gpkg").write_bytes(b"tile")
        (task_dir / lib.MARKER_NAME).write_text(
            json.dumps(
                {
                    "task_id": "Europe|San_Marino|San_Marino",
                    "download_dir": "data/Europe/San_Marino/San_Marino",
                    "status": "OK",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return task_dir

    def _post_requeue_completed(self, port: int, body: dict):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/queue/requeue-completed",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_queue_requeue_completed_deletes_download_and_tops(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            task_dir = self._make_completed_download(repo_root)
            marks_file = repo_root / "data" / "db_update_status.json"
            status_server.save_db_update_marks(
                marks_file,
                [{"dataset_key": "Europe|San_Marino|San_Marino", "updated": True,
                  "updated_at": "2026-01-01T00:00:00", "note": "已入库"}],
            )
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with self._post_requeue_completed(
                    port,
                    {
                        "task_id": "Europe|San_Marino|San_Marino",
                        "confirm_delete": True,
                        "expected_download_dir": "data/Europe/San_Marino/San_Marino",
                    },
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"], payload)
                self.assertFalse(task_dir.exists())
                self.assertEqual(payload["queue"][0], "Europe|San_Marino|San_Marino")
                self.assertNotIn("Europe|San_Marino|San_Marino", status_server.load_db_update_marks(marks_file))

                store = lib.StateStore(repo_root / "state.db")
                try:
                    row = store.get("Europe|San_Marino|San_Marino")
                    self.assertEqual(row["status"], lib.STATUS_PENDING)
                    self.assertIsNone(row["processed_3857_dir"])
                    self.assertEqual(store.pending_tasks()[0]["task_id"], "Europe|San_Marino|San_Marino")
                finally:
                    store.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_completed_requires_confirm_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            task_dir = self._make_completed_download(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    self._post_requeue_completed(
                        port,
                        {
                            "task_id": "Europe|San_Marino|San_Marino",
                            "expected_download_dir": "data/Europe/San_Marino/San_Marino",
                        },
                    )
                self.assertEqual(context.exception.code, 400)
                self.assertTrue(task_dir.exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_completed_rejects_stale_download_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            task_dir = self._make_completed_download(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    self._post_requeue_completed(
                        port,
                        {
                            "task_id": "Europe|San_Marino|San_Marino",
                            "confirm_delete": True,
                            "expected_download_dir": "data/Europe/San_Marino/Other",
                        },
                    )
                self.assertEqual(context.exception.code, 400)
                self.assertTrue(task_dir.exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_queue_requeue_completed_rejects_non_success_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with self.assertRaises(urllib.error.HTTPError) as context:
                    self._post_requeue_completed(
                        port,
                        {
                            "task_id": "Africa|Angola|Angola",
                            "confirm_delete": True,
                            "expected_download_dir": "data/Africa/Angola/Angola",
                        },
                    )
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_requeue_completed_download_rejects_external_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            store = lib.StateStore(repo_root / "state.db")
            try:
                store.set_existing_data("Europe|San_Marino|San_Marino", "legacy/San_Marino")
            finally:
                store.close()
            (repo_root / "legacy" / "San_Marino").mkdir(parents=True, exist_ok=True)
            with self.assertRaises(ValueError):
                lib.requeue_completed_download(
                    repo_root,
                    repo_root / "state.db",
                    "Europe|San_Marino|San_Marino",
                    "data/Europe/San_Marino/San_Marino",
                )

    def test_reset_task_after_download_redo_resets_ok_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            output_root = repo_root / "Tools" / "Oneshp_pipline_qgis" / "out_data"
            output_dir = output_root / "san_marino_pipeline"
            input_dir = output_root / "san_marino_pipeline_input"
            output_dir.mkdir(parents=True)
            input_dir.mkdir(parents=True)
            (output_dir / run_shp_process_tasks.PROCESS_MARKER_NAME).write_text(
                json.dumps({"dataset_key": "Europe|San_Marino|San_Marino"}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.conn.execute(
                    """
                    INSERT INTO process_tasks (
                        dataset_key, task_id, display_name, process_name_prefix, source_dir,
                        input_dir, output_dir, status, feature_count, updated_at, queue_order,
                        shp_files, attempts, note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Europe|San_Marino|San_Marino",
                        "Europe|San_Marino|San_Marino",
                        "San_Marino",
                        "san_marino",
                        "data/Europe/San_Marino",
                        str(input_dir),
                        str(output_dir),
                        run_shp_process_tasks.PROCESS_OK,
                        10,
                        lib.utc_now(),
                        1,
                        json.dumps(["data/Europe/San_Marino/San_Marino_buildings_height_gba.shp"]),
                        2,
                        "保留备注",
                    ),
                )
                store.conn.commit()
            finally:
                store.close()

            error = run_shp_process_tasks.reset_task_after_download_redo(
                repo_root, process_db, repo_root / "state.db", "Europe|San_Marino|San_Marino"
            )
            self.assertIsNone(error)
            self.assertFalse(output_dir.exists())
            self.assertFalse(input_dir.exists())
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                row = store.get("Europe|San_Marino|San_Marino")
                self.assertEqual(row["status"], run_shp_process_tasks.PROCESS_WAITING_DOWNLOAD)
                self.assertEqual(json.loads(row["shp_files"] or "[]"), [])
                self.assertEqual(row["attempts"], 2)
                self.assertEqual(row["note"], "保留备注")
                self.assertEqual(store.pending_tasks(), [])
            finally:
                store.close()

    def _make_part_shp(self, path: Path, features: int) -> None:
        gdf = gpd.GeoDataFrame(
            {"GBA_ID": [str(i) for i in range(features)]},
            geometry=[box(i, 0, i + 1, 1) for i in range(features)],
            crs=4326,
        )
        gdf.to_file(path)

    def _register_merge_task(self, process_db: Path, shp_files: list[str], status: str) -> None:
        store = run_shp_process_tasks.ProcessStore(process_db)
        try:
            store.upsert_source(
                {
                    "dataset_key": "Asia|China|Test",
                    "task_id": "Asia|China|Test",
                    "display_name": "Test",
                    "process_name_prefix": "Test",
                    "source_dir": "data/Asia/China/Test",
                    "shp_files": shp_files,
                    "status": status,
                }
            )
        finally:
            store.close()

    def test_merge_task_shp_parts_merges_and_deletes_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source_dir = repo_root / "data" / "Asia" / "China" / "Test"
            source_dir.mkdir(parents=True)
            part1 = source_dir / "Test_buildings_height_gba_part1.shp"
            part2 = source_dir / "Test_buildings_height_gba_part2.shp"
            self._make_part_shp(part1, 2)
            self._make_part_shp(part2, 3)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db,
                [lib.repo_relative(repo_root, part1), lib.repo_relative(repo_root, part2)],
                run_shp_process_tasks.PROCESS_PENDING,
            )

            def fake_run(command, **kwargs):
                output = Path(command[-2])
                source = Path(command[-1])
                frame = gpd.read_file(source)
                if output.exists():
                    frame = gpd.GeoDataFrame(
                        gpd.pd.concat([gpd.read_file(output), frame], ignore_index=True),
                        crs=frame.crs,
                    )
                frame.to_file(output)
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(run_shp_process_tasks, "resolve_ogr2ogr", return_value="ogr2ogr"), \
                 patch.object(run_shp_process_tasks.subprocess, "run", side_effect=fake_run):
                result = run_shp_process_tasks.merge_task_shp_parts(
                    repo_root, process_db, "Asia|China|Test", sync=False
                )

            final = source_dir / "Test_buildings_height_gba.shp"
            self.assertTrue(final.exists())
            self.assertFalse(part1.exists())
            self.assertFalse(part2.exists())
            self.assertEqual(result["shp_count"], 1)
            self.assertEqual(result["deleted_parts"], 2)

    def test_merge_task_shp_parts_rejects_single_or_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source_dir = repo_root / "data" / "Asia" / "China" / "Test"
            source_dir.mkdir(parents=True)
            single = source_dir / "Test_buildings_height_gba.shp"
            self._make_part_shp(single, 1)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db, [lib.repo_relative(repo_root, single)],
                run_shp_process_tasks.PROCESS_PENDING,
            )
            with self.assertRaises(ValueError):
                run_shp_process_tasks.merge_task_shp_parts(
                    repo_root, process_db, "Asia|China|Test", sync=False
                )

            part1 = source_dir / "Test_buildings_height_gba_part1.shp"
            part2 = source_dir / "Test_buildings_height_gba_part2.shp"
            self._make_part_shp(part1, 2)
            self._make_part_shp(part2, 2)
            self._register_merge_task(
                process_db,
                [lib.repo_relative(repo_root, part1), lib.repo_relative(repo_root, part2)],
                run_shp_process_tasks.PROCESS_PENDING,
            )
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.conn.execute(
                    "UPDATE process_tasks SET status = ? WHERE dataset_key = ?",
                    (run_shp_process_tasks.PROCESS_RUNNING, "Asia|China|Test"),
                )
                store.conn.commit()
            finally:
                store.close()
            with self.assertRaises(ValueError):
                run_shp_process_tasks.merge_task_shp_parts(
                    repo_root, process_db, "Asia|China|Test", sync=False
                )

    def test_process_merge_parts_endpoint_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db,
                [
                    "data/Asia/China/Test/Test_buildings_height_gba_part1.shp",
                    "data/Asia/China/Test/Test_buildings_height_gba_part2.shp",
                ],
                run_shp_process_tasks.PROCESS_PENDING,
            )
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server.process_queue,
                    "merge_task_shp_parts",
                    return_value={"ok": True, "shp_count": 1},
                ) as mocked:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/process/merge-parts",
                        data=json.dumps(
                            {
                                "dataset_key": "Asia|China|Test",
                                "confirm_delete": True,
                                "expected_source_dir": "data/Asia/China/Test",
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["state"], "running")

                    state = {"state": "running"}
                    for _ in range(200):
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/process/merge-status"
                            "?dataset_key=Asia%7CChina%7CTest",
                            timeout=10,
                        ) as response:
                            state = json.loads(response.read().decode("utf-8"))
                        if state.get("state") != "running":
                            break
                        time.sleep(0.02)
                self.assertEqual(state["state"], "done")
                mocked.assert_called_once()
            finally:
                server.shutdown()
                server.server_close()

    def test_process_merge_parts_endpoint_requires_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db,
                [
                    "data/Asia/China/Test/Test_buildings_height_gba_part1.shp",
                    "data/Asia/China/Test/Test_buildings_height_gba_part2.shp",
                ],
                run_shp_process_tasks.PROCESS_PENDING,
            )
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/merge-parts",
                    data=json.dumps({"dataset_key": "Asia|China|Test"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def _register_process_ok(self, process_db: Path, key: str, source_dir: str, shp_files: list[str]) -> None:
        store = run_shp_process_tasks.ProcessStore(process_db)
        try:
            store.conn.execute(
                """
                INSERT INTO process_tasks (
                    dataset_key, task_id, display_name, process_name_prefix, source_dir,
                    status, shp_files, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, key, key.split("|")[-1], key.split("|")[-1].lower(),
                 source_dir, run_shp_process_tasks.PROCESS_OK, json.dumps(shp_files), lib.utc_now()),
            )
            store.conn.commit()
        finally:
            store.close()

    def test_process_redownload_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            task_dir = self._make_completed_download(repo_root)
            marks_file = repo_root / "data" / "db_update_status.json"
            status_server.save_db_update_marks(
                marks_file,
                [{"dataset_key": "Europe|San_Marino|San_Marino", "updated": True,
                  "updated_at": "2026-01-01T00:00:00", "note": "x"}],
            )
            store = lib.StateStore(repo_root / "state.db")
            try:
                store.conn.execute(
                    "UPDATE tasks SET attempts = 3 WHERE task_id = ?",
                    ("Europe|San_Marino|San_Marino",),
                )
                store.conn.commit()
            finally:
                store.close()
            self._register_process_ok(
                repo_root / "process.db", "Europe|San_Marino|San_Marino",
                "data/Europe/San_Marino/San_Marino",
                ["data/Europe/San_Marino/San_Marino/San_Marino_buildings_height_gba_part1.shp"],
            )
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/redownload",
                    data=json.dumps({
                        "dataset_key": "Europe|San_Marino|San_Marino",
                        "confirm_delete": True,
                        "expected_download_dir": "data/Europe/San_Marino/San_Marino",
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"], payload)
                self.assertEqual(payload["redownload_count"], 1)
                self.assertFalse(task_dir.exists())

                dstore = lib.StateStore(repo_root / "state.db")
                try:
                    row = dstore.get("Europe|San_Marino|San_Marino")
                    self.assertEqual(row["status"], lib.STATUS_PENDING)
                    self.assertEqual(row["attempts"], 3)
                    self.assertEqual(row["redownload_count"], 1)
                finally:
                    dstore.close()

                pstore = run_shp_process_tasks.ProcessStore(repo_root / "process.db")
                try:
                    prow = pstore.get("Europe|San_Marino|San_Marino")
                    self.assertEqual(prow["status"], run_shp_process_tasks.PROCESS_WAITING_DOWNLOAD)
                    self.assertEqual(json.loads(prow["shp_files"] or "[]"), [])
                finally:
                    pstore.close()

                self.assertNotIn(
                    "Europe|San_Marino|San_Marino",
                    status_server.load_db_update_marks(marks_file),
                )
            finally:
                server.shutdown()
                server.server_close()

    def test_process_redownload_endpoint_requires_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            build_repo_fixture(repo_root)
            self._register_process_ok(
                repo_root / "process.db", "Europe|San_Marino|San_Marino",
                "data/Europe/San_Marino/San_Marino", [],
            )
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/redownload",
                    data=json.dumps({"dataset_key": "Europe|San_Marino|San_Marino"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_process_redownload_rejects_task_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.conn.execute(
                    """
                    INSERT INTO process_tasks (
                        dataset_key, task_id, display_name, process_name_prefix, source_dir,
                        status, shp_files, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("EXT|data/Asia/China/Scan", "", "Scan", "scan", "data/Asia/China/Scan",
                     run_shp_process_tasks.PROCESS_OK, "[]", lib.utc_now()),
                )
                store.conn.commit()
            finally:
                store.close()
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/process/redownload",
                    data=json.dumps({
                        "dataset_key": "EXT|data/Asia/China/Scan",
                        "confirm_delete": True,
                        "expected_download_dir": "",
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()

    def test_sync_reconcile_marks_missing_pending_as_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.conn.execute(
                    """
                    INSERT INTO process_tasks (
                        dataset_key, task_id, display_name, process_name_prefix, source_dir,
                        status, shp_files, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("Asia|China|Missing", "Asia|China|Missing", "Missing", "missing",
                     "data/Asia/China/Missing", run_shp_process_tasks.PROCESS_PENDING,
                     json.dumps(["data/Asia/China/Missing/Missing_buildings_height_gba.shp"]),
                     lib.utc_now()),
                )
                store.conn.commit()
            finally:
                store.close()

            run_shp_process_tasks.sync_process_tasks(
                repo_root, process_db, repo_root / "state.db", repo_root / "data", [], [],
                True, run_shp_process_tasks.DEFAULT_MANIFEST,
            )

            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                row = store.get("Asia|China|Missing")
                self.assertEqual(row["status"], run_shp_process_tasks.PROCESS_WAITING_DOWNLOAD)
                self.assertEqual(json.loads(row["shp_files"] or "[]"), [])
            finally:
                store.close()

    def test_waiting_download_recovers_to_pending_on_new_shp(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = repo_root / "process.db"
            store = run_shp_process_tasks.ProcessStore(process_db)
            try:
                store.conn.execute(
                    """
                    INSERT INTO process_tasks (
                        dataset_key, task_id, display_name, process_name_prefix, source_dir,
                        status, shp_files, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("Asia|China|Back", "Asia|China|Back", "Back", "back",
                     "data/Asia/China/Back", run_shp_process_tasks.PROCESS_WAITING_DOWNLOAD,
                     "[]", lib.utc_now()),
                )
                store.conn.commit()
                store.upsert_source(
                    {
                        "dataset_key": "Asia|China|Back",
                        "task_id": "Asia|China|Back",
                        "display_name": "Back",
                        "process_name_prefix": "back",
                        "source_dir": "data/Asia/China/Back",
                        "shp_files": ["data/Asia/China/Back/Back_buildings_height_gba.shp"],
                        "status": run_shp_process_tasks.PROCESS_PENDING,
                    }
                )
                row = store.get("Asia|China|Back")
                self.assertEqual(row["status"], run_shp_process_tasks.PROCESS_PENDING)
            finally:
                store.close()


    def _write_config_root(self, repo_root: Path, result_root: Path) -> None:
        status_server.save_dashboard_config(
            repo_root,
            {
                "database": {"host": "h", "port": 5432, "dbname": "b", "user": "u", "password": "p"},
                "result_root": str(result_root),
            },
        )

    def _insert_process_ok(self, process_db: Path, key: str, output_dir: str) -> None:
        store = run_shp_process_tasks.ProcessStore(process_db)
        try:
            store.conn.execute(
                """
                INSERT INTO process_tasks (
                    dataset_key, task_id, display_name, process_name_prefix, source_dir,
                    output_dir, status, shp_files, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (key, key, key.split("|")[-1], key.split("|")[-1].lower(),
                 "data/x", output_dir, run_shp_process_tasks.PROCESS_OK, "[]", lib.utc_now()),
            )
            store.conn.commit()
        finally:
            store.close()

    def _make_tiles(self, directory: Path, names: list[str]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            for suffix in (".shp", ".dbf", ".shx"):
                (directory / f"{name}{suffix}").write_bytes(b"x")

    def test_check_db_update_consistency(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            key = "Europe|San_Marino|San_Marino"
            output_dir = "Tools/Oneshp_pipline_qgis/out_data/san_marino_pipeline"
            self._make_tiles(repo_root / output_dir / "final", ["a_3857", "b_3857"])
            share_root = repo_root / "share"
            share = share_root / "Europe" / "San_Marino" / "San_Marino"
            self._make_tiles(share, ["a_3857", "b_3857"])
            self._write_config_root(repo_root, share_root)
            self._insert_process_ok(repo_root / "process.db", key, output_dir)
            config = {"repo_root": repo_root, "process_db": repo_root / "process.db"}

            result = status_server.check_db_update_consistency(config, key)
            self.assertTrue(result["consistent"], result)

            (share / "b_3857.shp").unlink()
            result = status_server.check_db_update_consistency(config, key)
            self.assertFalse(result["consistent"])
            self.assertEqual(result["missing"], ["b_3857"])

            self._make_tiles(share, ["c_3857"])
            result = status_server.check_db_update_consistency(config, key)
            self.assertFalse(result["consistent"])
            self.assertIn("c_3857", result["extra"])

    def test_db_updated_import_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            key = "Europe|San_Marino|San_Marino"
            output_dir = "Tools/Oneshp_pipline_qgis/out_data/san_marino_pipeline"
            self._make_tiles(repo_root / output_dir / "final", ["a_3857", "b_3857"])
            share_root = repo_root / "share"
            share = share_root / "Europe" / "San_Marino" / "San_Marino"
            self._make_tiles(share, ["a_3857", "b_3857"])
            self._write_config_root(repo_root, share_root)
            self._insert_process_ok(repo_root / "process.db", key, output_dir)
            tool_python = repo_root / status_server.DEFAULT_UPDATE_PYTHON
            tool_script = repo_root / status_server.DEFAULT_UPDATE_SCRIPT
            tool_python.parent.mkdir(parents=True, exist_ok=True)
            tool_python.write_bytes(b"x")
            tool_script.parent.mkdir(parents=True, exist_ok=True)
            tool_script.write_bytes(b"x")

            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(
                    status_server.subprocess, "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0,
                        '__IMPORT_SUMMARY__ {"deleted": 9, "inserted": 3, "skipped": 1}\n',
                        "",
                    ),
                ):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/db_updated/import",
                        data=json.dumps({"dataset_key": key}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["state"], "running")

                    state = {"state": "running"}
                    for _ in range(200):
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/db_updated/import-status"
                            "?dataset_key=Europe%7CSan_Marino%7CSan_Marino",
                            timeout=10,
                        ) as response:
                            state = json.loads(response.read().decode("utf-8"))
                        if state.get("state") != "running":
                            break
                        time.sleep(0.02)
                self.assertEqual(state["state"], "done")
                marks = status_server.load_db_update_marks(repo_root / "data" / "db_update_status.json")
                self.assertTrue(marks[key]["updated"])
                records = status_server._load_db_import_records(repo_root / status_server.DEFAULT_DB_IMPORT_STATUS)
                self.assertEqual(records[key]["state"], "done")
                self.assertEqual(records[key]["deleted"], 9)
                self.assertEqual(records[key]["inserted"], 3)
                self.assertEqual(records[key]["skipped"], 1)
            finally:
                server.shutdown()
                server.server_close()

    def test_db_updated_import_blocked_when_inconsistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            key = "Europe|San_Marino|San_Marino"
            output_dir = "Tools/Oneshp_pipline_qgis/out_data/san_marino_pipeline"
            self._make_tiles(repo_root / output_dir / "final", ["a_3857", "b_3857"])
            share_root = repo_root / "share"
            share = share_root / "Europe" / "San_Marino" / "San_Marino"
            self._make_tiles(share, ["a_3857"])
            self._write_config_root(repo_root, share_root)
            self._insert_process_ok(repo_root / "process.db", key, output_dir)

            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with patch.object(status_server.subprocess, "run") as mocked:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/db_updated/import",
                        data=json.dumps({"dataset_key": key}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["consistent"])
                self.assertIn("b_3857", payload["missing"])
                mocked.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()

    def test_db_import_status_falls_back_to_persisted_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            key = "Europe|San_Marino|San_Marino"
            self.assertEqual(
                status_server.build_db_import_status(repo_root, key)["state"], "idle"
            )
            status_server._save_db_import_record(
                repo_root / status_server.DEFAULT_DB_IMPORT_STATUS, key,
                {"state": "error", "message": "入库失败（退出码 1）", "error": "boom", "log_tail": "", "updated_at": "t"},
            )
            result = status_server.build_db_import_status(repo_root, key)
            self.assertEqual(result["state"], "error")
            self.assertFalse(result["ok"])

    def test_check_db_update_consistency_missing_share_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            key = "Europe|San_Marino|San_Marino"
            output_dir = "Tools/Oneshp_pipline_qgis/out_data/san_marino_pipeline"
            self._make_tiles(repo_root / output_dir / "final", ["a_3857"])
            self._write_config_root(repo_root, repo_root / "share")
            self._insert_process_ok(repo_root / "process.db", key, output_dir)
            config = {"repo_root": repo_root, "process_db": repo_root / "process.db"}
            result = status_server.check_db_update_consistency(config, key)
            self.assertFalse(result["consistent"])
            self.assertIn("共享目录不存在", result["message"])

    def test_merge_size_check_is_per_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source_dir = repo_root / "data" / "Asia" / "China" / "Test"
            source_dir.mkdir(parents=True)
            part1 = source_dir / "Test_buildings_height_gba_part1.shp"
            part2 = source_dir / "Test_buildings_height_gba_part2.shp"
            for path in (part1, part2):
                path.write_bytes(b"0" * 100)
                path.with_suffix(".dbf").write_bytes(b"0" * 200)
                path.with_suffix(".shx").write_bytes(b"0" * 100)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db,
                [lib.repo_relative(repo_root, part1), lib.repo_relative(repo_root, part2)],
                run_shp_process_tasks.PROCESS_PENDING,
            )

            # 单组件最大 ≈400B(dbf)，合计 ≈600B；阈值 500B：按组件应通过，按合计会被拒。
            with patch.object(run_shp_process_tasks, "resolve_ogr2ogr", return_value="ogr2ogr"), \
                 patch.object(
                     run_shp_process_tasks.subprocess, "run",
                     return_value=subprocess.CompletedProcess([], 0, "", ""),
                 ):
                result = run_shp_process_tasks.merge_task_shp_parts(
                    repo_root, process_db, "Asia|China|Test",
                    sync=False, max_output_gb=500 / (1 << 30),
                )
            self.assertTrue(result["ok"])

    def test_merge_size_check_rejects_oversized_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source_dir = repo_root / "data" / "Asia" / "China" / "Test"
            source_dir.mkdir(parents=True)
            part1 = source_dir / "Test_buildings_height_gba_part1.shp"
            part2 = source_dir / "Test_buildings_height_gba_part2.shp"
            for path in (part1, part2):
                path.write_bytes(b"0" * 100)
                path.with_suffix(".dbf").write_bytes(b"0" * 200)
                path.with_suffix(".shx").write_bytes(b"0" * 100)
            process_db = repo_root / "process.db"
            self._register_merge_task(
                process_db,
                [lib.repo_relative(repo_root, part1), lib.repo_relative(repo_root, part2)],
                run_shp_process_tasks.PROCESS_PENDING,
            )
            with self.assertRaises(ValueError):
                run_shp_process_tasks.merge_task_shp_parts(
                    repo_root, process_db, "Asia|China|Test",
                    sync=False, max_output_gb=300 / (1 << 30),
                )

    def test_parse_import_summary(self):
        self.assertIsNone(status_server.parse_import_summary(""))
        self.assertIsNone(status_server.parse_import_summary("没有汇总信息"))
        marker = '__IMPORT_SUMMARY__ {"region": "x", "deleted": 12, "inserted": 5, "skipped": 1}\n'
        parsed = status_server.parse_import_summary("log line\n" + marker)
        self.assertEqual((parsed["deleted"], parsed["inserted"], parsed["skipped"]), (12, 5, 1))
        fallback = status_server.parse_import_summary("更新完成: region=x, 删除=3, 新增=7, 跳过=0")
        self.assertEqual((fallback["deleted"], fallback["inserted"], fallback["skipped"]), (3, 7, 0))

    def test_build_db_update_snapshot_import_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            process_db = build_process_fixture(repo_root)
            status_server._save_db_import_record(
                repo_root / status_server.DEFAULT_DB_IMPORT_STATUS,
                "Europe|San_Marino|San_Marino",
                {"state": "done", "deleted": 4, "inserted": 2, "skipped": 0,
                 "message": "", "error": None, "log_tail": "", "updated_at": "t"},
            )
            snapshot = status_server.build_db_update_snapshot(repo_root, process_db, repo_root / "marks.json")
            row = snapshot["rows"][0]
            self.assertEqual(row["import_detail"]["deleted"], 4)
            self.assertEqual(row["import_detail"]["inserted"], 2)

    def test_check_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {"repo_root": repo_root, "process_python_exe": str(repo_root / "nope.exe")}
            result = status_server.check_environment(config)
            self.assertFalse(result["ok"])
            self.assertIn("入库工具解释器", result["missing"])
            qgis = next(item for item in result["items"] if item["label"] == "QGIS")
            expected = (Path(status_server.DEFAULT_QGIS_DIR) / "bin" / "qgis_process-qgis.bat").is_file()
            self.assertEqual(qgis["exists"], expected)

    def test_env_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/env", timeout=30) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.assertIn("items", data)
                self.assertFalse(data["ok"])
            finally:
                server.shutdown()
                server.server_close()

    def test_build_env_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {"repo_root": repo_root, "process_python_exe": str(repo_root / "nope.exe")}
            env = status_server.build_env_snapshot(config)
            self.assertEqual(env["repo_root"], str(repo_root))
            self.assertFalse(env["update_python_exists"])
            self.assertFalse(env["update_script_exists"])
            self.assertFalse(env["update_log_dir_exists"])
            self.assertFalse(env["process_python_exists"])

            python_exe = repo_root / status_server.DEFAULT_UPDATE_PYTHON
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_bytes(b"x")
            script = repo_root / status_server.DEFAULT_UPDATE_SCRIPT
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_bytes(b"x")
            env = status_server.build_env_snapshot(config)
            self.assertTrue(env["update_python_exists"])
            self.assertTrue(env["update_script_exists"])

    def test_load_dashboard_config_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = status_server.load_dashboard_config(repo_root)
            self.assertFalse(config["configured"])
            self.assertEqual(config["database"]["host"], status_server.DEFAULT_UPDATE_DB["host"])
            self.assertEqual(config["database"]["port"], status_server.DEFAULT_UPDATE_DB["port"])
            self.assertEqual(config["result_root"], status_server.DEFAULT_RESULT_ROOT)

    def test_save_and_load_dashboard_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            saved = status_server.save_dashboard_config(
                repo_root,
                {
                    "database": {"host": "10.0.0.5", "port": "5433", "dbname": "gis",
                                 "user": "u1", "password": "p1"},
                    "result_root": "\\\\srv\\share\\AutoGenerte",
                    "qgis_dir": "E:\\QGIS",
                },
            )
            self.assertEqual(saved["database"]["port"], 5433)
            self.assertEqual(saved["qgis_dir"], "E:\\QGIS")
            config = status_server.load_dashboard_config(repo_root)
            self.assertTrue(config["configured"])
            self.assertEqual(config["database"]["host"], "10.0.0.5")
            self.assertEqual(config["database"]["port"], 5433)
            self.assertEqual(config["database"]["dbname"], "gis")
            self.assertEqual(config["database"]["password"], "p1")
            self.assertEqual(config["result_root"], "\\\\srv\\share\\AutoGenerte")
            self.assertEqual(config["qgis_dir"], "E:\\QGIS")
            self.assertTrue((repo_root / "data" / "config.json").exists())

    def test_config_endpoint_get_and_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=10) as response:
                    initial = json.loads(response.read().decode("utf-8"))
                self.assertEqual(initial["database"]["dbname"], status_server.DEFAULT_UPDATE_DB["dbname"])

                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/config",
                    data=json.dumps({
                        "database": {"host": "1.2.3.4", "port": 5555, "dbname": "bld",
                                     "user": "pg", "password": "secret"},
                        "result_root": "\\\\host\\AutoGenerte",
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])

                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=10) as response:
                    saved = json.loads(response.read().decode("utf-8"))
                self.assertEqual(saved["database"]["host"], "1.2.3.4")
                self.assertEqual(saved["database"]["port"], 5555)
                self.assertEqual(saved["database"]["password"], "secret")
                self.assertEqual(saved["result_root"], "\\\\host\\AutoGenerte")
            finally:
                server.shutdown()
                server.server_close()

    def test_config_endpoint_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root)
            port = server.server_address[1]
            try:
                for body in (
                    {"database": {"host": "h", "port": 70000, "dbname": "b", "user": "u", "password": ""}, "result_root": "x"},
                    {"database": {"host": "h", "port": 5432, "dbname": "b", "user": "u", "password": ""}, "result_root": ""},
                ):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/config",
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

    def test_config_endpoint_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            server = self._make_server(repo_root, action_token="secret")
            port = server.server_address[1]
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/config",
                    data=json.dumps({"database": {"host": "h", "port": 5432, "dbname": "b", "user": "u", "password": ""}, "result_root": "x"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request, timeout=10)
                self.assertEqual(context.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
