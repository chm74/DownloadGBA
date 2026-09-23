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
DEFAULT_PROCESS_DB = "data/shp_process_state.db"
DEFAULT_DB_UPDATE_FILE = "data/db_update_status.json"
DEFAULT_PAGE = "scripts/status_page.html"
DEFAULT_BOUNDARIES_DIR = "data/boundaries"
DEFAULT_RUN_LOG = "data/world_tasks_run.log"
DEFAULT_RUN_ERR_LOG = "data/world_tasks_run.err.log"
DEFAULTS_FILE = "data/status_defaults.json"
RUNNER_MARKER = "run_world_building_tasks.py"
WORKER_MARKER = "download_gba_lod1_wfs"
PROCESS_RUNNER_MARKER = "run_shp_process_tasks.py"
PROCESS_WORKER_MARKER = "pipeline.py"
PROCESS_WORKER_DIR = "Oneshp_pipline_qgis"
DEFAULT_PROCESS_PYTHON = "Tools/Oneshp_pipline_qgis/.venv/Scripts/python.exe"
DEFAULT_PROCESS_LOG = "data/process_run.log"
DEFAULT_PROCESS_ERR_LOG = "data/process_run.err.log"
MIN_TILE_WORKERS = 1
MAX_TILE_WORKERS = 6
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
    parser.add_argument("--repo-root", default=None, help="仓库根目录，默认脚本上级目录。")
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
        for row in pending_rows:
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
            decorated_rows.append(item)

        snapshot["queue_total"] = len(decorated_rows)
        page_start = snapshot["queue_offset"]
        page_end = page_start + snapshot["queue_limit"]
        queue_items = []
        for index, row in enumerate(decorated_rows[page_start:page_end], start=1):
            queue_items.append(
                {
                    "order": page_start + index,
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


def build_processing_snapshot(
    repo_root: Path,
    process_db: Path,
    stale_minutes: float = DEFAULT_STALE_MINUTES,
    log_tail_lines: int = 3,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
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
        snapshot["queue"] = [
            {
                "order": index,
                "queue_order": _row_optional(row, "queue_order"),
                "dataset_key": row["dataset_key"],
                "task_id": row["task_id"],
                "display_name": row["display_name"] or row["dataset_key"],
                "process_name_prefix": _row_optional(row, "process_name_prefix"),
                "source_dir": row["source_dir"],
                "feature_count": row["feature_count"],
                "shp_count": _count_json_list(row["shp_files"]),
                "note": _row_optional(row, "note") or "",
                "updated_at": row["updated_at"],
            }
            for index, row in enumerate(queue_rows, start=1)
        ]

        snapshot["recent_done"] = [
            {
                "dataset_key": row["dataset_key"],
                "task_id": row["task_id"],
                "process_name_prefix": _row_optional(row, "process_name_prefix"),
                "tile_count": row["tile_count"],
                "feature_count": row["feature_count"],
                "duration_seconds": row["duration_seconds"],
                "finished_at": row["finished_at"],
                "output_dir": row["output_dir"],
            }
            for row in connection.execute(
                "SELECT * FROM process_tasks WHERE status = ? ORDER BY finished_at DESC LIMIT ?",
                ("OK", max(recent_limit, 0)),
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


def build_db_update_snapshot(repo_root: Path, process_db: Path, marks_file: Path) -> dict:
    repo_root = Path(repo_root)
    process_db = Path(process_db)
    marks_file = Path(marks_file)
    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "process_db": str(process_db),
        "marks_file": str(marks_file),
        "error": None,
        "counts": {"total": 0, "updated": 0, "pending": 0},
        "rows": [],
    }
    if not process_db.exists():
        snapshot["error"] = (
            f"处理状态库不存在: {process_db}；先运行 python scripts/run_shp_process_tasks.py --sync"
        )
        return snapshot
    marks = load_db_update_marks(marks_file)
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

    rows = []
    for row in records:
        key = row["dataset_key"]
        mark = marks.get(key, {})
        rows.append(
            {
                "dataset_key": key,
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
            }
        )
    snapshot["rows"] = rows
    snapshot["counts"] = {
        "total": len(rows),
        "updated": sum(1 for item in rows if item["updated"]),
        "pending": sum(1 for item in rows if not item["updated"]),
    }
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


def normalize_tile_workers(value: object) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("tile_workers 必须是整数") from exc
    if not (MIN_TILE_WORKERS <= workers <= MAX_TILE_WORKERS):
        raise ValueError(f"tile_workers 必须在 {MIN_TILE_WORKERS}~{MAX_TILE_WORKERS} 之间")
    return workers


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


def start_process_batch(repo_root: Path, command: list[str]) -> dict:
    log_path = Path(repo_root) / DEFAULT_PROCESS_LOG
    error_log_path = Path(repo_root) / DEFAULT_PROCESS_ERR_LOG
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
        python_exe = resolve_process_python(config)
        command = build_process_start_command(python_exe, config["repo_root"], limit, wait_resources)
        started = start_process_batch(config["repo_root"], command)
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


def _save_defaults(repo_root: Path, tile_workers: int) -> None:
    path = Path(repo_root) / DEFAULTS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tile_workers": tile_workers}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_defaults(repo_root: Path) -> dict:
    path = Path(repo_root) / DEFAULTS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_control_snapshot(config: dict) -> dict:
    detected = detect_batch()
    defaults = load_defaults(config["repo_root"])
    current = detected["tile_workers"] or int(defaults.get("tile_workers") or 2)
    return {
        "running": detected["runner"] is not None,
        "runner_pid": detected["runner"]["pid"] if detected["runner"] else None,
        "runner_count": detected.get("runner_count", 1 if detected["runner"] else 0),
        "worker_pids": [item["pid"] for item in detected["workers"]],
        "tile_workers": current,
        "min_tile_workers": MIN_TILE_WORKERS,
        "max_tile_workers": MAX_TILE_WORKERS,
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
        _save_defaults(config["repo_root"], tile_workers)
        return {
            "ok": True,
            "tile_workers": tile_workers,
            "stopped_pids": stopped,
            "runner_pid": started["pid"],
            "command": command,
        }


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
            )
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/control":
            body = json.dumps(build_control_snapshot(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/process/control":
            body = json.dumps(build_process_control_snapshot(config), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/db_updated":
            snapshot = build_db_update_snapshot(
                repo_root=config["repo_root"],
                process_db=config["process_db"],
                marks_file=config["db_update_file"],
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
            "/api/process/reorder",
            "/api/process/requeue",
            "/api/process/note",
            "/api/process/sync",
            "/api/process/start",
            "/api/process/stop",
            "/api/db_updated",
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
                limit = normalize_process_limit(payload.get("limit"))
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
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        try:
            tile_workers = normalize_tile_workers(payload.get("tile_workers"))
        except ValueError as exc:
            self._send(
                400,
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        try:
            result = restart_batch(config, tile_workers)
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
        "queue_limit": args.queue_limit,
        "recent_limit": args.recent_limit,
        "stale_minutes": args.log_stale_minutes,
        "log_tail_lines": args.log_tail_lines,
        "refresh_seconds": args.refresh_seconds,
    }
    return server


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    server = create_server(args, repo_root)
    print(f"状态看板已启动: http://127.0.0.1:{args.port}/  (监听 {args.host}:{args.port})")
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
