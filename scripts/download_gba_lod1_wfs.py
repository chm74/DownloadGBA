import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

import geopandas as gpd
import osmnx as ox
import pandas as pd
from shapely.geometry import box, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gba_wfs_client import WFS_URL, get_thread_client
from gba_contract import DATA_SEMANTICS_VERSION, GRID_MANIFEST_NAME, TILE_DIRECTORY_NAME
from gba_grid import GRID_ALGORITHM_VERSION, build_national_grid

TYPE_NAME = "global3D:lod1_global"
DATA_SOURCE_NAME = "GlobalBuildingAtlas LoD1 WFS"
TILE_COMPONENT_SUFFIXES = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".fix")

PRINT_LOCK = Lock()


def log_line(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def print_download_source(mode: str, boundary_source: str | None = None) -> None:
    print(f"Building SHP source: {DATA_SOURCE_NAME}")
    print(f"  WFS URL: {WFS_URL}")
    print(f"  TypeName: {TYPE_NAME}")
    print(f"  Mode: {mode}")
    if boundary_source:
        print(f"  Boundary source: {boundary_source}")


def features_to_gdf(features: list[dict]) -> gpd.GeoDataFrame:
    rows = []
    for feature in features:
        props = feature.get("properties", {}).copy()
        props["geometry"] = shape(feature["geometry"])
        rows.append(props)
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
    return gdf


def normalize_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = gdf.copy()
    if "id" in result.columns:
        result["GBA_ID"] = result["id"].astype(str)
    if "height" in result.columns:
        result["Height"] = pd.to_numeric(result["height"], errors="coerce")
    if "var" in result.columns:
        result["H_VAR"] = pd.to_numeric(result["var"], errors="coerce")
    if "source" in result.columns:
        result["SOURCE"] = result["source"].astype(str)
    if "region" in result.columns:
        result["REGION"] = result["region"].astype(str)

    keep_cols = [
        column
        for column in ["GBA_ID", "Height", "H_VAR", "SOURCE", "REGION", "TILE_ID", "geometry"]
        if column in result.columns
    ]
    return result[keep_cols].copy()


def fetch_bbox(minx: float, miny: float, maxx: float, maxy: float, page_size: int = 0, max_pages: int = 0) -> gpd.GeoDataFrame:
    client = get_thread_client()
    features, stats = client.fetch_bbox_features(
        minx,
        miny,
        maxx,
        maxy,
        max_requests=max_pages if max_pages and max_pages > 0 else 0,
    )
    log_line(
        f"bbox=({minx},{miny},{maxx},{maxy}) requests={stats.requests} "
        f"splits={stats.splits} features={len(features)}"
    )
    if not features:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)
    return gpd.GeoDataFrame(features_to_gdf(features), crs=4326)


def load_boundary(boundary_path: Path) -> gpd.GeoDataFrame:
    boundary = gpd.read_file(boundary_path)
    if boundary.crs is None:
        boundary = boundary.set_crs(4326)
    else:
        boundary = boundary.to_crs(4326)
    return boundary


def load_place_boundary(place: str) -> gpd.GeoDataFrame:
    boundary = ox.geocode_to_gdf(place)
    if boundary.crs is None:
        boundary = boundary.set_crs(4326)
    else:
        boundary = boundary.to_crs(4326)
    return boundary


def build_grid(boundary: gpd.GeoDataFrame, grid_size: float) -> gpd.GeoDataFrame:
    return build_national_grid(boundary, grid_size)


def save_gdf(gdf: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, encoding="utf-8")


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def tile_completion_path(output_path: Path) -> Path:
    return output_path.with_suffix(".done.json")


def dataset_components(path: Path) -> list[Path]:
    if path.suffix.lower() != ".shp":
        return [path] if path.exists() else []
    return [candidate for suffix in TILE_COMPONENT_SUFFIXES if (candidate := path.with_suffix(suffix)).exists()]


def remove_dataset(path: Path) -> None:
    if path.suffix.lower() == ".shp":
        for suffix in TILE_COMPONENT_SUFFIXES:
            path.with_suffix(suffix).unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)


def file_manifest(path: Path) -> dict[str, dict[str, int | str]]:
    return {
        component.name: {
            "size": component.stat().st_size,
            "sha256": sha256_file(component),
        }
        for component in dataset_components(path)
    }


def _validate_written_dataset(path: Path, expected_count: int) -> None:
    import pyogrio

    info = pyogrio.read_info(str(path))
    actual_count = int(info.get("features") or 0)
    if actual_count != expected_count:
        raise RuntimeError(f"写入要素数不一致: expected={expected_count} actual={actual_count} file={path}")
    if str(info.get("crs") or "").upper() != "EPSG:4326":
        raise RuntimeError(f"瓦片 CRS 不是 EPSG:4326: {path} crs={info.get('crs')}")


def _publish_temporary_dataset(temporary: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    remove_dataset(output_path)
    if output_path.suffix.lower() == ".shp":
        components = dataset_components(temporary)
        if not components:
            raise RuntimeError(f"临时 SHP 没有可发布组件: {temporary}")
        for component in components:
            os.replace(component, output_path.with_suffix(component.suffix))
    else:
        os.replace(temporary, output_path)


def write_tile_completion(
    output_path: Path,
    grid_id: str,
    bounds: tuple[float, float, float, float],
    status: str,
    feature_count: int,
) -> None:
    payload = {
        "status": status,
        "data_semantics_version": DATA_SEMANTICS_VERSION,
        "grid_algorithm": GRID_ALGORITHM_VERSION,
        "grid_id": grid_id,
        "bbox": list(bounds),
        "feature_count": int(feature_count),
        "crs": "EPSG:4326",
        "files": file_manifest(output_path) if status == "COMPLETE" else {},
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(tile_completion_path(output_path), payload)


def save_completed_tile(
    gdf: gpd.GeoDataFrame,
    output_path: Path,
    grid_id: str,
    bounds: tuple[float, float, float, float],
) -> None:
    partial_dir = output_path.parent / ".partial"
    partial_dir.mkdir(parents=True, exist_ok=True)
    temporary = partial_dir / output_path.name
    remove_dataset(temporary)
    try:
        save_gdf(gdf, temporary)
        _validate_written_dataset(temporary, len(gdf))
        _publish_temporary_dataset(temporary, output_path)
        write_tile_completion(output_path, grid_id, bounds, "COMPLETE", len(gdf))
    finally:
        remove_dataset(temporary)


def save_empty_tile(
    output_path: Path,
    grid_id: str,
    bounds: tuple[float, float, float, float],
) -> None:
    remove_dataset(output_path)
    write_tile_completion(output_path, grid_id, bounds, "EMPTY", 0)


def load_valid_tile_completion(output_path: Path, grid_id: str) -> dict | None:
    marker_path = tile_completion_path(output_path)
    if not marker_path.exists():
        return None
    try:
        metadata = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if metadata.get("data_semantics_version") != DATA_SEMANTICS_VERSION:
        return None
    if metadata.get("grid_algorithm") != GRID_ALGORITHM_VERSION:
        return None
    if metadata.get("grid_id") != grid_id:
        return None
    status = metadata.get("status")
    if status == "EMPTY":
        return metadata if int(metadata.get("feature_count") or 0) == 0 else None
    if status != "COMPLETE" or not output_path.exists():
        return None

    expected_files = metadata.get("files")
    if not isinstance(expected_files, dict) or not expected_files:
        return None
    actual_components = {component.name: component for component in dataset_components(output_path)}
    if set(actual_components) != set(expected_files):
        return None
    for name, expected in expected_files.items():
        component = actual_components[name]
        if component.stat().st_size != int(expected.get("size") or -1):
            return None
        if sha256_file(component) != expected.get("sha256"):
            return None
    try:
        _validate_written_dataset(output_path, int(metadata.get("feature_count") or 0))
    except (OSError, RuntimeError, ValueError):
        return None
    return metadata


def write_grid_manifest(
    output_dir: Path,
    boundary: gpd.GeoDataFrame,
    grid: gpd.GeoDataFrame,
    grid_size: float,
    boundary_source: str | None,
) -> Path:
    normalized = boundary.set_crs(4326) if boundary.crs is None else boundary.to_crs(4326)
    geom = normalized.union_all() if hasattr(normalized, "union_all") else normalized.unary_union
    payload = {
        "grid_algorithm": GRID_ALGORITHM_VERSION,
        "data_semantics_version": DATA_SEMANTICS_VERSION,
        "grid_size": float(grid_size),
        "origin": [-180.0, -90.0],
        "boundary_source": boundary_source,
        "boundary_sha256": hashlib.sha256(geom.wkb).hexdigest(),
        "expected_tile_count": len(grid),
        "grid_ids": grid["GRID_ID"].tolist(),
    }
    target = output_dir / GRID_MANIFEST_NAME
    write_json_atomic(target, payload)
    return target


def merge_outputs(tile_paths: list[Path], output_path: Path) -> None:
    if not tile_paths:
        empty = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)
        save_gdf(empty, output_path)
        log_line(f"[MERGE] 无分块，已写出空成果 -> {output_path}")
        return

    total_tiles = len(tile_paths)
    started = time.monotonic()
    log_line(f"[MERGE] 开始合并: 分块数={total_tiles} 输出={output_path}")

    frames = []
    total_features = 0
    for index, path in enumerate(tile_paths, start=1):
        frame = gpd.read_file(path).to_crs(4326)
        frames.append(frame)
        total_features += len(frame)
        if index % 25 == 0 or index == total_tiles:
            elapsed = time.monotonic() - started
            log_line(
                f"[MERGE] 已读取 {index}/{total_tiles} 个分块，要素累计={total_features:,}，"
                f"用时={elapsed:.0f}s"
            )

    merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=4326)
    log_line(f"[MERGE] 拼接完成: 要素={len(merged):,}，开始按几何去重...")
    merged["_GEOM_KEY"] = merged.geometry.to_wkb(hex=True)
    merged = merged.drop_duplicates(subset=["_GEOM_KEY"], keep="first").drop(columns=["_GEOM_KEY"]).copy()
    removed = total_features - len(merged)
    log_line(f"[MERGE] 去重完成: 去除重复={removed:,}，写出 {len(merged):,} 个要素 -> {output_path}")
    save_gdf(merged, output_path)
    elapsed = time.monotonic() - started
    log_line(f"[MERGE] 合并完成: 要素={len(merged):,}，总用时={elapsed:.0f}s")


def run_bbox_mode(bbox_text: str, output_path: Path, page_size: int, max_pages: int) -> None:
    print_download_source("bbox")
    minx, miny, maxx, maxy = [float(value) for value in bbox_text.split(",")]
    gdf = fetch_bbox(minx, miny, maxx, maxy, page_size=page_size, max_pages=max_pages)
    gdf = normalize_columns(gdf)
    save_gdf(gdf, output_path)
    print(f"Saved {len(gdf)} features to {output_path}")


def run_boundary_mode(
    boundary: gpd.GeoDataFrame,
    output_dir: Path,
    grid_size: float,
    page_size: int,
    max_pages: int,
    tile_format: str,
    merge_output: Path | None,
    boundary_source: str | None = None,
    source_mode: str = "boundary",
    tile_workers: int = 1,
) -> None:
    print_download_source(source_mode, boundary_source)
    log_line(f"Coverage semantics: full-grid ({DATA_SEMANTICS_VERSION})")
    log_line("Boundary usage: grid selection only")
    log_line("Feature boundary filtering: disabled")
    log_line(f"Grid algorithm: {GRID_ALGORITHM_VERSION}")
    grid = build_grid(boundary, grid_size)
    grid_path = output_dir / "gba_wfs_grid.gpkg"
    save_gdf(grid, grid_path)
    write_grid_manifest(output_dir, boundary, grid, grid_size, boundary_source)

    tile_suffix = ".shp" if tile_format.lower() == "shp" else ".gpkg"
    tile_dir = output_dir / TILE_DIRECTORY_NAME
    tile_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    empty_results: set[str] = set()
    pending: list[dict] = []

    for _, row in grid.iterrows():
        grid_id = row["GRID_ID"]
        output_path = tile_dir / f"{grid_id}{tile_suffix}"
        completion = load_valid_tile_completion(output_path, grid_id)
        if completion is not None and completion.get("status") == "COMPLETE":
            results[grid_id] = output_path
            log_line(f"Reused completed tile {output_path}")
            continue
        if completion is not None and completion.get("status") == "EMPTY":
            empty_results.add(grid_id)
            log_line(f"Reused empty tile marker {tile_completion_path(output_path)}")
            continue
        pending.append(
            {
                "grid_id": grid_id,
                "output_path": output_path,
                "bounds": tuple(row.geometry.bounds),
            }
        )

    def process_tile(entry: dict) -> tuple[str, Path | None, int]:
        grid_id = entry["grid_id"]
        output_path = entry["output_path"]
        minx, miny, maxx, maxy = entry["bounds"]
        gdf = fetch_bbox(minx, miny, maxx, maxy, page_size=page_size, max_pages=max_pages)
        if gdf.empty:
            save_empty_tile(output_path, grid_id, entry["bounds"])
            log_line(f"[TILE] {grid_id} completed empty -> {tile_completion_path(output_path)}")
            return grid_id, None, 0
        gdf["TILE_ID"] = grid_id
        gdf = normalize_columns(gdf)
        save_completed_tile(gdf, output_path, grid_id, entry["bounds"])
        log_line(f"[TILE] {grid_id} saved features={len(gdf)} -> {output_path}")
        return grid_id, output_path, len(gdf)

    if pending:
        effective_workers = max(1, min(int(tile_workers), len(pending)))
        log_line(f"Tile workers: {effective_workers} (pending tiles={len(pending)}, reused={len(results)})")
        if effective_workers == 1:
            for entry in pending:
                grid_id, output_path, _ = process_tile(entry)
                if output_path is not None:
                    results[grid_id] = output_path
                else:
                    empty_results.add(grid_id)
        else:
            errors: list[tuple[str, Exception]] = []
            with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                futures = {executor.submit(process_tile, entry): entry for entry in pending}
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        grid_id, output_path, _ = future.result()
                    except Exception as exc:
                        errors.append((entry["grid_id"], exc))
                        for other in futures:
                            other.cancel()
                        continue
                    if output_path is not None:
                        results[grid_id] = output_path
                    else:
                        empty_results.add(grid_id)
            if errors:
                details = "; ".join(f"{grid_id}: {exc}" for grid_id, exc in errors[:5])
                raise RuntimeError(f"{len(errors)} 个格网抓取失败，已保留完成分块: {details}")

    expected_ids = set(grid["GRID_ID"].tolist())
    completed_ids = set(results) | empty_results
    missing_ids = sorted(expected_ids - completed_ids)
    if missing_ids:
        preview = ", ".join(missing_ids[:10])
        raise RuntimeError(f"格网完整性检查失败: missing={len(missing_ids)} ids={preview}")

    tile_paths = [results[row["GRID_ID"]] for _, row in grid.iterrows() if row["GRID_ID"] in results]

    if merge_output is not None:
        merge_outputs(tile_paths, merge_output)
        log_line(
            f"Merged {len(tile_paths)} tile outputs to {merge_output}; "
            f"complete={len(results)} empty={len(empty_results)} expected={len(grid)}"
        )


def run_place_mode(
    place: str,
    output_dir: Path,
    grid_size: float,
    page_size: int,
    max_pages: int,
    tile_format: str,
    merge_output: Path | None,
    tile_workers: int = 1,
) -> None:
    boundary = load_place_boundary(place)
    boundary_path = output_dir / "place_boundary.gpkg"
    save_gdf(boundary, boundary_path)
    print(f"Saved place boundary to {boundary_path}")
    run_boundary_mode(
        boundary,
        output_dir,
        grid_size,
        page_size,
        max_pages,
        tile_format,
        merge_output,
        boundary_source=f"OSM Nominatim place query: {place}",
        source_mode="place",
        tile_workers=tile_workers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Directly export building SHP/GPKG from the official GBA WFS.")
    parser.add_argument("--bbox", help="Explicit bbox as minx,miny,maxx,maxy in EPSG:4326.")
    parser.add_argument("--output", help="Output file for bbox mode (.gpkg or .shp).")
    parser.add_argument("--place", help="Place name resolved by OSM Nominatim, for example Beijing, China.")
    parser.add_argument("--boundary", help="Boundary file for grid mode (.shp or .gpkg).")
    parser.add_argument("--output-dir", help="Output directory for grid mode.")
    parser.add_argument("--grid-size", type=float, default=0.2, help="Grid size in degrees for place/boundary mode.")
    parser.add_argument("--page-size", type=int, default=5000, help="WFS page size.")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional page limit for smoke testing.")
    parser.add_argument("--tile-format", choices=["gpkg", "shp"], default="gpkg", help="Per-tile output format for place/boundary mode.")
    parser.add_argument("--merge-output", help="Optional merged output file for place/boundary mode (.gpkg or .shp).")
    parser.add_argument("--tile-workers", type=int, default=1, help="Concurrent tile workers for place/boundary mode (default 1).")
    args = parser.parse_args()

    if args.bbox:
        if not args.output:
            raise SystemExit("--output is required when using --bbox")
        run_bbox_mode(args.bbox, Path(args.output), args.page_size, args.max_pages)
        return

    if args.place:
        if not args.output_dir:
            raise SystemExit("--output-dir is required when using --place")
        run_place_mode(
            args.place,
            Path(args.output_dir),
            args.grid_size,
            args.page_size,
            args.max_pages,
            args.tile_format,
            Path(args.merge_output) if args.merge_output else None,
            tile_workers=args.tile_workers,
        )
        return

    if args.boundary:
        if not args.output_dir:
            raise SystemExit("--output-dir is required when using --boundary")
        boundary = load_boundary(Path(args.boundary))
        run_boundary_mode(
            boundary,
            Path(args.output_dir),
            args.grid_size,
            args.page_size,
            args.max_pages,
            args.tile_format,
            Path(args.merge_output) if args.merge_output else None,
            tile_workers=args.tile_workers,
        )
        return

    raise SystemExit("Use either --bbox with --output, or --place/--boundary with --output-dir")


if __name__ == "__main__":
    main()
