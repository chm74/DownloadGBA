#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SHP文件裁剪模块
根据给定的裁剪范围，从SHP文件中提取与裁剪范围相交的部分，将要素修剪到和裁剪范围一样大
"""

from __future__ import annotations
from pathlib import Path
from typing import List, Union

try:
    import geopandas as gpd
    from shapely.geometry import Polygon, MultiPolygon
    import pandas as pd
except ImportError:
    pass


def _get_shapefile_list(folder_path: str, geometry_type: str = None) -> List[str]:
    """
    获取文件夹中的SHP文件列表

    Args:
        folder_path: SHP文件夹路径
        geometry_type: 要素类型过滤，None表示支持所有类型

    Returns:
        SHP文件路径列表
    """
    folder = Path(folder_path)
    if not folder.exists():
        raise FileNotFoundError(f"SHP文件夹不存在: {folder_path}")

    shp_files = []
    for shp_file in folder.glob("*.shp"):
        try:
            # 读取文件检查几何类型
            # temp_gdf = gpd.read_file(shp_file)
            # if geometry_type:
            #     # 如果指定了几何类型，只包含该类型
            #     if not temp_gdf.geometry.geom_type.isin([geometry_type, f'Multi{geometry_type}']).any():
            #         continue
            shp_files.append(str(shp_file))
        except Exception:
            # 如果无法读取文件，跳过
            continue

    return shp_files


def _get_geojson_file(geojson_path: str) -> str:
    """
    获取单个GeoJSON文件路径

    Args:
        geojson_path: 裁剪范围文件路径

    Returns:
        GeoJSON文件路径
    """
    file_path = Path(geojson_path)
    if not file_path.exists():
        raise FileNotFoundError(f"裁剪范围文件不存在: {geojson_path}")

    # 检查文件扩展名
    if file_path.suffix.lower() not in ['.geojson', '.json']:
        raise ValueError(f"不支持的文件类型: {file_path.suffix}. 仅支持 .geojson 和 .json 文件")

    return str(file_path)


def _load_clipping_area(geojson_file: str, logger) -> gpd.GeoDataFrame:
    """
    加载裁剪范围文件

    Args:
        geojson_file: GeoJSON文件路径
        logger: 日志记录器

    Returns:
        裁剪范围GeoDataFrame
    """
    logger.info(f"加载裁剪范围文件: {geojson_file}")
    try:
        gdf = gpd.read_file(geojson_file)

        # 确保坐标系一致，如果无坐标系则假设为EPSG:4326
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
            logger.warning(f"文件 {geojson_file} 无坐标系信息，设置为EPSG:4326")

        # 过滤出多边形要素
        polygon_mask = gdf.geometry.geom_type.isin(['Polygon', 'MultiPolygon'])
        gdf = gdf[polygon_mask]

        if len(gdf) == 0:
            raise ValueError(f"文件 {geojson_file} 中没有找到多边形要素")

        logger.info(f"  加载了 {len(gdf)} 个多边形要素")
        return gdf

    except Exception as e:
        logger.error(f"加载文件 {geojson_file} 失败: {str(e)}")
        raise


def _process_clipping(input_shp: str, clipping_area: gpd.GeoDataFrame,
                      output_folder: str, logger) -> None:
    """
    处理单个SHP文件的裁剪操作

    Args:
        input_shp: 输入SHP文件路径
        clipping_area: 裁剪范围GeoDataFrame
        output_folder: 输出文件夹路径
        logger: 日志记录器
    """
    logger.info(f"处理文件: {input_shp}")

    # 读取输入文件
    input_gdf = gpd.read_file(input_shp)
    original_count = len(input_gdf)

    if original_count == 0:
        logger.warning(f"文件 {input_shp} 为空，跳过处理")
        return

    # 确保坐标系一致
    if input_gdf.crs is None:
        input_gdf = input_gdf.set_crs("EPSG:4326")
        logger.warning(f"输入文件 {input_shp} 无坐标系信息，设置为EPSG:4326")

    # 如果坐标系不一致，转换裁剪范围的坐标系
    if input_gdf.crs != clipping_area.crs:
        logger.info(f"转换裁剪范围坐标系: {clipping_area.crs} -> {input_gdf.crs}")
        clipping_area = clipping_area.to_crs(input_gdf.crs)

    # 创建输出文件夹
    output_path = Path(output_folder)
    output_path.mkdir(parents=True, exist_ok=True)

    # 生成输出文件名（保持原文件名）
    input_filename = Path(input_shp).stem
    output_file = output_path / f"{input_filename}.shp"

    # 使用空间索引提高性能
    spatial_index = input_gdf.sindex

    # 找到可能与裁剪范围相交的要素
    possible_matches_index = []
    for clip_geom in clipping_area.geometry:
        possible_matches_index.extend(list(spatial_index.intersection(clip_geom.bounds)))

    if not possible_matches_index:
        logger.info(f"没有要素与裁剪范围相交，输出空文件")
        # 创建空的GeoDataFrame并保存
        empty_gdf = input_gdf.iloc[0:0]  # 保留结构但无数据
        empty_gdf.to_file(output_file)
        logger.info(f"输出文件: {output_file} (0 个要素)")
        return

    # 获取可能匹配的要素
    possible_matches = input_gdf.iloc[possible_matches_index]

    # 找到实际相交的要素
    actually_intersecting = possible_matches[
        possible_matches.geometry.intersects(clipping_area.unary_union)
    ]

    if len(actually_intersecting) == 0:
        logger.info(f"没有要素与裁剪范围实际相交，输出空文件")
        empty_gdf = input_gdf.iloc[0:0]  # 保留结构但无数据
        empty_gdf.to_file(output_file)
        logger.info(f"输出文件: {output_file} (0 个要素)")
        return

    logger.info(f"对 {len(actually_intersecting)} 个相交要素进行裁剪处理")

    # 创建结果DataFrame
    result_gdfs = []

    # 对每个相交要素进行交集计算
    for idx, row in actually_intersecting.iterrows():
        original_geom = row['geometry']

        # 计算与裁剪范围的交集
        clipped_geom = original_geom.intersection(clipping_area.unary_union)

        if clipped_geom.is_empty:
            # 如果交集为空，跳过此要素
            continue
        elif isinstance(clipped_geom, (Polygon, MultiPolygon)):
            # 有效的多边形结果
            new_row = row.copy()
            new_row['geometry'] = clipped_geom
            result_gdfs.append(new_row)
        else:
            # 如果是其他几何类型且非空，也保留（如点、线等）
            if not clipped_geom.is_empty:
                new_row = row.copy()
                new_row['geometry'] = clipped_geom
                result_gdfs.append(new_row)

    if not result_gdfs:
        logger.warning(f"所有要素裁剪后无结果，输出空文件")
        empty_gdf = input_gdf.iloc[0:0]  # 保留结构但无数据
        empty_gdf.to_file(output_file)
        logger.info(f"输出文件: {output_file} (0 个要素)")
        return

    # 合并所有结果
    final_gdf = gpd.GeoDataFrame(result_gdfs, crs=input_gdf.crs)
    final_count = len(final_gdf)

    # 保存结果
    final_gdf.to_file(output_file)
    logger.info(f"输出文件: {output_file} (保留 {final_count} 个要素，从原 {original_count} 个要素裁剪而来)")


def setup_logger(log_file: str, console_handler: bool = True):
    import logging
    logger = logging.getLogger("GISFileTools")
    logger.setLevel(logging.INFO)
    
    # 清除已有的处理器，避免重复添加
    logger.handlers.clear()
    
    # 格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console_handler:
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger


def clipping_command(shp_folder: str, boundary_file: str,
                    output_folder: str, log_path: str) -> None:
    """
    执行裁剪操作

    Args:
        shp_folder: SHP文件夹路径
        boundary_file: 裁剪范围文件路径
        output_folder: 输出文件夹路径
        log_path: 日志存放路径

    Raises:
        Exception: 当裁剪失败时抛出异常
    """
    logger = setup_logger(log_path)
    logger.info("开始执行SHP文件裁剪任务")

    try:
        # 获取SHP文件列表（支持所有几何类型）
        shp_files = _get_shapefile_list(shp_folder, None)
        if not shp_files:
            logger.error(f"SHP文件夹中未找到任何SHP文件: {shp_folder}")
            raise Exception(f"SHP文件夹中未找到任何SHP文件: {shp_folder}")

        logger.info(f"找到 {len(shp_files)} 个SHP文件")

        # 获取裁剪范围文件
        geojson_file = _get_geojson_file(boundary_file)
        logger.info(f"使用裁剪范围文件: {geojson_file}")

        # 加载裁剪范围
        clipping_area = _load_clipping_area(geojson_file, logger)

        # 处理每个SHP文件
        total_processed = 0
        for shp_file in shp_files:
            try:
                _process_clipping(shp_file, clipping_area, output_folder, logger)
                total_processed += 1
            except Exception as e:
                logger.error(f"处理文件 {shp_file} 时出错: {str(e)}")
                continue

        logger.info(f"裁剪任务完成！成功处理 {total_processed}/{len(shp_files)} 个文件")

    except Exception as e:
        logger.error(f"执行失败: {str(e)}", exc_info=True)
        raise


if __name__ == "__main__":
    import time
    # 示例用法
    shp_folder = r"F:\SceneAutomationV3\Tools\pyshp2pgsql\shp3857"
    boundary_file = r"F:\SceneAutomationV3\Tools\pyshp2pgsql\讯飞简概范围.geojson"
    output_folder = r"F:\SceneAutomationV3\Tools\pyshp2pgsql\shp_output"
    log_path = r"log_clipping.txt"

    # 开始时间
    time_start = time.time()
    clipping_command(shp_folder, boundary_file, output_folder, log_path)
    # 结束时间
    time_end = time.time()
    print(f'运行时间: {time_end - time_start} 秒')