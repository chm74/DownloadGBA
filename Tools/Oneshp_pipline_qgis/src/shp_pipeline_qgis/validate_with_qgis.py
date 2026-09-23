from dataclasses import dataclass
from pathlib import Path
import subprocess
import argparse

import geopandas as gpd

from shp_pipeline_qgis.paths import QGIS_PROCESS_BAT


@dataclass(frozen=True)
class ValidationResult:
    output_path: Path
    valid_count: int
    invalid_count: int
    error_count: int


def validate_shapefile_with_qgis(source_path: Path, output_dir: Path) -> ValidationResult:
    output_dir.mkdir(parents=True, exist_ok=True)

    valid_output = output_dir / f"{source_path.stem}_valid.shp"
    invalid_output = output_dir / f"{source_path.stem}_invalid.shp"
    error_output = output_dir / f"{source_path.stem}_errors.gpkg"

    command = [
        "cmd.exe",
        "/c",
        str(QGIS_PROCESS_BAT),
        "run",
        "qgis:checkvalidity",
        "--",
        f"INPUT_LAYER={source_path}",
        "METHOD=2",
        "IGNORE_RING_SELF_INTERSECTION=0",
        f"VALID_OUTPUT={valid_output}",
        f"INVALID_OUTPUT={invalid_output}",
        f"ERROR_OUTPUT={error_output}",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)

    valid_gdf = gpd.read_file(valid_output)
    invalid_gdf = gpd.read_file(invalid_output) if invalid_output.exists() else gpd.GeoDataFrame()
    error_gdf = gpd.read_file(error_output) if error_output.exists() else gpd.GeoDataFrame()

    return ValidationResult(
        output_path=valid_output,
        valid_count=len(valid_gdf),
        invalid_count=len(invalid_gdf),
        error_count=len(error_gdf),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a shapefile with QGIS and keep only valid features.")
    parser.add_argument("input_shp", type=Path, help="Input shapefile path.")
    parser.add_argument("output_dir", type=Path, help="Output directory for QGIS validation results.")
    args = parser.parse_args()
    validate_shapefile_with_qgis(args.input_shp, args.output_dir)
