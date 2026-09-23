#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定向更新 world_building 中指定城市的数据。

默认流程：
1. 在数据根目录下定位名为“北京市”的城市目录
2. 校验目录存在且包含 shp 文件
3. 删除 world_building 中该城市的旧数据
4. 读取 shp，计算外包矩形并写入 PostGIS

目录结构约定：
root/continent/country/city/*.shp
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import psycopg2


DB_CFG = dict(
    dbname="building",
    user="postgres",
    password="frontfree",
    host="127.0.0.1",
    port=5432,
)

TABLE = "world_building"
DATA_ROOT = Path(r"\\192.168.2.121\BuildingData\AutoGenerate")
TARGET_CITY = "北京市"
TARGET_CONTINENT: str | None = "亚洲"
TARGET_COUNTRY: str | None = "中国"
LOG_PATH = Path("log_main3_beijing.log")


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("main3")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_conn():
    return psycopg2.connect(**DB_CFG)


def iter_city_dirs(root: Path) -> Iterable[tuple[str, str, str, Path]]:
    for continent_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        continent = continent_dir.name
        for country_dir in sorted(d for d in continent_dir.iterdir() if d.is_dir()):
            country = country_dir.name
            for city_dir in sorted(d for d in country_dir.iterdir() if d.is_dir()):
                city = city_dir.name
                yield continent, country, city, city_dir


def find_target_city_dir(
    root: Path,
    target_city: str,
    target_continent: str | None = None,
    target_country: str | None = None,
) -> tuple[str, str, str, Path]:
    matches: list[tuple[str, str, str, Path]] = []

    for continent, country, city, city_dir in iter_city_dirs(root):
        if city != target_city:
            continue
        if target_continent and continent != target_continent:
            continue
        if target_country and country != target_country:
            continue
        matches.append((continent, country, city, city_dir))

    if not matches:
        raise FileNotFoundError(
            f"未找到目标城市目录: city={target_city}, "
            f"continent={target_continent}, country={target_country}"
        )

    if len(matches) > 1:
        candidates = ", ".join(str(item[3]) for item in matches)
        raise RuntimeError(
            "找到多个同名城市目录，请补充 TARGET_CONTINENT / TARGET_COUNTRY 缩小范围: "
            f"{candidates}"
        )

    return matches[0]


def find_shp_files(city_dir: Path) -> list[Path]:
    return sorted(path for path in city_dir.rglob("*.shp") if path.is_file())


def delete_city_data(conn, continent: str, country: str, city: str) -> int:
    sql = f"""
    DELETE FROM {TABLE}
    WHERE continent = %s AND country = %s AND city = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (continent, country, city))
        return cur.rowcount


def ensure_crs(gdf: gpd.GeoDataFrame, shp_path: Path, logger: logging.Logger) -> gpd.GeoDataFrame:
    if gdf.crs is not None:
        return gdf

    logger.warning("文件 %s 缺少 CRS，按 EPSG:4326 处理", shp_path)
    return gdf.set_crs("EPSG:4326")


def build_bbox_wkt(shp_path: Path, logger: logging.Logger) -> str | None:
    gdf = gpd.read_file(shp_path)
    if gdf.empty:
        logger.warning("文件为空，跳过: %s", shp_path)
        return None

    gdf = ensure_crs(gdf, shp_path, logger)
    bounds = gdf.to_crs(epsg=3857).total_bounds
    return (
        f"POLYGON(({bounds[0]} {bounds[1]},"
        f"{bounds[2]} {bounds[1]},"
        f"{bounds[2]} {bounds[3]},"
        f"{bounds[0]} {bounds[3]},"
        f"{bounds[0]} {bounds[1]}))"
    )


def insert_one(
    conn,
    continent: str,
    country: str,
    city: str,
    shp_name: str,
    bbox_wkt: str,
) -> None:
    sql = f"""
    INSERT INTO {TABLE} (continent, country, city, shp_name, bounding_box)
    VALUES (%s, %s, %s, %s, ST_GeomFromText(%s, 3857));
    """
    with conn.cursor() as cur:
        cur.execute(sql, (continent, country, city, shp_name, bbox_wkt))


def refresh_city_data(
    root: Path,
    target_city: str,
    logger: logging.Logger,
    target_continent: str | None = None,
    target_country: str | None = None,
) -> None:
    continent, country, city, city_dir = find_target_city_dir(
        root=root,
        target_city=target_city,
        target_continent=target_continent,
        target_country=target_country,
    )

    shp_files = find_shp_files(city_dir)
    if not shp_files:
        raise FileNotFoundError(f"目标目录下未找到 shp 文件: {city_dir}")

    logger.info("目标目录: %s", city_dir)
    logger.info("识别区域: %s / %s / %s", continent, country, city)
    logger.info("共发现 %s 个 shp 文件", len(shp_files))

    inserted_count = 0
    skipped_count = 0

    with get_conn() as conn:
        deleted_count = delete_city_data(conn, continent, country, city)
        logger.info("已删除旧数据 %s 条", deleted_count)

        for shp_path in shp_files:
            logger.info("处理文件: %s", shp_path)
            bbox_wkt = build_bbox_wkt(shp_path, logger)
            if bbox_wkt is None:
                skipped_count += 1
                continue

            insert_one(
                conn=conn,
                continent=continent,
                country=country,
                city=city,
                shp_name=shp_path.stem,
                bbox_wkt=bbox_wkt,
            )
            inserted_count += 1

    logger.info(
        "更新完成: city=%s, 删除=%s, 新增=%s, 跳过=%s",
        city,
        deleted_count,
        inserted_count,
        skipped_count,
    )


def main() -> int:
    logger = setup_logger(LOG_PATH)

    try:
        root = DATA_ROOT.expanduser().resolve()
        if not root.is_dir():
            logger.error("数据根目录不存在: %s", root)
            return 1

        refresh_city_data(
            root=root,
            target_city=TARGET_CITY,
            logger=logger,
            target_continent=TARGET_CONTINENT,
            target_country=TARGET_COUNTRY,
        )
        return 0
    except Exception as exc:
        logger.exception("执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
