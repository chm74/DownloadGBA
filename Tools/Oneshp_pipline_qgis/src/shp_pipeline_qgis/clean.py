from dataclasses import dataclass
from pathlib import Path
import argparse

import geopandas as gpd
import pandas as pd


@dataclass(frozen=True)
class CleanResult:
    output_path: Path
    input_count: int
    output_count: int
    removed_empty_count: int
    removed_zero_height_count: int
    removed_duplicate_count: int


def clean_shapefile(source_path: Path, output_dir: Path) -> CleanResult:
    gdf = gpd.read_file(source_path)
    input_count = len(gdf)

    non_empty = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    removed_empty_count = input_count - len(non_empty)

    height_field_name = _resolve_height_field_name(non_empty)
    without_zero_height = non_empty
    removed_zero_height_count = 0
    if height_field_name is not None:
        numeric_height = pd.to_numeric(non_empty[height_field_name], errors="coerce")
        zero_height_mask = numeric_height.notna() & (numeric_height <= 0)
        without_zero_height = non_empty.loc[~zero_height_mask].copy()
        removed_zero_height_count = int(zero_height_mask.sum())

    without_zero_height["_geom_key"] = without_zero_height.geometry.to_wkb(hex=True)
    cleaned = without_zero_height.drop_duplicates(subset=["_geom_key"]).drop(columns=["_geom_key"])
    removed_duplicate_count = len(without_zero_height) - len(cleaned)

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{source_path.stem}_clean.shp"
    cleaned.to_file(target)

    return CleanResult(
        output_path=target,
        input_count=input_count,
        output_count=len(cleaned),
        removed_empty_count=removed_empty_count,
        removed_zero_height_count=removed_zero_height_count,
        removed_duplicate_count=removed_duplicate_count,
    )


def _resolve_height_field_name(gdf: gpd.GeoDataFrame) -> str | None:
    for field_name in ("HEIGHT", "Height"):
        if field_name in gdf.columns:
            return field_name
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove empty geometries, zero-height buildings, and duplicate geometries from a shapefile."
    )
    parser.add_argument("input_shp", type=Path, help="Input shapefile path.")
    parser.add_argument("output_dir", type=Path, help="Output directory for cleaned shapefiles.")
    args = parser.parse_args()
    clean_shapefile(args.input_shp, args.output_dir)


if __name__ == "__main__":
    main()
