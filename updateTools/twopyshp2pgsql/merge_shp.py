# -*- coding: utf-8 -*-
"""
批量合并任意数量 Shapefile（含坐标系统一、可选去重）
python merge_shps.py
"""
import geopandas as gpd
import contextlib
import os
import io
import sys
import time
import warnings
from pathlib import Path

# ========== 1. 参数区（按需改） ==========
root_dir = Path(r'E:\WorkSapce\平台部\Product\图观场景自动生成\工程代码\Tools\pyshp2pgsql\查询数据_0424')  # 根目录，脚本会递归搜 *.shp
out_file = Path(r'E:\WorkSapce\平台部\Product\图观场景自动生成\工程代码\Tools\pyshp2pgsql\输出数据_0424\buildings_merged.shp')               # 输出文件名
drop_dup = True                                       # 是否删除完全重叠的几何
# ========================================

IGNORABLE_WARNING_SNIPPETS = (
    'invalid winding order',
    'autocorrecting them',
)

def find_shps(path: Path):
    """递归找出目录下所有 shp"""
    return list(path.rglob('*.shp'))

def unify_crs(gdf_list):
    """把列表里所有 gdf 投影到第一个 gdf 的坐标系"""
    target_crs = gdf_list[0].crs
    return [gdf.to_crs(target_crs) if gdf.crs != target_crs else gdf for gdf in gdf_list]


def is_ignorable_winding_warning(text: str) -> bool:
    """只屏蔽 OGR 自动修正 ring winding order 时的已知告警。"""
    normalized = text.lower()
    return all(snippet in normalized for snippet in IGNORABLE_WARNING_SNIPPETS)

def read_shp_quietly(shp: Path):
    """读取 shp，并拦截 pyogrio/OGR 输出的 ring winding order 告警。"""
    stderr_buffer = io.StringIO()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        with contextlib.redirect_stderr(stderr_buffer):
            gdf = gpd.read_file(shp)

    ignored_warning = False
    for warning_message in caught:
        if is_ignorable_winding_warning(str(warning_message.message)):
            ignored_warning = True
            continue
        warnings.showwarning(
            warning_message.message,
            warning_message.category,
            warning_message.filename,
            warning_message.lineno,
            file=sys.stderr,
            line=warning_message.line,
        )

    for line in stderr_buffer.getvalue().splitlines(keepends=True):
        stripped = line.strip()
        if is_ignorable_winding_warning(line):
            continue
        if ignored_warning and (
            stripped.startswith('return ogr_read(') or set(stripped) <= {'^', '~'}
        ):
            continue
        sys.stderr.write(line)

    return gdf

def main():
    t0 = time.time()

    shp_files = find_shps(root_dir)
    if not shp_files:
        raise FileNotFoundError(f'在 {root_dir} 下没找到任何 shp 文件！')

    print(f'共发现 {len(shp_files)} 个 shp，开始读取…')
    gdfs = []
    for shp in shp_files:
        gdfs.append(read_shp_quietly(shp))

    print('统一坐标系…')
    gdfs = unify_crs(gdfs)

    print('合并中…')
    merged = gpd.pd.concat(gdfs, ignore_index=True)

    if drop_dup:
        print('去重中…')
        merged = merged.drop_duplicates(subset=['geometry'])

    # 写出
    out_file.parent.mkdir(parents=True, exist_ok=True)
    merged.to_file(out_file)
    print(f'已完成！输出路径：{out_file.absolute()}')
    print(f'耗时 {time.time() - t0:.2f} 秒')

if __name__ == '__main__':
    main()

# import shutil
# import json
# from pathlib import Path
# import os

# def copy_shp_with_deps(shp_path: Path, destination: Path):
#     """把 shp 及其所有同名附属文件复制到目标目录"""
#     destination.mkdir(parents=True, exist_ok=True)
#     base_name = shp_path.stem
#     for ext in ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.sbn', '.sbx', '.shp.xml']:
#         sidecar = shp_path.with_suffix(ext)
#         if sidecar.exists():
#             shutil.copy2(sidecar, destination / sidecar.name)

# def main():
#     # ========== 1. 按需修改这两行 ==========
#     pre_shp_dir = r"\\172.16.0.116\BuildingData\AutoGenerte"
#     # 文件后缀
#     file_type = ".shp"
#     # 读取json文件，获取 shp 文件列表
#     json_path = r"查询结果_0424.json"
#     with open(json_path, 'r', encoding='utf-8') as f:
#         data = json.load(f)
#     shp_list = data.get("list", [])  # 假设 JSON 中
#     shp_list = [os.path.join(pre_shp_dir, shp + file_type) for shp in shp_list]
#     print(f"共找到 {len(shp_list)} 个 shp 文件，准备复制…")
#     # 你的 shp 文件列表（可手动写，也可前面代码生成）
#     dst_dir = r"E:\WorkSapce\平台部\Product\图观场景自动生成\工程代码\Tools\pyshp2pgsql\查询数据_0424"
#     # ========================================
#     for shp in shp_list:
#         try:
#             copy_shp_with_deps(Path(shp), Path(dst_dir))
#             print(f"已复制 {shp} 到 {dst_dir}")
#         except Exception as e:
#             print(f"复制 {shp} 失败: {e}")

# main()