'''
Author: yejinliang
Date: 2026-04-23 18:33:28
LastEditTime: 2026-04-23 19:35:30
LastEditors: yejinliang
Description: 
'''
import shutil
from pathlib import Path

city_map = {
    "beijing":"北京市"
}


def classify_and_move(input_dir: Path):
    """按 city_map 前缀分类文件并移动到对应中文省份文件夹中。"""
    for file in input_dir.iterdir():
        if not file.is_file():
            continue
        for key in city_map:
            if file.name.startswith(key + "_"):
                target_dir = input_dir / city_map[key]
                target_dir.mkdir(exist_ok=True)
                shutil.move(str(file), target_dir / file.name)
                break


if __name__ == "__main__":
    input_dir = Path(r"D:\2024全国含高度建筑轮廓矢量shp数据【GIS】\中国_20260424\final")
    classify_and_move(input_dir)