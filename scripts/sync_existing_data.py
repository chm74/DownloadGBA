import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world_tasks_lib as lib

DEFAULT_SCAN_ROOT = "data/亚洲/中国"
DEFAULT_STATE_DB = "data/world_building_download_tasks_state.db"
CHINA_COUNTRY_KEYS = {"中国", "china"}


def find_existing_datasets(root: Path) -> list[dict]:
    datasets: list[dict] = []
    if not root.is_dir():
        return datasets
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        shps = sorted(entry.glob("*_buildings_height_gba.shp"))
        merged = sorted(entry.glob("*_buildings_height_gba.gpkg"))
        if not shps and not merged:
            continue
        datasets.append(
            {
                "dir": entry,
                "shp": shps[0] if shps else None,
                "merge": merged[0] if merged else None,
                "file_count": sum(1 for _ in entry.iterdir()),
            }
        )
    return datasets


def match_task(rows: list[dict], directory_name: str) -> dict | None:
    key = lib.normalize_name(directory_name)
    for row in rows:
        if lib.normalize_name(row["country"]) not in CHINA_COUNTRY_KEYS:
            continue
        if lib.normalize_name(row["city"]) == key:
            return row
    return None


def sync_existing_datasets(
    repo_root: Path,
    state_db: Path,
    scan_root: Path,
    mark_status: str | None = None,
    dry_run: bool = False,
) -> dict:
    store = lib.StateStore(state_db)
    try:
        rows = store.all_tasks()
        datasets = find_existing_datasets(scan_root)
        matched: list[tuple[str, str]] = []
        unmatched: list[str] = []

        for dataset in datasets:
            row = match_task(rows, dataset["dir"].name)
            if row is None:
                unmatched.append(dataset["dir"].name)
                continue
            relative = lib.repo_relative(repo_root, dataset["dir"])
            matched.append((row["task_id"], relative))
            if dry_run:
                continue
            store.set_existing_data(row["task_id"], relative)
            store.add_event(row["task_id"], "EXISTING_DATA", relative)
            if mark_status:
                store.set_status(row["task_id"], mark_status)

        return {
            "total": len(datasets),
            "matched": matched,
            "unmatched": unmatched,
        }
    finally:
        store.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描已下载的省/市数据目录，把路径同步到任务状态库。")
    parser.add_argument("--root", default=DEFAULT_SCAN_ROOT, help="要扫描的数据根目录。")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB, help="SQLite 状态库路径。")
    parser.add_argument("--repo-root", default=None, help="仓库根目录，默认脚本上级目录。")
    parser.add_argument("--dry-run", action="store_true", help="只对比并打印结果，不写状态库。")
    parser.add_argument(
        "--mark-status",
        choices=["none", "skipped", "ok"],
        default="none",
        help="可选：把匹配到的任务状态改为 skipped 或 ok（默认 none，只记录路径）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    scan_root = Path(args.root)
    if not scan_root.is_absolute():
        scan_root = repo_root / scan_root
    state_db = Path(args.state_db)
    if not state_db.is_absolute():
        state_db = repo_root / state_db

    if not scan_root.is_dir():
        print(f"[ERROR] 扫描目录不存在: {scan_root}")
        return 1

    mark_status = None
    if args.mark_status == "skipped":
        mark_status = lib.STATUS_SKIPPED
    elif args.mark_status == "ok":
        mark_status = lib.STATUS_OK

    result = sync_existing_datasets(
        repo_root=repo_root,
        state_db=state_db,
        scan_root=scan_root,
        mark_status=mark_status,
        dry_run=args.dry_run,
    )

    print(f"扫描目录: {scan_root}")
    print(f"发现已下载数据集: {result['total']} 个")
    print(f"匹配任务: {len(result['matched'])} 个")
    for task_id, relative in result["matched"]:
        suffix = " [dry-run]" if args.dry_run else ""
        print(f"  {task_id} -> {relative}{suffix}")
    if result["unmatched"]:
        print(f"未匹配到任务的目录: {len(result['unmatched'])} 个")
        for name in result["unmatched"]:
            print(f"  {name}")
    if mark_status:
        print(f"匹配任务状态已更新为: {mark_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
