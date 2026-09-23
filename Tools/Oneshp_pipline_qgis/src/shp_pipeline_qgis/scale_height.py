from dataclasses import dataclass
from pathlib import Path
import argparse

import geopandas as gpd
import pandas as pd


@dataclass(frozen=True)
class ScaleHeightResult:
    source_path: Path
    output_path: Path
    feature_count: int
    scaled_count: int
    null_count: int
    non_numeric_count: int


def scale_height_path(input_path: Path, output_dir: Path, field_name: str, factor: float) -> list[ScaleHeightResult]:
    shapefiles = _resolve_shapefiles(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return [
        scale_height_shapefile(source_path=source_path, output_dir=output_dir, field_name=field_name, factor=factor)
        for source_path in shapefiles
    ]


def scale_height_shapefile(source_path: Path, output_dir: Path, field_name: str, factor: float) -> ScaleHeightResult:
    gdf = gpd.read_file(source_path)
    if field_name not in gdf.columns:
        raise ValueError(f"{source_path} is missing field {field_name!r}")

    numeric_values = pd.to_numeric(gdf[field_name], errors="coerce")
    original_values = gdf[field_name]
    null_mask = original_values.isna()
    scaled_mask = numeric_values.notna()
    non_numeric_mask = original_values.notna() & ~scaled_mask

    scaled_gdf = gdf.copy()
    scaled_gdf.loc[scaled_mask, field_name] = numeric_values.loc[scaled_mask] * factor

    target = output_dir / source_path.name
    scaled_gdf.to_file(target)
    return ScaleHeightResult(
        source_path=source_path,
        output_path=target,
        feature_count=len(scaled_gdf),
        scaled_count=int(scaled_mask.sum()),
        null_count=int(null_mask.sum()),
        non_numeric_count=int(non_numeric_mask.sum()),
    )


def _resolve_shapefiles(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".shp":
            raise ValueError(f"{input_path} is not a shapefile")
        return [input_path]

    if input_path.is_dir():
        shapefiles = sorted(input_path.glob("*.shp"))
        if not shapefiles:
            raise ValueError(f"{input_path} does not contain any .shp files")
        return shapefiles

    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scale a height field for one shapefile or every shapefile in a directory."
    )
    parser.add_argument("input_path", type=Path, help="Input shapefile path or directory containing shapefiles.")
    parser.add_argument("output_dir", type=Path, help="Output directory for scaled shapefiles.")
    parser.add_argument("factor", type=float, help="Multiplier applied to the height field, for example 0.65.")
    parser.add_argument(
        "--field",
        default="HEIGHT",
        help="Height field name to scale. Defaults to HEIGHT.",
    )
    args = parser.parse_args()

    results = scale_height_path(
        input_path=args.input_path,
        output_dir=args.output_dir,
        field_name=args.field,
        factor=args.factor,
    )
    for result in results:
        print(
            f"Processed {result.source_path.name}: "
            f"features={result.feature_count}, "
            f"scaled={result.scaled_count}, "
            f"null={result.null_count}, "
            f"non_numeric={result.non_numeric_count}"
        )

    print(
        f"Completed {len(results)} shapefile(s) with factor={args.factor} on field {args.field!r}. "
        f"Output directory: {args.output_dir}"
    )


if __name__ == "__main__":
    main()
