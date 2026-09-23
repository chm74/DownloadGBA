import csv
import functools
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gba_contract import DATA_SEMANTICS_VERSION, GRID_ALGORITHM_VERSION, GRID_MANIFEST_NAME, TILE_DIRECTORY_NAME

MANIFEST_FIELDS = ("continent", "country", "city", "download_dir")
MARKER_NAME = "_TASK_DONE.json"
RAW_DATA_PATTERNS = ("*_buildings_height_gba.gpkg", "*_buildings_height_gba.shp")
BOUNDARY_NAME = "place_boundary.gpkg"
DEFAULT_BOUNDARIES_DIR = "data/boundaries"
ADMIN0_GPKG = "ne_admin0.gpkg"
ADMIN1_GPKG = "ne_admin1.gpkg"
NATIONAL_GRID_ALGORITHM = GRID_ALGORITHM_VERSION
NATIONAL_GRID_SEMANTICS = DATA_SEMANTICS_VERSION
NATIONAL_GRID_MANIFEST = GRID_MANIFEST_NAME
NATIONAL_GRID_TILE_DIR = TILE_DIRECTORY_NAME

ADMIN0_NAME_FIELDS = ("NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT", "NAME_EN", "NAME_ZH", "NAME_ZHT", "NAME_ALT", "FORMAL_EN")
ADMIN1_NAME_FIELDS = ("name", "name_en", "name_zh", "name_local", "gn_name", "name_alt", "woe_name", "gns_name")
CHINESE_ADMIN_SUFFIXES = ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市")

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_OK = "OK"
STATUS_OK_EMPTY = "OK_EMPTY"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
SUCCESS_STATUSES = (STATUS_OK, STATUS_OK_EMPTY)
ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_OK,
    STATUS_OK_EMPTY,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

COUNTRY_QUERY_ALIASES = {
    "united states of america": "United States",
    "usa": "United States",
    "congo(drc)": "Democratic Republic of the Congo",
    "congo (drc)": "Democratic Republic of the Congo",
    "republic of the congo": "Republic of the Congo",
    "ivory coast": "Ivory Coast",
    "turkmensitan": "Turkmenistan",
    "turkmenistann": "Turkmenistan",
    "swaziland": "Eswatini",
    "republic of yemen": "Yemen",
    "state of qatar": "Qatar",
    "sultanate of oman": "Oman",
    "the gambia": "Gambia",
    "the bahamas": "Bahamas",
    "vatican city": "Vatican City",
    "czech republic": "Czech Republic",
    "timor leste": "Timor-Leste",
    "burkinafaso": "Burkina Faso",
    "guineabissau": "Guinea-Bissau",
    "中国": "China",
}

OVERRIDE_KINDS = ("bbox", "boundary", "query", "skip")

DOWNLOAD_OPTION_FIELDS = (
    "engine",
    "grid_size",
    "page_size",
    "tile_format",
    "merge_format",
    "max_pages",
    "tile_workers",
    "split_threshold",
    "failure_split_retries",
    "min_grid_size",
    "probe_page_size",
)


class BoundaryResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadOptions:
    engine: str = "adaptive"
    grid_size: float = 0.5
    page_size: int = 5000
    tile_format: str = "gpkg"
    merge_format: str = "gpkg"
    max_pages: int = 0
    tile_workers: int = 1
    split_threshold: int = 20000
    failure_split_retries: int = 5
    min_grid_size: float = 0.05
    probe_page_size: int = 1


@dataclass(frozen=True)
class Task:
    continent: str
    country: str
    city: str
    download_dir: str
    source_row: int = 0

    @property
    def task_id(self) -> str:
        return f"{self.continent}|{self.country}|{self.city}"


@dataclass(frozen=True)
class RangeOverride:
    continent: str
    country: str
    city: str
    kind: str
    value: str
    note: str = ""


@dataclass
class BoundaryResult:
    path: Path | None
    source: dict
    skipped: bool = False
    skip_reason: str = ""


def utc_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def task_key(value: str) -> str:
    return collapse_spaces((value or "").replace("_", " ")).lower()


def detect_encoding(path: Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码，请转存为 UTF-8 或 GBK: {path}")


def default_download_dir(continent: str, country: str, city: str) -> str:
    return "/".join(
        [
            "data",
            sanitize_path_segment(continent),
            sanitize_path_segment(country),
            sanitize_path_segment(city),
        ]
    )


def sanitize_path_segment(value: str) -> str:
    cleaned = collapse_spaces(value).replace("\\", "_").replace("/", "_")
    return cleaned or "unknown"


def normalize_download_dir(raw: str) -> str:
    cleaned = (raw or "").strip().replace("\\", "/")
    cleaned = re.sub(r"/+", "/", cleaned).strip("/")
    if not cleaned:
        raise ValueError("download_dir 不能为空")
    if any(part == ".." for part in cleaned.split("/")):
        raise ValueError(f"download_dir 不允许包含 .. 路径: {raw}")
    return cleaned


def normalize_country_for_query(country: str) -> str:
    cleaned = collapse_spaces((country or "").replace("_", " "))
    return COUNTRY_QUERY_ALIASES.get(cleaned.lower(), cleaned)


def query_candidates(task: Task) -> list[object]:
    country_query = normalize_country_for_query(task.country)
    city_query = collapse_spaces((task.city or "").replace("_", " "))
    if task_key(task.city) == task_key(task.country):
        return [{"country": country_query}, country_query]
    return [
        {"state": city_query, "country": country_query},
        {"city": city_query, "country": country_query},
        f"{city_query}, {country_query}",
        city_query,
    ]


def read_manifest(path: Path) -> list[Task]:
    manifest_path = Path(path)
    encoding = detect_encoding(manifest_path)
    tasks: list[Task] = []
    seen: dict[str, int] = {}

    with manifest_path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [field for field in MANIFEST_FIELDS if field not in fieldnames]
        if missing:
            raise ValueError(f"清单缺少字段 {missing}: {manifest_path}")

        for row_number, row in enumerate(reader, start=2):
            values = {field: collapse_spaces(row.get(field) or "") for field in MANIFEST_FIELDS}
            if not any(values.values()):
                continue
            missing_values = [field for field in MANIFEST_FIELDS if not values[field]]
            if missing_values:
                raise ValueError(f"清单第 {row_number} 行缺少字段 {missing_values}: {values}")

            task = Task(
                continent=values["continent"],
                country=values["country"],
                city=values["city"],
                download_dir=normalize_download_dir(values["download_dir"]),
                source_row=row_number,
            )
            if task.task_id in seen:
                raise ValueError(
                    f"清单存在重复任务 {task.task_id}，行号 {seen[task.task_id]} 与 {row_number}"
                )
            seen[task.task_id] = row_number
            tasks.append(task)

    if not tasks:
        raise ValueError(f"清单为空: {manifest_path}")
    return tasks


def load_overrides(path: Path) -> list[RangeOverride]:
    override_path = Path(path)
    if not override_path.exists():
        return []

    encoding = detect_encoding(override_path)
    overrides: list[RangeOverride] = []
    with override_path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            kind = collapse_spaces(row.get("kind") or "").lower()
            if not kind:
                continue
            if kind not in OVERRIDE_KINDS:
                raise ValueError(f"覆盖表第 {row_number} 行 kind 非法: {kind}")
            overrides.append(
                RangeOverride(
                    continent=task_key(row.get("continent") or ""),
                    country=task_key(row.get("country") or ""),
                    city=task_key(row.get("city") or ""),
                    kind=kind,
                    value=collapse_spaces(row.get("value") or ""),
                    note=collapse_spaces(row.get("note") or ""),
                )
            )
    return overrides


def match_override(task: Task, overrides: list[RangeOverride]) -> RangeOverride | None:
    for override in overrides:
        if override.continent and override.continent != task_key(task.continent):
            continue
        if override.country and override.country != task_key(task.country):
            continue
        if override.city and override.city != task_key(task.city):
            continue
        return override
    return None


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox 需要 4 个数值 minx,miny,maxx,maxy: {value}")
    try:
        minx, miny, maxx, maxy = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"bbox 数值非法: {value}") from exc
    if minx >= maxx or miny >= maxy:
        raise ValueError(f"bbox 范围非法: {value}")
    return minx, miny, maxx, maxy


def parse_query_value(value: str) -> object:
    stripped = value.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(stripped)
    return stripped


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("_", " ").replace("-", " ").replace("'", "").replace("’", "")
    text = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def strip_admin_suffix(value: str) -> str:
    text = collapse_spaces(value)
    for suffix in CHINESE_ADMIN_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def name_keys(value: str) -> set[str]:
    cleaned = collapse_spaces((value or "").replace("_", " "))
    return {
        normalize_name(value),
        normalize_name(cleaned),
        normalize_name(strip_admin_suffix(cleaned)),
    }


def split_name_values(value: object) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in re.split(r"[|;]", str(value)) if part.strip()]


@functools.lru_cache(maxsize=4)
def load_admin_boundary(boundaries_dir: str, layer: str):
    import geopandas as gpd

    file_name = ADMIN0_GPKG if layer == "admin0" else ADMIN1_GPKG
    path = Path(boundaries_dir) / file_name
    if not path.exists():
        return None
    return gpd.read_file(path)


def find_admin0_row(task: Task, boundaries_dir: Path):
    admin0 = load_admin_boundary(str(boundaries_dir), "admin0")
    if admin0 is None:
        return None
    keys = name_keys(task.country)
    keys.add(normalize_name(normalize_country_for_query(task.country)))
    for _, row in admin0.iterrows():
        for field in ADMIN0_NAME_FIELDS:
            if field not in admin0.columns:
                continue
            for name in split_name_values(row.get(field)):
                if normalize_name(name) in keys:
                    return row
    return None


def find_admin1_row(name: str, boundaries_dir: Path, admin_names: set[str] | None = None):
    admin1 = load_admin_boundary(str(boundaries_dir), "admin1")
    if admin1 is None:
        return None
    subset = admin1
    if admin_names and "admin" in admin1.columns:
        subset = admin1[admin1["admin"].isin(admin_names)]
    keys = name_keys(name)
    for _, row in subset.iterrows():
        for field in ADMIN1_NAME_FIELDS:
            if field not in subset.columns:
                continue
            for candidate in split_name_values(row.get(field)):
                if normalize_name(strip_admin_suffix(candidate)) in keys or normalize_name(candidate) in keys:
                    return row
    return None


def match_local_boundary(task: Task, boundaries_dir: Path) -> dict | None:
    boundaries_dir = Path(boundaries_dir)
    admin0_row = find_admin0_row(task, boundaries_dir)
    if admin0_row is not None:
        if task_key(task.city) == task_key(task.country):
            return {
                "geometry": admin0_row.geometry,
                "level": "admin0",
                "matched_name": str(admin0_row.get("NAME") or task.country),
            }
        admin_names = {str(admin0_row.get(field)) for field in ("NAME", "ADMIN", "SOVEREIGNT", "NAME_LONG") if admin0_row.get(field)}
        admin1_row = find_admin1_row(task.city, boundaries_dir, admin_names=admin_names)
        if admin1_row is not None:
            return {
                "geometry": admin1_row.geometry,
                "level": "admin1",
                "matched_name": str(admin1_row.get("name") or task.city),
            }
        fallback_row = find_admin1_row(task.city, boundaries_dir)
        if fallback_row is not None:
            return {
                "geometry": fallback_row.geometry,
                "level": "admin1_global",
                "matched_name": str(fallback_row.get("name") or task.city),
            }
        return None

    fallback_row = find_admin1_row(task.country, boundaries_dir)
    if fallback_row is None:
        fallback_row = find_admin1_row(task.city, boundaries_dir)
    if fallback_row is not None:
        return {
            "geometry": fallback_row.geometry,
            "level": "admin1_fallback",
            "matched_name": str(fallback_row.get("name") or task.country),
        }
    return None


def save_geometry_boundary(geometry, target: Path) -> Path:
    import geopandas as gpd

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"geometry": [geometry]}, crs=4326).to_file(target, driver="GPKG")
    return target


def generate_slug(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value or "").strip()
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    return cleaned or "task"


def merge_output_path(task_dir: Path, task: Task, merge_format: str) -> Path:
    slug = generate_slug(task.city)
    return Path(task_dir) / f"{slug}_buildings_height_gba.{merge_format}"


def resolve_task_dir(repo_root: Path, download_dir: str) -> Path:
    normalized = normalize_download_dir(download_dir)
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = Path(repo_root) / candidate
    resolved = candidate.resolve()
    repo_resolved = Path(repo_root).resolve()
    if resolved != repo_resolved and repo_resolved not in resolved.parents:
        raise ValueError(f"download_dir 超出仓库根目录: {download_dir}")
    return resolved


def repo_relative(repo_root: Path, path: Path) -> str:
    return os.path.relpath(str(Path(path).resolve()), str(Path(repo_root).resolve())).replace("\\", "/")


def child_process_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_worker_command(
    worker_script: Path,
    python_exe: str,
    boundary_path: Path,
    task_dir: Path,
    merge_output: Path,
    options: DownloadOptions,
) -> list[str]:
    command = [
        python_exe,
        "-u",
        str(worker_script),
        "--boundary",
        str(boundary_path),
        "--output-dir",
        str(task_dir),
        "--grid-size",
        str(options.grid_size),
        "--page-size",
        str(options.page_size),
        "--tile-format",
        options.tile_format,
        "--merge-output",
        str(merge_output),
    ]
    if options.max_pages > 0:
        command.extend(["--max-pages", str(options.max_pages)])
    if options.tile_workers > 1:
        command.extend(["--tile-workers", str(options.tile_workers)])
    if options.engine == "adaptive":
        command.extend(
            [
                "--split-threshold",
                str(options.split_threshold),
                "--failure-split-retries",
                str(options.failure_split_retries),
                "--min-grid-size",
                str(options.min_grid_size),
                "--probe-page-size",
                str(options.probe_page_size),
            ]
        )
    return command


def options_snapshot(options: DownloadOptions) -> dict:
    return {field: getattr(options, field) for field in DOWNLOAD_OPTION_FIELDS}


def marker_path(task_dir: Path) -> Path:
    return Path(task_dir) / MARKER_NAME


def write_marker(task_dir: Path, payload: dict) -> Path:
    target = marker_path(task_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_marker(task_dir: Path) -> dict | None:
    target = marker_path(task_dir)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_national_grid_progress(task_dir: Path) -> dict | None:
    manifest_path = Path(task_dir) / NATIONAL_GRID_MANIFEST
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.get("grid_algorithm") != NATIONAL_GRID_ALGORITHM:
        return None
    if manifest.get("data_semantics_version") != NATIONAL_GRID_SEMANTICS:
        return None
    grid_ids = manifest.get("grid_ids")
    if not isinstance(grid_ids, list) or any(not isinstance(item, str) or not item for item in grid_ids):
        return None
    expected_ids = list(dict.fromkeys(grid_ids))
    tile_dir = Path(task_dir) / NATIONAL_GRID_TILE_DIR
    complete = 0
    empty = 0
    for grid_id in expected_ids:
        marker_path = tile_dir / f"{grid_id}.done.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if marker.get("grid_algorithm") != NATIONAL_GRID_ALGORITHM:
            continue
        if marker.get("data_semantics_version") != NATIONAL_GRID_SEMANTICS:
            continue
        if marker.get("grid_id") != grid_id:
            continue
        status = marker.get("status")
        if status == "COMPLETE":
            complete += 1
        elif status == "EMPTY":
            empty += 1
    done = complete + empty
    expected = len(expected_ids)
    return {
        "grid_algorithm": NATIONAL_GRID_ALGORITHM,
        "data_semantics_version": NATIONAL_GRID_SEMANTICS,
        "expected": expected,
        "complete": complete,
        "empty": empty,
        "done": done,
        "missing": max(expected - done, 0),
    }


def marker_is_valid(
    marker: dict | None,
    repo_root: Path,
    task_dir: Path,
    merge_output: Path,
) -> bool:
    if not isinstance(marker, dict):
        return False
    if marker.get("status") not in SUCCESS_STATUSES:
        return False
    recorded_dir = marker.get("download_dir")
    recorded_merge = marker.get("merge_output")
    if not recorded_dir or not recorded_merge:
        return False
    try:
        resolved_dir = resolve_task_dir(repo_root, recorded_dir)
        resolved_merge = resolve_task_dir(repo_root, recorded_merge)
    except ValueError:
        return False
    if os.path.normcase(str(resolved_dir)) != os.path.normcase(str(Path(task_dir).resolve())):
        return False
    if os.path.normcase(str(resolved_merge)) != os.path.normcase(str(Path(merge_output).resolve())):
        return False
    merge_file = Path(merge_output)
    if not merge_file.exists():
        return False
    recorded_size = marker.get("merge_size_bytes")
    if isinstance(recorded_size, int) and recorded_size != merge_file.stat().st_size:
        return False
    return True


def count_features(path: Path) -> int:
    import pyogrio

    info = pyogrio.read_info(str(path))
    return int(info.get("features") or 0)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_completion(merge_output: Path) -> tuple[bool, int, str]:
    merge_file = Path(merge_output)
    if not merge_file.exists():
        return False, 0, f"合并产物不存在: {merge_file}"
    if merge_file.stat().st_size <= 0:
        return False, 0, f"合并产物为空文件: {merge_file}"
    try:
        feature_count = count_features(merge_file)
    except Exception as exc:
        return False, 0, f"合并产物无法读取: {exc}"
    return True, feature_count, ""


def has_merged_raw_data(directory: Path) -> bool:
    target = Path(directory)
    if not target.is_dir():
        return False
    for pattern in RAW_DATA_PATTERNS:
        if next(target.glob(pattern), None) is not None:
            return True
    return False


def read_log_text(path: Path) -> str:
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError:
        return ""
    lines: list[str] = []
    for chunk in raw.split(b"\n"):
        try:
            lines.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(chunk.decode("gbk", errors="replace"))
    return "\n".join(lines)


def tail_lines(path: Path, limit: int = 3, max_chars: int = 500) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return ""
    lines = read_log_text(file_path).splitlines()
    tail = [line.strip() for line in lines if line.strip()][-limit:]
    return " | ".join(tail)[:max_chars]


def save_bbox_boundary(bounds: tuple[float, float, float, float], target: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import box

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame({"geometry": [box(*bounds)]}, crs=4326)
    gdf.to_file(target, driver="GPKG")
    return target


def save_geocode_boundary(query: object, target: Path) -> Path:
    import geopandas as gpd
    import osmnx as ox

    target = Path(target)
    gdf = ox.geocode_to_gdf(query)
    if gdf is None or gdf.empty:
        raise BoundaryResolutionError(f"地名解析无结果: {query!r}")
    gdf = gdf.to_crs(4326)
    geometry = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
    if geometry is None or geometry.is_empty:
        raise BoundaryResolutionError(f"地名解析几何为空: {query!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"geometry": [geometry]}, crs=4326).to_file(target, driver="GPKG")
    return target


def resolve_boundary(
    task: Task,
    overrides: list[RangeOverride],
    task_dir: Path,
    repo_root: Path,
    refresh: bool = False,
    boundaries_dir: Path | None = None,
) -> BoundaryResult:
    override = match_override(task, overrides)
    if override is not None and override.kind == "skip":
        return BoundaryResult(
            path=None,
            source={"type": "override_skip", "note": override.note},
            skipped=True,
            skip_reason=override.note or "覆盖表要求跳过",
        )

    cache_path = Path(task_dir) / BOUNDARY_NAME
    if cache_path.exists() and cache_path.stat().st_size > 0 and not refresh:
        return BoundaryResult(path=cache_path, source={"type": "cache", "path": str(cache_path)})

    if override is not None and override.kind == "boundary":
        source_path = Path(override.value)
        if not source_path.is_absolute():
            source_path = Path(repo_root) / source_path
        if not source_path.exists():
            raise BoundaryResolutionError(f"覆盖表边界文件不存在: {source_path}")
        return BoundaryResult(
            path=source_path.resolve(),
            source={"type": "override_boundary", "path": str(source_path), "note": override.note},
        )

    if override is not None and override.kind == "bbox":
        bounds = parse_bbox(override.value)
        save_bbox_boundary(bounds, cache_path)
        return BoundaryResult(
            path=cache_path,
            source={"type": "override_bbox", "bbox": override.value, "note": override.note},
        )

    if override is not None and override.kind == "query":
        query = parse_query_value(override.value)
        save_geocode_boundary(query, cache_path)
        return BoundaryResult(
            path=cache_path,
            source={"type": "override_query", "query": query, "note": override.note},
        )

    if boundaries_dir is not None:
        local_match = match_local_boundary(task, Path(boundaries_dir))
        if local_match is not None:
            save_geometry_boundary(local_match["geometry"], cache_path)
            return BoundaryResult(
                path=cache_path,
                source={
                    "type": "local_boundary",
                    "level": local_match.get("level", ""),
                    "name": local_match.get("matched_name", ""),
                },
            )

    errors: list[str] = []
    for query in query_candidates(task):
        try:
            save_geocode_boundary(query, cache_path)
            return BoundaryResult(path=cache_path, source={"type": "geocode", "query": query})
        except Exception as exc:
            errors.append(f"{query!r}: {exc}")
    raise BoundaryResolutionError("地名解析全部失败: " + " | ".join(errors))


def select_tasks(
    rows: list[dict],
    only: list[str] | None = None,
    continents: list[str] | None = None,
    countries: list[str] | None = None,
    statuses: list[str] | None = None,
    max_tasks: int = 0,
) -> list[dict]:
    only_tokens = [token.strip().lower() for token in (only or []) if token.strip()]
    continent_set = {value.strip().lower() for value in (continents or []) if value.strip()}
    country_set = {value.strip().lower() for value in (countries or []) if value.strip()}
    status_set = {value.strip().upper() for value in (statuses or []) if value.strip()}

    selected: list[dict] = []
    for row in rows:
        if status_set and str(row.get("status", "")).upper() not in status_set:
            continue
        if continent_set and str(row.get("continent", "")).strip().lower() not in continent_set:
            continue
        if country_set and str(row.get("country", "")).strip().lower() not in country_set:
            continue
        if only_tokens:
            task_id = str(row.get("task_id", "")).lower()
            if not any(token in task_id for token in only_tokens):
                continue
        selected.append(row)

    if max_tasks and max_tasks > 0:
        selected = selected[:max_tasks]
    return selected


class StateStore:
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
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                continent TEXT NOT NULL,
                country TEXT NOT NULL,
                city TEXT NOT NULL,
                download_dir TEXT NOT NULL,
                manifest_order INTEGER NOT NULL DEFAULT 0,
                queue_order INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER,
                feature_count INTEGER,
                merge_output TEXT,
                boundary_source TEXT,
                existing_data_dir TEXT,
                processed_3857_dir TEXT,
                last_error TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id);
            """
        )
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        if "existing_data_dir" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN existing_data_dir TEXT")
        if "processed_3857_dir" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN processed_3857_dir TEXT")
        if "queue_order" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN queue_order INTEGER")
            self.conn.execute("UPDATE tasks SET queue_order = manifest_order + 1")

    def close(self) -> None:
        self.conn.close()

    def sync_manifest(self, tasks: list[Task]) -> dict:
        added = 0
        updated = 0
        reset = 0
        now = utc_now()
        next_queue_order = int(
            self.conn.execute("SELECT COALESCE(MAX(queue_order), 0) AS max_order FROM tasks").fetchone()["max_order"]
        ) + 1
        for order, task in enumerate(tasks):
            row = self.get(task.task_id)
            if row is None:
                self.conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, continent, country, city, download_dir,
                        manifest_order, queue_order, status, attempts, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        task.task_id,
                        task.continent,
                        task.country,
                        task.city,
                        task.download_dir,
                        order,
                        next_queue_order,
                        STATUS_PENDING,
                        now,
                    ),
                )
                next_queue_order += 1
                self.add_event(task.task_id, "IMPORT", f"source_row={task.source_row}")
                added += 1
                continue

            if (
                row["continent"] != task.continent
                or row["country"] != task.country
                or row["city"] != task.city
                or row["download_dir"] != task.download_dir
                or row["manifest_order"] != order
            ):
                self.conn.execute(
                    """
                    UPDATE tasks
                    SET continent = ?, country = ?, city = ?, download_dir = ?,
                        manifest_order = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        task.continent,
                        task.country,
                        task.city,
                        task.download_dir,
                        order,
                        now,
                        task.task_id,
                    ),
                )
                updated += 1
                if row["download_dir"] != task.download_dir and row["status"] in SUCCESS_STATUSES:
                    self.conn.execute(
                        """
                        UPDATE tasks
                        SET status = ?, feature_count = NULL, merge_output = NULL,
                            last_error = NULL, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (STATUS_PENDING, now, task.task_id),
                    )
                    self.add_event(task.task_id, "RESET", "download_dir 变更，状态重置为 PENDING")
                    reset += 1
        self.conn.commit()
        return {"added": added, "updated": updated, "reset": reset}

    def get(self, task_id: str) -> dict | None:
        cursor = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def all_tasks(self) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM tasks ORDER BY queue_order IS NULL, queue_order, manifest_order, task_id"
        )
        return [dict(row) for row in cursor.fetchall()]

    def pending_tasks(self) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? "
            "ORDER BY queue_order IS NULL, queue_order, manifest_order, task_id",
            (STATUS_PENDING,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def apply_pending_order(self, ordered_task_ids: list[str]) -> None:
        now = utc_now()
        with self.conn:
            for position, task_id in enumerate(ordered_task_ids, start=1):
                self.conn.execute(
                    "UPDATE tasks SET queue_order = ?, updated_at = ? WHERE task_id = ?",
                    (position, now, task_id),
                )

    def move_task(self, task_id: str, direction: str) -> list[str]:
        if direction not in ("up", "down", "top"):
            raise ValueError(f"不支持的排序方向: {direction}")
        keys = [row["task_id"] for row in self.pending_tasks()]
        if task_id not in keys:
            raise KeyError(f"待处理队列中没有任务: {task_id}")
        index = keys.index(task_id)
        if direction == "top":
            target = 0
        elif direction == "up":
            target = max(index - 1, 0)
        else:
            target = min(index + 1, len(keys) - 1)
        if target != index:
            keys.pop(index)
            keys.insert(target, task_id)
        self.apply_pending_order(keys)
        self.add_event(task_id, "QUEUE_ORDER", f"direction={direction} position={keys.index(task_id) + 1}")
        return keys

    def counts(self) -> dict[str, int]:
        cursor = self.conn.execute("SELECT status, COUNT(*) AS total FROM tasks GROUP BY status")
        return {row["status"]: row["total"] for row in cursor.fetchall()}

    def set_status(self, task_id: str, status: str, **fields) -> None:
        if status not in ALL_STATUSES:
            raise ValueError(f"非法状态: {status}")
        allowed = {
            "attempts",
            "exit_code",
            "feature_count",
            "merge_output",
            "boundary_source",
            "existing_data_dir",
            "processed_3857_dir",
            "last_error",
            "started_at",
            "finished_at",
            "duration_seconds",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"set_status 不支持的字段: {sorted(unknown)}")
        columns = ["status = ?", "updated_at = ?"]
        values: list[object] = [status, utc_now()]
        for key, value in fields.items():
            columns.append(f"{key} = ?")
            values.append(value)
        values.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(columns)} WHERE task_id = ?", values)
        self.conn.commit()

    def set_existing_data(self, task_id: str, path: str | None) -> None:
        self.conn.execute(
            "UPDATE tasks SET existing_data_dir = ?, updated_at = ? WHERE task_id = ?",
            (path, utc_now(), task_id),
        )
        self.conn.commit()

    def set_processed_3857(self, task_id: str, path: str | None) -> None:
        self.conn.execute(
            "UPDATE tasks SET processed_3857_dir = ?, updated_at = ? WHERE task_id = ?",
            (path, utc_now(), task_id),
        )
        self.conn.commit()

    def reset_task(self, task_id: str) -> None:
        self.conn.execute(
            """
            UPDATE tasks
            SET status = ?, attempts = 0, exit_code = NULL, feature_count = NULL,
                last_error = NULL, started_at = NULL, finished_at = NULL,
                duration_seconds = NULL, updated_at = ?
            WHERE task_id = ?
            """,
            (STATUS_PENDING, utc_now(), task_id),
        )
        self.conn.commit()
        self.add_event(task_id, "RESET", "手动重置")

    def requeue_task(self, task_id: str, position: str = "tail") -> list[str]:
        if position not in ("tail", "top"):
            raise ValueError(f"不支持的入队位置: {position}")
        row = self.get(task_id)
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        requeueable_statuses = (STATUS_PENDING, STATUS_FAILED, STATUS_OK, STATUS_OK_EMPTY)
        if row["status"] not in requeueable_statuses:
            raise ValueError(f"只有待执行、失败或已完成任务可以重新入队，当前状态: {row['status']}")

        pending = [item["task_id"] for item in self.pending_tasks() if item["task_id"] != task_id]
        ordered = [task_id, *pending] if position == "top" else [*pending, task_id]
        if row["status"] != STATUS_PENDING:
            self.conn.execute(
                """
                UPDATE tasks
                SET status = ?, attempts = 0, exit_code = NULL, feature_count = NULL, last_error = NULL,
                    started_at = NULL, finished_at = NULL, duration_seconds = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (STATUS_PENDING, utc_now(), task_id),
            )
        self.apply_pending_order(ordered)
        self.add_event(task_id, "REQUEUE", f"position={position}")
        return ordered

    def add_event(self, task_id: str, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event, detail, utc_now()),
        )
        self.conn.commit()

    def recent_events(self, task_id: str, limit: int = 10) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY event_id DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def move_queue_task(state_db: Path, task_id: str, direction: str) -> list[str]:
    store = StateStore(state_db)
    try:
        return store.move_task(task_id, direction)
    finally:
        store.close()


def requeue_task(state_db: Path, task_id: str, position: str = "tail") -> list[str]:
    store = StateStore(state_db)
    try:
        return store.requeue_task(task_id, position)
    finally:
        store.close()
