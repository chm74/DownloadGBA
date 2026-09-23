import argparse
import ast
import csv
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ProvinceSpec:
    slug: str
    place_query: str
    aliases: tuple[str, ...] = ()


PROVINCES = [
    ProvinceSpec("anhui", "Anhui, China", ("安徽省", "安徽")),
    ProvinceSpec("beijing", "Beijing, China", ("北京市", "北京")),
    ProvinceSpec("chongqing", "Chongqing, China", ("重庆市", "重庆")),
    ProvinceSpec("fujian", "Fujian, China", ("福建省", "福建")),
    ProvinceSpec("gansu", "Gansu, China", ("甘肃省", "甘肃")),
    ProvinceSpec("guangdong", "Guangdong, China", ("广东省", "广东")),
    ProvinceSpec("guangxi", "Guangxi, China", ("广西壮族自治区", "广西")),
    ProvinceSpec("guizhou", "Guizhou, China", ("贵州省", "贵州")),
    ProvinceSpec("hainan", "Hainan, China", ("海南省", "海南")),
    ProvinceSpec("hebei", "Hebei, China", ("河北省", "河北")),
    ProvinceSpec("heilongjiang", "Heilongjiang, China", ("黑龙江省", "黑龙江")),
    ProvinceSpec("henan", "Henan, China", ("河南省", "河南")),
    ProvinceSpec("hubei", "Hubei, China", ("湖北省", "湖北")),
    ProvinceSpec("hunan", "Hunan, China", ("湖南省", "湖南")),
    ProvinceSpec("inner_mongolia", "Inner Mongolia, China", ("内蒙古自治区", "内蒙古")),
    ProvinceSpec("jiangsu", "Jiangsu, China", ("江苏省", "江苏")),
    ProvinceSpec("jiangxi", "Jiangxi, China", ("江西省", "江西")),
    ProvinceSpec("jilin", "Jilin, China", ("吉林省", "吉林")),
    ProvinceSpec("liaoning", "Liaoning, China", ("辽宁省", "辽宁")),
    ProvinceSpec("ningxia", "Ningxia, China", ("宁夏回族自治区", "宁夏")),
    ProvinceSpec("qinghai", "Qinghai, China", ("青海省", "青海")),
    ProvinceSpec("shaanxi", "Shaanxi, China", ("陕西省", "陕西")),
    ProvinceSpec("shandong", "Shandong, China", ("山东省", "山东")),
    ProvinceSpec("shanghai", "Shanghai, China", ("上海市", "上海")),
    ProvinceSpec("shanxi", "Shanxi, China", ("山西省", "山西")),
    ProvinceSpec("sichuan", "Sichuan, China", ("四川省", "四川")),
    ProvinceSpec("tianjin", "Tianjin, China", ("天津市", "天津")),
    ProvinceSpec("xinjiang", "Xinjiang, China", ("新疆维吾尔自治区", "新疆")),
    ProvinceSpec("xizang", "Tibet Autonomous Region, China", ("西藏自治区", "西藏", "tibet")),
    ProvinceSpec("yunnan", "Yunnan, China", ("云南省", "云南")),
    ProvinceSpec("zhejiang", "Zhejiang, China", ("浙江省", "浙江")),
]

PROVINCE_INDEX = {item.slug: item for item in PROVINCES}

SUMMARY_FIELDS = [
    "province",
    "mode",
    "boundary_file",
    "output_dir",
    "merge_output",
    "grid_size",
    "page_size",
    "tile_format",
    "merge_format",
    "status",
    "exit_code",
    "started_at",
    "finished_at",
    "duration_seconds",
]

BUILDING_SOURCE_NAME = "GlobalBuildingAtlas LoD1 WFS"
BUILDING_SOURCE_URL = "https://tubvsig-so2sat-vm1.srv.mwn.de/geoserver/ows"
BUILDING_SOURCE_LAYER = "global3D:lod1_global"


def print_download_source(mode: str, boundary_file: Path | None, place_query: str) -> None:
    boundary_source = str(boundary_file) if boundary_file else f"OSM Nominatim place query: {place_query}"
    print(
        f"[SOURCE] building={BUILDING_SOURCE_NAME} "
        f"url={BUILDING_SOURCE_URL} layer={BUILDING_SOURCE_LAYER} "
        f"mode={mode} boundary_source={boundary_source}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-run provincial GBA WFS building exports by reusing scripts/download_gba_lod1_wfs.py. "
            "Preferred production mode is local boundary files; otherwise the script falls back to --place."
        )
    )
    parser.add_argument(
        "--provinces",
        nargs="+",
        help="Province slugs or aliases to run, for example: guangdong zhejiang 四川省.",
    )
    parser.add_argument(
        "--province-list",
        help=(
            "Province list string in JSON/Python format, for example: "
            "\"['海南省','上海市','黑龙江省']\". Execution follows list order."
        ),
    )
    parser.add_argument(
        "--all-mainland",
        action="store_true",
        help="Run all built-in provincial-level mainland targets.",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Province slugs to skip, for example: beijing shanghai tianjin.",
    )
    parser.add_argument(
        "--boundary-root",
        default="data/province_admin",
        help="Optional root directory for province boundaries. Matching slug files are used before --place.",
    )
    parser.add_argument(
        "--require-boundary",
        action="store_true",
        help="Fail a province when no local boundary file is found instead of falling back to --place.",
    )
    parser.add_argument(
        "--output-root",
        default="data/province_batches",
        help="Root output directory. Each province writes to <output-root>/<slug>_gba_wfs.",
    )
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="Python executable used to invoke scripts/download_gba_lod1_wfs.py.",
    )
    parser.add_argument(
        "--grid-size",
        type=float,
        default=0.5,
        help="Grid size in degrees for province-scale runs. 0.5 is a pragmatic default for large areas.",
    )
    parser.add_argument("--page-size", type=int, default=5000, help="WFS page size.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional page limit for smoke tests.")
    parser.add_argument(
        "--tile-format",
        choices=["gpkg", "shp"],
        default="gpkg",
        help="Per-tile output format. gpkg is recommended for resume stability.",
    )
    parser.add_argument(
        "--merge-format",
        choices=["shp", "gpkg"],
        default="shp",
        help="Final merged output format. shp is convenient but may hit DBF size limits on large provinces.",
    )
    parser.add_argument(
        "--skip-existing-merge",
        action="store_true",
        help="Skip a province when the final merged output file already exists.",
    )
    parser.add_argument(
        "--summary-file",
        help="Optional summary CSV path. Defaults to <output-root>/batch_summary.csv.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without executing them.")
    return parser.parse_args()


def normalize_province_key(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("-", "_")


def build_alias_index() -> dict[str, ProvinceSpec]:
    alias_index: dict[str, ProvinceSpec] = {}
    for item in PROVINCES:
        names = {
            item.slug,
            item.place_query.split(",")[0],
            *(item.aliases or ()),
        }
        for name in names:
            alias_index[normalize_province_key(name)] = item
    return alias_index


PROVINCE_ALIAS_INDEX = build_alias_index()


def normalize_province_list_text(value: str) -> str:
    return (
        value.strip()
        .replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("，", ",")
        .replace("【", "[")
        .replace("】", "]")
    )


def parse_province_list(value: str) -> list[str]:
    normalized = normalize_province_list_text(value)
    if not normalized:
        return []

    if normalized.startswith("[") and normalized.endswith("]"):
        parsed = ast.literal_eval(normalized)
        if isinstance(parsed, str):
            return [parsed]
        if not isinstance(parsed, list):
            raise SystemExit("--province-list must be a list string, for example: ['海南省','上海市']")
        return [str(item) for item in parsed]

    return [item.strip() for item in normalized.split(",") if item.strip()]


def resolve_province_token(token: str) -> ProvinceSpec:
    normalized = normalize_province_key(token)
    province = PROVINCE_ALIAS_INDEX.get(normalized)
    if province is None:
        raise SystemExit(f"Unknown province value: {token}")
    return province


def resolve_provinces(args: argparse.Namespace) -> list[ProvinceSpec]:
    if not args.provinces and not args.province_list and not args.all_mainland:
        raise SystemExit("Use --provinces, --province-list, or --all-mainland.")

    if args.province_list:
        selected = [resolve_province_token(item) for item in parse_province_list(args.province_list)]
    elif args.provinces:
        selected = [resolve_province_token(item) for item in args.provinces]
    else:
        selected = list(PROVINCES)

    exclude_set = {resolve_province_token(item).slug for item in (args.exclude or [])}
    return [item for item in selected if item.slug not in exclude_set]


def find_boundary_file(boundary_root: Path, province: ProvinceSpec) -> Path | None:
    candidates = [
        boundary_root / f"{province.slug}_adm1.shp",
        boundary_root / f"{province.slug}_adm1.gpkg",
        boundary_root / f"{province.slug}.shp",
        boundary_root / f"{province.slug}.gpkg",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_command(
    repo_root: Path,
    worker_script: Path,
    province: ProvinceSpec,
    args: argparse.Namespace,
    output_dir: Path,
    merge_output: Path,
    boundary_file: Path | None,
) -> tuple[list[str], str]:
    command = [
        args.python_exe,
        str(worker_script),
        "--output-dir",
        str(output_dir),
        "--grid-size",
        str(args.grid_size),
        "--page-size",
        str(args.page_size),
        "--tile-format",
        args.tile_format,
        "--merge-output",
        str(merge_output),
    ]
    if args.max_pages > 0:
        command.extend(["--max-pages", str(args.max_pages)])

    if boundary_file is not None:
        command.extend(["--boundary", str(boundary_file)])
        mode = "boundary"
    else:
        command.extend(["--place", province.place_query])
        mode = "place"

    return command, mode


def upsert_summary(summary_file: Path, row: dict[str, str]) -> None:
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if summary_file.exists():
        with summary_file.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)

    row_key = (row["province"], row["output_dir"], row["started_at"])
    replaced = False
    normalized_row = {field: row.get(field, "") for field in SUMMARY_FIELDS}

    for index, current in enumerate(rows):
        current_key = (current.get("province", ""), current.get("output_dir", ""), current.get("started_at", ""))
        if current_key == row_key:
            rows[index] = normalized_row
            replaced = True
            break

    if not replaced:
        rows.append(normalized_row)

    with summary_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def run_one(
    repo_root: Path,
    worker_script: Path,
    boundary_root: Path,
    output_root: Path,
    summary_file: Path,
    province: ProvinceSpec,
    args: argparse.Namespace,
) -> int:
    output_dir = output_root / f"{province.slug}_gba_wfs"
    merge_output = output_dir / f"{province.slug}_buildings_height_gba.{args.merge_format}"
    boundary_file = find_boundary_file(boundary_root, province)

    if boundary_file is None and args.require_boundary:
        started_at = datetime.now().isoformat(timespec="seconds")
        finished_at = datetime.now().isoformat(timespec="seconds")
        upsert_summary(
            summary_file,
            {
                "province": province.slug,
                "mode": "missing-boundary",
                "boundary_file": "",
                "output_dir": str(output_dir),
                "merge_output": str(merge_output),
                "grid_size": str(args.grid_size),
                "page_size": str(args.page_size),
                "tile_format": args.tile_format,
                "merge_format": args.merge_format,
                "status": "FAILED",
                "exit_code": "2",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": "0",
            },
        )
        print(f"[FAILED] {province.slug}: no boundary file found under {boundary_root}")
        return 2

    if args.skip_existing_merge and merge_output.exists():
        started_at = datetime.now().isoformat(timespec="seconds")
        finished_at = datetime.now().isoformat(timespec="seconds")
        upsert_summary(
            summary_file,
            {
                "province": province.slug,
                "mode": "skip-existing-merge",
                "boundary_file": str(boundary_file) if boundary_file else "",
                "output_dir": str(output_dir),
                "merge_output": str(merge_output),
                "grid_size": str(args.grid_size),
                "page_size": str(args.page_size),
                "tile_format": args.tile_format,
                "merge_format": args.merge_format,
                "status": "SKIPPED",
                "exit_code": "",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": "0",
            },
        )
        print(f"[SKIPPED] {province.slug}: merged output already exists")
        return 0

    command, mode = build_command(repo_root, worker_script, province, args, output_dir, merge_output, boundary_file)
    print(f"[RUN] {province.slug} mode={mode} output={output_dir}")
    print_download_source(mode, boundary_file, province.place_query)
    print("      " + " ".join(command))

    started_dt = datetime.now()
    started_at = started_dt.isoformat(timespec="seconds")
    if args.dry_run:
        upsert_summary(
            summary_file,
            {
                "province": province.slug,
                "mode": mode,
                "boundary_file": str(boundary_file) if boundary_file else "",
                "output_dir": str(output_dir),
                "merge_output": str(merge_output),
                "grid_size": str(args.grid_size),
                "page_size": str(args.page_size),
                "tile_format": args.tile_format,
                "merge_format": args.merge_format,
                "status": "DRY_RUN",
                "exit_code": "",
                "started_at": started_at,
                "finished_at": started_at,
                "duration_seconds": "0",
            },
        )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = output_dir / "run.log"
    stderr_log = output_dir / "run.err.log"

    upsert_summary(
        summary_file,
        {
            "province": province.slug,
            "mode": mode,
            "boundary_file": str(boundary_file) if boundary_file else "",
            "output_dir": str(output_dir),
            "merge_output": str(merge_output),
            "grid_size": str(args.grid_size),
            "page_size": str(args.page_size),
            "tile_format": args.tile_format,
            "merge_format": args.merge_format,
            "status": "RUNNING",
            "exit_code": "",
            "started_at": started_at,
            "finished_at": "",
            "duration_seconds": "",
        },
    )

    with stdout_log.open("a", encoding="utf-8") as out_handle, stderr_log.open("a", encoding="utf-8") as err_handle:
        out_handle.write(f"\n=== {started_at} START {province.slug} mode={mode} ===\n")
        err_handle.write(f"\n=== {started_at} START {province.slug} mode={mode} ===\n")
        result = subprocess.run(
            command,
            cwd=repo_root,
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    finished_dt = datetime.now()
    finished_at = finished_dt.isoformat(timespec="seconds")
    duration_seconds = int((finished_dt - started_dt).total_seconds())
    status = "OK" if result.returncode == 0 else "FAILED"

    upsert_summary(
        summary_file,
        {
            "province": province.slug,
            "mode": mode,
            "boundary_file": str(boundary_file) if boundary_file else "",
            "output_dir": str(output_dir),
            "merge_output": str(merge_output),
            "grid_size": str(args.grid_size),
            "page_size": str(args.page_size),
            "tile_format": args.tile_format,
            "merge_format": args.merge_format,
            "status": status,
            "exit_code": str(result.returncode),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": str(duration_seconds),
        },
    )
    print(f"[{status}] {province.slug} exit={result.returncode}")
    return result.returncode


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    worker_script = repo_root / "scripts" / "download_gba_lod1_wfs.py"
    if not worker_script.exists():
        raise SystemExit(f"Worker script not found: {worker_script}")

    provinces = resolve_provinces(args)
    boundary_root = (repo_root / args.boundary_root).resolve()
    output_root = (repo_root / args.output_root).resolve()
    summary_file = Path(args.summary_file).resolve() if args.summary_file else output_root / "batch_summary.csv"

    exit_code = 0
    for province in provinces:
        province_code = run_one(repo_root, worker_script, boundary_root, output_root, summary_file, province, args)
        if province_code != 0:
            exit_code = province_code

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
