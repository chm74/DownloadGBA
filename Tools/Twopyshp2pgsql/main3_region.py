#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按参数更新 world_building 中指定区域的数据。

目录结构约定：
root/continent/country/region/*.shp

示例：
python main3_region.py --region 北京市
python main3_region.py --region 广东省 上海市
python main3_region.py --continent 亚洲 --country 中国 --region 北京市
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import psycopg2
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

DB_CFG = dict(
    dbname=os.getenv("PG_DBNAME", "building"),
    user=os.getenv("PG_USER", "postgres"),
    password=os.getenv("PG_PASSWORD", "frontfree"),
    host=os.getenv("PG_HOST", "127.0.0.1"),
    port=int(os.getenv("PG_PORT", "5432")),
)

TABLE = "world_building"
DATA_ROOT = Path(os.getenv("DATA_ROOT", r"\\192.168.2.121\BuildingData\AutoGenerate"))
DEFAULT_CONTINENT = "亚洲"
DEFAULT_COUNTRY = "中国"
DEFAULT_LOG_DIR = Path(".")


@dataclass(frozen=True)
class TargetRegion:
    continent: str
    country: str
    region: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按参数更新 world_building 中指定区域的数据"
    )
    parser.add_argument(
        "--continent",
        default=DEFAULT_CONTINENT,
        help=f"洲目录名，默认 {DEFAULT_CONTINENT}",
    )
    parser.add_argument(
        "--country",
        default=DEFAULT_COUNTRY,
        help=f"国家目录名，默认 {DEFAULT_COUNTRY}",
    )
    parser.add_argument(
        "--region",
        nargs="+",
        required=True,
        help="要更新的第三级区域目录名，可一次传多个",
    )
    parser.add_argument(
        "--root",
        default=str(DATA_ROOT),
        help=f"数据根目录，默认 {DATA_ROOT}",
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"日志输出目录，默认 {DEFAULT_LOG_DIR}",
    )
    return parser.parse_args()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\\\|?*\\s]+', "_", name).strip("_") or "region"


def build_log_path(log_dir: Path, target: TargetRegion) -> Path:
    file_name = (
        f"log_main3_{sanitize_filename(target.country)}_"
        f"{sanitize_filename(target.region)}.log"
    )
    return log_dir / file_name


def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("main3_region")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    log_path.parent.mkdir(parents=True, exist_ok=True)

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


def refresh_region_data(root: Path, target: TargetRegion, logger: logging.Logger) -> None:
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


def refresh_targets(root: Path, targets: list[TargetRegion], log_dir: Path) -> int:
    failed_targets: list[str] = []

    for target in targets:
        logger = setup_logger(build_log_path(log_dir, target))
        try:
            refresh_region_data(root=root, target=target, logger=logger)
        except Exception:
            logger.exception(
                "执行失败: continent=%s, country=%s, region=%s",
                target.continent,
                target.country,
                target.region,
            )
            failed_targets.append(target.region)

    return 1 if failed_targets else 0


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    log_dir = Path(args.log_dir).expanduser().resolve()

    if not root.is_dir():
        print(f"数据根目录不存在: {root}", file=sys.stderr)
        return 1

    targets = [
        TargetRegion(
            continent=args.continent,
            country=args.country,
            region=region,
        )
        for region in args.region
    ]

    return refresh_targets(root=root, targets=targets, log_dir=log_dir)


if __name__ == "__main__":
    sys.exit(main())
