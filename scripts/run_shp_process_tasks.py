import argparse
import csv
import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world_tasks_lib as lib

DEFAULT_PROCESS_DB = "data/shp_process_state.db"
DEFAULT_DOWNLOAD_DB = "data/world_building_download_tasks_state.db"
DEFAULT_MANIFEST = "data/world_building_download_tasks.csv"
DEFAULT_SCAN_ROOT = "data"
DEFAULT_OUTPUT_ROOT = "Tools/Oneshp_pipline_qgis/out_data"
DEFAULT_PIPELINE_DIR = "Tools/Oneshp_pipline_qgis"
PIPELINE_SCRIPT_NAME = "pipeline.py"
PIPELINE_VENV_PYTHON = "Tools/Oneshp_pipline_qgis/.venv/Scripts/python.exe"
PROCESS_MARKER_NAME = "_PROCESS_DONE.json"
SHP_PATTERN = "*_buildings_height_gba*.shp"
PART_SUFFIX_PATTERN = re.compile(r"_part\d+$", re.IGNORECASE)
LEGACY_STEM_SUFFIX = "_buildings_height_gba"
SIDECAR_SUFFIXES = (".shx", ".dbf", ".prj", ".cpg", ".fix", ".qix")
SKIP_DIR_PREFIXES = (".", "_")
ESTIMATED_TILE_FEATURES = 80000

PROCESS_PENDING = "PENDING"
PROCESS_RUNNING = "RUNNING"
PROCESS_OK = "OK"
PROCESS_FAILED = "FAILED"
PROCESS_BLOCKED = "BLOCKED_NO_SHP"
PROCESS_ALL_STATUSES = (PROCESS_PENDING, PROCESS_RUNNING, PROCESS_OK, PROCESS_FAILED, PROCESS_BLOCKED)
SUCCESS_PROCESS_STATUSES = (PROCESS_OK,)

STAGE_LABELS = {
    "PREPARE": "准备输入",
    "SPLIT": "切分",
    "VALIDATE": "QGIS 校验",
    "CLEAN": "清洗",
    "REPROJECT": "重投影 3857",
    "VERIFY": "产物校验",
}

DEFAULT_MIN_FREE_RAM_GB = 3.0
DEFAULT_MIN_FREE_DISK_GB = 50.0
PROGRESS_WRITE_INTERVAL = 5.0
RESOURCE_WAIT_SECONDS = 60.0


class ProcessStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS process_tasks (
                dataset_key TEXT PRIMARY KEY,
                task_id TEXT,
                display_name TEXT,
                process_name_prefix TEXT,
                source_dir TEXT NOT NULL,
                input_dir TEXT,
                output_dir TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                feature_count INTEGER,
                tile_count INTEGER,
                shp_files TEXT,
                stage TEXT,
                stage_detail TEXT,
                tiles_done INTEGER,
                tiles_total INTEGER,
                last_log TEXT,
                last_error TEXT,
                log_file TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds INTEGER,
                updated_at TEXT NOT NULL,
                queue_order INTEGER,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS process_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_key TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_process_tasks_status ON process_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_process_events_key ON process_events(dataset_key);
            """
        )
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(process_tasks)")}
        if "process_name_prefix" not in columns:
            self.conn.execute("ALTER TABLE process_tasks ADD COLUMN process_name_prefix TEXT")
        if "queue_order" not in columns:
            self.conn.execute("ALTER TABLE process_tasks ADD COLUMN queue_order INTEGER")
        if "note" not in columns:
            self.conn.execute("ALTER TABLE process_tasks ADD COLUMN note TEXT")

    def close(self) -> None:
        self.conn.close()

    def get(self, dataset_key: str) -> dict | None:
        cursor = self.conn.execute("SELECT * FROM process_tasks WHERE dataset_key = ?", (dataset_key,))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def all_tasks(self) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM process_tasks ORDER BY queue_order IS NULL, queue_order, dataset_key"
        )
        return [dict(row) for row in cursor.fetchall()]

    def pending_tasks(self) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM process_tasks WHERE status = ? "
            "ORDER BY queue_order IS NULL, queue_order, dataset_key",
            (PROCESS_PENDING,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def counts(self) -> dict[str, int]:
        cursor = self.conn.execute("SELECT status, COUNT(*) AS total FROM process_tasks GROUP BY status")
        return {row["status"]: row["total"] for row in cursor.fetchall()}

    def upsert_source(self, source: dict) -> str:
        key = source["dataset_key"]
        row = self.get(key)
        now = lib.utc_now()
        shp_files = json.dumps(source["shp_files"], ensure_ascii=False)
        if row is None:
            next_order = self.conn.execute(
                "SELECT COALESCE(MAX(queue_order), 0) + 1 FROM process_tasks"
            ).fetchone()[0]
            self.conn.execute(
                """
                INSERT INTO process_tasks (
                    dataset_key, task_id, display_name, process_name_prefix, source_dir,
                    status, feature_count, shp_files, last_error, updated_at, queue_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    source.get("task_id"),
                    source.get("display_name"),
                    source.get("process_name_prefix"),
                    source["source_dir"],
                    source.get("status", PROCESS_PENDING),
                    source.get("feature_count"),
                    shp_files,
                    source.get("last_error"),
                    now,
                    next_order,
                ),
            )
            self.add_event(key, "PROCESS_SYNC", source["source_dir"])
            result = "added"
        else:
            self.conn.execute(
                """
                UPDATE process_tasks
                SET task_id = ?, display_name = ?, process_name_prefix = ?, source_dir = ?,
                    feature_count = ?, shp_files = ?, updated_at = ?
                WHERE dataset_key = ?
                """,
                (
                    source.get("task_id"),
                    source.get("display_name"),
                    source.get("process_name_prefix"),
                    source["source_dir"],
                    source.get("feature_count"),
                    shp_files,
                    now,
                    key,
                ),
            )
            if row["status"] == PROCESS_BLOCKED and source["shp_files"]:
                self.conn.execute(
                    "UPDATE process_tasks SET status = ?, last_error = NULL, updated_at = ? WHERE dataset_key = ?",
                    (PROCESS_PENDING, now, key),
                )
                self.add_event(key, "PROCESS_SYNC", "已补齐交付 SHP，恢复为 PENDING")
            result = "updated"
        self.conn.commit()
        return result

    def set_status(self, dataset_key: str, status: str, **fields) -> None:
        if status not in PROCESS_ALL_STATUSES:
            raise ValueError(f"非法处理状态: {status}")
        allowed = {
            "attempts",
            "feature_count",
            "tile_count",
            "shp_files",
            "stage",
            "stage_detail",
            "tiles_done",
            "tiles_total",
            "last_log",
            "last_error",
            "log_file",
            "input_dir",
            "output_dir",
            "started_at",
            "finished_at",
            "duration_seconds",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"set_status 不支持的字段: {sorted(unknown)}")
        columns = ["status = ?", "updated_at = ?"]
        values: list[object] = [status, lib.utc_now()]
        for key, value in fields.items():
            columns.append(f"{key} = ?")
            values.append(value)
        values.append(dataset_key)
        self.conn.execute(f"UPDATE process_tasks SET {', '.join(columns)} WHERE dataset_key = ?", values)
        self.conn.commit()

    def set_progress(self, dataset_key: str, stage: str, detail: str, tiles_done: int, tiles_total: int | None, last_log: str) -> None:
        self.conn.execute(
            """
            UPDATE process_tasks
            SET stage = ?, stage_detail = ?, tiles_done = ?, tiles_total = ?, last_log = ?, updated_at = ?
            WHERE dataset_key = ?
            """,
            (stage, detail, tiles_done, tiles_total, last_log, lib.utc_now(), dataset_key),
        )
        self.conn.commit()

    def set_note(self, dataset_key: str, note: str) -> str:
        value = (note or "").strip()
        self.conn.execute(
            "UPDATE process_tasks SET note = ?, updated_at = ? WHERE dataset_key = ?",
            (value, lib.utc_now(), dataset_key),
        )
        self.conn.commit()
        return value

    def reset_task(self, dataset_key: str, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE process_tasks
            SET status = ?, stage = NULL, stage_detail = NULL, tiles_done = NULL,
                tiles_total = NULL, last_error = NULL, started_at = NULL, finished_at = NULL,
                duration_seconds = NULL, updated_at = ?
            WHERE dataset_key = ?
            """,
            (PROCESS_PENDING, lib.utc_now(), dataset_key),
        )
        self.conn.commit()
        self.add_event(dataset_key, "PROCESS_RESET", reason)

    def requeue_task(self, dataset_key: str, position: str = "tail") -> list[str]:
        if position not in ("tail", "top"):
            raise ValueError(f"不支持的入队位置: {position}")
        row = self.get(dataset_key)
        if row is None:
            raise KeyError(f"未找到处理任务: {dataset_key}")
        if row["status"] != PROCESS_FAILED:
            raise ValueError(f"只有失败任务可以重新加入队列，当前状态: {row['status']}")

        pending = [item["dataset_key"] for item in self.pending_tasks()]
        ordered = [dataset_key, *pending] if position == "top" else [*pending, dataset_key]
        self.conn.execute(
            """
            UPDATE process_tasks
            SET status = ?, stage = NULL, stage_detail = NULL, tiles_done = NULL,
                tiles_total = NULL, last_error = NULL, started_at = NULL, finished_at = NULL,
                duration_seconds = NULL, updated_at = ?
            WHERE dataset_key = ?
            """,
            (PROCESS_PENDING, lib.utc_now(), dataset_key),
        )
        self.apply_pending_order(ordered)
        self.add_event(dataset_key, "PROCESS_REQUEUE", f"position={position}")
        return ordered

    def move_task(self, dataset_key: str, direction: str) -> list[str]:
        if direction not in ("up", "down", "top"):
            raise ValueError(f"不支持的排序方向: {direction}")
        keys = [row["dataset_key"] for row in self.pending_tasks()]
        if dataset_key not in keys:
            raise KeyError(f"待处理队列中没有任务: {dataset_key}")
        index = keys.index(dataset_key)
        if direction == "top":
            target = 0
        elif direction == "up":
            target = max(index - 1, 0)
        else:
            target = min(index + 1, len(keys) - 1)
        if target != index:
            keys.pop(index)
            keys.insert(target, dataset_key)
        self.apply_pending_order(keys)
        self.add_event(dataset_key, "PROCESS_ORDER", f"direction={direction} position={keys.index(dataset_key) + 1}")
        return keys

    def reorder_pending(self, ordered_keys: list[str]) -> list[str]:
        keys = [row["dataset_key"] for row in self.pending_tasks()]
        unknown = [key for key in ordered_keys if key not in keys]
        if unknown:
            raise KeyError(f"待处理队列中没有任务: {', '.join(unknown)}")
        head = list(dict.fromkeys(ordered_keys))
        tail = [key for key in keys if key not in head]
        ordered = head + tail
        self.apply_pending_order(ordered)
        self.add_event(ordered[0], "PROCESS_ORDER", f"order={len(ordered)}")
        return ordered

    def apply_pending_order(self, ordered_keys: list[str]) -> None:
        now = lib.utc_now()
        with self.conn:
            for position, key in enumerate(ordered_keys, start=1):
                self.conn.execute(
                    "UPDATE process_tasks SET queue_order = ?, updated_at = ? WHERE dataset_key = ?",
                    (position, now, key),
                )

    def add_event(self, dataset_key: str, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO process_events (dataset_key, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (dataset_key, event, detail, lib.utc_now()),
        )
        self.conn.commit()


def collapse_filters(value: str) -> list[str]:
    return [token.strip() for token in (value or "").split(",") if token.strip()]


def matches_filters(source: dict, only_tokens: list[str]) -> bool:
    if not only_tokens:
        return True
    haystack = (
        f"{source['dataset_key']} {source.get('task_id') or ''} {source['source_dir']} "
        f"{source.get('display_name') or ''} {source.get('process_name_prefix') or ''}"
    )
    return any(token in haystack for token in only_tokens)


def resolve_pending_keys(store: ProcessStore, tokens: list[str]) -> list[str]:
    pending = store.pending_tasks()
    resolved: list[str] = []
    for token in tokens:
        matches = [row["dataset_key"] for row in pending if token in row["dataset_key"]]
        if not matches:
            matches = [
                row["dataset_key"]
                for row in pending
                if token in (row.get("process_name_prefix") or "")
            ]
        if not matches:
            raise KeyError(f"待处理队列中未找到匹配: {token}")
        if len(matches) > 1:
            raise ValueError(f"匹配到多个任务，请写完整 dataset_key: {token} -> {', '.join(matches)}")
        resolved.append(matches[0])
    return resolved


def move_queue_task(process_db: Path, dataset_key: str, direction: str) -> list[str]:
    store = ProcessStore(process_db)
    try:
        return store.move_task(dataset_key, direction)
    finally:
        store.close()


def requeue_task(process_db: Path, dataset_key: str, position: str = "tail") -> list[str]:
    store = ProcessStore(process_db)
    try:
        return store.requeue_task(dataset_key, position)
    finally:
        store.close()


def reset_running_in_db(process_db: Path) -> int:
    store = ProcessStore(process_db)
    try:
        return reset_running_tasks(store)
    finally:
        store.close()


def build_run_command(
    python_exe: str,
    repo_root: Path,
    limit: int = 0,
    wait_resources: bool = True,
    only: str = "",
) -> list[str]:
    script = Path(repo_root) / "scripts" / "run_shp_process_tasks.py"
    arguments = ["-u", str(script), "--run"]
    if int(limit) > 0:
        arguments.extend(["--limit", str(int(limit))])
    if only:
        arguments.extend(["--only", only])
    if wait_resources:
        arguments.append("--wait-resources")
    return [python_exe, *arguments]


def print_pending_order(store: ProcessStore) -> None:
    for position, row in enumerate(store.pending_tasks(), start=1):
        print(f"[QUEUE {position:02d}] {row['dataset_key']}  prefix={row.get('process_name_prefix') or '-'}")


def is_skipped_dir(path: Path) -> bool:
    return any(part.startswith(SKIP_DIR_PREFIXES) for part in Path(path).parts)


def load_manifest_prefixes(repo_root: Path, manifest_path: Path | str) -> dict[str, str]:
    path = Path(manifest_path)
    if not path.is_absolute():
        path = Path(repo_root) / path
    if not path.exists():
        return {}
    try:
        encoding = lib.detect_encoding(path)
    except ValueError:
        return {}

    prefixes: dict[str, str] = {}
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        if "process_name_prefix" not in (reader.fieldnames or []):
            return {}
        for row in reader:
            continent = (row.get("continent") or "").strip()
            country = (row.get("country") or "").strip()
            city = (row.get("city") or "").strip()
            prefix = (row.get("process_name_prefix") or "").strip()
            if not (continent and country and city and prefix):
                continue
            prefixes[f"{continent}|{country}|{city}"] = lib.generate_slug(prefix)
    return prefixes


def derive_prefix_from_files(shp_files: list[str]) -> str | None:
    for relative in shp_files:
        stem = Path(str(relative)).stem
        stem = PART_SUFFIX_PATTERN.sub("", stem)
        if stem.endswith(LEGACY_STEM_SUFFIX):
            stem = stem[: -len(LEGACY_STEM_SUFFIX)]
        stem = stem.strip("_")
        if stem:
            return lib.generate_slug(stem)
    return None


def resolve_process_name_prefix(source: dict, prefix_map: dict[str, str] | None) -> str:
    task_id = source.get("task_id")
    if task_id and prefix_map and task_id in prefix_map:
        return prefix_map[task_id]
    derived = derive_prefix_from_files(source.get("shp_files") or [])
    if derived:
        return derived
    return lib.generate_slug(source["dataset_key"])


def find_shp_files(directory: Path) -> list[Path]:
    return sorted(path for path in Path(directory).glob(SHP_PATTERN) if not is_skipped_dir(path.parent))


def count_shp_features(shp_files: list[Path]) -> int | None:
    total = 0
    import pyogrio

    for shp in shp_files:
        try:
            info = pyogrio.read_info(str(shp))
        except Exception:
            return None
        total += int(info.get("features") or 0)
    return total


def read_download_candidates(repo_root: Path, download_db: Path) -> list[dict]:
    if not Path(download_db).exists():
        return []
    resolved = Path(download_db).resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM tasks ORDER BY manifest_order, task_id").fetchall()
    finally:
        connection.close()
    candidates: list[dict] = []
    for row in rows:
        row_keys = row.keys()
        existing = row["existing_data_dir"] if "existing_data_dir" in row_keys else None
        directory = existing or (row["download_dir"] if row["status"] in lib.SUCCESS_STATUSES else None)
        if not directory:
            continue
        try:
            task_dir = lib.resolve_task_dir(repo_root, directory)
        except ValueError:
            continue
        candidates.append({"task_id": row["task_id"], "dir": task_dir, "from_existing": bool(existing)})
    return candidates


def discover_sources(
    repo_root: Path,
    download_db: Path,
    scan_root: Path,
    extra_dirs: list[Path],
    scan_enabled: bool = True,
    prefix_map: dict[str, str] | None = None,
) -> list[dict]:
    repo_root = Path(repo_root)
    sources: dict[str, dict] = {}

    def register(directory: Path, task_id: str | None, from_existing: bool) -> None:
        directory = Path(directory).resolve()
        if is_skipped_dir(directory) or not directory.is_dir():
            return
        shps = find_shp_files(directory)
        if task_id:
            key = task_id
            display = directory.name
        else:
            key = f"EXT|{lib.repo_relative(repo_root, directory)}"
            display = directory.name
        if key in sources:
            return
        prefix = resolve_process_name_prefix(
            {"dataset_key": key, "task_id": task_id, "shp_files": [shp.name for shp in shps]},
            prefix_map,
        )
        if not shps:
            merged = sorted(directory.glob("*_buildings_height_gba.gpkg"))
            if merged:
                sources[key] = {
                    "dataset_key": key,
                    "task_id": task_id,
                    "display_name": display,
                    "process_name_prefix": prefix,
                    "source_dir": lib.repo_relative(repo_root, directory),
                    "shp_files": [],
                    "feature_count": None,
                    "status": PROCESS_BLOCKED,
                    "last_error": "只有合并 gpkg，缺少交付 SHP；先运行 run_world_building_tasks.py --export-shp-only",
                }
            return
        sources[key] = {
            "dataset_key": key,
            "task_id": task_id,
            "display_name": display,
            "process_name_prefix": prefix,
            "source_dir": lib.repo_relative(repo_root, directory),
            "shp_files": [lib.repo_relative(repo_root, shp) for shp in shps],
            "feature_count": count_shp_features(shps),
            "status": PROCESS_PENDING,
        }

    for candidate in read_download_candidates(repo_root, download_db):
        if candidate["from_existing"]:
            register(candidate["dir"], candidate["task_id"], True)
    for candidate in read_download_candidates(repo_root, download_db):
        if not candidate["from_existing"]:
            register(candidate["dir"], candidate["task_id"], False)

    if scan_enabled and Path(scan_root).is_dir():
        seen_dirs = {str((repo_root / source["source_dir"]).resolve()) for source in sources.values()}
        for shp in sorted(Path(scan_root).rglob(SHP_PATTERN)):
            directory = shp.parent
            if is_skipped_dir(directory):
                continue
            if str(directory.resolve()) in seen_dirs:
                continue
            register(directory, None, False)
            seen_dirs.add(str(directory.resolve()))

    for directory in extra_dirs:
        target = Path(directory)
        if not target.is_absolute():
            target = repo_root / target
        register(target, None, False)

    return sorted(sources.values(), key=lambda item: item["dataset_key"])


def sync_process_tasks(
    repo_root: Path,
    process_db: Path,
    download_db: Path,
    scan_root: Path,
    extra_dirs: list[Path],
    only_tokens: list[str],
    scan_enabled: bool = True,
    manifest_path: Path | str = DEFAULT_MANIFEST,
) -> dict:
    store = ProcessStore(process_db)
    try:
        prefix_map = load_manifest_prefixes(repo_root, manifest_path)
        sources = discover_sources(repo_root, download_db, scan_root, extra_dirs, scan_enabled, prefix_map)
        selected = [source for source in sources if matches_filters(source, only_tokens)]
        added = 0
        updated = 0
        for source in selected:
            result = store.upsert_source(source)
            if result == "added":
                added += 1
            else:
                updated += 1
        return {"total": len(selected), "added": added, "updated": updated, "sources": selected}
    finally:
        store.close()


def available_memory_bytes() -> int | None:
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
    except OSError:
        return None
    return int(status.ullAvailPhys)


def check_resources(repo_root: Path, min_ram_gb: float, min_disk_gb: float) -> tuple[bool, str]:
    reasons: list[str] = []
    free_ram = available_memory_bytes()
    if free_ram is not None and free_ram < min_ram_gb * (1 << 30):
        reasons.append(f"可用内存 {free_ram / (1 << 30):.1f}GB < {min_ram_gb:.1f}GB")
    try:
        free_disk = shutil.disk_usage(repo_root).free
    except OSError:
        free_disk = None
    if free_disk is not None and free_disk < min_disk_gb * (1 << 30):
        reasons.append(f"磁盘剩余 {free_disk / (1 << 30):.1f}GB < {min_disk_gb:.1f}GB")
    if reasons:
        return False, "；".join(reasons)
    return True, ""


def wait_for_resources(repo_root: Path, min_ram_gb: float, min_disk_gb: float, wait: bool) -> tuple[bool, str]:
    ok, reason = check_resources(repo_root, min_ram_gb, min_disk_gb)
    if ok or not wait:
        return ok, reason
    print(f"[WAIT] 资源不足：{reason}；{int(RESOURCE_WAIT_SECONDS)} 秒后重试", flush=True)
    while not ok:
        time.sleep(RESOURCE_WAIT_SECONDS)
        ok, reason = check_resources(repo_root, min_ram_gb, min_disk_gb)
        if not ok:
            print(f"[WAIT] 资源仍不足：{reason}", flush=True)
    print("[WAIT] 资源已满足，继续执行", flush=True)
    return True, ""


def dataset_slug(dataset_key: str) -> str:
    return lib.generate_slug(dataset_key)


def prepare_input_dir(repo_root: Path, source: dict, output_root: Path) -> tuple[Path, list[dict]]:
    prefix = source.get("process_name_prefix") or dataset_slug(source["dataset_key"])
    input_dir = Path(output_root) / f"{prefix}_pipeline_input"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    shp_entries = list(source["shp_files"])
    renames: list[dict] = []
    used_names: set[str] = set()
    for index, relative in enumerate(shp_entries, start=1):
        shp = Path(repo_root) / relative
        if len(shp_entries) == 1:
            target_name = f"{prefix}.shp"
        else:
            target_name = f"{prefix}_part{index:02d}.shp"
        candidate = Path(target_name)
        suffix_index = 2
        while candidate.name.lower() in used_names:
            candidate = Path(f"{candidate.stem}_{suffix_index}{candidate.suffix}")
            suffix_index += 1
        used_names.add(candidate.name.lower())

        target = input_dir / candidate.name
        shutil.copy2(shp, target)
        for suffix in SIDECAR_SUFFIXES:
            sidecar = shp.with_suffix(suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, input_dir / candidate.with_suffix(suffix).name)
        renames.append({"source": relative, "input": candidate.name})
    return input_dir, renames


def resolve_pipeline_python(repo_root: Path, configured: str | None) -> str:
    if configured:
        return configured
    venv_python = Path(repo_root) / PIPELINE_VENV_PYTHON
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def build_pipeline_command(repo_root: Path, python_exe: str, input_dir: Path, output_dir: Path) -> list[str]:
    script = Path(repo_root) / DEFAULT_PIPELINE_DIR / PIPELINE_SCRIPT_NAME
    return [python_exe, "-u", str(script), str(input_dir), str(output_dir)]


def parse_stage(line: str, state: dict) -> None:
    if line.startswith("Splitting ") and "chunk(s)" not in line:
        state["stage"] = "SPLIT"
        state["stage_detail"] = line
        return
    if " into " in line and line.endswith("chunk(s)") and line.startswith("Split "):
        state["stage"] = "SPLIT"
        state["stage_detail"] = line
        try:
            chunks = int(line.rsplit(" ", 2)[1])
        except (ValueError, IndexError):
            chunks = 0
        if chunks > 0:
            state["tiles_total"] = (state.get("tiles_total") or 0) + chunks
        return
    if line.startswith("Skipping split for "):
        state["stage"] = "SPLIT"
        state["stage_detail"] = line
        state["tiles_total"] = (state.get("tiles_total") or 0) + 1
        return
    if line.startswith("Loaded "):
        state["stage"] = "SPLIT"
        state["stage_detail"] = line
        return
    if line.startswith("Running QGIS validity check: "):
        state["stage"] = "VALIDATE"
        state["stage_detail"] = line
        return
    if line.startswith("QGIS result for "):
        state["stage"] = "VALIDATE"
        state["stage_detail"] = line
        return
    if line.startswith("Cleaning valid output: "):
        state["stage"] = "CLEAN"
        state["stage_detail"] = line
        return
    if line.startswith("Reprojecting to EPSG:3857: "):
        state["stage"] = "REPROJECT"
        state["stage_detail"] = line
        return
    if line.startswith("Completed "):
        state["stage"] = "VERIFY"
        state["stage_detail"] = line
        return
    if line.startswith("Starting pipeline:"):
        state["stage"] = "PREPARE"
        state["stage_detail"] = line


def count_final_tiles(output_dir: Path) -> int:
    final_dir = Path(output_dir) / "final"
    if not final_dir.is_dir():
        return 0
    return sum(1 for _ in final_dir.glob("*_3857.shp"))


def run_pipeline_process(
    repo_root: Path,
    command: list[str],
    log_path: Path,
    on_progress,
) -> int:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(Path(repo_root) / DEFAULT_PIPELINE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=lib.child_process_env(),
            creationflags=creationflags,
        )
        if process.stdout is not None:
            for line in process.stdout:
                stripped = line.rstrip("\r\n")
                log_handle.write(stripped + "\n")
                log_handle.flush()
                if stripped:
                    on_progress(stripped)
        process.wait()
        return int(process.returncode)


def verify_output(output_dir: Path) -> tuple[bool, int, int, str]:
    import pyogrio

    final_dir = Path(output_dir) / "final"
    tiles = sorted(final_dir.glob("*_3857.shp")) if final_dir.is_dir() else []
    if not tiles:
        return False, 0, 0, f"final 目录缺少 3857 分片: {final_dir}"
    total_features = 0
    for tile in tiles:
        try:
            info = pyogrio.read_info(str(tile))
        except Exception as exc:
            return False, 0, 0, f"分片无法读取: {tile.name}: {exc}"
        total_features += int(info.get("features") or 0)
    crs_text = str(pyogrio.read_info(str(tiles[0])).get("crs") or "")
    if "3857" not in crs_text:
        try:
            from pyproj import CRS

            if CRS.from_user_input(crs_text).to_epsg() != 3857:
                return False, 0, 0, f"分片 CRS 不是 EPSG:3857: {tiles[0].name}"
        except Exception:
            pass
    return True, len(tiles), total_features, ""


def write_process_marker(output_dir: Path, payload: dict) -> Path:
    target = Path(output_dir) / PROCESS_MARKER_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def write_back_processed_dir(repo_root: Path, download_db: Path, task_id: str | None, processed_dir: str) -> None:
    if not task_id or not Path(download_db).exists():
        return
    store = lib.StateStore(download_db)
    try:
        if store.get(task_id) is None:
            return
        store.set_processed_3857(task_id, processed_dir)
        store.add_event(task_id, "PROCESS_DONE", processed_dir)
    finally:
        store.close()


def execute_process_task(repo_root: Path, store: ProcessStore, source: dict, args, output_root: Path) -> dict:
    key = source["dataset_key"]
    now = lib.utc_now()
    previous = store.get(key) or {}
    attempts = int(previous.get("attempts") or 0) + 1
    prefix = source.get("process_name_prefix") or dataset_slug(key)
    try:
        input_dir, input_renames = prepare_input_dir(repo_root, source, output_root)
    except OSError as exc:
        store.set_status(
            key,
            PROCESS_FAILED,
            attempts=attempts,
            last_error=f"准备输入失败: {exc}"[:2000],
            finished_at=lib.utc_now(),
        )
        store.add_event(key, "PROCESS_FAIL", f"prepare_input: {exc}"[:500])
        return {"dataset_key": key, "status": PROCESS_FAILED, "exit_code": -1}
    output_dir = Path(output_root) / f"{prefix}_pipeline"
    log_path = output_dir / "stdout.log"
    python_exe = resolve_pipeline_python(repo_root, args.python_exe)
    command = build_pipeline_command(repo_root, python_exe, input_dir, output_dir)
    started = time.monotonic()

    store.set_status(
        key,
        PROCESS_RUNNING,
        attempts=attempts,
        input_dir=lib.repo_relative(repo_root, input_dir),
        output_dir=lib.repo_relative(repo_root, output_dir),
        log_file=lib.repo_relative(repo_root, log_path),
        stage="PREPARE",
        stage_detail=f"已准备 {len(source['shp_files'])} 个 SHP",
        tiles_done=0,
        tiles_total=None,
        last_error=None,
        started_at=now,
        finished_at=None,
        duration_seconds=None,
    )
    store.add_event(key, "PROCESS_START", f"attempt={attempts}")

    state: dict = {"stage": "PREPARE", "stage_detail": "", "tiles_total": None}
    last_write = {"time": 0.0}

    def on_progress(line: str) -> None:
        parse_stage(line, state)
        current = time.monotonic()
        if current - last_write["time"] < PROGRESS_WRITE_INTERVAL:
            return
        last_write["time"] = current
        store.set_progress(
            key,
            state["stage"],
            state["stage_detail"],
            count_final_tiles(output_dir),
            state["tiles_total"],
            "\n".join(lib.tail_lines(log_path, 3).splitlines()) if log_path.exists() else line,
        )

    try:
        exit_code = run_pipeline_process(repo_root, command, log_path, on_progress)
    except OSError as exc:
        exit_code = -1
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[ERROR] 无法启动处理进程: {exc}\n")

    duration = int(time.monotonic() - started)
    if exit_code != 0:
        tail = lib.tail_lines(log_path, 5) if log_path.exists() else ""
        store.set_status(
            key,
            PROCESS_FAILED,
            last_error=f"处理进程退出码 {exit_code}；{tail}"[:2000],
            stage=None,
            tiles_done=count_final_tiles(output_dir),
            finished_at=lib.utc_now(),
            duration_seconds=duration,
        )
        store.add_event(key, "PROCESS_FAIL", f"exit_code={exit_code}")
        return {"dataset_key": key, "status": PROCESS_FAILED, "exit_code": exit_code}

    store.set_status(key, PROCESS_RUNNING, stage="VERIFY", stage_detail="校验 3857 产物")
    ok, tile_count, feature_count, reason = verify_output(output_dir)
    if not ok:
        store.set_status(
            key,
            PROCESS_FAILED,
            last_error=reason[:2000],
            finished_at=lib.utc_now(),
            duration_seconds=duration,
        )
        store.add_event(key, "PROCESS_VERIFY_FAIL", reason[:500])
        return {"dataset_key": key, "status": PROCESS_FAILED, "exit_code": 0, "reason": reason}

    processed_dir = lib.repo_relative(repo_root, output_dir)
    store.set_status(
        key,
        PROCESS_OK,
        stage=None,
        stage_detail=None,
        tile_count=tile_count,
        feature_count=feature_count,
        tiles_done=tile_count,
        tiles_total=tile_count,
        last_error=None,
        finished_at=lib.utc_now(),
        duration_seconds=duration,
    )
    store.add_event(key, "PROCESS_DONE", f"tiles={tile_count} features={feature_count}")
    write_process_marker(
        output_dir,
        {
            "dataset_key": key,
            "task_id": source.get("task_id"),
            "status": PROCESS_OK,
            "process_name_prefix": prefix,
            "source_dir": source["source_dir"],
            "output_dir": processed_dir,
            "shp_files": source["shp_files"],
            "input_renames": input_renames,
            "tile_count": tile_count,
            "feature_count": feature_count,
            "log_file": lib.repo_relative(repo_root, log_path),
            "finished_at": lib.utc_now(),
            "pipeline_dir": DEFAULT_PIPELINE_DIR,
        },
    )
    write_back_processed_dir(repo_root, args.download_db, source.get("task_id"), processed_dir)
    return {
        "dataset_key": key,
        "status": PROCESS_OK,
        "tiles": tile_count,
        "features": feature_count,
        "duration_seconds": duration,
    }


def select_tasks(store: ProcessStore, only_tokens: list[str], retry_failed: bool, force: bool, limit: int) -> list[dict]:
    tasks = []
    for row in store.all_tasks():
        if only_tokens and not matches_filters(row, only_tokens):
            continue
        status = row["status"]
        if status == PROCESS_PENDING:
            tasks.append(row)
        elif status == PROCESS_FAILED and retry_failed:
            tasks.append(row)
        elif status == PROCESS_OK and force:
            tasks.append(row)
    if limit > 0:
        tasks = tasks[:limit]
    return tasks


def reset_running_tasks(store: ProcessStore) -> int:
    reset = 0
    for row in store.all_tasks():
        if row["status"] == PROCESS_RUNNING:
            store.reset_task(row["dataset_key"], "检测到中断的 RUNNING，自动重置为 PENDING")
            reset += 1
    return reset


def print_summary(store: ProcessStore) -> None:
    counts = store.counts()
    total = sum(counts.values())
    print(
        f"[PROCESS] total={total} ok={counts.get(PROCESS_OK, 0)} pending={counts.get(PROCESS_PENDING, 0)} "
        f"running={counts.get(PROCESS_RUNNING, 0)} failed={counts.get(PROCESS_FAILED, 0)} "
        f"blocked={counts.get(PROCESS_BLOCKED, 0)}"
    )
    for row in store.all_tasks():
        if row["status"] == PROCESS_PENDING:
            print(
                f"[PENDING] {row['dataset_key']}  prefix={row.get('process_name_prefix') or '-'}  "
                f"features={row['feature_count']}  source={row['source_dir']}"
            )
    for row in store.all_tasks():
        if row["status"] == PROCESS_BLOCKED:
            print(f"[BLOCKED] {row['dataset_key']}  {row['last_error']}")
    for row in store.all_tasks():
        if row["status"] == PROCESS_FAILED:
            print(f"[FAILED] {row['dataset_key']}  attempts={row['attempts']}  {row['last_error']}")


def print_running_tasks(store: ProcessStore) -> None:
    running = [row for row in store.all_tasks() if row["status"] == PROCESS_RUNNING]
    if not running:
        print("没有正在执行的处理任务")
        return
    for row in running:
        stage = STAGE_LABELS.get(row["stage"] or "", row["stage"] or "-")
        tiles = f"{row['tiles_done'] or 0}/{row['tiles_total'] or '?'}"
        print(
            f"[RUNNING] {row['dataset_key']}  prefix={row.get('process_name_prefix') or '-'}  "
            f"stage={stage}  tiles={tiles}  "
            f"started_at={row['started_at'] or '-'}  attempt={row['attempts']}"
        )
        if row["stage_detail"]:
            print(f"          {row['stage_detail']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="建筑 SHP 处理队列：切分 → QGIS 校验 → 清洗 → 重投影 3857。")
    parser.add_argument("--process-db", default=DEFAULT_PROCESS_DB, help="处理状态库路径。")
    parser.add_argument("--download-db", default=DEFAULT_DOWNLOAD_DB, help="下载状态库路径（用于发现已完成数据与回写处理路径）。")
    parser.add_argument("--tasks", default=DEFAULT_MANIFEST, help="下载任务清单 CSV（process_name_prefix 权威来源）。")
    parser.add_argument("--repo-root", default=None, help="仓库根目录，默认取脚本上级目录。")
    parser.add_argument("--scan-root", default=DEFAULT_SCAN_ROOT, help="扫描交付 SHP 的根目录，默认 data。")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="处理输入/输出根目录。")
    parser.add_argument("--python-exe", default=None, help="运行处理工具的 Python，默认使用 Tools 内的 .venv。")
    parser.add_argument("--only", default="", help="按 dataset_key / 来源目录子串过滤，逗号分隔。")
    parser.add_argument("--register", default="", help="额外登记的数据目录，逗号分隔（相对于仓库根或用绝对路径）。")
    parser.add_argument("--limit", type=int, default=0, help="最多执行多少个任务，0 表示不限制。")
    parser.add_argument("--retry-failed", action="store_true", help="把 FAILED 任务加入本次执行。")
    parser.add_argument("--force", action="store_true", help="把 OK 任务重新执行（覆盖输出）。")
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="任务之间休眠秒数。")
    parser.add_argument("--min-free-ram-gb", type=float, default=DEFAULT_MIN_FREE_RAM_GB, help="执行前要求的最小可用内存。")
    parser.add_argument("--min-free-disk-gb", type=float, default=DEFAULT_MIN_FREE_DISK_GB, help="执行前要求的最小磁盘剩余。")
    parser.add_argument("--wait-resources", action="store_true", help="资源不足时轮询等待，而不是直接退出。")
    parser.add_argument("--no-scan", action="store_true", help="只使用下载状态库中发现的数据集，不扫描目录。")
    parser.add_argument("--sync", action="store_true", help="只把可处理数据集同步进处理队列。")
    parser.add_argument("--run", action="store_true", help="执行队列中的任务。")
    parser.add_argument("--dry-run", action="store_true", help="只打印执行计划，不复制、不处理、不改状态。")
    parser.add_argument("--summary", action="store_true", help="打印处理队列统计。")
    parser.add_argument("--status", action="store_true", help="同 --summary。")
    parser.add_argument("--running", action="store_true", help="打印正在执行的处理任务。")
    parser.add_argument("--reset-task", default="", help="把指定 dataset_key 重置为 PENDING，逗号分隔。")
    parser.add_argument("--move-up", default="", help="把指定任务（dataset_key 或前缀子串，逗号分隔）在待处理队列中上移一位。")
    parser.add_argument("--move-down", default="", help="把指定任务在待处理队列中下移一位。")
    parser.add_argument("--move-top", default="", help="把指定任务置顶。")
    parser.add_argument("--order", default="", help="按给定顺序重排待处理队列（未列出的保持原相对顺序），逗号分隔。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    process_db = Path(args.process_db)
    if not process_db.is_absolute():
        process_db = repo_root / process_db
    download_db = Path(args.download_db)
    if not download_db.is_absolute():
        download_db = repo_root / download_db
    scan_root = Path(args.scan_root)
    if not scan_root.is_absolute():
        scan_root = repo_root / scan_root
    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    args.download_db = download_db

    store = ProcessStore(process_db)
    try:
        if args.running:
            print_running_tasks(store)
            return 0

        if args.reset_task:
            for key in collapse_filters(args.reset_task):
                if store.get(key) is None:
                    print(f"[SKIP] 未找到任务: {key}")
                    continue
                store.reset_task(key, "手动重置")
                print(f"[RESET] {key}")
            return 0

        if args.move_up or args.move_down or args.move_top or args.order:
            try:
                if args.order:
                    keys = resolve_pending_keys(store, collapse_filters(args.order))
                    ordered = store.reorder_pending(keys)
                    print(f"[ORDER] 已按指定顺序重排 {len(ordered)} 个待处理任务")
                for direction, value in (("up", args.move_up), ("down", args.move_down), ("top", args.move_top)):
                    for key in resolve_pending_keys(store, collapse_filters(value)):
                        store.move_task(key, direction)
                        print(f"[MOVE-{direction.upper()}] {key}")
            except (KeyError, ValueError) as exc:
                print(f"[ERROR] {exc}")
                return 1
            print_pending_order(store)
            return 0

        if args.summary or args.status:
            print_summary(store)
            return 0

        only_tokens = collapse_filters(args.only)
        extra_dirs = [Path(token) for token in collapse_filters(args.register)]
        if not args.no_scan or extra_dirs or args.sync or args.run:
            result = sync_process_tasks(
                repo_root,
                process_db,
                download_db,
                scan_root,
                extra_dirs,
                only_tokens,
                scan_enabled=not args.no_scan,
                manifest_path=args.tasks,
            )
            print(
                f"[SYNC] process_db={process_db} 发现={result['total']} 新增={result['added']} 更新={result['updated']}",
                flush=True,
            )
        if args.sync and not args.run:
            print_summary(store)
            return 0

        if args.run:
            reset_count = reset_running_tasks(store)
            if reset_count:
                print(f"[RESET] 已把 {reset_count} 个中断的 RUNNING 任务重置为 PENDING", flush=True)
            tasks = select_tasks(store, only_tokens, args.retry_failed, args.force, args.limit)
            if not tasks:
                print("[PLAN] 没有可执行的处理任务", flush=True)
                print_summary(store)
                return 0
            for index, row in enumerate(tasks, start=1):
                source = {
                    "dataset_key": row["dataset_key"],
                    "task_id": row["task_id"],
                    "display_name": row["display_name"],
                    "process_name_prefix": row.get("process_name_prefix")
                    or lib.generate_slug(row["dataset_key"]),
                    "source_dir": row["source_dir"],
                    "shp_files": json.loads(row["shp_files"] or "[]"),
                    "feature_count": row["feature_count"],
                }
                plan = (
                    f"[PLAN {index}/{len(tasks)}] {row['dataset_key']}  "
                    f"prefix={source['process_name_prefix']}  features={row['feature_count']}  "
                    f"shp={len(source['shp_files'])}  source={row['source_dir']}"
                )
                print(plan, flush=True)
                if args.dry_run:
                    continue
                ok, reason = wait_for_resources(repo_root, args.min_free_ram_gb, args.min_free_disk_gb, args.wait_resources)
                if not ok:
                    print(f"[STOP] 资源不足，暂停处理队列：{reason}", flush=True)
                    return 3
                if args.force:
                    store.reset_task(row["dataset_key"], "强制重跑")
                result = execute_process_task(repo_root, store, source, args, output_root)
                print(
                    f"[DONE] {result['dataset_key']} status={result['status']} "
                    f"tiles={result.get('tiles', '-')} features={result.get('features', '-')}",
                    flush=True,
                )
                if index < len(tasks) and args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
            print_summary(store)
            return 0

        print_summary(store)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
