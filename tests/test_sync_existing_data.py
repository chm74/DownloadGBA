import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import sync_existing_data as sync
import world_tasks_lib as lib


class SyncExistingDataTests(unittest.TestCase):
    def _build_fixture(self, repo_root: Path) -> tuple[Path, Path]:
        state_db = repo_root / "state.db"
        store = lib.StateStore(state_db)
        try:
            tasks = [
                lib.Task("亚洲", "中国", "上海市", "data/亚洲/中国/上海市"),
                lib.Task("亚洲", "中国", "广东省", "data/亚洲/中国/广东省"),
                lib.Task("Africa", "Algeria", "Algeria", "data/Africa/Algeria/Algeria"),
            ]
            store.sync_manifest(tasks)
        finally:
            store.close()

        scan_root = repo_root / "data" / "亚洲" / "中国"
        (scan_root / "上海市").mkdir(parents=True)
        (scan_root / "上海市" / "shanghai_buildings_height_gba.shp").write_bytes(b"fake")
        (scan_root / "北京市").mkdir(parents=True)
        (scan_root / "北京市" / "beijing_buildings_height_gba.shp").write_bytes(b"fake")
        return state_db, scan_root

    def test_find_existing_datasets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _, scan_root = self._build_fixture(repo_root)
            datasets = sync.find_existing_datasets(scan_root)
            self.assertEqual([item["dir"].name for item in datasets], ["上海市", "北京市"])

    def test_sync_updates_state_and_reports_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db, scan_root = self._build_fixture(repo_root)

            result = sync.sync_existing_datasets(repo_root, state_db, scan_root)

            self.assertEqual(result["total"], 2)
            self.assertEqual(len(result["matched"]), 1)
            self.assertEqual(result["unmatched"], ["北京市"])

            store = lib.StateStore(state_db)
            try:
                row = store.get("亚洲|中国|上海市")
                self.assertEqual(row["existing_data_dir"], "data/亚洲/中国/上海市")
                self.assertIsNone(store.get("亚洲|中国|广东省")["existing_data_dir"])
            finally:
                store.close()

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db, scan_root = self._build_fixture(repo_root)

            sync.sync_existing_datasets(repo_root, state_db, scan_root, dry_run=True)

            store = lib.StateStore(state_db)
            try:
                self.assertIsNone(store.get("亚洲|中国|上海市")["existing_data_dir"])
            finally:
                store.close()

    def test_mark_status_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_db, scan_root = self._build_fixture(repo_root)

            sync.sync_existing_datasets(
                repo_root,
                state_db,
                scan_root,
                mark_status=lib.STATUS_SKIPPED,
            )

            store = lib.StateStore(state_db)
            try:
                self.assertEqual(store.get("亚洲|中国|上海市")["status"], lib.STATUS_SKIPPED)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
