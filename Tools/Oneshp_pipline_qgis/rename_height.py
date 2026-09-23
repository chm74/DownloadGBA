"""将目录中所有 shp 文件的高度列重命名为 Height。"""

import argparse
from pathlib import Path

import geopandas as gpd


def _resolve_height_column(columns: list[str]) -> str | None:
    for column_name in ("Height", "height", "HEIGHT"):
        if column_name in columns:
            return column_name
    return None


def rename_height_column(directory: str) -> None:
    target_dir = Path(directory)
    if not target_dir.is_dir():
        print(f"错误: {directory} 不是有效目录")
        return

    shp_files = list(target_dir.glob("*.shp"))
    if not shp_files:
        print(f"在 {directory} 中未找到 shp 文件")
        return

    for shp_file in shp_files:
        gdf = gpd.read_file(shp_file)
        source_column = _resolve_height_column(gdf.columns.tolist())
        if source_column is None:
            print(f"跳过 {shp_file.name}: 没有 height 或 HEIGHT 列")
            continue

        if source_column == "Height":
            print(f"跳过 {shp_file.name}: 已经是 Height 列")
            continue

        gdf = gdf.rename(columns={source_column: "Height"})
        gdf.to_file(shp_file, encoding="utf-8")
        print(f"已处理: {shp_file.name} ({source_column} -> Height)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将 shp 文件中的 height 或 HEIGHT 列重命名为 Height")
    parser.add_argument("directory", help="包含 shp 文件的目录路径")
    args = parser.parse_args()
    rename_height_column(args.directory)
