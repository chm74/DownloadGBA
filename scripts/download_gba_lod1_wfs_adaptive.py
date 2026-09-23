import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_gba_lod1_wfs import (  # noqa: E402
    WFS_URL,
    run_bbox_mode as standard_run_bbox_mode,
    run_boundary_mode as standard_run_boundary_mode,
    run_place_mode as standard_run_place_mode,
)

TYPE_NAME = "global3D:lod1_global"
DATA_SOURCE_NAME = "GlobalBuildingAtlas LoD1 WFS"


def run_bbox_mode(bbox_text: str, output_path: Path, page_size: int, max_pages: int, retries: int) -> None:
    standard_run_bbox_mode(bbox_text, output_path, page_size, max_pages)


def run_boundary_mode_adaptive(
    boundary,
    output_dir: Path,
    grid_size: float,
    page_size: int,
    max_pages: int,
    tile_format: str,
    merge_output: Path | None,
    split_threshold: int,
    failure_split_retries: int,
    min_grid_size: float,
    probe_page_size: int,
    boundary_source: str | None = None,
    source_mode: str = "boundary",
    tile_workers: int = 1,
) -> None:
    print(
        "Adaptive fetch: 切分逻辑已内置于 WFS 客户端（单请求跨度上限 0.09°，"
        f"截断/超限/失败自动四分裂，最小 {min_grid_size}°）"
    )
    standard_run_boundary_mode(
        boundary,
        output_dir,
        grid_size,
        page_size,
        max_pages,
        tile_format,
        merge_output,
        boundary_source=boundary_source,
        source_mode=source_mode,
        tile_workers=tile_workers,
    )


def run_place_mode(
    place: str,
    output_dir: Path,
    grid_size: float,
    page_size: int,
    max_pages: int,
    tile_format: str,
    merge_output: Path | None,
    split_threshold: int,
    failure_split_retries: int,
    min_grid_size: float,
    probe_page_size: int,
    tile_workers: int = 1,
) -> None:
    standard_run_place_mode(
        place,
        output_dir,
        grid_size,
        page_size,
        max_pages,
        tile_format,
        merge_output,
        tile_workers=tile_workers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Directly export building SHP/GPKG from the official GBA WFS with adaptive splitting for oversized tiles."
    )
    parser.add_argument("--bbox", help="Explicit bbox as minx,miny,maxx,maxy in EPSG:4326.")
    parser.add_argument("--output", help="Output file for bbox mode (.gpkg or .shp).")
    parser.add_argument("--place", help="Place name resolved by OSM Nominatim, for example Beijing, China.")
    parser.add_argument("--boundary", help="Boundary file for grid mode (.shp or .gpkg).")
    parser.add_argument("--output-dir", help="Output directory for grid mode.")
    parser.add_argument("--grid-size", type=float, default=0.2, help="Grid size in degrees for place/boundary mode.")
    parser.add_argument("--page-size", type=int, default=5000, help="WFS page size (compatibility only).")
    parser.add_argument("--max-pages", type=int, default=0, help="Optional request limit for smoke testing.")
    parser.add_argument("--tile-format", choices=["gpkg", "shp"], default="gpkg", help="Per-tile output format for place/boundary mode.")
    parser.add_argument("--merge-output", help="Optional merged output file for place/boundary mode (.gpkg or .shp).")
    parser.add_argument("--split-threshold", type=int, default=20000, help="Compatibility option; splitting is handled by the client.")
    parser.add_argument("--failure-split-retries", type=int, default=5, help="Retry count before splitting a failing request.")
    parser.add_argument("--min-grid-size", type=float, default=0.05, help="Minimum adaptive child tile size in degrees.")
    parser.add_argument("--probe-page-size", type=int, default=1, help="Compatibility option; no separate probe is used.")
    parser.add_argument("--tile-workers", type=int, default=1, help="Concurrent tile workers for place/boundary mode (default 1).")
    args = parser.parse_args()

    if args.bbox:
        if not args.output:
            raise SystemExit("--output is required when using --bbox")
        run_bbox_mode(args.bbox, Path(args.output), args.page_size, args.max_pages, args.failure_split_retries)
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
            args.split_threshold,
            args.failure_split_retries,
            args.min_grid_size,
            args.probe_page_size,
            tile_workers=args.tile_workers,
        )
        return

    if args.boundary:
        if not args.output_dir:
            raise SystemExit("--output-dir is required when using --boundary")
        from download_gba_lod1_wfs import load_boundary  # noqa: PLC0415

        boundary = load_boundary(Path(args.boundary))
        run_boundary_mode_adaptive(
            boundary,
            Path(args.output_dir),
            args.grid_size,
            args.page_size,
            args.max_pages,
            args.tile_format,
            Path(args.merge_output) if args.merge_output else None,
            args.split_threshold,
            args.failure_split_retries,
            args.min_grid_size,
            args.probe_page_size,
            tile_workers=args.tile_workers,
        )
        return

    raise SystemExit("Use either --bbox with --output, or --place/--boundary with --output-dir")


if __name__ == "__main__":
    main()
