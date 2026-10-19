import argparse
import functools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_shp_process_tasks as process_queue
import world_tasks_lib as lib

DEFAULT_STATE_DB = "data/world_building_download_tasks_state.db"
DEFAULT_TASKS_CSV = "data/world_building_download_tasks.csv"
MANIFEST_KEYWORD_MAX_LEN = 100
MANIFEST_SEARCH_LIMIT = 50
DEFAULT_PROCESS_DB = "data/shp_process_state.db"
DEFAULT_DB_UPDATE_FILE = "data/db_update_status.json"
DEFAULT_PAGE = "scripts/status_page.html"
DEFAULT_BOUNDARIES_DIR = "data/boundaries"
DEFAULT_LOG_ROOT = "data/logs"
DEFAULT_RUN_LOG = "data/logs/download/run.log"
DEFAULT_RUN_ERR_LOG = "data/logs/download/run.err.log"
DEFAULT_CONFIG_FILE = "data/config.json"
DEFAULT_UPDATE_DB = {
    "host": "172.16.1.145",
    "port": 5432,
    "dbname": "building",
    "user": "postgres",
    "password": "frontfree",
}
DEFAULT_RESULT_ROOT = r"\\172.16.0.116\BuildingData\AutoGenerete"
DEFAULT_QGIS_DIR = r"D:\QGIS"
DEFAULT_UPDATE_PYTHON = "Tools/Twopyshp2pgsql/.venv/Scripts/python.exe"
DEFAULT_UPDATE_SCRIPT = "Tools/Twopyshp2pgsql/main3_region.py"
DEFAULT_UPDATE_LOG_DIR = "data/logs/update"
DEFAULT_DB_IMPORT_STATUS = "data/db_import_status.json"
DEFAULT_DB_COPY_STATUS = "data/db_copy_status.json"
RUNNER_MARKER = "run_world_building_tasks.py"
WORKER_MARKER = "download_gba_lod1_wfs"
PROCESS_RUNNER_MARKER = "run_shp_process_tasks.py"
PROCESS_WORKER_MARKER = "pipeline.py"
PROCESS_WORKER_DIR = "Oneshp_pipline_qgis"
DEFAULT_PROCESS_PYTHON = "Tools/Oneshp_pipline_qgis/.venv/Scripts/python.exe"
DEFAULT_PROCESS_LOG = "data/logs/process/run.log"
DEFAULT_PROCESS_ERR_LOG = "data/logs/process/run.err.log"
DEFAULT_TILE_WORKERS = 2
CONTROL_LOCK = threading.Lock()

PRESERVED_VALUE_FLAGS = (
    "--only",
    "--continent",
    "--country",
    "--status",
    "--max-tasks",
    "--grid-size",
    "--page-size",
    "--sleep-seconds",
    "--task-attempts",
    "--merge-format",
    "--engine",
    "--boundaries-dir",
    "--overrides",
)
PRESERVED_SWITCH_FLAGS = (
    "--retry-failed",
    "--force",
    "--stop-on-error",
    "--hash",
    "--refresh-boundary",
)
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_QUEUE_LIMIT = 15
DEFAULT_RECENT_LIMIT = 10
DEFAULT_STALE_MINUTES = 10
DEFAULT_REFRESH_SECONDS = 15

GRID_TOTAL_CACHE: dict[tuple[str, float], int] = {}
RAW_DATA_CACHE: dict[str, tuple[float, str | None]] = {}
MERGE_JOBS: dict[str, dict] = {}
MERGE_LOCK = threading.Lock()
DB_IMPORT_JOBS: dict[str, dict] = {}
DB_IMPORT_LOCK = threading.Lock()
DB_COPY_JOBS: dict[str, dict] = {}
DB_COPY_LOCK = threading.Lock()
DB_COPY_STATUS_LOCK = threading.Lock()

CONTINENT_ZH = {
    "Africa": "非洲",
    "America": "美洲",
    "Asia": "亚洲",
    "Europe": "欧洲",
    "Oceania": "大洋洲",
    "Antarctica": "南极洲",
}
CONTINENT_EN_BY_ZH = {chinese: english for english, chinese in CONTINENT_ZH.items()}
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def format_continent(value: str) -> str:
    raw = str(value or "").strip()
    english = CONTINENT_EN_BY_ZH.get(raw, raw)
    chinese = CONTINENT_ZH.get(english)
    if chinese:
        return f"{english}({chinese})"
    return raw


def format_bilingual(value: str, chinese: str) -> str:
    raw = str(value or "").strip()
    if not raw or CJK_PATTERN.search(raw):
        return raw
    translation = str(chinese or "").strip()
    if translation.lower() in {"", "nan", "none", "<na>", "nat"}:
        return raw
    if translation.lower() == raw.lower():
        return raw
    return f"{raw}({translation})"


def _clean_zh(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "nan", "none", "<na>", "nat"}:
        return ""
    return text


@functools.lru_cache(maxsize=4096)
def _chinese_names(continent: str, country: str, city: str, boundaries_dir_value: str) -> tuple[str, str]:
    if not boundaries_dir_value:
        return "", ""
    boundaries_dir = Path(boundaries_dir_value)
    if not (boundaries_dir / lib.ADMIN0_GPKG).exists():
        return "", ""

    task = lib.Task(continent=continent, country=country, city=city, download_dir="")
    country_zh = ""
    city_zh = ""
    admin0_row = lib.find_admin0_row(task, boundaries_dir)
    if admin0_row is not None:
        country_zh = _clean_zh(admin0_row.get("NAME_ZH"))

    if lib.task_key(city) == lib.task_key(country):
        city_zh = country_zh
        return country_zh, city_zh

    admin1_row = None
    if admin0_row is not None:
        admin_names = {
            str(admin0_row.get(field))
            for field in ("NAME", "ADMIN", "SOVEREIGNT", "NAME_LONG")
            if admin0_row.get(field)
        }
        admin1_row = lib.find_admin1_row(city, boundaries_dir, admin_names=admin_names)
    if admin1_row is None:
        admin1_row = lib.find_admin1_row(city, boundaries_dir)
    if admin1_row is not None:
        city_zh = _clean_zh(admin1_row.get("name_zh"))
    if not city_zh:
        city_task = lib.Task(continent=continent, country=city, city=city, download_dir="")
        city_row = lib.find_admin0_row(city_task, boundaries_dir)
        if city_row is not None:
            city_zh = _clean_zh(city_row.get("NAME_ZH"))
    return country_zh, city_zh


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GBA 下载任务状态看板（只读）。")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 0.0.0.0。")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8765。")
    parser.add_argument("--repo-root", default=None, help="仓库根目录，默认当前工作目录（启动看板时所在目录）。")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB, help="SQLite 状态库路径。")
    parser.add_argument("--process-db", default=DEFAULT_PROCESS_DB, help="SHP 处理队列状态库路径。")
    parser.add_argument(
        "--db-update-file",
        default=DEFAULT_DB_UPDATE_FILE,
        help="数据库表已更新看板的手动维护数据（JSON）。",
    )
    parser.add_argument("--page", default=DEFAULT_PAGE, help="状态页面 HTML 路径。")
    parser.add_argument("--boundaries-dir", default=DEFAULT_BOUNDARIES_DIR, help="本地行政边界目录，用于国家/城市中文名。")
    parser.add_argument("--queue-limit", type=int, default=DEFAULT_QUEUE_LIMIT, help="队列显示条数。")
    parser.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_LIMIT, help="最近完成显示条数。")
    parser.add_argument("--log-stale-minutes", type=float, default=DEFAULT_STALE_MINUTES, help="日志多久未更新视为可能卡住。")
    parser.add_argument("--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS, help="页面自动刷新间隔秒数。")
    parser.add_argument("--log-tail-lines", type=int, default=2, help="当前任务日志尾部行数。")
    parser.add_argument("--python-exe", default=sys.executable, help="重启批处理时使用的 Python 解释器。")
    parser.add_argument(
        "--process-python-exe",
        default=DEFAULT_PROCESS_PYTHON,
        help="启动数据处理队列时使用的 Python 解释器，默认 Tools 内的 .venv。",
    )
    parser.add_argument(
        "--action-token",
        default="",
        help="可选操作令牌；设置后页面重启操作必须携带匹配令牌（局域网防误操作）。",
    )
    parser.add_argument(
        "--merge-engine",
        default=process_queue.MERGE_ENGINE_OGR2OGR,
        help="交付分卷「自动合并」使用的引擎，目前支持 ogr2ogr。",
    )
    parser.add_argument(
        "--ogr2ogr",
        default="",
        help="ogr2ogr 可执行文件路径，默认自动探测（QGIS 自带）。",
    )
    return parser.parse_args()


def _open_state_db(state_db: Path, read_only: bool = True) -> sqlite3.Connection:
    resolved = Path(state_db).resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not Path(path).exists():
        return []
    lines = lib.read_log_text(Path(path)).splitlines()
    return [line.strip() for line in lines if line.strip()][-limit:]


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_file():
                    total += entry.stat().st_size
    except OSError:
        return 0
    return total


def _grid_total(task_dir: Path) -> int:
    progress = lib.read_national_grid_progress(task_dir)
    if progress is not None:
        return int(progress["expected"])
    grid_path = Path(task_dir) / "gba_wfs_grid.gpkg"
    if not grid_path.exists():
        return 0
    try:
        mtime = grid_path.stat().st_mtime
    except OSError:
        return 0
    key = (str(grid_path), mtime)
    cached = GRID_TOTAL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import pyogrio

        total = int(pyogrio.read_info(str(grid_path)).get("features") or 0)
    except Exception:
        total = 0
    GRID_TOTAL_CACHE[key] = total
    return total


def _detected_raw_data_dir(repo_root: Path, download_dir: str) -> str | None:
    if not download_dir:
        return None
    task_dir = Path(repo_root) / download_dir
    try:
        mtime = task_dir.stat().st_mtime
    except OSError:
        return None
    cached = RAW_DATA_CACHE.get(download_dir)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    detected = download_dir if lib.has_merged_raw_data(task_dir) else None
    RAW_DATA_CACHE[download_dir] = (mtime, detected)
    return detected


def _parse_seconds(started_at: str | None) -> int | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return max(int((datetime.now() - started).total_seconds()), 0)


def _age_seconds(timestamp: str | None) -> int | None:
    return _parse_seconds(timestamp)


def _count_tiles(task_dir: Path) -> int:
    progress = lib.read_national_grid_progress(task_dir)
    if progress is not None:
        return int(progress["done"])
    return sum(1 for path in Path(task_dir).glob("GBA_*.gpkg") if path.name.startswith("GBA_"))


def _running_entry(
    row: sqlite3.Row,
    repo_root: Path,
    stale_minutes: float,
    log_tail_lines: int,
    worker_command_lines: list[str] | None = None,
) -> dict:
    task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
    tiles_done = _count_tiles(task_dir)
    tiles_total = _grid_total(task_dir)
    progress = tiles_done / tiles_total if tiles_total > 0 else None
    elapsed = _parse_seconds(row["started_at"])
    eta = None
    if elapsed is not None and tiles_done > 0 and tiles_total > 0 and tiles_total > tiles_done:
        eta = int(elapsed / tiles_done * (tiles_total - tiles_done))

    log_path = task_dir / "run.log"
    last_log = _tail_lines(log_path, log_tail_lines)
    log_stale = False
    try:
        if log_path.exists():
            log_stale = (datetime.now().timestamp() - log_path.stat().st_mtime) > stale_minutes * 60
    except OSError:
        log_stale = False

    worker_active = False
    if worker_command_lines:
        task_dir_key = os.path.normcase(str(task_dir))
        worker_active = any(task_dir_key in os.path.normcase(line) for line in worker_command_lines if line)

    shp_parts = len(list(task_dir.glob("*_buildings_height_gba*.shp")))
    return {
        "task_id": row["task_id"],
        "continent": row["continent"],
        "country": row["country"],
        "city": row["city"],
        "attempts": row["attempts"],
        "started_at": row["started_at"],
        "elapsed_seconds": elapsed,
        "tiles_done": tiles_done,
        "tiles_total": tiles_total,
        "progress": progress,
        "eta_seconds": eta,
        "output_size_bytes": _dir_size_bytes(task_dir),
        "output_dir": str(task_dir),
        "shp_parts": shp_parts,
        "last_log": last_log,
        "log_stale": log_stale,
        "worker_active": worker_active,
    }


def build_snapshot(
    repo_root: Path,
    state_db: Path,
    queue_limit: int = DEFAULT_QUEUE_LIMIT,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    stale_minutes: float = DEFAULT_STALE_MINUTES,
    log_tail_lines: int = 2,
    boundaries_dir: Path | None = None,
    queue_offset: int = 0,
    worker_command_lines: list[str] | None = None,
    queue_search: str = "",
) -> dict:
    repo_root = Path(repo_root)
    state_db = Path(state_db)
    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root": str(repo_root),
        "state_db": str(state_db),
        "error": None,
        "counts": {},
        "total": 0,
        "batch_state": "unknown",
        "running": [],
        "queue": [],
        "queue_total": 0,
        "queue_total_all": 0,
        "queue_search": str(queue_search or "").strip()[:100],
        "queue_offset": max(int(queue_offset), 0),
        "queue_limit": max(int(queue_limit), 1),
        "recent_done": [],
        "failed": [],
        "disk_free_bytes": None,
    }
    try:
        snapshot["disk_free_bytes"] = shutil.disk_usage(repo_root).free
    except OSError:
        pass

    if not state_db.exists():
        snapshot["error"] = f"状态库不存在: {state_db}"
        return snapshot

    try:
        connection = _open_state_db(state_db)
    except sqlite3.Error as exc:
        snapshot["error"] = f"状态库无法打开: {exc}"
        return snapshot

    try:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute("SELECT status, COUNT(*) AS total FROM tasks GROUP BY status")
        }
        snapshot["counts"] = counts
        snapshot["total"] = sum(counts.values())
        pending = counts.get(lib.STATUS_PENDING, 0)
        running = counts.get(lib.STATUS_RUNNING, 0)
        if running > 0:
            snapshot["batch_state"] = "running"
        elif pending > 0:
            snapshot["batch_state"] = "stopped"
        else:
            snapshot["batch_state"] = "done"

        running_rows = connection.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY started_at", (lib.STATUS_RUNNING,)
        ).fetchall()
        snapshot["running"] = [
            _running_entry(row, repo_root, stale_minutes, log_tail_lines, worker_command_lines)
            for row in running_rows
        ]

        boundaries_value = str(boundaries_dir) if boundaries_dir else ""
        try:
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        except sqlite3.Error:
            task_columns = set()
        if "queue_order" in task_columns:
            order_clause = "ORDER BY queue_order IS NULL, queue_order, manifest_order, task_id"
        else:
            order_clause = "ORDER BY manifest_order, task_id"

        pending_rows = connection.execute(
            f"SELECT * FROM tasks WHERE status = ? {order_clause}",
            (lib.STATUS_PENDING,),
        ).fetchall()
        snapshot["queue_total_all"] = len(pending_rows)

        search_text = str(snapshot["queue_search"] or "")
        needle = search_text.casefold()
        decorated_rows: list[dict] = []
        for global_order, row in enumerate(pending_rows, start=1):
            country_zh, city_zh = _chinese_names(
                row["continent"],
                row["country"],
                row["city"],
                boundaries_value,
            )
            if needle:
                haystack = f"{row['city']} {city_zh}".casefold()
                if needle not in haystack:
                    continue
            item = dict(row)
            item["_country_zh"] = country_zh
            item["_city_zh"] = city_zh
            item["_global_order"] = global_order
            decorated_rows.append(item)

        snapshot["queue_total"] = len(decorated_rows)
        page_start = snapshot["queue_offset"]
        page_end = page_start + snapshot["queue_limit"]
        queue_items = []
        for index, row in enumerate(decorated_rows[page_start:page_end], start=1):
            queue_items.append(
                {
                    "order": page_start + index,
                    "global_order": row["_global_order"],
                    "task_id": row["task_id"],
                    "continent": row["continent"],
                    "continent_display": format_continent(row["continent"]),
                    "country": row["country"],
                    "country_display": format_bilingual(row["country"], row["_country_zh"]),
                    "city": row["city"],
                    "city_display": format_bilingual(row["city"], row["_city_zh"]),
                }
            )
        snapshot["queue"] = queue_items

        recent_items = []
        for row in connection.execute(
            "SELECT * FROM tasks WHERE status IN (?, ?) ORDER BY finished_at DESC LIMIT ?",
            (lib.STATUS_OK, lib.STATUS_OK_EMPTY, max(recent_limit, 0)),
        ):
            row_keys = row.keys() if hasattr(row, "keys") else []
            existing_data_dir = row["existing_data_dir"] if "existing_data_dir" in row_keys else None
            if not existing_data_dir:
                existing_data_dir = _detected_raw_data_dir(repo_root, row["download_dir"])
            recent_items.append(
                {
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "feature_count": row["feature_count"],
                    "duration_seconds": row["duration_seconds"],
                    "finished_at": row["finished_at"],
                    "download_dir": row["download_dir"],
                    "existing_data_dir": existing_data_dir,
                    "redownload_count": row["redownload_count"] if "redownload_count" in row_keys else 0,
                }
            )
        snapshot["recent_done"] = recent_items

        snapshot["failed"] = [
            {
                "task_id": row["task_id"],
                "attempts": row["attempts"],
                "last_error": (row["last_error"] or "")[:300],
                "finished_at": row["finished_at"],
            }
            for row in connection.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (lib.STATUS_FAILED, max(recent_limit, 0)),
            )
        ]
    except sqlite3.Error as exc:
        snapshot["error"] = f"状态库读取失败: {exc}"
    finally:
        connection.close()
    return snapshot


STAGE_LABELS = {
    "PREPARE": "准备输入",
    "SPLIT": "切分",
    "VALIDATE": "QGIS 校验",
    "CLEAN": "清洗",
    "REPROJECT": "重投影 3857",
    "VERIFY": "产物校验",
}
PROCESS_QUEUE_LIMIT = 500
PROCESS_NOTE_LIMIT = 200


def _count_json_list(value: str | None) -> int:
    try:
        payload = json.loads(value or "[]")
    except ValueError:
        return 0
    return len(payload) if isinstance(payload, list) else 0


def _row_optional(row, key: str):
    keys = row.keys() if hasattr(row, "keys") else []
    return row[key] if key in keys else None


def _processing_running_entry(
    row: dict,
    repo_root: Path,
    stale_minutes: float,
    log_tail_lines: int,
) -> dict:
    tiles_done = int(row.get("tiles_done") or 0)
    tiles_total = row.get("tiles_total")
    progress = None
    eta = None
    if tiles_total:
        progress = min(tiles_done / int(tiles_total), 1.0)
    elapsed = _parse_seconds(row.get("started_at"))
    if progress and tiles_total and tiles_done > 0 and int(tiles_total) > tiles_done and elapsed is not None:
        eta = int(elapsed / tiles_done * (int(tiles_total) - tiles_done))

    output_dir = None
    output_size = 0
    if row.get("output_dir"):
        try:
            output_dir = lib.resolve_task_dir(repo_root, row["output_dir"])
            output_size = _dir_size_bytes(Path(output_dir) / "final")
        except ValueError:
            output_dir = None

    updated_age = _age_seconds(row.get("updated_at"))
    log_stale = updated_age is not None and updated_age > stale_minutes * 60
    last_log = [line for line in str(row.get("last_log") or "").splitlines() if line.strip()][-log_tail_lines:]

    return {
        "dataset_key": row["dataset_key"],
        "task_id": row.get("task_id"),
        "display_name": row.get("display_name") or row["dataset_key"],
        "process_name_prefix": row.get("process_name_prefix"),
        "source_dir": row.get("source_dir"),
        "feature_count": row.get("feature_count"),
        "attempts": row.get("attempts"),
        "stage": row.get("stage"),
        "stage_label": STAGE_LABELS.get(row.get("stage") or "", row.get("stage") or "-"),
        "stage_detail": row.get("stage_detail"),
        "tiles_done": tiles_done,
        "tiles_total": tiles_total,
        "progress": progress,
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        "output_dir": str(output_dir) if output_dir else None,
        "output_size_bytes": output_size,
        "started_at": row.get("started_at"),
        "last_log": last_log,
        "log_stale": log_stale,
    }


MERGE_EVENT_STATES = {
    "PROCESS_MERGE_REJECT": "error",
    "PROCESS_MERGE_FAIL": "error",
    "PROCESS_MERGE_DONE": "done",
}


def _last_merge_event(connection, dataset_key: str) -> dict | None:
    """取该任务最近一条合并事件（持久在 process_events）。"""
    try:
        row = connection.execute(
            "SELECT event, detail FROM process_events "
            "WHERE dataset_key = ? "
            "AND event IN ('PROCESS_MERGE_REJECT', 'PROCESS_MERGE_FAIL', 'PROCESS_MERGE_DONE') "
            "ORDER BY created_at DESC LIMIT 1",
            (dataset_key,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "state": MERGE_EVENT_STATES.get(row["event"], "done"),
        "detail": row["detail"] or "",
    }


def _merge_log_relpath(row) -> str:
    prefix = _row_optional(row, "process_name_prefix")
    source_dir = row["source_dir"]
    if prefix and source_dir:
        return f"{source_dir}/merge_{prefix}.log"
    return ""


def build_processing_snapshot(
    repo_root: Path,
    process_db: Path,
    stale_minutes: float = DEFAULT_STALE_MINUTES,
    log_tail_lines: int = 3,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    recent_offset: int = 0,
) -> dict:
    repo_root = Path(repo_root)
    process_db = Path(process_db)
    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(process_db),
        "error": None,
        "counts": {},
        "total": 0,
        "batch_state": "unknown",
        "running": [],
        "queue": [],
        "queue_total": 0,
        "recent_done": [],
        "recent_total": 0,
        "recent_limit": max(recent_limit, 1),
        "recent_offset": max(recent_offset, 0),
        "failed": [],
        "blocked": [],
    }
    if not process_db.exists():
        snapshot["error"] = (
            f"处理状态库不存在: {process_db}；先运行 python scripts/run_shp_process_tasks.py --sync"
        )
        return snapshot
    try:
        connection = _open_state_db(process_db)
    except sqlite3.Error as exc:
        snapshot["error"] = f"处理状态库无法打开: {exc}"
        return snapshot
    try:
        counts = {
            row["status"]: row["total"]
            for row in connection.execute("SELECT status, COUNT(*) AS total FROM process_tasks GROUP BY status")
        }
        snapshot["counts"] = counts
        snapshot["total"] = sum(counts.values())
        snapshot["recent_total"] = counts.get("OK", 0)
        if counts.get("RUNNING", 0) > 0:
            snapshot["batch_state"] = "running"
        elif counts.get("PENDING", 0) > 0:
            snapshot["batch_state"] = "stopped"
        elif snapshot["total"] > 0:
            snapshot["batch_state"] = "done"

        running_rows = connection.execute(
            "SELECT * FROM process_tasks WHERE status = ? ORDER BY started_at", ("RUNNING",)
        ).fetchall()
        snapshot["running"] = [
            _processing_running_entry(dict(row), repo_root, stale_minutes, log_tail_lines) for row in running_rows
        ]

        try:
            queue_rows = connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? "
                "ORDER BY queue_order IS NULL, queue_order, dataset_key LIMIT ?",
                ("PENDING", PROCESS_QUEUE_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError:
            queue_rows = connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? ORDER BY updated_at, dataset_key LIMIT ?",
                ("PENDING", PROCESS_QUEUE_LIMIT),
            ).fetchall()
        snapshot["queue_total"] = counts.get("PENDING", 0)
        queue_items = []
        for index, row in enumerate(queue_rows, start=1):
            shp_count = _count_json_list(row["shp_files"])
            merge_state = None
            merge_error = ""
            if shp_count >= 2:
                job = MERGE_JOBS.get(row["dataset_key"])
                if job is not None:
                    merge_state = job.get("state")
                    merge_error = job.get("error") or job.get("message") or ""
                else:
                    last = _last_merge_event(connection, row["dataset_key"])
                    if last is not None and last["state"] == "error":
                        merge_state = "error"
                        merge_error = last["detail"]
            queue_items.append(
                {
                    "order": index,
                    "queue_order": _row_optional(row, "queue_order"),
                    "dataset_key": row["dataset_key"],
                    "task_id": row["task_id"],
                    "display_name": row["display_name"] or row["dataset_key"],
                    "process_name_prefix": _row_optional(row, "process_name_prefix"),
                    "source_dir": row["source_dir"],
                    "feature_count": row["feature_count"],
                    "shp_count": shp_count,
                    "note": _row_optional(row, "note") or "",
                    "updated_at": row["updated_at"],
                    "merge_state": merge_state,
                    "merge_error": merge_error,
                    "merge_log": _merge_log_relpath(row) if shp_count >= 2 else "",
                }
            )
        snapshot["queue"] = queue_items

        snapshot["recent_done"] = [
            {
                "dataset_key": row["dataset_key"],
                "task_id": row["task_id"],
                "source_dir": row["source_dir"],
                "process_name_prefix": _row_optional(row, "process_name_prefix"),
                "tile_count": row["tile_count"],
                "feature_count": row["feature_count"],
                "duration_seconds": row["duration_seconds"],
                "finished_at": row["finished_at"],
                "output_dir": row["output_dir"],
                "input_dir": row["input_dir"] or (
                    Path(row["output_dir"]).with_name(Path(row["output_dir"]).name + "_input").as_posix()
                    if row["output_dir"] else None
                ),
            }
            for row in connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? "
                "ORDER BY finished_at DESC, dataset_key DESC LIMIT ? OFFSET ?",
                ("OK", snapshot["recent_limit"], snapshot["recent_offset"]),
            )
        ]

        snapshot["failed"] = [
            {
                "dataset_key": row["dataset_key"],
                "attempts": row["attempts"],
                "last_error": (row["last_error"] or "")[:300],
                "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            }
            for row in connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                ("FAILED", max(recent_limit, 0)),
            )
        ]

        snapshot["blocked"] = [
            {
                "dataset_key": row["dataset_key"],
                "source_dir": row["source_dir"],
                "reason": row["last_error"],
            }
            for row in connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? ORDER BY dataset_key LIMIT ?",
                ("BLOCKED_NO_SHP", PROCESS_QUEUE_LIMIT),
            )
        ]
    except sqlite3.Error as exc:
        snapshot["error"] = f"处理状态库读取失败: {exc}"
    finally:
        connection.close()
    return snapshot


DB_UPDATE_NOTE_LIMIT = 200
DB_UPDATE_STAMP_LIMIT = 40


def load_db_update_marks(path: Path) -> dict[str, dict]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    marks: dict[str, dict] = {}
    for key, value in rows.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        marks[key] = {
            "updated": bool(value.get("updated")),
            "updated_at": str(value.get("updated_at") or "")[:DB_UPDATE_STAMP_LIMIT],
            "note": str(value.get("note") or "")[:DB_UPDATE_NOTE_LIMIT],
        }
    return marks


def save_db_update_marks(path: Path, rows: list[dict]) -> int:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    marks = load_db_update_marks(file_path)
    saved = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("rows 的元素必须是对象")
        key = str(row.get("dataset_key") or "").strip()
        if not key:
            raise ValueError("dataset_key 不能为空")
        marks[key] = {
            "updated": bool(row.get("updated")),
            "updated_at": str(row.get("updated_at") or "")[:DB_UPDATE_STAMP_LIMIT],
            "note": str(row.get("note") or "")[:DB_UPDATE_NOTE_LIMIT],
        }
        saved += 1
    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": marks,
    }
    temporary = file_path.with_name(f".{file_path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(file_path)
    return saved


def reset_db_update_mark(path: Path, dataset_key: str) -> bool:
    """把某数据集的手工标记重置为“未更新”；标记不存在时不做改动。"""
    marks = load_db_update_marks(path)
    if dataset_key not in marks:
        return False
    save_db_update_marks(
        path,
        [{"dataset_key": dataset_key, "updated": False, "updated_at": "", "note": ""}],
    )
    return True


def delete_db_update_mark(path: Path, dataset_key: str) -> bool:
    """从标记文件中彻底移除某数据集的手工标记；不存在时不做改动。"""
    file_path = Path(path)
    marks = load_db_update_marks(file_path)
    if dataset_key not in marks:
        return False
    del marks[dataset_key]
    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": marks,
    }
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_name(f".{file_path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(file_path)
    return True


def build_merge_status(dataset_key: str) -> dict:
    with MERGE_LOCK:
        job = MERGE_JOBS.get(dataset_key)
        if job is None:
            return {"ok": True, "dataset_key": dataset_key, "state": "idle"}
        snapshot = dict(job)
    snapshot["ok"] = snapshot.get("state") != "error"
    snapshot["dataset_key"] = dataset_key
    return snapshot


def start_merge_job(config: dict, dataset_key: str, expected_source_dir: str) -> dict:
    """校验后启动后台合并任务，立即返回 running 状态。"""
    store = process_queue.ProcessStore(Path(config["process_db"]))
    try:
        row = store.get(dataset_key)
        if row is None:
            raise KeyError(f"未找到处理任务: {dataset_key}")
        if row["status"] == process_queue.PROCESS_RUNNING:
            raise ValueError("任务正在处理中，不能合并分卷")
        if expected_source_dir and row["source_dir"] != expected_source_dir:
            raise ValueError("来源目录已变化，请刷新页面后重试")
        shp_count = len(json.loads(row["shp_files"] or "[]"))
        if shp_count < 2:
            raise ValueError("该任务只有一个 SHP，无需合并")
        source_dir_rel = row["source_dir"]
        merge_prefix = row.get("process_name_prefix") or process_queue.dataset_slug(dataset_key)
    finally:
        store.close()

    repo_root = Path(config["repo_root"])
    merge_log_path = lib.resolve_task_dir(repo_root, source_dir_rel) / f"merge_{merge_prefix}.log"

    def append_merge_log(message: str) -> None:
        try:
            merge_log_path.parent.mkdir(parents=True, exist_ok=True)
            with merge_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().isoformat(timespec='seconds')} - {message}\n")
        except OSError:
            pass

    append_merge_log(f"===== 开始合并（{shp_count} 个分卷） =====")

    with MERGE_LOCK:
        existing = MERGE_JOBS.get(dataset_key)
        if existing is not None and existing.get("state") == "running":
            raise ValueError("该任务已有合并任务进行中")
        MERGE_JOBS[dataset_key] = {
            "state": "running",
            "done": 0,
            "total": shp_count,
            "message": "开始合并…",
            "error": None,
            "result": None,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    engine = config.get("merge_engine") or process_queue.MERGE_ENGINE_OGR2OGR
    ogr2ogr_exe = config.get("ogr2ogr") or None
    if not ogr2ogr_exe:
        qgis_bat = load_dashboard_config(repo_root).get("qgis_process_bat_effective") or ""
        if qgis_bat:
            candidate = Path(qgis_bat).parent / "ogr2ogr.exe"
            if candidate.is_file():
                ogr2ogr_exe = str(candidate)

    def progress(done: int, total: int) -> None:
        with MERGE_LOCK:
            job = MERGE_JOBS.get(dataset_key)
            if job is not None:
                job.update(done=done, total=total, message=f"已合并 {done}/{total} 个分卷",
                           updated_at=datetime.now().isoformat(timespec="seconds"))

    def worker() -> None:
        try:
            result = process_queue.merge_task_shp_parts(
                Path(config["repo_root"]),
                Path(config["process_db"]),
                dataset_key,
                engine=engine,
                ogr2ogr_exe=ogr2ogr_exe,
                download_db=Path(config["state_db"]),
                progress_cb=progress,
                log_cb=append_merge_log,
            )
            with MERGE_LOCK:
                MERGE_JOBS[dataset_key].update(
                    state="done", result=result, message="合并完成，已同步处理队列", error=None,
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                )
        except Exception as exc:  # noqa: BLE001
            append_merge_log(f"合并失败：{exc}")
            with MERGE_LOCK:
                MERGE_JOBS[dataset_key].update(
                    state="error", error=str(exc), message=f"合并失败：{exc}",
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                )

    threading.Thread(target=worker, name=f"merge-{dataset_key}", daemon=True).start()
    return {"ok": True, "dataset_key": dataset_key, "state": "running"}


def _process_task_row(config: dict, dataset_key: str) -> dict | None:
    store = process_queue.ProcessStore(Path(config["process_db"]))
    try:
        return store.get(dataset_key)
    finally:
        store.close()


def check_db_update_consistency(config: dict, dataset_key: str) -> dict:
    """检查共享目录中的 SHP 是否与处理产物 final/*_3857.shp 一致。"""
    repo_root = Path(config["repo_root"])
    row = _process_task_row(config, dataset_key)
    if row is None:
        raise KeyError(f"未找到处理任务: {dataset_key}")
    parts = [part.strip() for part in dataset_key.split("|")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("dataset_key 需为 洲|国家|区域 三段")
    continent, country, region = parts
    result_root = str(load_dashboard_config(repo_root).get("result_root") or "").strip()
    if not result_root:
        raise ValueError("未配置区域成果 SHP 共享目录")
    output_dir = row.get("output_dir")
    if not output_dir:
        raise ValueError("处理产物目录未记录")
    expected_dir = (repo_root / output_dir).resolve() / "final"
    base = {
        "continent": continent,
        "country": country,
        "region": region,
        "expected": [],
        "actual": [],
        "missing": [],
        "extra": [],
    }
    if not expected_dir.is_dir():
        return {**base, "consistent": False, "message": f"处理产物缺失：{expected_dir}"}
    try:
        expected = {path.stem for path in expected_dir.glob("*_3857.shp")}
    except OSError as exc:
        return {**base, "consistent": False, "message": f"处理产物目录不可读：{exc}"}
    base["expected"] = sorted(expected)
    share_dir = Path(result_root) / continent / country / region
    base["share_dir"] = str(share_dir)
    if not share_dir.is_dir():
        return {**base, "consistent": False, "missing": sorted(expected),
                "message": f"共享目录不存在：{share_dir}"}
    try:
        actual = {path.stem for path in share_dir.glob("*.shp")}
    except OSError as exc:
        return {**base, "consistent": False, "missing": sorted(expected),
                "message": f"共享目录不可读：{exc}"}
    base["actual"] = sorted(actual)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    base["missing"] = missing
    base["extra"] = extra
    consistent = bool(expected) and not missing and not extra
    return {**base, "consistent": consistent,
            "message": "一致" if consistent else "内容不一致，请手动检查共享目录"}


def parse_import_summary(text: str) -> dict | None:
    if not text:
        return None
    marker = "__IMPORT_SUMMARY__"
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(marker):
            try:
                data = json.loads(stripped[len(marker):].strip())
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    match = re.search(r"删除=(\d+).*?新增=(\d+).*?跳过=(\d+)", text)
    if match:
        return {"deleted": int(match.group(1)), "inserted": int(match.group(2)), "skipped": int(match.group(3))}
    return None


def _load_db_import_records(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_db_import_record(path: Path, dataset_key: str, record: dict) -> None:
    path = Path(path)
    records = _load_db_import_records(path)
    records[dataset_key] = record
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def build_db_import_status(repo_root: Path, dataset_key: str) -> dict:
    with DB_IMPORT_LOCK:
        job = DB_IMPORT_JOBS.get(dataset_key)
        snapshot = dict(job) if job is not None else None
    if snapshot is None:
        records = _load_db_import_records(Path(repo_root) / DEFAULT_DB_IMPORT_STATUS)
        record = records.get(dataset_key)
        if record is None:
            return {"ok": True, "dataset_key": dataset_key, "state": "idle"}
        snapshot = dict(record)
    snapshot["ok"] = snapshot.get("state") != "error"
    snapshot["dataset_key"] = dataset_key
    return snapshot


def check_publish_ready(config: dict, dataset_key: str) -> dict:
    """更新到库前置：共享目录该区域有 SHP，且内容齐全。

    齐全的判定：
    - 处理产物 final 仍在 → 共享目录需包含 final 的全部文件（名称一致、大小一致，适用于手动拷贝）；
    - final 已清空（自动拷贝完成）→ 共享目录非空即可。
    拷贝中断（共享目录缺文件/大小不一致）会被拦截。
    """
    repo_root = Path(config["repo_root"])
    parts = [part.strip() for part in dataset_key.split("|")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("dataset_key 需为 洲|国家|区域 三段")
    continent, country, region = parts
    row = _process_task_row(config, dataset_key)
    if row is None:
        raise KeyError(f"未找到处理任务: {dataset_key}")
    result_root = str(load_dashboard_config(repo_root).get("result_root") or "").strip()
    if not result_root:
        raise ValueError("未配置区域成果 SHP 共享目录")
    share_dir = Path(result_root) / continent / country / region
    base = {"continent": continent, "country": country, "region": region, "share_dir": str(share_dir)}
    if not share_dir.is_dir():
        return {**base, "ready": False, "message": f"共享目录不存在：{share_dir}；请先「拷贝到共享目录」或手动拷贝"}
    try:
        share_shps = list(share_dir.glob("*.shp"))
    except OSError as exc:
        return {**base, "ready": False, "message": f"共享目录不可读：{exc}"}
    if not share_shps:
        return {**base, "ready": False, "message": f"共享目录无 SHP：{share_dir}；请先「拷贝到共享目录」或手动拷贝"}
    output_dir = row.get("output_dir")
    if output_dir:
        final_dir = (repo_root / output_dir).resolve() / "final"
        if final_dir.is_dir():
            try:
                sources: list[Path] = []
                for shp in sorted(final_dir.glob("*_3857.shp")):
                    sources.append(shp)
                    for suffix in process_queue.SIDECAR_SUFFIXES:
                        side = shp.with_suffix(suffix)
                        if side.is_file():
                            sources.append(side)
            except OSError:
                sources = []
            missing = []
            for src in sources:
                dest = share_dir / src.name
                try:
                    if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
                        missing.append(src.name)
                except OSError:
                    missing.append(src.name)
            if missing:
                return {**base, "ready": False,
                        "message": f"共享目录尚未齐全（缺 {len(missing)} 个 / 大小不一致）：请「拷贝到共享目录」或手动拷贝后再更新"}
    return {**base, "ready": True, "message": "已发布，可入库"}


def _load_db_copy_records(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("rows") if isinstance(raw, dict) else None
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_db_copy_record(path: Path, dataset_key: str, record: dict) -> None:
    path = Path(path)
    with DB_COPY_STATUS_LOCK:
        records = _load_db_copy_records(path)
        records[dataset_key] = record
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": records,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass


def build_copy_status(dataset_key: str) -> dict:
    with DB_COPY_LOCK:
        job = DB_COPY_JOBS.get(dataset_key)
        snapshot = dict(job) if job is not None else None
    if snapshot is None:
        return {"ok": True, "dataset_key": dataset_key, "state": "idle"}
    snapshot["ok"] = snapshot.get("state") != "error"
    snapshot["dataset_key"] = dataset_key
    return snapshot


def _append_update_log(repo_root: Path, dataset_key: str, message: str) -> None:
    """把拷贝过程追加到该数据集的库表更新日志（与入库共用同一份日志）。"""
    rel_path = _update_log_relpath(dataset_key)
    if not rel_path:
        return
    path = Path(repo_root) / rel_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
    except OSError:
        pass


def copy_final_to_share(config: dict, dataset_key: str) -> dict:
    """清空共享目录该区域文件后，把处理产物 final 拷入，校验齐全后删除 final 内容。"""
    repo_root = Path(config["repo_root"])
    parts = [part.strip() for part in dataset_key.split("|")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("dataset_key 需为 洲|国家|区域 三段")
    continent, country, region = parts
    row = _process_task_row(config, dataset_key)
    if row is None:
        raise KeyError(f"未找到处理任务: {dataset_key}")
    if row.get("status") != process_queue.PROCESS_OK:
        raise ValueError("任务尚未处理完成，无法拷贝")
    output_dir = row.get("output_dir")
    if not output_dir:
        raise ValueError("处理产物目录未记录")
    result_root = str(load_dashboard_config(repo_root).get("result_root") or "").strip()
    if not result_root:
        raise ValueError("未配置区域成果 SHP 共享目录")

    final_dir = (repo_root / output_dir).resolve() / "final"
    if not final_dir.is_dir():
        raise ValueError(f"处理产物目录不存在：{final_dir}")
    targets = sorted(final_dir.glob("*_3857.shp"))
    if not targets:
        raise ValueError("处理产物为空（final 已无 *_3857.shp）")

    files: list[Path] = []
    for shp in targets:
        files.append(shp)
        for suffix in process_queue.SIDECAR_SUFFIXES:
            side = shp.with_suffix(suffix)
            if side.is_file():
                files.append(side)

    share_dir = Path(result_root) / continent / country / region
    copy_status_file = repo_root / DEFAULT_DB_COPY_STATUS
    copy_log_rel = _update_log_relpath(dataset_key)
    started_at = datetime.now()
    total = len(files)

    def save_copy_record(state: str, copied_count: int, finished: str = "", error: str | None = None) -> None:
        _save_db_copy_record(copy_status_file, dataset_key, {
            "state": state,
            "copied": copied_count,
            "total": total,
            "source_dir": str(final_dir),
            "share_dir": str(share_dir),
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished,
            "error": error,
            "log_path": copy_log_rel,
        })

    with DB_COPY_LOCK:
        existing = DB_COPY_JOBS.get(dataset_key)
        if existing is not None and existing.get("state") == "running":
            raise ValueError("该区域已有拷贝任务进行中")
        DB_COPY_JOBS[dataset_key] = {
            "state": "running",
            "copied": 0,
            "total": total,
            "message": "开始拷贝…",
            "error": None,
            "source_dir": str(final_dir),
            "share_dir": str(share_dir),
            "log_path": copy_log_rel,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    # 在启动后台线程前同步创建日志和状态记录，保证点击后立即可见
    _append_update_log(repo_root, dataset_key, "===== 开始拷贝到共享目录 =====")
    _append_update_log(repo_root, dataset_key, f"开始时间: {started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    _append_update_log(repo_root, dataset_key, f"源目录: {final_dir}")
    _append_update_log(repo_root, dataset_key, f"目标目录: {share_dir}")
    _append_update_log(repo_root, dataset_key, f"文件数: {total}")
    save_copy_record("copying", 0)

    def set_job(**fields) -> None:
        with DB_COPY_LOCK:
            job = DB_COPY_JOBS.get(dataset_key)
            if job is not None:
                job.update(updated_at=datetime.now().isoformat(timespec="seconds"), **fields)

    def worker() -> None:
        copied = 0
        try:
            share_dir.mkdir(parents=True, exist_ok=True)
            for entry in share_dir.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError as exc:
                        _append_update_log(repo_root, dataset_key, f"清理旧文件失败: {entry.name}（{exc}）")
            log_step = max(1, total // 10)
            for src in files:
                shutil.copy2(src, share_dir / src.name)
                copied += 1
                set_job(copied=copied, message=f"已拷贝 {copied}/{total}")
                if copied == total or copied % log_step == 0:
                    _append_update_log(repo_root, dataset_key, f"已拷贝 {copied}/{total}")
                    save_copy_record("copying", copied)
            missing = [
                src.name for src in files
                if not (share_dir / src.name).is_file()
                or (share_dir / src.name).stat().st_size != src.stat().st_size
            ]
            if missing:
                raise ValueError(f"拷贝校验失败（缺失/大小不一致 {len(missing)} 个）：" + "、".join(missing[:5]))
            _append_update_log(repo_root, dataset_key, f"校验通过: {copied} 个文件")
            final_failed = 0
            for entry in final_dir.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError as exc:
                        final_failed += 1
                        _append_update_log(repo_root, dataset_key, f"清理 final 失败: {entry.name}（{exc}）")
            finished_at = datetime.now()
            elapsed = int((finished_at - started_at).total_seconds())
            if final_failed:
                _append_update_log(repo_root, dataset_key, f"final 目录仍有 {final_failed} 个文件未能删除")
            else:
                _append_update_log(repo_root, dataset_key, "已清空 final 目录")
            _append_update_log(repo_root, dataset_key, f"结束时间: {finished_at.strftime('%Y-%m-%d %H:%M:%S')}")
            _append_update_log(repo_root, dataset_key, f"===== 拷贝完成（耗时 {elapsed} 秒）=====")
            save_copy_record("done", copied, finished_at.isoformat(timespec="seconds"))
            set_job(
                state="done", error=None,
                message=f"已拷贝 {copied} 个文件并清空 final",
                finished_at=finished_at.isoformat(timespec="seconds"),
            )
        except Exception as exc:  # noqa: BLE001
            finished_at = datetime.now()
            elapsed = int((finished_at - started_at).total_seconds())
            _append_update_log(repo_root, dataset_key, f"拷贝失败: {exc}")
            _append_update_log(repo_root, dataset_key, f"结束时间: {finished_at.strftime('%Y-%m-%d %H:%M:%S')}")
            _append_update_log(repo_root, dataset_key, f"===== 拷贝结束（失败，耗时 {elapsed} 秒）=====")
            save_copy_record("error", copied, finished_at.isoformat(timespec="seconds"), error=str(exc))
            set_job(
                state="error", error=str(exc), message=f"拷贝失败：{exc}",
                finished_at=finished_at.isoformat(timespec="seconds"),
            )

    threading.Thread(target=worker, name=f"copy-{dataset_key}", daemon=True).start()
    return {"ok": True, "state": "running", "total": len(files), "share_dir": str(share_dir)}


def start_db_import_job(config: dict, dataset_key: str) -> dict:
    repo_root = Path(config["repo_root"])
    precheck = check_publish_ready(config, dataset_key)
    if not precheck.get("ready"):
        return {"ok": False, "state": "blocked", **precheck}

    python_exe = repo_root / DEFAULT_UPDATE_PYTHON
    script = repo_root / DEFAULT_UPDATE_SCRIPT
    if not python_exe.is_file():
        raise ValueError(f"未找到入库解释器：{python_exe}")
    if not script.is_file():
        raise ValueError(f"未找到入库脚本：{script}")

    cfg = load_dashboard_config(repo_root)
    db = cfg["database"]
    result_root = cfg["result_root"]
    log_dir = repo_root / DEFAULT_UPDATE_LOG_DIR
    continent = precheck["continent"]
    country = precheck["country"]
    region = precheck["region"]

    with DB_IMPORT_LOCK:
        existing = DB_IMPORT_JOBS.get(dataset_key)
        if existing is not None and existing.get("state") == "running":
            raise ValueError("该区域已有入库任务进行中")
        DB_IMPORT_JOBS[dataset_key] = {
            "state": "running",
            "message": "开始入库…",
            "error": None,
            "log_tail": "",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    command = [
        str(python_exe),
        str(script),
        "--continent", continent,
        "--country", country,
        "--region", region,
        "--root", result_root,
        "--log-dir", str(log_dir),
    ]
    env = os.environ.copy()
    env.update({
        "PG_DBNAME": str(db.get("dbname") or ""),
        "PG_USER": str(db.get("user") or ""),
        "PG_PASSWORD": str(db.get("password") or ""),
        "PG_HOST": str(db.get("host") or ""),
        "PG_PORT": str(db.get("port") or ""),
        "DATA_ROOT": result_root,
        "PYTHONIOENCODING": "utf-8",
    })

    status_file = repo_root / DEFAULT_DB_IMPORT_STATUS

    def finish(state: str, message: str, error: str | None, tail: str, summary: dict | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with DB_IMPORT_LOCK:
            DB_IMPORT_JOBS[dataset_key].update(
                state=state, message=message, error=error, log_tail=tail, updated_at=now,
            )
        record = {"state": state, "message": message, "error": error, "log_tail": tail, "updated_at": now}
        if summary:
            for field in ("deleted", "inserted", "skipped"):
                if summary.get(field) is not None:
                    record[field] = summary[field]
        _save_db_import_record(status_file, dataset_key, record)
        try:
            event_store = process_queue.ProcessStore(Path(config["process_db"]))
            try:
                event_store.add_event(
                    dataset_key,
                    "PROCESS_DB_IMPORT_DONE" if state == "done" else "PROCESS_DB_IMPORT_FAIL",
                    (message or "")[:500],
                )
            finally:
                event_store.close()
        except Exception:  # noqa: BLE001
            pass

    def worker() -> None:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env)
            combined = (proc.stdout or "") + (proc.stderr or "")
            tail = combined[-2000:]
            summary = parse_import_summary(combined)
            if proc.returncode == 0:
                marks = load_db_update_marks(Path(config["db_update_file"]))
                note = (marks.get(dataset_key) or {}).get("note") or ""
                save_db_update_marks(
                    Path(config["db_update_file"]),
                    [{"dataset_key": dataset_key, "updated": True,
                      "updated_at": datetime.now().isoformat(timespec="seconds"), "note": note}],
                )
                finish("done", "入库完成，已登记为已更新", None, tail, summary)
            else:
                finish("error", f"入库失败（退出码 {proc.returncode}）", tail, tail, summary)
        except Exception as exc:  # noqa: BLE001
            finish("error", f"入库异常：{exc}", str(exc), "")

    threading.Thread(target=worker, name=f"db-import-{dataset_key}", daemon=True).start()
    return {"ok": True, "state": "running"}


def _update_log_relpath(dataset_key: str) -> str:
    """由 dataset_key 推出入库日志相对路径（与 main3_region 命名规则一致）。"""
    parts = str(dataset_key).split("|")
    if len(parts) != 3:
        return ""

    def sanitize(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("_") or "region"

    return f"data/logs/update/log_main3_{sanitize(parts[1])}_{sanitize(parts[2])}.log"


def load_available_dataset_keys(
    process_db: Path | None,
    marks_file: Path | None = None,
) -> set[str]:
    """已完成处理或已登记更新到库的数据集，视为建筑数据已获取。"""
    keys: set[str] = set()
    if process_db is not None:
        process_path = Path(process_db)
        if process_path.exists():
            try:
                connection = _open_state_db(process_path)
            except sqlite3.Error:
                connection = None
            if connection is not None:
                try:
                    rows = connection.execute(
                        "SELECT dataset_key FROM process_tasks WHERE status = ?",
                        (process_queue.PROCESS_OK,),
                    ).fetchall()
                    keys.update(str(row["dataset_key"]) for row in rows)
                except sqlite3.Error:
                    pass
                finally:
                    connection.close()
    if marks_file is not None:
        marks = load_db_update_marks(Path(marks_file))
        keys.update(key for key, mark in marks.items() if mark.get("updated"))
    return keys


def load_download_continent_stats(
    state_db: Path | None,
    available_keys: set[str] | None = None,
) -> dict[str, dict[str, int]] | None:
    """按大洲统计下载任务总数、已下载数与未下载数（含未处理任务）。"""
    if state_db is None:
        return None
    file_path = Path(state_db)
    if not file_path.exists():
        return None
    try:
        connection = _open_state_db(file_path)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT task_id, continent, status FROM tasks"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    available = available_keys or set()
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        label = format_continent(row["continent"])
        item = stats.setdefault(
            label,
            {"total": 0, "downloaded": 0, "not_downloaded": 0, "other": 0},
        )
        item["total"] += 1
        status = str(row["status"] or "")
        if status in lib.SUCCESS_STATUSES or str(row["task_id"] or "") in available:
            item["downloaded"] += 1
        elif status == lib.STATUS_PENDING:
            item["not_downloaded"] += 1
        else:
            item["other"] += 1
    return stats


def load_not_downloaded_tasks(
    repo_root: Path,
    state_db: Path,
    continent: str,
    available_keys: set[str] | None = None,
) -> dict:
    """按大洲查询尚未成功下载建筑数据的任务。"""
    repo_root = Path(repo_root)
    file_path = Path(state_db)
    label = str(continent or "").strip()
    if not label:
        raise ValueError("continent 不能为空")
    if not file_path.exists():
        raise RuntimeError(f"下载状态库不存在: {file_path}")
    try:
        connection = _open_state_db(file_path)
    except sqlite3.Error as exc:
        raise RuntimeError(f"下载状态库无法打开: {exc}") from exc
    try:
        rows = connection.execute(
            "SELECT task_id, continent, country, city, download_dir, status, attempts, "
            "queue_order, manifest_order, last_error, started_at, finished_at, updated_at "
            "FROM tasks"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"下载状态库读取失败: {exc}") from exc
    finally:
        connection.close()

    available = available_keys or set()
    matched = [
        dict(row)
        for row in rows
        if format_continent(row["continent"]) == label
        and str(row["status"] or "") == lib.STATUS_PENDING
        and str(row["task_id"] or "") not in available
    ]
    priority = {"RUNNING": 0, "PENDING": 1, "FAILED": 2, "SKIPPED": 3}
    matched.sort(
        key=lambda item: (
            priority.get(str(item["status"] or ""), 9),
            item["queue_order"] if item["queue_order"] is not None else 10**9,
            item["manifest_order"] if item["manifest_order"] is not None else 10**9,
            str(item["task_id"] or ""),
        )
    )

    counts = {status: 0 for status in ("PENDING", "RUNNING", "FAILED", "SKIPPED")}
    resolved_root = repo_root.resolve()
    result_rows = []
    for item in matched:
        status = str(item["status"] or "")
        counts[status] = counts.get(status, 0) + 1
        download_dir = str(item["download_dir"] or "")
        log_path = ""
        if download_dir:
            for log_name in ("run.log", "run.err.log"):
                try:
                    candidate = (repo_root / download_dir / log_name).resolve()
                    candidate.relative_to(resolved_root)
                    if candidate.is_file():
                        log_path = candidate.relative_to(resolved_root).as_posix()
                        break
                except (OSError, ValueError):
                    continue
        result_rows.append(
            {
                "task_id": item["task_id"],
                "continent": item["continent"],
                "country": item["country"],
                "city": item["city"],
                "download_dir": download_dir,
                "status": status,
                "attempts": item["attempts"],
                "queue_order": item["queue_order"],
                "manifest_order": item["manifest_order"],
                "last_error": str(item["last_error"] or "")[:300],
                "started_at": item["started_at"],
                "finished_at": item["finished_at"],
                "updated_at": item["updated_at"],
                "log_path": log_path,
            }
        )
    return {
        "ok": True,
        "continent": label,
        "total": len(result_rows),
        "counts": counts,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": result_rows,
    }


def _build_copy_info(
    repo_root: Path,
    dataset_key: str,
    output_dir: str | None,
    record: dict | None,
    active_job: dict | None,
    updated: bool,
) -> dict:
    """合并内存任务与持久化拷贝记录，给出该行共享目录状态。"""
    log_path = _update_log_relpath(dataset_key)
    base = {
        "state": "unknown",
        "copied": 0,
        "total": 0,
        "started_at": "",
        "finished_at": "",
        "error": None,
        "share_dir": "",
        "source_dir": "",
        "log_path": log_path,
    }
    if active_job:
        state_map = {"running": "copying", "done": "done", "error": "error"}
        state = state_map.get(str(active_job.get("state") or ""), "copying")
        return {
            **base,
            "state": state,
            "copied": int(active_job.get("copied") or 0),
            "total": int(active_job.get("total") or 0),
            "started_at": active_job.get("started_at") or "",
            "finished_at": active_job.get("finished_at") or "",
            "error": active_job.get("error"),
            "share_dir": active_job.get("share_dir") or "",
            "source_dir": active_job.get("source_dir") or "",
        }
    if isinstance(record, dict):
        state = str(record.get("state") or "")
        merged = {
            **base,
            "copied": int(record.get("copied") or 0),
            "total": int(record.get("total") or 0),
            "started_at": record.get("started_at") or "",
            "finished_at": record.get("finished_at") or "",
            "error": record.get("error"),
            "share_dir": record.get("share_dir") or "",
            "source_dir": record.get("source_dir") or "",
        }
        if state == "done":
            return {**merged, "state": "done", "error": None}
        if updated:
            # 已入库的任务即使拷贝记录异常，也按历史已交付显示
            return {**base, "state": "done"}
        if state == "error":
            return {**merged, "state": "error"}
        if state == "copying":
            # 后台任务已不在内存中，说明上次拷贝中断，不能继续显示“拷贝中”
            return {**merged, "state": "error", "error": "上次拷贝任务已中断，请重新拷贝"}
    elif updated:
        # 已入库的历史任务，按共享目录已交付处理（旧数据没有拷贝记录）
        return {**base, "state": "done"}
    # 没有记录且尚未入库：final 还有待交付 SHP，说明还未拷贝
    if output_dir:
        final_dir = (Path(repo_root) / output_dir).resolve() / "final"
        try:
            final_files = list(final_dir.glob("*_3857.shp")) if final_dir.is_dir() else []
        except OSError:
            final_files = []
        if final_files:
            return {
                **base,
                "state": "not_copied",
                "total": len(final_files),
                "source_dir": str(final_dir),
            }
    return base


def build_db_update_snapshot(
    repo_root: Path,
    process_db: Path,
    marks_file: Path,
    state_db: Path | None = None,
) -> dict:
    repo_root = Path(repo_root)
    process_db = Path(process_db)
    marks_file = Path(marks_file)
    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "process_db": str(process_db),
        "marks_file": str(marks_file),
        "state_db": str(state_db) if state_db else "",
        "error": None,
        "counts": {"total": 0, "updated": 0, "pending": 0},
        "continent_actual_totals": {},
        "continent_not_downloaded": {},
        "actual_total": None,
        "not_downloaded_total": None,
        "copy_counts": {"done": 0, "copying": 0, "error": 0, "not_copied": 0, "unknown": 0},
        "rows": [],
    }
    if not process_db.exists():
        snapshot["error"] = (
            f"处理状态库不存在: {process_db}；先运行 python scripts/run_shp_process_tasks.py --sync"
        )
        return snapshot
    marks = load_db_update_marks(marks_file)
    copy_records = _load_db_copy_records(repo_root / DEFAULT_DB_COPY_STATUS)
    with DB_COPY_LOCK:
        active_copy_jobs = {key: dict(job) for key, job in DB_COPY_JOBS.items()}
    try:
        connection = _open_state_db(process_db)
    except sqlite3.Error as exc:
        snapshot["error"] = f"处理状态库无法打开: {exc}"
        return snapshot
    try:
        records = connection.execute(
            "SELECT * FROM process_tasks WHERE status = ? ORDER BY finished_at DESC, dataset_key",
            (process_queue.PROCESS_OK,),
        ).fetchall()
    except sqlite3.Error as exc:
        snapshot["error"] = f"处理状态库读取失败: {exc}"
        return snapshot
    finally:
        connection.close()

    import_records = _load_db_import_records(repo_root / DEFAULT_DB_IMPORT_STATUS)
    rows = []
    for row in records:
        key = row["dataset_key"]
        continent_raw = key.split("|", 1)[0] if "|" in key else key
        continent_display = "其它(EXT)" if continent_raw.upper() == "EXT" else format_continent(continent_raw)
        mark = marks.get(key, {})
        record = import_records.get(key)
        copy_info = _build_copy_info(
            repo_root,
            key,
            row["output_dir"],
            copy_records.get(key),
            active_copy_jobs.get(key),
            bool(mark.get("updated")),
        )
        import_detail = None
        if isinstance(record, dict):
            import_detail = {
                "deleted": record.get("deleted"),
                "inserted": record.get("inserted"),
                "skipped": record.get("skipped"),
                "state": record.get("state"),
                "updated_at": record.get("updated_at"),
                "import_log": _update_log_relpath(key),
            }
        rows.append(
            {
                "dataset_key": key,
                "continent": continent_raw,
                "continent_display": continent_display,
                "task_id": row["task_id"],
                "display_name": row["display_name"] or key,
                "process_name_prefix": _row_optional(row, "process_name_prefix"),
                "source_dir": row["source_dir"],
                "output_dir": row["output_dir"],
                "tile_count": row["tile_count"],
                "feature_count": row["feature_count"],
                "finished_at": row["finished_at"],
                "updated": bool(mark.get("updated")),
                "updated_at": mark.get("updated_at") or "",
                "note": mark.get("note") or "",
                "import_detail": import_detail,
                "copy_state": copy_info["state"],
                "copy_copied": copy_info["copied"],
                "copy_total": copy_info["total"],
                "copy_started_at": copy_info["started_at"],
                "copy_finished_at": copy_info["finished_at"],
                "copy_error": copy_info["error"],
                "copy_share_dir": copy_info["share_dir"],
                "copy_source_dir": copy_info["source_dir"],
                "copy_log": copy_info["log_path"],
            }
        )
    snapshot["rows"] = rows
    copy_counts = {"done": 0, "copying": 0, "error": 0, "not_copied": 0, "unknown": 0}
    for item in rows:
        state = str(item.get("copy_state") or "unknown")
        copy_counts[state] = copy_counts.get(state, 0) + 1
    snapshot["copy_counts"] = copy_counts
    snapshot["counts"] = {
        "total": len(rows),
        "updated": sum(1 for item in rows if item["updated"]),
        "pending": sum(1 for item in rows if not item["updated"]),
    }
    available_keys = {str(row["dataset_key"]) for row in records}
    available_keys.update(key for key, mark in marks.items() if mark.get("updated"))
    download_stats = load_download_continent_stats(state_db, available_keys)
    if download_stats is not None:
        snapshot["continent_actual_totals"] = {
            label: item["total"] for label, item in download_stats.items()
        }
        snapshot["continent_not_downloaded"] = {
            label: item["not_downloaded"] for label, item in download_stats.items()
        }
        snapshot["actual_total"] = sum(item["total"] for item in download_stats.values())
        snapshot["not_downloaded_total"] = sum(
            item["not_downloaded"] for item in download_stats.values()
        )
    return snapshot


def parse_int_query(query: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def normalize_reorder_payload(payload: dict) -> tuple[str, str]:
    dataset_key = str(payload.get("dataset_key") or "").strip()
    direction = str(payload.get("direction") or "").strip().lower()
    if not dataset_key:
        raise ValueError("dataset_key 不能为空")
    if direction not in ("up", "down", "top"):
        raise ValueError("direction 只支持 up / down / top")
    return dataset_key, direction


def normalize_requeue_payload(payload: dict) -> tuple[str, str]:
    task_id = str(payload.get("task_id") or payload.get("dataset_key") or "").strip()
    position = str(payload.get("position") or "tail").strip().lower()
    if not task_id:
        raise ValueError("task_id 不能为空")
    if position not in ("tail", "top"):
        raise ValueError("position 只支持 tail / top")
    return task_id, position


def normalize_note_payload(payload: dict) -> tuple[str, str]:
    dataset_key = str(payload.get("dataset_key") or "").strip()
    if not dataset_key:
        raise ValueError("dataset_key 不能为空")
    note = str(payload.get("note") or "").strip()
    if len(note) > PROCESS_NOTE_LIMIT:
        raise ValueError(f"备注最多 {PROCESS_NOTE_LIMIT} 个字符")
    return dataset_key, note


def set_process_note(config: dict, dataset_key: str, note: str) -> str:
    store = process_queue.ProcessStore(Path(config["process_db"]))
    try:
        if store.get(dataset_key) is None:
            raise KeyError(f"未找到处理任务: {dataset_key}")
        return store.set_note(dataset_key, note)
    finally:
        store.close()


def extract_flag_value(command_line: str, flag: str) -> str | None:
    match = re.search(rf"{re.escape(flag)}\s+(\"[^\"]*\"|\S+)", command_line or "")
    if not match:
        return None
    return match.group(1).strip('"')


def _list_python_processes() -> list[dict]:
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Select-Object ProcessId,CommandLine,@{n='Created';e={$_.CreationDate.ToFileTimeUtc()}} | "
            "ConvertTo-Json -Compress"
        ),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    raw = result.stdout or b""
    try:
        text = raw.decode("utf-8-sig").strip() if isinstance(raw, bytes) else str(raw).strip()
    except UnicodeDecodeError:
        return []
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    return [item for item in payload if isinstance(item, dict)]


def _created_key(item: dict) -> int:
    try:
        return int(item.get("Created") or 0)
    except (TypeError, ValueError):
        return 0


def detect_batch() -> dict:
    runners: list[dict] = []
    workers: list[dict] = []
    for item in _list_python_processes():
        command_line = str(item.get("CommandLine") or "")
        pid = item.get("ProcessId")
        created = _created_key(item)
        if RUNNER_MARKER in command_line:
            runners.append({"pid": pid, "command_line": command_line, "created": created})
        elif WORKER_MARKER in command_line:
            workers.append({"pid": pid, "command_line": command_line, "created": created})
    runners.sort(key=lambda item: item["created"], reverse=True)
    runner = runners[0] if runners else None
    tile_workers = extract_flag_value(runner["command_line"], "--tile-workers") if runner else None
    try:
        tile_workers_value = int(tile_workers) if tile_workers else None
    except ValueError:
        tile_workers_value = None
    return {
        "runner": runner,
        "runners": runners,
        "runner_count": len(runners),
        "workers": workers,
        "tile_workers": tile_workers_value,
    }


def detect_process_batch() -> dict:
    runners: list[dict] = []
    workers: list[dict] = []
    for item in _list_python_processes():
        command_line = str(item.get("CommandLine") or "")
        pid = item.get("ProcessId")
        created = _created_key(item)
        if PROCESS_RUNNER_MARKER in command_line:
            runners.append({"pid": pid, "command_line": command_line, "created": created})
        elif PROCESS_WORKER_MARKER in command_line and PROCESS_WORKER_DIR in command_line:
            workers.append({"pid": pid, "command_line": command_line, "created": created})
    runners.sort(key=lambda item: item["created"], reverse=True)
    runner = runners[0] if runners else None
    limit = extract_flag_value(runner["command_line"], "--limit") if runner else None
    try:
        limit_value = int(limit) if limit else 0
    except ValueError:
        limit_value = 0
    return {
        "runner": runner,
        "runners": runners,
        "runner_count": len(runners),
        "workers": workers,
        "limit": limit_value,
        "wait_resources": "--wait-resources" in (runner["command_line"] if runner else ""),
    }


def resolve_process_python(config: dict) -> str:
    repo_root = Path(config["repo_root"])
    configured = str(config.get("process_python_exe") or "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.exists():
            return str(candidate)
    fallback = repo_root / DEFAULT_PROCESS_PYTHON
    if fallback.exists():
        return str(fallback)
    return config["python_exe"]


def build_process_start_command(python_exe: str, repo_root: Path, limit: int, wait_resources: bool) -> list[str]:
    return process_queue.build_run_command(
        python_exe,
        repo_root,
        limit=max(int(limit), 0),
        wait_resources=bool(wait_resources),
    )


def start_process_batch(repo_root: Path, command: list[str], extra_env: dict | None = None) -> dict:
    log_path = Path(repo_root) / DEFAULT_PROCESS_LOG
    error_log_path = Path(repo_root) / DEFAULT_PROCESS_ERR_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = lib.child_process_env()
    if extra_env:
        env.update(extra_env)
    with log_path.open("w", encoding="utf-8") as out_handle, error_log_path.open("w", encoding="utf-8") as err_handle:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=out_handle,
            stderr=err_handle,
            creationflags=creationflags,
            env=env,
        )
    return {"pid": process.pid, "command": command}


def stop_batch_tree(pids: list[int]) -> list[int]:
    stopped: list[int] = []
    for pid in pids:
        if not pid:
            continue
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                stopped.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    return stopped


def _stop_all_process_queue_processes() -> tuple[dict, list[int]]:
    detected = detect_process_batch()
    runner_pids = [item["pid"] for item in (detected.get("runners") or []) if item.get("pid")]
    if not runner_pids and detected.get("runner"):
        runner_pids = [detected["runner"]["pid"]]
    stopped: list[int] = []

    if runner_pids:
        stopped.extend(stop_batch_tree(runner_pids))
        _wait_for_exit(runner_pids, timeout=20)

    worker_pids = {item["pid"] for item in detected["workers"] if item.get("pid")}
    worker_pids.update(item["pid"] for item in detect_process_batch()["workers"] if item.get("pid"))
    if worker_pids:
        ordered_workers = sorted(worker_pids)
        stopped.extend(stop_batch_tree(ordered_workers))
        _wait_for_exit(ordered_workers, timeout=15)
    return detected, stopped


def _wait_process_batch_clear(timeout: float = 5.0, interval: float = 0.5) -> dict:
    """轮询探测，直到处理 runner/worker 全部退出或超时；返回最后一次检测结果。"""
    deadline = time.time() + max(timeout, 0.0)
    detected = detect_process_batch()
    while (detected["runner"] or detected["workers"]) and time.time() < deadline:
        time.sleep(interval)
        detected = detect_process_batch()
    return detected


def sync_process_queue(config: dict) -> dict:
    repo_root = Path(config["repo_root"])
    result = process_queue.sync_process_tasks(
        repo_root,
        Path(config["process_db"]),
        repo_root / process_queue.DEFAULT_DOWNLOAD_DB,
        repo_root / process_queue.DEFAULT_SCAN_ROOT,
        [],
        [],
        scan_enabled=True,
        manifest_path=repo_root / process_queue.DEFAULT_MANIFEST,
    )
    return {"total": result["total"], "added": result["added"], "updated": result["updated"]}


def start_process_action(config: dict, limit: int, wait_resources: bool) -> dict:
    with CONTROL_LOCK:
        detected = detect_process_batch()
        if detected["runner"]:
            return {
                "ok": False,
                "error": "处理任务已在运行",
                "runner_pid": detected["runner"]["pid"],
            }
        if detected["workers"]:
            worker_pids = [item["pid"] for item in detected["workers"]]
            return {
                "ok": False,
                "error": (
                    f"检测到残留处理进程（PID: {', '.join(map(str, worker_pids))}），"
                    "请先点「停止处理」并确认未运行后再开始"
                ),
                "worker_pids": worker_pids,
            }
        python_exe = resolve_process_python(config)
        command = build_process_start_command(python_exe, config["repo_root"], limit, wait_resources)
        qgis_cfg = load_dashboard_config(config["repo_root"])
        qgis_dir = qgis_cfg.get("qgis_dir") or ""
        qgis_process_bat = qgis_cfg.get("qgis_process_bat_effective") or ""
        extra_env: dict[str, str] = {}
        if qgis_dir:
            extra_env["QGIS_DIR"] = qgis_dir
        if qgis_process_bat:
            extra_env["QGIS_PROCESS_BAT"] = qgis_process_bat
        started = start_process_batch(config["repo_root"], command, extra_env or None)
        return {
            "ok": True,
            "runner_pid": started["pid"],
            "limit": max(int(limit), 0),
            "wait_resources": bool(wait_resources),
            "python_exe": python_exe,
            "command": command,
        }


def stop_process_action(config: dict) -> dict:
    with CONTROL_LOCK:
        detected, stopped = _stop_all_process_queue_processes()
        remaining = _wait_process_batch_clear(timeout=5.0)
        if remaining["runner"] or remaining["workers"]:
            survivor_pids = [
                item["pid"]
                for item in ([remaining["runner"]] if remaining["runner"] else []) + remaining["workers"]
            ]
            raise RuntimeError(
                f"处理进程未完全停止（PID: {', '.join(map(str, survivor_pids))}），"
                "任务状态保持 RUNNING；请稍后重试「停止处理」"
            )
        reset_count = 0
        reset_error = None
        try:
            reset_count = process_queue.reset_running_in_db(Path(config["process_db"]))
        except Exception as exc:  # noqa: BLE001
            reset_error = str(exc)
        return {
            "ok": True,
            "stopped_pids": stopped,
            "runner_pid": detected["runner"]["pid"] if detected["runner"] else None,
            "reset_tasks": reset_count,
            "reset_error": reset_error,
        }


def build_process_control_snapshot(config: dict) -> dict:
    detected = detect_process_batch()
    current_task = None
    try:
        connection = _open_state_db(Path(config["process_db"]))
    except sqlite3.Error:
        connection = None
    if connection is not None:
        try:
            row = connection.execute(
                "SELECT dataset_key, stage, tiles_done, tiles_total FROM process_tasks "
                "WHERE status = ? ORDER BY started_at LIMIT 1",
                ("RUNNING",),
            ).fetchone()
            if row is not None:
                current_task = {
                    "dataset_key": row["dataset_key"],
                    "stage": row["stage"],
                    "stage_label": STAGE_LABELS.get(row["stage"] or "", row["stage"] or "-"),
                    "tiles_done": row["tiles_done"],
                    "tiles_total": row["tiles_total"],
                }
        except sqlite3.Error:
            current_task = None
        finally:
            connection.close()
    return {
        "running": detected["runner"] is not None,
        "runner_pid": detected["runner"]["pid"] if detected["runner"] else None,
        "runner_count": detected.get("runner_count", 1 if detected["runner"] else 0),
        "worker_pids": [item["pid"] for item in detected["workers"]],
        "limit": detected.get("limit", 0),
        "wait_resources": detected.get("wait_resources", False),
        "current_task": current_task,
        "log_file": DEFAULT_PROCESS_LOG,
        "action_token_required": bool(config.get("action_token")),
    }


def normalize_process_limit(value: object) -> int:
    try:
        limit = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是整数") from exc
    if limit < 0:
        raise ValueError("limit 不能为负数")
    return limit


def normalize_wait_resources(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def build_restart_command(
    python_exe: str,
    repo_root: Path,
    tile_workers: int,
    base_command_line: str | None = None,
) -> list[str]:
    runner_script = Path(repo_root) / "scripts" / "run_world_building_tasks.py"
    values: dict[str, str] = {}
    switches: list[str] = []
    if base_command_line:
        for flag in PRESERVED_VALUE_FLAGS:
            value = extract_flag_value(base_command_line, flag)
            if value is not None:
                values[flag] = value
        for flag in PRESERVED_SWITCH_FLAGS:
            if re.search(rf"(?:^|\s){re.escape(flag)}(?=\s|$)", base_command_line):
                switches.append(flag)

    values.setdefault("--sleep-seconds", "2")
    values.setdefault("--task-attempts", "2")
    values["--tile-workers"] = str(tile_workers)

    ordered_flags = [
        "--only",
        "--continent",
        "--country",
        "--status",
        "--max-tasks",
        "--grid-size",
        "--page-size",
        "--sleep-seconds",
        "--task-attempts",
        "--merge-format",
        "--engine",
        "--boundaries-dir",
        "--overrides",
        "--tile-workers",
    ]
    arguments = ["-u", str(runner_script)]
    for flag in ordered_flags:
        if flag in values:
            arguments.extend([flag, values[flag]])
    arguments.extend(switches)
    return [python_exe, *arguments]


def _process_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return str(pid) in (result.stdout or "")


def stop_batch(pids: list[int]) -> list[int]:
    stopped: list[int] = []
    for pid in pids:
        if not pid:
            continue
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                stopped.append(pid)
        except (OSError, subprocess.SubprocessError):
            continue
    return stopped


def start_batch(repo_root: Path, command: list[str]) -> dict:
    log_path = Path(repo_root) / DEFAULT_RUN_LOG
    error_log_path = Path(repo_root) / DEFAULT_RUN_ERR_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("w", encoding="utf-8") as out_handle, error_log_path.open("w", encoding="utf-8") as err_handle:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=out_handle,
            stderr=err_handle,
            creationflags=creationflags,
            env=lib.child_process_env(),
        )
    return {"pid": process.pid, "command": command}


def _normalize_config_port(value: object, default: int | None) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _normalize_process_default_limit(value: object) -> int:
    if value is None or str(value).strip() == "":
        return 0
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return 0
    return parsed if parsed >= 0 else 0


def _normalize_qgis_process_bat(value: object) -> str:
    """统一 qgis_process 批处理路径的引号与分隔符，避免页面显示值不一致。"""
    text = str(value or "").strip().strip('"').strip()
    if not text:
        return ""
    return str(Path(text).expanduser())


def _resolve_qgis_process_bat(config: dict) -> Path:
    """优先使用「配置 → QGIS」里实际填写的批处理路径，否则按安装目录推导。"""
    value = config.get("qgis_process_bat_effective") or config.get("qgis_process_bat")
    if value:
        return Path(str(value))
    qgis_dir = config.get("qgis_dir") or DEFAULT_QGIS_DIR
    return Path(qgis_dir) / "bin" / "qgis_process-qgis.bat"


def load_dashboard_config(repo_root: Path) -> dict:
    path = Path(repo_root) / DEFAULT_CONFIG_FILE
    stored: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                stored = raw
        except (OSError, ValueError):
            stored = {}
    db = stored.get("database") if isinstance(stored.get("database"), dict) else {}
    port = _normalize_config_port(db.get("port"), None)
    database = {
        "host": str(db.get("host") or DEFAULT_UPDATE_DB["host"]),
        "port": port if port is not None else DEFAULT_UPDATE_DB["port"],
        "dbname": str(db.get("dbname") or DEFAULT_UPDATE_DB["dbname"]),
        "user": str(db.get("user") or DEFAULT_UPDATE_DB["user"]),
        "password": str(db.get("password") if db.get("password") is not None else DEFAULT_UPDATE_DB["password"]),
    }
    result_root = str(stored.get("result_root") or DEFAULT_RESULT_ROOT)
    qgis_dir = str(stored.get("qgis_dir") or DEFAULT_QGIS_DIR)
    qgis_process_bat = _normalize_qgis_process_bat(stored.get("qgis_process_bat"))
    qgis_process_bat_effective = qgis_process_bat or str(
        Path(qgis_dir) / "bin" / "qgis_process-qgis.bat"
    )
    process = stored.get("process") if isinstance(stored.get("process"), dict) else {}
    return {
        "configured": path.exists(),
        "database": database,
        "result_root": result_root,
        "qgis_dir": qgis_dir,
        "qgis_process_bat": qgis_process_bat,
        "qgis_process_bat_effective": qgis_process_bat_effective,
        "process": {"default_limit": _normalize_process_default_limit(process.get("default_limit"))},
    }


def save_dashboard_config(repo_root: Path, config: dict) -> dict:
    db = config.get("database") if isinstance(config.get("database"), dict) else {}
    host = str(db.get("host") or "").strip()
    dbname = str(db.get("dbname") or "").strip()
    user = str(db.get("user") or "").strip()
    result_root = str(config.get("result_root") or "").strip()
    qgis_dir = str(config.get("qgis_dir") or "").strip()
    qgis_process_bat = _normalize_qgis_process_bat(config.get("qgis_process_bat"))
    if not host:
        raise ValueError("数据库主机不能为空")
    if not dbname:
        raise ValueError("数据库名不能为空")
    if not user:
        raise ValueError("数据库用户不能为空")
    if not result_root:
        raise ValueError("区域成果 SHP 共享目录不能为空")
    port = _normalize_config_port(db.get("port"), None)
    if port is None:
        raise ValueError("数据库端口必须是 1~65535 的整数")
    raw_password = db.get("password")
    process = config.get("process") if isinstance(config.get("process"), dict) else {}
    raw_limit = process.get("default_limit")
    if raw_limit is None or str(raw_limit).strip() == "":
        default_limit = 0
    else:
        try:
            default_limit = int(str(raw_limit).strip())
        except ValueError:
            raise ValueError("处理默认数量必须是 ≥0 的整数") from None
        if default_limit < 0:
            raise ValueError("处理默认数量必须是 ≥0 的整数")
    payload = {
        "database": {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": user,
            "password": "" if raw_password is None else str(raw_password),
        },
        "result_root": result_root,
        "qgis_dir": qgis_dir,
        "qgis_process_bat": qgis_process_bat,
        "process": {"default_limit": default_limit},
    }
    path = Path(repo_root) / DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return payload


def _probe_python_version(python_exe: Path) -> str | None:
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode == 0:
        return (proc.stdout or "").strip() or None
    return None


def build_env_snapshot(config: dict) -> dict:
    repo_root = Path(config["repo_root"])
    update_python = repo_root / DEFAULT_UPDATE_PYTHON
    update_script = repo_root / DEFAULT_UPDATE_SCRIPT
    update_log_dir = repo_root / DEFAULT_UPDATE_LOG_DIR
    process_python = Path(config.get("process_python_exe") or (repo_root / DEFAULT_PROCESS_PYTHON))
    qgis_cfg = load_dashboard_config(repo_root)
    qgis_dir = qgis_cfg.get("qgis_dir") or DEFAULT_QGIS_DIR
    qgis_bat = _resolve_qgis_process_bat(qgis_cfg)
    update_python_exists = update_python.is_file()
    return {
        "repo_root": str(repo_root),
        "update_python": str(update_python),
        "update_python_exists": update_python_exists,
        "update_python_version": _probe_python_version(update_python) if update_python_exists else None,
        "update_script": str(update_script),
        "update_script_exists": update_script.is_file(),
        "update_log_dir": str(update_log_dir),
        "update_log_dir_exists": update_log_dir.is_dir(),
        "process_python": str(process_python),
        "process_python_exists": Path(process_python).is_file(),
        "qgis_dir": str(qgis_dir),
        "qgis_process_bat": str(qgis_bat),
        "qgis_process_bat_exists": qgis_bat.is_file(),
    }


def _probe_imports(python_exe: Path, modules: list[str]) -> dict:
    code = "import " + ", ".join(modules)
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", code],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)}
    if proc.returncode == 0:
        return {"ok": True, "detail": ""}
    return {"ok": False, "detail": ((proc.stderr or proc.stdout or "").strip()[-200:])}


def check_environment(config: dict) -> dict:
    repo_root = Path(config["repo_root"])
    cfg = load_dashboard_config(repo_root)
    qgis_bat = _resolve_qgis_process_bat(cfg)
    process_python = Path(config.get("process_python_exe") or (repo_root / DEFAULT_PROCESS_PYTHON))

    items: list[dict] = []

    def add(label: str, path: Path, kind: str = "file", modules: list[str] | None = None) -> None:
        exists = path.is_file() if kind == "file" else path.is_dir()
        ok = exists
        detail = ""
        if exists and modules:
            probe = _probe_imports(path, modules)
            ok = probe["ok"]
            detail = probe["detail"]
        items.append({"label": label, "path": str(path), "exists": exists, "ok": ok, "detail": detail})

    add("入库工具解释器", repo_root / DEFAULT_UPDATE_PYTHON, "file", ["geopandas", "psycopg2", "dotenv"])
    add("入库脚本", repo_root / DEFAULT_UPDATE_SCRIPT)
    add("入库日志目录", repo_root / DEFAULT_UPDATE_LOG_DIR, "dir")
    add("处理工具解释器", process_python, "file", ["geopandas", "shapely", "pyproj"])
    add("QGIS", qgis_bat)

    missing = [item["label"] for item in items if not item["ok"]]
    return {"ok": not missing, "missing": missing, "items": items}


LOG_CATEGORIES = (
    ("download", "建筑数据下载"),
    ("process", "数据处理"),
)


def _resolve_log_path(repo_root: Path, rel_path: str) -> Path | None:
    """把仓库相对路径解析为白名单内的日志文件；越权返回 None。"""
    if not rel_path:
        return None
    repo_root = Path(repo_root).resolve()
    candidate = (repo_root / rel_path).resolve()
    try:
        rel = candidate.relative_to(repo_root)
    except ValueError:
        return None
    if candidate.is_symlink() or not candidate.is_file():
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "logs":
        return candidate
    if len(parts) == 5 and parts[0] == "data" and (
        parts[-1] in ("run.log", "run.err.log")
        or (parts[-1].startswith("merge_") and parts[-1].endswith(".log"))
    ):
        return candidate
    if (
        len(parts) >= 4
        and parts[0] == "Tools"
        and parts[1] == "Oneshp_pipline_qgis"
        and parts[2] == "out_data"
        and parts[-1] == "stdout.log"
    ):
        return candidate
    return None


def _log_entry(repo_root: Path, path: Path) -> dict:
    stat = path.stat()
    rel = path.relative_to(repo_root)
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "logs":
        label = path.name
    else:
        label = "/".join(parts[-4:] if len(parts) >= 4 else parts)
    return {
        "path": rel.as_posix(),
        "name": path.name,
        "label": label,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def list_log_files(repo_root: Path, process_db: Path | None = None) -> dict:
    repo_root = Path(repo_root)

    def entry(path: Path) -> dict | None:
        try:
            return _log_entry(repo_root, path)
        except OSError:
            return None

    def collect(pattern: str) -> list[dict]:
        out: list[dict] = []
        try:
            matches = sorted(repo_root.glob(pattern))
        except OSError:
            return out
        for path in matches:
            if path.is_file():
                item = entry(path)
                if item and item["size"] > 0:
                    out.append(item)
        return out

    # 建筑数据下载：按任务（data/<洲>/<国家>/<区域>/run.log(.err.log)）
    download_tasks: dict[str, dict] = {}
    for region_dir in sorted(repo_root.glob("data/*/*/*")):
        parts = region_dir.relative_to(repo_root).parts
        if len(parts) != 4 or parts[1] == "logs" or not region_dir.is_dir():
            continue
        files = []
        for name in ("run.log", "run.err.log"):
            candidate = region_dir / name
            if candidate.is_file():
                item = entry(candidate)
                if item and item["size"] > 0:
                    files.append(item)
        if files:
            key = "/".join(parts[1:])
            download_tasks[key] = {"key": key, "label": key, "files": files}

    # 数据处理：按区域统一任务（merge_<前缀>.log 与 <前缀>_pipeline/stdout.log）
    out_root = repo_root / "Tools" / "Oneshp_pipline_qgis" / "out_data"
    process_tasks: dict[str, dict] = {}

    def ensure_task(key: str) -> dict:
        task = process_tasks.get(key)
        if task is None:
            task = {"key": key, "label": key, "files": []}
            process_tasks[key] = task
        return task

    def add_file(task: dict, path: Path) -> None:
        if not path.is_file():
            return
        item = entry(path)
        if item and item["size"] > 0 and all(existing["path"] != item["path"] for existing in task["files"]):
            task["files"].append(item)

    prefix_to_region: dict[str, str] = {}
    rows: list = []
    if process_db is not None:
        try:
            conn = sqlite3.connect(str(process_db))
            conn.row_factory = sqlite3.Row
            try:
                rows = list(conn.execute(
                    "SELECT dataset_key, process_name_prefix, source_dir FROM process_tasks"
                ))
            finally:
                conn.close()
        except sqlite3.Error:
            rows = []

    for row in rows:
        parts = str(row["dataset_key"] or "").split("|")
        if len(parts) != 3 or not all(parts):
            continue
        region_key = "/".join(parts)
        prefix = row["process_name_prefix"] or process_queue.dataset_slug(row["dataset_key"])
        prefix_to_region[prefix] = region_key
        task = ensure_task(region_key)
        source_dir = row["source_dir"]
        if source_dir:
            add_file(task, lib.resolve_task_dir(repo_root, source_dir) / f"merge_{prefix}.log")
        add_file(task, out_root / f"{prefix}_pipeline" / "stdout.log")

    # 兜底：磁盘上未被上面覆盖的 merge_*.log 与 *_pipeline/stdout.log
    for merge_path in sorted(repo_root.glob("data/*/*/*/merge_*.log")):
        parts = merge_path.relative_to(repo_root).parts
        if len(parts) != 5 or parts[1] == "logs":
            continue
        add_file(ensure_task("/".join(parts[1:4])), merge_path)
    if out_root.is_dir():
        for pipeline_dir in sorted(out_root.glob("*_pipeline")):
            stdout_path = pipeline_dir / "stdout.log"
            if not stdout_path.is_file():
                continue
            prefix = pipeline_dir.name[: -len("_pipeline")]
            region_key = prefix_to_region.get(prefix)
            add_file(ensure_task(region_key or pipeline_dir.name), stdout_path)

    # 库表更新：data/logs/update/*.log（每个文件作为一条任务）
    update_tasks: dict[str, dict] = {}
    for item in collect("data/logs/update/*.log"):
        name = item["name"]
        label = name[len("log_main3_"):] if name.startswith("log_main3_") else name
        file_entry = dict(item)
        file_entry["label"] = label
        update_tasks[item["path"]] = {"key": item["path"], "label": label, "files": [file_entry]}

    def sort_tasks(tasks: dict[str, dict]) -> list[dict]:
        return sorted(
            (task for task in tasks.values() if task["files"]),
            key=lambda task: task["key"].lower(),
        )

    return {
        "ok": True,
        "groups": [
            {
                "key": "download",
                "label": "建筑数据下载",
                "global": [],
                "tasks": sort_tasks(download_tasks),
            },
            {
                "key": "process",
                "label": "数据处理",
                "global": [],
                "tasks": sort_tasks(process_tasks),
            },
            {
                "key": "update",
                "label": "库表更新",
                "global": [],
                "tasks": sort_tasks(update_tasks),
            },
        ],
    }


def read_log_file(repo_root: Path, rel_path: str, lines: int) -> dict:
    target = _resolve_log_path(repo_root, rel_path)
    if target is None:
        raise ValueError("日志文件不存在或不允许访问")
    text = lib.read_log_text(target)
    all_lines = text.splitlines()
    total = len(all_lines)
    shown = all_lines[-lines:] if lines > 0 else all_lines
    stat = target.stat()
    return {
        "ok": True,
        "path": rel_path,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "total_lines": total,
        "lines": shown,
        "truncated": total > len(shown),
    }


def open_local_folder(repo_root: Path, rel_path: str) -> str:
    """在资源管理器中打开仓库 data 目录下的子目录（仅限白名单）。"""
    if not rel_path:
        raise ValueError("缺少目录参数")
    repo_root = Path(repo_root).resolve()
    data_root = (repo_root / "data").resolve()
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError:
        raise ValueError("只允许打开仓库 data 目录下的路径")
    if not candidate.is_dir():
        raise ValueError(f"目录不存在：{candidate}")
    if os.name == "nt":
        os.startfile(str(candidate))  # noqa: S606 - Windows 打开资源管理器
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(candidate)])
    return str(candidate)


def build_control_snapshot(config: dict) -> dict:
    detected = detect_batch()
    current = detected["tile_workers"] or DEFAULT_TILE_WORKERS
    return {
        "running": detected["runner"] is not None,
        "runner_pid": detected["runner"]["pid"] if detected["runner"] else None,
        "runner_count": detected.get("runner_count", 1 if detected["runner"] else 0),
        "worker_pids": [item["pid"] for item in detected["workers"]],
        "tile_workers": current,
        "action_token_required": bool(config.get("action_token")),
    }


def _wait_for_exit(pids: list[int], timeout: float) -> None:
    if not pids:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_process_alive(pid) for pid in pids):
            return
        time.sleep(0.5)


def _stop_all_batch_processes() -> tuple[dict, list[int]]:
    detected = detect_batch()
    runner_pids = [item["pid"] for item in (detected.get("runners") or []) if item.get("pid")]
    if not runner_pids and detected.get("runner"):
        runner_pids = [detected["runner"]["pid"]]
    stopped: list[int] = []

    if runner_pids:
        stopped.extend(stop_batch(runner_pids))
        _wait_for_exit(runner_pids, timeout=20)

    worker_pids = {item["pid"] for item in detected["workers"] if item["pid"]}
    worker_pids.update(item["pid"] for item in detect_batch()["workers"] if item["pid"])
    if worker_pids:
        ordered_workers = sorted(worker_pids)
        stopped.extend(stop_batch(ordered_workers))
        _wait_for_exit(ordered_workers, timeout=15)
    return detected, stopped


def stop_batch_action(config: dict) -> dict:
    with CONTROL_LOCK:
        detected, stopped = _stop_all_batch_processes()
        return {
            "ok": True,
            "stopped_pids": stopped,
            "runner_pid": detected["runner"]["pid"] if detected["runner"] else None,
        }


def stop_current_task_action(config: dict, task_id: str) -> dict:
    with CONTROL_LOCK:
        store = lib.StateStore(Path(config["state_db"]))
        try:
            row = store.get(task_id)
        finally:
            store.close()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        if row["status"] != lib.STATUS_RUNNING:
            raise ValueError(f"只有运行中的任务可以停止并重新入队，当前状态: {row['status']}")

        detected, stopped = _stop_all_batch_processes()
        remaining = detect_batch()
        if remaining.get("runner") or remaining.get("workers"):
            raise RuntimeError("下载进程未完全停止，任务状态保持 RUNNING")

        queue = lib.stop_running_task_to_tail(Path(config["state_db"]), task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "position": "tail",
            "stopped_pids": stopped,
            "runner_pid": detected["runner"]["pid"] if detected.get("runner") else None,
            "queue": queue,
            "pending_total": len(queue),
        }


def restart_batch(config: dict, tile_workers: int) -> dict:
    with CONTROL_LOCK:
        detected, stopped = _stop_all_batch_processes()
        base_command_line = detected["runner"]["command_line"] if detected["runner"] else None

        command = build_restart_command(
            config["python_exe"],
            config["repo_root"],
            tile_workers,
            base_command_line,
        )
        started = start_batch(config["repo_root"], command)
        return {
            "ok": True,
            "tile_workers": tile_workers,
            "stopped_pids": stopped,
            "runner_pid": started["pid"],
            "command": command,
        }


def sync_manifest_to_state(config: dict) -> dict:
    """把任务清单 CSV 同步进下载状态库（只登记/更新/对账，不下载）。"""
    repo_root = Path(config["repo_root"])
    manifest_path = repo_root / DEFAULT_TASKS_CSV
    if not manifest_path.is_file():
        raise ValueError(f"未找到清单文件：{manifest_path}")

    detected = detect_batch()
    if detected.get("runner") or detected.get("workers"):
        raise RuntimeError("下载运行中，请先「停止下载任务」再同步")

    tasks = lib.read_manifest(manifest_path)
    store = lib.StateStore(Path(config["state_db"]))
    try:
        result = store.sync_manifest(tasks)
    finally:
        store.close()
    return {
        "ok": True,
        "total": len(tasks),
        "added": result["added"],
        "updated": result["updated"],
        "reset": result["reset"],
    }


def manifest_diff(config: dict) -> dict:
    """空跑比对清单 CSV 与状态库：同步是否会带来变更（不写库）。"""
    repo_root = Path(config["repo_root"])
    manifest_path = repo_root / DEFAULT_TASKS_CSV
    if not manifest_path.is_file():
        return {"ok": False, "changed": False, "error": "未找到清单文件"}
    try:
        tasks = lib.read_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "changed": False, "error": str(exc)}
    store = lib.StateStore(Path(config["state_db"]))
    try:
        result = store.sync_manifest(tasks, dry_run=True)
    finally:
        store.close()
    changed = (result["added"] + result["updated"] + result["reset"]) > 0
    return {"ok": True, "changed": changed, "total": len(tasks), **result}


def _find_download_task_by_region(
    state_db: Path, continent: str, country: str, city: str
) -> dict | None:
    try:
        connection = _open_state_db(state_db)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT task_id, continent, country, city, status FROM tasks"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    target = (continent.casefold(), country.casefold(), city.casefold())
    for row in rows:
        current = (
            str(row["continent"] or "").casefold(),
            str(row["country"] or "").casefold(),
            str(row["city"] or "").casefold(),
        )
        if current == target:
            return {"task_id": row["task_id"], "status": row["status"]}
    return None


def _find_process_prefix_owner(process_db: Path, prefix: str) -> dict | None:
    try:
        connection = _open_state_db(process_db)
    except sqlite3.Error:
        return None
    try:
        rows = connection.execute(
            "SELECT dataset_key, process_name_prefix FROM process_tasks "
            "WHERE process_name_prefix IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    prefix_fold = prefix.casefold()
    for row in rows:
        if str(row["process_name_prefix"] or "").casefold() == prefix_fold:
            return {
                "dataset_key": row["dataset_key"],
                "process_name_prefix": row["process_name_prefix"],
            }
    return None


def build_manifest_check(config: dict, keyword: str) -> dict:
    """在任务清单 CSV 与下载状态库中查找关键词（供「CSV清单管理」页查询）。"""
    needle = lib.collapse_spaces(keyword)
    if not needle:
        raise ValueError("关键词不能为空")
    if len(needle) > MANIFEST_KEYWORD_MAX_LEN:
        raise ValueError(f"关键词过长（最多 {MANIFEST_KEYWORD_MAX_LEN} 字符）")
    repo_root = Path(config["repo_root"])
    manifest_path = repo_root / DEFAULT_TASKS_CSV
    in_csv = lib.find_manifest_matches(manifest_path, needle) if manifest_path.is_file() else []
    in_state: list[dict] = []
    try:
        connection = _open_state_db(Path(config["state_db"]))
    except sqlite3.Error:
        connection = None
    if connection is not None:
        try:
            lower = needle.casefold()
            for row in connection.execute(
                "SELECT task_id, continent, country, city, status FROM tasks ORDER BY task_id"
            ):
                haystack = " ".join(
                    str(row[field] or "") for field in ("task_id", "continent", "country", "city")
                ).casefold()
                if lower in haystack:
                    in_state.append({"task_id": row["task_id"], "status": row["status"]})
                    if len(in_state) >= MANIFEST_SEARCH_LIMIT:
                        break
        finally:
            connection.close()
    return {
        "ok": True,
        "keyword": needle,
        "manifest_exists": manifest_path.is_file(),
        "in_csv": in_csv[:MANIFEST_SEARCH_LIMIT],
        "in_state": in_state,
    }


def append_manifest_task(
    config: dict, continent: str, country: str, city: str, process_name_prefix: str
) -> dict:
    """校验（状态库/处理库查重）后向任务清单追加一行，返回新行信息。"""
    repo_root = Path(config["repo_root"])
    manifest_path = repo_root / DEFAULT_TASKS_CSV
    if not manifest_path.is_file():
        raise ValueError(f"任务清单不存在：{DEFAULT_TASKS_CSV}（请先由维护人员从数据库导出）")
    continent = lib.collapse_spaces(continent)
    country = lib.collapse_spaces(country)
    city = lib.collapse_spaces(city)
    process_name_prefix = lib.collapse_spaces(process_name_prefix)
    missing = [
        label
        for label, value in (
            ("洲", continent),
            ("国家", country),
            ("城市/区域", city),
            ("命名前缀", process_name_prefix),
        )
        if not value
    ]
    if missing:
        raise ValueError("缺少必填项：" + "、".join(missing))

    with CONTROL_LOCK:
        existing = _find_download_task_by_region(Path(config["state_db"]), continent, country, city)
        if existing:
            raise ValueError(
                f"该任务已存在于下载状态库（{existing['task_id']}，状态 {existing['status']}），"
                "无需追加；如需重新下载请到「建筑数据下载」页操作"
            )
        owner = _find_process_prefix_owner(Path(config["process_db"]), process_name_prefix)
        if owner:
            raise ValueError(
                f"命名前缀已被处理任务占用：{owner['dataset_key']}（前缀 {owner['process_name_prefix']}），"
                "请更换"
            )
        existing_row = lib.find_manifest_task(manifest_path, continent, country, city)
        if existing_row:
            return {
                "ok": True,
                "existed": True,
                "row_number": existing_row["row"],
                "row": {field: existing_row[field] for field in lib.MANIFEST_APPEND_FIELDS},
                "download_dir": existing_row["download_dir"],
            }
        result = lib.append_manifest_row(manifest_path, continent, country, city, process_name_prefix)
    return {"ok": True, "existed": False, **result}


def render_page(page_path: Path, refresh_seconds: int) -> bytes:
    if page_path.exists():
        html = page_path.read_text(encoding="utf-8")
    else:
        html = (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>GBA 状态</title><body><p>状态页面模板缺失，请检查 "
            f"{page_path}</p></body></html>"
        )
    html = html.replace("%%REFRESH_MS%%", str(max(refresh_seconds, 1) * 1000))
    return html.encode("utf-8")


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "GbaStatus/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        config = self.server.config
        if path in ("/", "/index.html"):
            self._send(200, render_page(config["page"], config["refresh_seconds"]), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            query = parse_qs(urlparse(self.path).query)
            queue_offset = parse_int_query(query, "queue_offset", 0, 0, 10_000_000)
            queue_limit = parse_int_query(query, "queue_limit", config["queue_limit"], 1, 500)
            process_recent_offset = parse_int_query(query, "process_recent_offset", 0, 0, 10_000_000)
            queue_search = (query.get("queue_search") or [""])[0]
            worker_lines = [
                str(item.get("command_line") or "")
                for item in detect_batch().get("workers", [])
            ]
            snapshot = build_snapshot(
                repo_root=config["repo_root"],
                state_db=config["state_db"],
                queue_limit=queue_limit,
                queue_offset=queue_offset,
                recent_limit=config["recent_limit"],
                stale_minutes=config["stale_minutes"],
                log_tail_lines=config["log_tail_lines"],
                boundaries_dir=config["boundaries_dir"],
                worker_command_lines=worker_lines,
                queue_search=queue_search,
            )
            snapshot["processing"] = build_processing_snapshot(
                repo_root=config["repo_root"],
                process_db=config["process_db"],
                stale_minutes=config["stale_minutes"],
                log_tail_lines=config["log_tail_lines"],
                recent_limit=config["recent_limit"],
                recent_offset=process_recent_offset,
            )
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/control":
            body = json.dumps(build_control_snapshot(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/manifest-diff":
            body = json.dumps(manifest_diff(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/process/control":
            body = json.dumps(build_process_control_snapshot(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/process/merge-status":
            dataset_key = (parse_qs(urlparse(self.path).query).get("dataset_key") or [""])[0]
            body = json.dumps(build_merge_status(dataset_key), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/config":
            snapshot = load_dashboard_config(config["repo_root"])
            snapshot["env"] = build_env_snapshot(config)
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/env":
            body = json.dumps(check_environment(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/logs":
            body = json.dumps(list_log_files(config["repo_root"], config["process_db"]), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/log":
            query = parse_qs(urlparse(self.path).query)
            rel_path = (query.get("path") or [""])[0]
            lines = parse_int_query(query, "lines", 200, 1, 5000)
            try:
                payload = read_log_file(config["repo_root"], rel_path, lines)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/db_updated/import-status":
            dataset_key = (parse_qs(urlparse(self.path).query).get("dataset_key") or [""])[0]
            body = json.dumps(build_db_import_status(config["repo_root"], dataset_key), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/db_updated/copy-status":
            dataset_key = (parse_qs(urlparse(self.path).query).get("dataset_key") or [""])[0]
            body = json.dumps(build_copy_status(dataset_key), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/db_updated/not-downloaded":
            query = parse_qs(urlparse(self.path).query)
            continent = (query.get("continent") or [""])[0]
            try:
                available_keys = load_available_dataset_keys(
                    process_db=config["process_db"],
                    marks_file=config["db_update_file"],
                )
                payload = load_not_downloaded_tasks(
                    repo_root=config["repo_root"],
                    state_db=config["state_db"],
                    continent=continent,
                    available_keys=available_keys,
                )
                status, body = 200, payload
            except ValueError as exc:
                status, body = 400, {"ok": False, "error": str(exc)}
            except (OSError, RuntimeError) as exc:
                status, body = 500, {"ok": False, "error": str(exc)}
            self._send(
                status,
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        if path == "/api/db_updated":
            snapshot = build_db_update_snapshot(
                repo_root=config["repo_root"],
                process_db=config["process_db"],
                marks_file=config["db_update_file"],
                state_db=config["state_db"],
            )
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        config = self.server.config
        if path not in (
            "/api/restart",
            "/api/stop",
            "/api/queue/reorder",
            "/api/queue/requeue",
            "/api/queue/requeue-completed",
            "/api/queue/stop-current",
            "/api/process/reorder",
            "/api/process/requeue",
            "/api/process/requeue-completed",
            "/api/process/note",
            "/api/process/sync",
            "/api/process/start",
            "/api/process/stop",
            "/api/process/merge-parts",
            "/api/process/redownload",
            "/api/open-folder",
            "/api/sync-manifest",
            "/api/manifest/check",
            "/api/manifest/append",
            "/api/db_updated",
            "/api/db_updated/import",
            "/api/db_updated/copy-to-share",
            "/api/config",
        ):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        token = config.get("action_token")
        if token and self.headers.get("X-Action-Token") != token:
            self._send(403, b'{"ok": false, "error": "invalid action token"}', "application/json; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw) if raw.strip() else {}
        except (ValueError, OSError):
            self._send(400, b'{"ok": false, "error": "invalid json body"}', "application/json; charset=utf-8")
            return

        if path == "/api/db_updated/import":
            try:
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not dataset_key:
                    raise ValueError("dataset_key 不能为空")
                result = start_db_import_job(config, dataset_key)
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/db_updated/copy-to-share":
            try:
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not dataset_key:
                    raise ValueError("dataset_key 不能为空")
                result = copy_final_to_share(config, dataset_key)
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/config":
            try:
                with CONTROL_LOCK:
                    saved = save_dashboard_config(config["repo_root"], payload)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "error": f"写入失败: {exc}"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "config": saved}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/db_updated":
            rows = payload.get("rows")
            if not isinstance(rows, list):
                self._send(
                    400,
                    json.dumps({"ok": False, "error": "rows 必须是数组"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            try:
                with CONTROL_LOCK:
                    saved = save_db_update_marks(config["db_update_file"], rows)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "error": f"写入失败: {exc}"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "saved": saved}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/stop":
            try:
                result = stop_batch_action(config)
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/queue/stop-current":
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": "task_id 不能为空"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            try:
                result = stop_current_task_action(config, task_id)
            except KeyError as exc:
                self._send(
                    404,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except RuntimeError as exc:
                self._send(
                    409,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/queue/reorder":
            try:
                task_id, direction = normalize_reorder_payload(
                    {
                        "dataset_key": payload.get("task_id") or payload.get("dataset_key"),
                        "direction": payload.get("direction"),
                    }
                )
                queue = lib.move_queue_task(config["state_db"], task_id, direction)
            except (KeyError, ValueError) as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "direction": direction, "queue": queue}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/queue/requeue":
            try:
                task_id, position = normalize_requeue_payload(payload)
                queue = lib.requeue_task(config["state_db"], task_id, position)
            except KeyError as exc:
                self._send(
                    404,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(
                {"ok": True, "task_id": task_id, "position": position, "queue": queue, "pending_total": len(queue)},
                ensure_ascii=False,
            )
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/queue/requeue-completed":
            try:
                task_id = str(payload.get("task_id") or payload.get("dataset_key") or "").strip()
                if not task_id:
                    raise ValueError("task_id 不能为空")
                if payload.get("confirm_delete") is not True:
                    raise ValueError("必须确认删除下载数据")
                expected_download_dir = str(payload.get("expected_download_dir") or "").strip()
                if not expected_download_dir:
                    raise ValueError("缺少已确认的下载目录，请刷新页面后重试")
                with CONTROL_LOCK:
                    result = lib.requeue_completed_download(
                        Path(config["repo_root"]), Path(config["state_db"]),
                        task_id, expected_download_dir,
                    )
                    downstream_error = None
                    try:
                        downstream_error = process_queue.reset_task_after_download_redo(
                            Path(config["repo_root"]), Path(config["process_db"]),
                            Path(config["state_db"]), task_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        downstream_error = f"下游处理任务重置失败：{exc}"
                    mark_error = None
                    try:
                        delete_db_update_mark(Path(config["db_update_file"]), task_id)
                    except Exception as exc:  # noqa: BLE001
                        mark_error = f"删除数据库更新记录失败：{exc}"
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                errors = [
                    item
                    for item in (result.get("cleanup_error"), downstream_error, mark_error)
                    if item
                ]
                body = json.dumps({
                    "ok": not errors,
                    "task_id": task_id,
                    "queue": result["queue"],
                    "pending_total": len(result["queue"]),
                    "deleted_dir": result.get("deleted_dir"),
                    "error": "；".join(errors) if errors else None,
                }, ensure_ascii=False)
                self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/process/reorder":
            try:
                dataset_key, direction = normalize_reorder_payload(payload)
                queue = process_queue.move_queue_task(config["process_db"], dataset_key, direction)
            except (KeyError, ValueError) as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "direction": direction, "queue": queue}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/process/requeue":
            try:
                dataset_key, position = normalize_requeue_payload(payload)
                queue = process_queue.requeue_task(config["process_db"], dataset_key, position)
            except KeyError as exc:
                self._send(
                    404,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(
                {
                    "ok": True,
                    "dataset_key": dataset_key,
                    "position": position,
                    "queue": queue,
                    "pending_total": len(queue),
                },
                ensure_ascii=False,
            )
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/process/requeue-completed":
            try:
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not dataset_key:
                    raise ValueError("dataset_key 不能为空")
                if payload.get("confirm_delete") is not True:
                    raise ValueError("必须确认删除处理产物")
                expected_output_dir = str(payload.get("expected_output_dir") or "")
                expected_input_dir = str(payload.get("expected_input_dir") or "")
                if not expected_output_dir or not expected_input_dir:
                    raise ValueError("缺少已确认的删除目录，请刷新页面后重试")
                with CONTROL_LOCK:
                    result = process_queue.requeue_completed_task(
                        Path(config["repo_root"]), Path(config["process_db"]),
                        Path(config["state_db"]), dataset_key,
                        expected_output_dir, expected_input_dir,
                    )
                    mark_error = None
                    try:
                        reset_db_update_mark(Path(config["db_update_file"]), dataset_key)
                    except Exception as exc:  # noqa: BLE001
                        mark_error = f"重置数据库更新标记失败：{exc}"
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                errors = []
                if result.get("cleanup_error"):
                    errors.append(f"旧产物清理未完成：{result['cleanup_error']}")
                if mark_error:
                    errors.append(mark_error)
                body = json.dumps({
                    "ok": not errors,
                    "dataset_key": dataset_key,
                    "queue": result["queue"],
                    "pending_total": len(result["queue"]),
                    "error": "；".join(errors) if errors else None,
                }, ensure_ascii=False)
                self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/process/redownload":
            try:
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not dataset_key:
                    raise ValueError("dataset_key 不能为空")
                if payload.get("confirm_delete") is not True:
                    raise ValueError("必须确认删除原始下载数据")
                expected_download_dir = str(payload.get("expected_download_dir") or "").strip()
                store = process_queue.ProcessStore(Path(config["process_db"]))
                try:
                    row = store.get(dataset_key)
                finally:
                    store.close()
                if row is None:
                    raise KeyError(f"未找到处理任务: {dataset_key}")
                task_id = str(row.get("task_id") or "").strip()
                if not task_id:
                    raise ValueError("该任务无对应下载任务，无法重新下载")
                with CONTROL_LOCK:
                    result = lib.requeue_completed_download(
                        Path(config["repo_root"]), Path(config["state_db"]),
                        task_id, expected_download_dir or None,
                    )
                    downstream_error = None
                    try:
                        downstream_error = process_queue.reset_task_after_download_redo(
                            Path(config["repo_root"]), Path(config["process_db"]),
                            Path(config["state_db"]), dataset_key,
                        )
                    except Exception as exc:  # noqa: BLE001
                        downstream_error = f"处理队列重置失败：{exc}"
                    mark_error = None
                    try:
                        delete_db_update_mark(Path(config["db_update_file"]), dataset_key)
                    except Exception as exc:  # noqa: BLE001
                        mark_error = f"删除数据库更新记录失败：{exc}"
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                errors = [
                    item
                    for item in (result.get("cleanup_error"), downstream_error, mark_error)
                    if item
                ]
                body = json.dumps({
                    "ok": not errors,
                    "dataset_key": dataset_key,
                    "task_id": task_id,
                    "queue": result["queue"],
                    "pending_total": len(result["queue"]),
                    "redownload_count": result.get("redownload_count"),
                    "deleted_dir": result.get("deleted_dir"),
                    "error": "；".join(errors) if errors else None,
                }, ensure_ascii=False)
                self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/process/merge-parts":
            try:
                dataset_key = str(payload.get("dataset_key") or "").strip()
                if not dataset_key:
                    raise ValueError("dataset_key 不能为空")
                if payload.get("confirm_delete") is not True:
                    raise ValueError("必须确认删除原分卷")
                expected_source_dir = str(payload.get("expected_source_dir") or "")
                result = start_merge_job(config, dataset_key, expected_source_dir)
            except KeyError as exc:
                status, error = 404, str(exc)
            except ValueError as exc:
                status, error = 400, str(exc)
            except Exception as exc:  # noqa: BLE001
                status, error = 500, str(exc)
            else:
                body = json.dumps(result, ensure_ascii=False)
                self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(status, json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/process/note":
            try:
                dataset_key, note = normalize_note_payload(payload)
                saved = set_process_note(config, dataset_key, note)
            except KeyError as exc:
                self._send(
                    404,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "dataset_key": dataset_key, "note": saved}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/process/sync":
            try:
                result = sync_process_queue(config)
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, **result}, ensure_ascii=False)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/process/start":
            try:
                raw_limit = payload.get("limit")
                if raw_limit is None or str(raw_limit).strip() == "":
                    # 未显式传 limit 时使用「配置」页的「处理默认数量」（0=全部）
                    limit = load_dashboard_config(config["repo_root"])["process"]["default_limit"]
                else:
                    limit = normalize_process_limit(raw_limit)
                wait_resources = normalize_wait_resources(payload.get("wait_resources"))
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            try:
                result = start_process_action(config, limit, wait_resources)
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            status = 200 if result.get("ok") else 409
            self._send(status, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/process/stop":
            try:
                result = stop_process_action(config)
            except RuntimeError as exc:
                self._send(
                    409,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        if path == "/api/open-folder":
            try:
                rel_path = str(payload.get("path") or "").strip()
                opened = open_local_folder(config["repo_root"], rel_path)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except OSError as exc:
                self._send(
                    500,
                    json.dumps({"ok": False, "error": f"打开失败: {exc}"}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps({"ok": True, "path": opened}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if path == "/api/sync-manifest":
            try:
                result = sync_manifest_to_state(config)
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except RuntimeError as exc:
                self._send(
                    409,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if path == "/api/manifest/check":
            try:
                result = build_manifest_check(config, str(payload.get("keyword") or ""))
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        if path == "/api/manifest/append":
            try:
                result = append_manifest_task(
                    config,
                    str(payload.get("continent") or ""),
                    str(payload.get("country") or ""),
                    str(payload.get("city") or ""),
                    str(payload.get("process_name_prefix") or ""),
                )
            except PermissionError:
                self._send(
                    409,
                    json.dumps(
                        {"ok": False, "error": "任务清单文件被占用，请关闭编辑器（Excel / WPS）后重试"},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except ValueError as exc:
                self._send(
                    400,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        try:
            result = restart_batch(config, DEFAULT_TILE_WORKERS)
        except Exception as exc:  # noqa: BLE001
            self._send(
                500,
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def ensure_log_dirs(repo_root: Path) -> None:
    for sub in ("download", "download/province", "process", "update", "server", "legacy"):
        (Path(repo_root) / DEFAULT_LOG_ROOT / sub).mkdir(parents=True, exist_ok=True)


def create_server(args: argparse.Namespace, repo_root: Path) -> ThreadingHTTPServer:
    state_db = Path(args.state_db)
    if not state_db.is_absolute():
        state_db = repo_root / state_db
    process_db = Path(getattr(args, "process_db", DEFAULT_PROCESS_DB) or DEFAULT_PROCESS_DB)
    if not process_db.is_absolute():
        process_db = repo_root / process_db
    db_update_file = Path(getattr(args, "db_update_file", DEFAULT_DB_UPDATE_FILE) or DEFAULT_DB_UPDATE_FILE)
    if not db_update_file.is_absolute():
        db_update_file = repo_root / db_update_file
    page = Path(args.page)
    if not page.is_absolute():
        page = repo_root / page
    boundaries_dir = Path(args.boundaries_dir)
    if not boundaries_dir.is_absolute():
        boundaries_dir = repo_root / boundaries_dir

    server = ThreadingHTTPServer((args.host, args.port), StatusHandler)
    server.config = {
        "repo_root": repo_root,
        "state_db": state_db,
        "process_db": process_db,
        "db_update_file": db_update_file,
        "page": page,
        "boundaries_dir": boundaries_dir,
        "python_exe": args.python_exe,
        "process_python_exe": getattr(args, "process_python_exe", DEFAULT_PROCESS_PYTHON) or DEFAULT_PROCESS_PYTHON,
        "action_token": args.action_token or "",
        "merge_engine": getattr(args, "merge_engine", process_queue.MERGE_ENGINE_OGR2OGR),
        "ogr2ogr": getattr(args, "ogr2ogr", "") or "",
        "queue_limit": args.queue_limit,
        "recent_limit": args.recent_limit,
        "stale_minutes": args.log_stale_minutes,
        "log_tail_lines": args.log_tail_lines,
        "refresh_seconds": args.refresh_seconds,
    }
    return server


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd().resolve()
    ensure_log_dirs(repo_root)
    server = create_server(args, repo_root)
    print(f"状态看板已启动: http://127.0.0.1:{args.port}/  (监听 {args.host}:{args.port})")
    print(f"仓库根目录: {repo_root}")
    print(f"状态库: {server.config['state_db']}")
    print(f"处理队列库: {server.config['process_db']}")
    print(f"数据库已更新看板数据: {server.config['db_update_file']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("状态看板已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
