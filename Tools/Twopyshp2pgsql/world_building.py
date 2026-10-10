#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""world_building 表读写的公共模块。

统一数据库连接配置与表操作，避免多处硬编码漂移：
- 连接配置优先级：CLI 覆盖参数 > 环境变量(PG_*) > 目录内 .env > 内置默认
- 表名常量 TABLE
- 删除/插入/包围盒计算等公共函数
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import psycopg2
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

TABLE = "world_building"
DEFAULT_DATA_ROOT = r"\\192.168.2.121\BuildingData\AutoGenerate"

DEFAULT_DB = {
    "dbname": "building",
    "user": "postgres",
    "password": "frontfree",
    "host": "127.0.0.1",
    "port": 5432,
}


@dataclass(frozen=True)
class TargetRegion:
    continent: str
    country: str
    region: str


def load_db_config(overrides: dict | None = None) -> dict:
    """按优先级返回数据库连接配置：CLI > PG_* 环境变量 > .env > 默认。"""
    config = {
        "dbname": os.getenv("PG_DBNAME", DEFAULT_DB["dbname"]),
        "user": os.getenv("PG_USER", DEFAULT_DB["user"]),
        "password": os.getenv("PG_PASSWORD", DEFAULT_DB["password"]),
        "host": os.getenv("PG_HOST", DEFAULT_DB["host"]),
        "port": int(os.getenv("PG_PORT", str(DEFAULT_DB["port"]))),
    }
    if overrides:
        for key, value in overrides.items():
            if key not in config or value in (None, ""):
                continue
            config[key] = int(value) if key == "port" else value
    return config


def get_conn(config: dict | None = None):
    return psycopg2.connect(**(config or load_db_config()))


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("_") or "region"


def iter_region_dirs(root: Path) -> Iterable[tuple[str, str, str, Path]]:
    for continent_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        continent = continent_dir.name
        for country_dir in sorted(d for d in continent_dir.iterdir() if d.is_dir()):
            country = country_dir.name
            for region_dir in sorted(d for d in country_dir.iterdir() if d.is_dir()):
                region = region_dir.name
                yield continent, country, region, region_dir


def resolve_target_region_dir(root: Path, target: TargetRegion) -> tuple[str, str, str, Path]:
    direct_dir = root / target.continent / target.country / target.region
    if direct_dir.is_dir():
        return target.continent, target.country, target.region, direct_dir

    matches: list[tuple[str, str, str, Path]] = []
    for continent, country, region, region_dir in iter_region_dirs(root):
        if region != target.region:
            continue
        if continent != target.continent:
            continue
        if country != target.country:
            continue
        matches.append((continent, country, region, region_dir))

    if not matches:
        raise FileNotFoundError(
            "未找到目标区域目录: "
            f"continent={target.continent}, country={target.country}, region={target.region}"
        )

    return matches[0]


def find_shp_files(region_dir: Path) -> list[Path]:
    return sorted(path for path in region_dir.rglob("*.shp") if path.is_file())


def delete_region_data(conn, continent: str, country: str, region: str) -> int:
    sql = f"""
    DELETE FROM {TABLE}
    WHERE continent = %s AND country = %s AND city = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (continent, country, region))
        return cur.rowcount


def ensure_crs(gdf: gpd.GeoDataFrame, shp_path: Path, logger) -> gpd.GeoDataFrame:
    if gdf.crs is not None:
        return gdf

    logger.warning("文件 %s 缺少 CRS，按 EPSG:4326 处理", shp_path)
    return gdf.set_crs("EPSG:4326")


def build_bbox_wkt(shp_path: Path, logger) -> str | None:
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
    region: str,
    shp_name: str,
    bbox_wkt: str,
) -> None:
    sql = f"""
    INSERT INTO {TABLE} (continent, country, city, shp_name, bounding_box)
    VALUES (%s, %s, %s, %s, ST_GeomFromText(%s, 3857));
    """
    with conn.cursor() as cur:
        cur.execute(sql, (continent, country, region, shp_name, bbox_wkt))


def refresh_region_data(root: Path, target: TargetRegion, logger) -> dict:
    """删除并重建指定区域的记录，返回 {continent,country,region,deleted,inserted,skipped}。"""
    continent, country, region, region_dir = resolve_target_region_dir(root, target)

    shp_files = find_shp_files(region_dir)
    if not shp_files:
        raise FileNotFoundError(f"目标目录下未找到 shp 文件: {region_dir}")

    logger.info("目标目录: %s", region_dir)
    logger.info("识别区域: %s / %s / %s", continent, country, region)
    logger.info("共发现 %s 个 shp 文件", len(shp_files))

    inserted_count = 0
    skipped_count = 0

    with get_conn() as conn:
        deleted_count = delete_region_data(conn, continent, country, region)
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
                region=region,
                shp_name=shp_path.stem,
                bbox_wkt=bbox_wkt,
            )
            inserted_count += 1

    logger.info(
        "更新完成: region=%s, 删除=%s, 新增=%s, 跳过=%s",
        region,
        deleted_count,
        inserted_count,
        skipped_count,
    )
    return {
        "continent": continent,
        "country": country,
        "region": region,
        "deleted": deleted_count,
        "inserted": inserted_count,
        "skipped": skipped_count,
    }
