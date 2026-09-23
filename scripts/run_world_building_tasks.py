import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world_tasks_lib as lib

WORKER_SCRIPTS = {
    "adaptive": "download_gba_lod1_wfs_adaptive.py",
    "standard": "download_gba_lod1_wfs.py",
}


def split_tokens(value: str) -> list[str]:
    return [token.strip() for token in (value or "").split(",") if token.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按任务清单批量执行 GBA WFS 建筑下载，状态保存到 SQLite。",
    )
    parser.add_argument("--tasks", default="data/world_building_download_tasks.csv", help="任务清单 CSV 路径。")
    parser.add_argument(
        "--state-db",
        default="data/world_building_download_tasks_state.db",
        help="SQLite 任务状态库路径。",
    )
    parser.add_argument(
        "--overrides",
        default="data/world_place_overrides.csv",
        help="地名/范围覆盖表路径，可选。",
    )
    parser.add_argument(
        "--boundaries-dir",
        default="data/boundaries",
        help="本地行政边界目录（ne_admin0.gpkg / ne_admin1.gpkg），用于替代在线地名解析。",
    )
    parser.add_argument("--repo-root", default=None, help="仓库根目录，默认取脚本上级目录。")
    parser.add_argument("--python-exe", default=sys.executable, help="调用下载脚本使用的 Python 解释器。")
    parser.add_argument("--engine", choices=["adaptive", "standard"], default="adaptive", help="下载引擎，默认 adaptive。")
    parser.add_argument("--grid-size", type=float, default=0.5, help="格网大小（度）。")
    parser.add_argument("--page-size", type=int, default=5000, help="WFS 分页大小。")
    parser.add_argument("--tile-format", choices=["gpkg", "shp"], default="gpkg", help="中间分块格式。")
    parser.add_argument("--merge-format", choices=["gpkg", "shp"], default="gpkg", help="最终合并格式。")
    parser.add_argument("--max-pages", type=int, default=0, help="冒烟测试用的分页上限，0 表示不限制。")
    parser.add_argument(
        "--tile-workers",
        type=int,
        default=1,
        help="单任务内并发抓取的格网数，默认 1；建议试点 2，谨慎提高。",
    )
    parser.add_argument("--split-threshold", type=int, default=20000, help="自适应切分阈值。")
    parser.add_argument("--failure-split-retries", type=int, default=5, help="失败切分前的重试次数。")
    parser.add_argument("--min-grid-size", type=float, default=0.05, help="自适应最小格网。")
    parser.add_argument("--probe-page-size", type=int, default=1, help="探测 numberMatched 的页大小。")
    parser.add_argument("--only", default="", help="按 task_id 子串过滤，逗号分隔。")
    parser.add_argument("--continent", default="", help="按大洲过滤，逗号分隔。")
    parser.add_argument("--country", default="", help="按国家过滤，逗号分隔。")
    parser.add_argument(
        "--status",
        default="",
        help="显式指定要执行的状态，逗号分隔，例如 pending,failed。",
    )
    parser.add_argument("--retry-failed", action="store_true", help="在默认范围上追加 FAILED 任务。")
    parser.add_argument("--force", action="store_true", help="忽略状态与完成标记，强制重跑选中任务。")
    parser.add_argument("--max-tasks", type=int, default=0, help="最多执行多少个任务，0 表示不限制。")
    parser.add_argument("--sleep-seconds", type=float, default=2.0, help="任务之间休眠秒数。")
    parser.add_argument("--task-attempts", type=int, default=1, help="单个任务最多尝试次数。")
    parser.add_argument("--refresh-boundary", action="store_true", help="忽略边界缓存，重新解析范围。")
    parser.add_argument("--hash", action="store_true", help="为合并产物计算 sha256 写入标记。")
    parser.add_argument(
        "--shp-export",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="下载完成后自动导出 SHP（默认开启，超大任务自动分卷）。",
    )
    parser.add_argument(
        "--shp-max-features",
        type=int,
        default=1000000,
        help="单个 SHP 分卷的最大要素数，默认 1000000。",
    )
    parser.add_argument(
        "--export-shp-only",
        action="store_true",
        help="只为已完成任务补导出 SHP 后退出，不重新下载。",
    )
    parser.add_argument(
        "--process-sync",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="任务下载成功后自动登记到数据处理队列（默认开启，可用 --no-process-sync 关闭）。",
    )
    parser.add_argument(
        "--process-db",
        default="data/shp_process_state.db",
        help="数据处理队列状态库路径，用于下载完成后的自动登记。",
    )
    parser.add_argument("--no-verify-ok", action="store_true", help="跳过对 OK 任务完成标记的一致性校验。")
    parser.add_argument("--stop-on-error", action="store_true", help="遇到失败任务立即停止。")
    parser.add_argument("--dry-run", action="store_true", help="只打印执行计划，不解析范围、不下载、不改状态。")
    parser.add_argument("--sync-only", action="store_true", help="只把清单同步进状态库后退出。")
    parser.add_argument("--summary", action="store_true", help="打印状态库统计后退出。")
    parser.add_argument("--running", action="store_true", help="打印当前 RUNNING 任务及分块进度后退出。")
    parser.add_argument("--reset-task", default="", help="把指定 task_id 重置为 PENDING 后退出。")
    parser.add_argument("--move-up", default="", help="把指定任务（task_id 或子串，逗号分隔）在待处理队列中上移一位。")
    parser.add_argument("--move-down", default="", help="把指定任务在待处理队列中下移一位。")
    parser.add_argument("--move-top", default="", help="把指定任务置顶（跨页生效，逗号分隔多个时按输入顺序排列）。")
    return parser.parse_args()


def build_options(args: argparse.Namespace) -> lib.DownloadOptions:
    return lib.DownloadOptions(
        engine=args.engine,
        grid_size=args.grid_size,
        page_size=args.page_size,
        tile_format=args.tile_format,
        merge_format=args.merge_format,
        max_pages=args.max_pages,
        tile_workers=max(int(getattr(args, "tile_workers", 1) or 1), 1),
        split_threshold=args.split_threshold,
        failure_split_retries=args.failure_split_retries,
        min_grid_size=args.min_grid_size,
        probe_page_size=args.probe_page_size,
    )


def row_to_task(row: dict) -> lib.Task:
    return lib.Task(
        continent=row["continent"],
        country=row["country"],
        city=row["city"],
        download_dir=row["download_dir"],
    )


def append_task_log(task_dir: Path, message: str) -> None:
    try:
        with (Path(task_dir) / "run.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[RUNNER] {datetime.now().strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def export_shp_parts(
    merge_output: Path,
    task_dir: Path,
    slug: str,
    max_features: int,
    log: Callable[[str], None] | None = None,
) -> list[Path]:
    import pyogrio

    merge_output = Path(merge_output)
    task_dir = Path(task_dir)
    if not merge_output.exists():
        return []
    total = int(pyogrio.read_info(str(merge_output)).get("features") or 0)
    if total <= 0:
        return []

    limit = max(int(max_features), 1)
    if total <= limit:
        ranges = [(0, total, "")]
    else:
        ranges = []
        offset = 0
        index = 1
        while offset < total:
            count = min(limit, total - offset)
            ranges.append((offset, count, f"_part{index}"))
            offset += count
            index += 1

    if log is not None:
        log(f"[SHP] 开始导出: 要素={total:,}，分卷={len(ranges)}")

    parts: list[Path] = []
    for index, (offset, count, suffix) in enumerate(ranges, start=1):
        gdf = pyogrio.read_dataframe(str(merge_output), skip_features=offset, max_features=count)
        target = task_dir / f"{slug}_buildings_height_gba{suffix}.shp"
        for extension in (".shx", ".dbf", ".prj", ".cpg", ".fix", ".qix"):
            sidecar = target.with_suffix(extension)
            sidecar.unlink(missing_ok=True)
        gdf.to_file(target, driver="ESRI Shapefile", encoding="utf-8")
        parts.append(target)
        if log is not None:
            log(f"[SHP] 已写出 {index}/{len(ranges)} 卷，要素={len(gdf):,} -> {target.name}")
    if log is not None:
        log(f"[SHP] 导出完成: {len(parts)} 个分卷")
    return parts


def backfill_shp_export(
    repo_root: Path,
    store: lib.StateStore,
    options: lib.DownloadOptions,
    args: argparse.Namespace,
) -> list[str]:
    updated: list[str] = []
    for row in store.all_tasks():
        if row["status"] not in lib.SUCCESS_STATUSES:
            continue
        task = row_to_task(row)
        task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
        merge_output = lib.merge_output_path(task_dir, task, options.merge_format)
        if not merge_output.exists():
            continue
        parts = export_shp_parts(
            merge_output,
            task_dir,
            lib.generate_slug(task.city),
            args.shp_max_features,
            log=lambda message: append_task_log(task_dir, message),
        )
        if not parts:
            continue
        marker = lib.read_marker(task_dir) or {}
        marker["task_id"] = task.task_id
        marker["shp_outputs"] = [lib.repo_relative(repo_root, part) for part in parts]
        lib.write_marker(task_dir, marker)
        store.add_event(task.task_id, "SHP_EXPORT", f"parts={len(parts)}")
        print(f"[SHP] {task.task_id} -> {len(parts)} 个分卷")
        updated.append(task.task_id)
    return updated


def finish_failure(
    store: lib.StateStore,
    task_id: str,
    error: str,
    started_monotonic: float,
    boundary_source: str | None = None,
    exit_code: int | None = None,
) -> dict:
    duration = int(time.monotonic() - started_monotonic)
    store.set_status(
        task_id,
        lib.STATUS_FAILED,
        exit_code=exit_code,
        boundary_source=boundary_source,
        last_error=error,
        finished_at=lib.utc_now(),
        duration_seconds=duration,
    )
    store.add_event(task_id, "FAIL", error)
    print(f"[FAILED] {task_id}: {error}")
    return {"status": lib.STATUS_FAILED, "feature_count": None}


def register_task_in_process_queue(
    repo_root: Path,
    args: argparse.Namespace,
    task_id: str,
) -> None:
    if not getattr(args, "process_sync", True):
        return
    process_db = Path(getattr(args, "process_db", "") or "data/shp_process_state.db")
    if not process_db.is_absolute():
        process_db = repo_root / process_db
    state_db = Path(getattr(args, "state_db", "") or "data/world_building_download_tasks_state.db")
    if not state_db.is_absolute():
        state_db = repo_root / state_db
    tasks_path = Path(getattr(args, "tasks", "") or "data/world_building_download_tasks.csv")
    if not tasks_path.is_absolute():
        tasks_path = repo_root / tasks_path
    try:
        import run_shp_process_tasks as process_queue

        result = process_queue.sync_process_tasks(
            repo_root,
            process_db,
            state_db,
            repo_root / "data",
            [],
            [task_id],
            scan_enabled=False,
            manifest_path=tasks_path,
        )
        print(
            f"[PROCESS] 已登记处理队列: {task_id} "
            f"added={result['added']} updated={result['updated']}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 处理队列自动登记失败（不影响下载）: {exc}")


def execute_task(
    repo_root: Path,
    worker_script: Path,
    args: argparse.Namespace,
    store: lib.StateStore,
    overrides: list[lib.RangeOverride],
    options: lib.DownloadOptions,
    row: dict,
    boundaries_dir: Path | None = None,
) -> dict:
    task = row_to_task(row)
    task_id = task.task_id
    task_dir = lib.resolve_task_dir(repo_root, task.download_dir)
    task_dir.mkdir(parents=True, exist_ok=True)

    previous = store.get(task_id) or {}
    attempt = int(previous.get("attempts") or 0) + 1
    started_at = lib.utc_now()
    started_monotonic = time.monotonic()
    store.set_status(
        task_id,
        lib.STATUS_RUNNING,
        attempts=attempt,
        started_at=started_at,
        exit_code=None,
        finished_at=None,
        duration_seconds=None,
        last_error=None,
    )
    store.add_event(task_id, "START", f"attempt={attempt} engine={options.engine}")
    print(f"[RUN] {task_id} -> {task_dir}")

    try:
        boundary = lib.resolve_boundary(
            task,
            overrides,
            task_dir,
            repo_root,
            refresh=args.refresh_boundary,
            boundaries_dir=boundaries_dir,
        )
    except lib.BoundaryResolutionError as exc:
        return finish_failure(store, task_id, str(exc), started_monotonic)

    boundary_source = json.dumps(boundary.source, ensure_ascii=False)

    if boundary.skipped:
        duration = int(time.monotonic() - started_monotonic)
        store.set_status(
            task_id,
            lib.STATUS_SKIPPED,
            boundary_source=boundary_source,
            last_error=boundary.skip_reason,
            finished_at=lib.utc_now(),
            duration_seconds=duration,
        )
        store.add_event(task_id, "SKIP", boundary.skip_reason)
        print(f"[SKIP] {task_id}: {boundary.skip_reason}")
        return {"status": lib.STATUS_SKIPPED, "feature_count": None}

    merge_output = lib.merge_output_path(task_dir, task, options.merge_format)
    command = lib.build_worker_command(
        worker_script,
        args.python_exe,
        boundary.path,
        task_dir,
        merge_output,
        options,
    )

    log_path = task_dir / "run.log"
    error_log_path = task_dir / "run.err.log"
    header = f"\n=== {started_at} START {task_id} attempt={attempt} ==="
    with log_path.open("a", encoding="utf-8") as out_handle, error_log_path.open("a", encoding="utf-8") as err_handle:
        out_handle.write(header + "\n")
        out_handle.write(" ".join(command) + "\n")
        err_handle.write(header + "\n")
        result = subprocess.run(
            command,
            cwd=repo_root,
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            env=lib.child_process_env(),
        )

    duration = int(time.monotonic() - started_monotonic)
    finished_at = lib.utc_now()

    if result.returncode != 0:
        error = lib.tail_lines(error_log_path, 3) or f"worker 退出码 {result.returncode}"
        return finish_failure(
            store,
            task_id,
            error,
            started_monotonic,
            boundary_source=boundary_source,
            exit_code=result.returncode,
        )

    grid_progress = lib.read_national_grid_progress(task_dir)
    if grid_progress is not None and grid_progress["missing"] > 0:
        return finish_failure(
            store,
            task_id,
            (
                "标准格网完整性检查失败: "
                f"done={grid_progress['done']} expected={grid_progress['expected']} "
                f"missing={grid_progress['missing']}"
            ),
            started_monotonic,
            boundary_source=boundary_source,
            exit_code=0,
        )

    ok, feature_count, error = lib.verify_completion(merge_output)
    if not ok:
        return finish_failure(
            store,
            task_id,
            error,
            started_monotonic,
            boundary_source=boundary_source,
            exit_code=0,
        )

    status = lib.STATUS_OK if feature_count > 0 else lib.STATUS_OK_EMPTY
    merge_size = merge_output.stat().st_size
    shp_outputs: list[str] = []
    if getattr(args, "shp_export", True) and feature_count > 0:
        parts = export_shp_parts(
            merge_output,
            task_dir,
            lib.generate_slug(task.city),
            getattr(args, "shp_max_features", 1000000),
            log=lambda message: append_task_log(task_dir, message),
        )
        shp_outputs = [lib.repo_relative(repo_root, part) for part in parts]
        if shp_outputs:
            print(f"[SHP] {task_id} -> {len(shp_outputs)} 个分卷")
    marker = {
        "task_id": task_id,
        "continent": task.continent,
        "country": task.country,
        "city": task.city,
        "download_dir": lib.repo_relative(repo_root, task_dir),
        "status": status,
        "feature_count": feature_count,
        "merge_output": lib.repo_relative(repo_root, merge_output),
        "shp_outputs": shp_outputs,
        "merge_size_bytes": merge_size,
        "merge_mtime": datetime.fromtimestamp(merge_output.stat().st_mtime).isoformat(timespec="seconds"),
        "boundary_source": boundary.source,
        "params": lib.options_snapshot(options),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "runner_exit_code": 0,
    }
    if grid_progress is not None:
        marker.update(
            {
                "data_semantics_version": grid_progress["data_semantics_version"],
                "grid_algorithm": grid_progress["grid_algorithm"],
                "expected_tile_count": grid_progress["expected"],
                "complete_tile_count": grid_progress["complete"],
                "empty_tile_count": grid_progress["empty"],
                "missing_tile_count": grid_progress["missing"],
            }
        )
    if args.hash:
        marker["merge_sha256"] = lib.sha256_file(merge_output)
    lib.write_marker(task_dir, marker)

    store.set_status(
        task_id,
        status,
        exit_code=0,
        feature_count=feature_count,
        merge_output=lib.repo_relative(repo_root, merge_output),
        boundary_source=boundary_source,
        last_error=None,
        finished_at=finished_at,
        duration_seconds=duration,
    )
    raw_data_dir = lib.repo_relative(repo_root, task_dir)
    store.set_existing_data(task_id, raw_data_dir)
    store.add_event(task_id, "EXISTING_DATA", raw_data_dir)
    store.add_event(task_id, "DONE", f"status={status} features={feature_count}")
    if status == lib.STATUS_OK and shp_outputs:
        register_task_in_process_queue(repo_root, args, task_id)
    print(f"[{status}] {task_id} features={feature_count} elapsed={duration}s")
    return {"status": status, "feature_count": feature_count}


def verify_success_tasks(
    store: lib.StateStore,
    repo_root: Path,
    options: lib.DownloadOptions,
    dry_run: bool,
) -> list[str]:
    broken: list[str] = []
    for row in store.all_tasks():
        if row["status"] not in lib.SUCCESS_STATUSES:
            continue
        task = row_to_task(row)
        task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
        merge_output = lib.merge_output_path(task_dir, task, options.merge_format)
        marker = lib.read_marker(task_dir)
        if lib.marker_is_valid(marker, repo_root, task_dir, merge_output):
            continue
        broken.append(row["task_id"])
        if not dry_run:
            store.set_status(
                row["task_id"],
                lib.STATUS_PENDING,
                last_error="完成标记校验失败，待重跑",
            )
            store.add_event(row["task_id"], "VERIFY_FAIL", "标记缺失或与产物不一致")
    return broken


def describe_task(
    task: lib.Task,
    overrides: list[lib.RangeOverride],
    boundaries_dir: Path | None = None,
) -> str:
    override = lib.match_override(task, overrides)
    if override is not None:
        detail = override.value or override.note or ""
        return f"override={override.kind}:{detail}"
    if boundaries_dir is not None:
        local_match = lib.match_local_boundary(task, boundaries_dir)
        if local_match is not None:
            return f"local={local_match.get('level', '')}:{local_match.get('matched_name', '')}"
    return "geocode=" + json.dumps(lib.query_candidates(task), ensure_ascii=False)


def print_plan(
    repo_root: Path,
    worker_script: Path,
    rows: list[dict],
    overrides: list[lib.RangeOverride],
    options: lib.DownloadOptions,
    boundaries_dir: Path | None = None,
) -> None:
    for row in rows:
        task = row_to_task(row)
        task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
        merge_output = lib.merge_output_path(task_dir, task, options.merge_format)
        boundary_path = task_dir / lib.BOUNDARY_NAME
        command = lib.build_worker_command(
            worker_script,
            "python",
            boundary_path,
            task_dir,
            merge_output,
            options,
        )
        print(f"[PLAN] {task.task_id}")
        print(f"        dir={task_dir}")
        print(f"        range={describe_task(task, overrides, boundaries_dir)}")
        print("        " + " ".join(command))


def resolve_status_filter(args: argparse.Namespace) -> list[str]:
    if args.status:
        return split_tokens(args.status)
    if args.force:
        return []
    statuses = [lib.STATUS_PENDING, lib.STATUS_RUNNING]
    if args.retry_failed:
        statuses.append(lib.STATUS_FAILED)
    return statuses


def print_counts(store: lib.StateStore) -> None:
    counts = store.counts()
    if not counts:
        print("[STATE] 状态库为空")
        return
    ordered = [f"{status}={counts[status]}" for status in lib.ALL_STATUSES if status in counts]
    print("[STATE] " + ", ".join(ordered))


def print_running_tasks(store: lib.StateStore, repo_root: Path) -> None:
    rows = [row for row in store.all_tasks() if row["status"] == lib.STATUS_RUNNING]
    if not rows:
        print("[RUNNING] 当前没有正在执行的任务")
        return
    for row in rows:
        task_dir = lib.resolve_task_dir(repo_root, row["download_dir"])
        national_progress = lib.read_national_grid_progress(task_dir)
        if national_progress is not None:
            tile_count = national_progress["done"]
            total = national_progress["expected"]
            progress = f" tiles={tile_count}/{total} ({tile_count / total * 100:.1f}%)" if total else " tiles=0/0"
        else:
            tile_count = sum(1 for path in task_dir.glob("GBA_*.gpkg") if path.name.startswith("GBA_"))
            progress = f" tiles={tile_count}"
            grid_path = task_dir / "gba_wfs_grid.gpkg"
        if national_progress is None and grid_path.exists():
            try:
                import pyogrio

                total = int(pyogrio.read_info(str(grid_path)).get("features") or 0)
                if total > 0:
                    progress = f" tiles={tile_count}/{total} ({tile_count / total * 100:.1f}%)"
            except Exception:
                pass
        print(
            f"[RUNNING] {row['task_id']} attempts={row['attempts']} "
            f"started_at={row['started_at']} dir={task_dir}{progress}"
        )


def resolve_pending_task(store: lib.StateStore, token: str) -> str:
    pending = [row["task_id"] for row in store.pending_tasks()]
    if token in pending:
        return token
    lowered = token.lower()
    matches = [task_id for task_id in pending if lowered in task_id.lower()]
    if not matches:
        raise KeyError(f"待处理队列中没有匹配的任务: {token}")
    if len(matches) > 1:
        raise KeyError(f"匹配到多个任务: {token} -> {matches[:3]}")
    return matches[0]


def handle_move_requests(args: argparse.Namespace, store: lib.StateStore) -> bool:
    move_requests = ((args.move_up, "up"), (args.move_down, "down"), (args.move_top, "top"))
    if not any(value for value, _ in move_requests):
        return False

    last_queue: list[str] = []
    for value, direction in move_requests:
        tokens = split_tokens(value)
        if direction == "top":
            tokens = list(reversed(tokens))
        for token in tokens:
            try:
                task_id = resolve_pending_task(store, token)
                last_queue = store.move_task(task_id, direction)
            except (KeyError, ValueError) as exc:
                print(f"[MOVE-{direction.upper()}] {token} 失败: {exc}")
                continue
            print(f"[MOVE-{direction.upper()}] {task_id} -> 第 {last_queue.index(task_id) + 1} 位")

    if not last_queue:
        last_queue = [row["task_id"] for row in store.pending_tasks()]
    preview = ", ".join(last_queue[:10])
    print(f"[QUEUE] 共 {len(last_queue)} 个待执行；前 10：{preview}")
    return True


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = repo_root / tasks_path
    state_db_path = Path(args.state_db)
    if not state_db_path.is_absolute():
        state_db_path = repo_root / state_db_path
    overrides_path = Path(args.overrides)
    if not overrides_path.is_absolute():
        overrides_path = repo_root / overrides_path
    boundaries_path = Path(args.boundaries_dir)
    if not boundaries_path.is_absolute():
        boundaries_path = repo_root / boundaries_path
    boundaries_dir = boundaries_path if (boundaries_path / "ne_admin0.gpkg").exists() else None
    if boundaries_dir is None:
        print(f"[WARN] 本地边界索引不存在，将回退到在线地名解析: {boundaries_path}")

    tasks = lib.read_manifest(tasks_path)
    overrides = lib.load_overrides(overrides_path)
    options = build_options(args)

    store = lib.StateStore(state_db_path)
    try:
        sync_result = store.sync_manifest(tasks)
        print(
            f"[SYNC] manifest={tasks_path} tasks={len(tasks)} "
            f"added={sync_result['added']} updated={sync_result['updated']} reset={sync_result['reset']}"
        )

        if handle_move_requests(args, store):
            return 0

        if args.reset_task:
            store.reset_task(args.reset_task)
            print(f"[RESET] {args.reset_task}")
            return 0

        if args.sync_only:
            print_counts(store)
            return 0

        if args.summary:
            print_counts(store)
            return 0

        if args.running:
            print_running_tasks(store, repo_root)
            return 0

        if args.export_shp_only:
            updated_tasks = backfill_shp_export(repo_root, store, options, args)
            print(f"[SHP] 已为 {len(updated_tasks)} 个已完成任务导出 SHP")
            for task_id in updated_tasks:
                register_task_in_process_queue(repo_root, args, task_id)
            return 0

        if not args.no_verify_ok and not args.force:
            broken = verify_success_tasks(store, repo_root, options, args.dry_run)
            if broken:
                print(f"[VERIFY] {len(broken)} 个已完成任务的标记失效，已置回 PENDING: {broken[:10]}")

        status_filter = resolve_status_filter(args)
        selected = lib.select_tasks(
            store.all_tasks(),
            only=split_tokens(args.only),
            continents=split_tokens(args.continent),
            countries=split_tokens(args.country),
            statuses=status_filter,
            max_tasks=args.max_tasks,
        )

        if not selected:
            print("[DONE] 没有需要执行的任务")
            print_counts(store)
            return 0

        print(f"[SELECT] {len(selected)} 个任务待执行（状态过滤={status_filter or '全部'}）")

        worker_script = repo_root / "scripts" / WORKER_SCRIPTS[options.engine]

        if args.dry_run:
            print_plan(repo_root, worker_script, selected, overrides, options, boundaries_dir=boundaries_dir)
            print(f"[DRY-RUN] 共 {len(selected)} 个任务，未执行下载、未修改状态")
            return 0

        if not worker_script.exists():
            raise SystemExit(f"下载脚本不存在: {worker_script}")

        succeeded = 0
        empty = 0
        failed = 0
        skipped = 0
        signalled_stop = False

        for index, row in enumerate(selected, start=1):
            print(f"--- [{index}/{len(selected)}] {row['task_id']} ---")
            result = None
            for attempt in range(1, max(args.task_attempts, 1) + 1):
                if attempt > 1:
                    print(f"[RETRY] {row['task_id']} attempt={attempt}")
                result = execute_task(
                    repo_root,
                    worker_script,
                    args,
                    store,
                    overrides,
                    options,
                    row,
                    boundaries_dir=boundaries_dir,
                )
                if result["status"] in lib.SUCCESS_STATUSES or result["status"] == lib.STATUS_SKIPPED:
                    break
            if result is None:
                continue
            if result["status"] == lib.STATUS_OK:
                succeeded += 1
            elif result["status"] == lib.STATUS_OK_EMPTY:
                empty += 1
            elif result["status"] == lib.STATUS_SKIPPED:
                skipped += 1
            else:
                failed += 1
                if args.stop_on_error:
                    signalled_stop = True
                    print("[STOP] --stop-on-error 生效，提前结束")
                    break
            if index < len(selected) and args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

        print(
            f"[SUMMARY] selected={len(selected)} ok={succeeded} ok_empty={empty} "
            f"skipped={skipped} failed={failed}"
        )
        print_counts(store)
        return 1 if failed or signalled_stop else 0
    except KeyboardInterrupt:
        print("[INTERRUPT] 收到中断，RUNNING 任务将在下次运行自动重试")
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
