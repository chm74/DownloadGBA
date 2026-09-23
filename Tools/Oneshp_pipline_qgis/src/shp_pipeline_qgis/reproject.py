from pathlib import Path
import argparse

import geopandas as gpd


def reproject_shapefile(source_path: Path, output_dir: Path) -> Path:
    gdf = gpd.read_file(source_path)
    if gdf.crs is None:
        raise ValueError(f"{source_path} is missing CRS information")

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{_clean_output_stem(source_path.stem)}_3857.shp"
    gdf.to_crs(epsg=3857).to_file(target)
    return target


def _clean_output_stem(stem: str) -> str:
    clean_stem = stem
    for suffix in ("_valid", "_clean"):
        clean_stem = clean_stem.replace(suffix, "")
    return clean_stem


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproject a shapefile to EPSG:3857.")
    parser.add_argument("input_shp", type=Path, help="Input shapefile path.")
    parser.add_argument("output_dir", type=Path, help="Output directory for EPSG:3857 shapefiles.")
    args = parser.parse_args()
    reproject_shapefile(args.input_shp, args.output_dir)
