from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import tempfile

import geopandas as gpd

from shp_pipeline_qgis.clean import clean_shapefile
from shp_pipeline_qgis.reproject import reproject_shapefile
from shp_pipeline_qgis.splitter import MAX_SPLIT_TRIGGER, build_split_plan
from shp_pipeline_qgis.validate_with_qgis import validate_shapefile_with_qgis


@dataclass(frozen=True)
class PipelineItemSummary:
    source_path: Path
    split_outputs: list[Path]
    valid_outputs: list[Path]
    final_outputs: list[Path]


@dataclass(frozen=True)
class PipelineSummary:
    items: list[PipelineItemSummary]


def run_pipeline(input_dir: Path, output_dir: Path) -> PipelineSummary:
    final_dir = output_dir / "final"
    log_dir = output_dir / "logs"

    output_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[PipelineItemSummary] = []
    log_lines: list[str] = []
    source_paths = sorted(input_dir.glob("*.shp"))
    print(f"Starting pipeline: input={input_dir} output={output_dir}", flush=True)
    print(f"Found {len(source_paths)} shapefile(s)", flush=True)

    for index, source_path in enumerate(source_paths, start=1):
        print(f"Processing {index}/{len(source_paths)}: {source_path.name}", flush=True)
        with tempfile.TemporaryDirectory(prefix=".shp_pipeline_qgis_", dir=output_dir) as temp_root:
            temp_root_path = Path(temp_root)
            split_dir = temp_root_path / "split"
            valid_dir = temp_root_path / "valid"
            clean_dir = temp_root_path / "clean"
            split_dir.mkdir(parents=True, exist_ok=True)
            valid_dir.mkdir(parents=True, exist_ok=True)
            clean_dir.mkdir(parents=True, exist_ok=True)

            source_gdf = gpd.read_file(source_path)
            print(f"Loaded {source_path.name}: feature_count={len(source_gdf)}", flush=True)
            if len(source_gdf) > MAX_SPLIT_TRIGGER:
                print(f"Splitting {source_path.name} because feature_count>{MAX_SPLIT_TRIGGER}", flush=True)
                plan = build_split_plan(source_gdf)
                work_items = []
                for chunk_index, chunk in enumerate(plan.chunks, start=1):
                    chunk_gdf = source_gdf.iloc[list(chunk.indices)].copy()
                    target = split_dir / f"{source_path.stem}_001_{chunk_index:03d}.shp"
                    chunk_gdf.to_file(target)
                    work_items.append(target)
                print(f"Split {source_path.name} into {len(work_items)} chunk(s)", flush=True)
            else:
                work_items = [source_path]
                print(f"Skipping split for {source_path.name}", flush=True)

            final_outputs: list[Path] = []
            for work_item in work_items:
                print(f"Running QGIS validity check: {work_item.name}", flush=True)
                validation = validate_shapefile_with_qgis(work_item, valid_dir)
                print(
                    f"QGIS result for {work_item.name}: valid={validation.valid_count} "
                    f"invalid={validation.invalid_count} errors={validation.error_count}",
                    flush=True,
                )
                print(f"Cleaning valid output: {validation.output_path.name}", flush=True)
                clean_result = clean_shapefile(validation.output_path, clean_dir)
                print(
                    f"Clean result for {validation.output_path.name}: output={clean_result.output_count} "
                    f"empty_removed={clean_result.removed_empty_count} "
                    f"zero_height_removed={clean_result.removed_zero_height_count} "
                    f"duplicate_removed={clean_result.removed_duplicate_count}",
                    flush=True,
                )
                print(f"Reprojecting to EPSG:3857: {clean_result.output_path.name}", flush=True)
                final_outputs.append(reproject_shapefile(clean_result.output_path, final_dir))
                log_lines.append(
                    f"{source_path.name}\t{work_item.name}\tvalid={validation.valid_count}\t"
                    f"invalid={validation.invalid_count}\terrors={validation.error_count}\t"
                    f"clean={clean_result.output_count}\tempty_removed={clean_result.removed_empty_count}\t"
                    f"zero_height_removed={clean_result.removed_zero_height_count}\t"
                    f"duplicate_removed={clean_result.removed_duplicate_count}"
                )

            summaries.append(
                PipelineItemSummary(
                    source_path=source_path,
                    split_outputs=[],
                    valid_outputs=[],
                    final_outputs=final_outputs,
                )
            )
            print(f"Completed {source_path.name}; cleaned temporary split/valid data", flush=True)

    (log_dir / "pipeline.log").write_text("\n".join(log_lines), encoding="utf-8")
    print(f"Pipeline finished: final_dir={final_dir} log_file={log_dir / 'pipeline.log'}", flush=True)
    return PipelineSummary(items=summaries)


def main() -> None:
    parser = argparse.ArgumentParser(description="Split shapefiles, validate with QGIS, and reproject to EPSG:3857.")
    parser.add_argument("input_dir", type=Path, help="Directory containing source shapefiles.")
    parser.add_argument("output_dir", type=Path, help="Directory where processed files will be written.")
    args = parser.parse_args()
    run_pipeline(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
