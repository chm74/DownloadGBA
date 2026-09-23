#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量把“洲/国家/省份”目录结构的 shp 外包矩形写入 PostGIS
python insert_shp_extent.py  /your/data/root
"""
import os
import sys
import shutil
from pathlib import Path

import geopandas as gpd
import psycopg2
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 设置log路径
log_path = "log4.log"
file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


# ---------- 配置 ----------
DB_CFG = dict(
    dbname="building",
    user="postgres",
    password="frontfree",
    host="172.16.1.145",
    port=5432,
)
TABLE = "world_building"
# --------------------------

def get_conn():
    return psycopg2.connect(**DB_CFG)


def create_table_if_not_exists():
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        id SERIAL PRIMARY KEY,
        continent varchar(100) NOT NULL,
        country varchar(100) NOT NULL,
        province varchar(100) NOT NULL,
        shp_name varchar(100) NOT NULL,
        bounding_box GEOMETRY(POLYGON,3857) NOT NULL,
        coordinate_system varchar(50) DEFAULT 'EPSG:3857',
        created_at timestamp DEFAULT now(),
        updated_at timestamp DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_{TABLE.replace('.', '_')}_bbox
      ON {TABLE} USING GIST (bounding_box);
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        conn.commit()

def shp_files_in_dir(dir_path: Path):
    """返回目录下所有 .shp 绝对路径"""
    return [p for p in dir_path.rglob("*.shp") if p.is_file()]

def insert_one(continent: str, country: str, city: str, shp_name: str, wkt_geom: str):
    sql = f"""
    INSERT INTO {TABLE} (continent, country, city, shp_name, bounding_box)
    VALUES (%s, %s, %s, %s, ST_GeomFromText(%s, 3857))
    ON CONFLICT DO NOTHING;
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (continent, country, city, shp_name, wkt_geom))
        conn.commit()

def process_rar(rar_path: Path, continent: str, country: str, city: str):
    """解压一个 rar 并处理其中所有 shp"""
    # 1. 以“洲/国家”为子目录，方便人工查看，保持原始层级结构
    sub = rar_path / f"{continent}" / f"{country}" / f"{city}"
    # print(f"处理目录 {sub}")
    # # 3. 遍历所有 shp，取外包矩形写入数据库
    for shp in sub.rglob("*.shp"):
        print(f"处理文件 {shp.stem}")
        try:
            gdf = gpd.read_file(shp)
            if gdf.empty:
                continue
            # 取整体外包矩形
            bounds = gdf.total_bounds  # minx, miny, maxx, maxy
            gdf_3857 = gdf.to_crs(epsg=3857)  # ← 关键：转投影
            bounds = gdf_3857.total_bounds    # 已经是 3857 米单位
            bbox_wkt = f"POLYGON(({bounds[0]} {bounds[1]},{bounds[2]} {bounds[1]}," \
                        f"{bounds[2]} {bounds[3]},{bounds[0]} {bounds[3]},{bounds[0]} {bounds[1]}))"
            shp_name = shp.stem
            insert_one(continent, country, city, shp_name, bbox_wkt)
            logger.info(f"添加数据成功 {sub} / {shp_name}")
        except Exception as e:
            logger.error(f"添加数据失败 {shp} : {e}")

def main(root: Path, blacklist_continents):
    # create_table_if_not_exists()
    for cont_dir in sorted([d for d in root.iterdir() if d.is_dir()]):
        continent = cont_dir.name
        if continent in blacklist_continents:
            continue
        for child_dir in sorted([j for j in cont_dir.iterdir() if j.is_dir()]):
            country = child_dir.name
            for child_dir2 in sorted([x for x in child_dir.iterdir() if x.is_dir()]):
                city = child_dir2.name
                process_rar(root, continent, country, city)
                print(f"处理 {continent} / {country} / {city} ")


if __name__ == "__main__":
    # if len(sys.argv) != 2:
    #     print("用法: python insert_shp_extent.py  /your/data/root")
    #     sys.exit(1)
    try:
        dir_path = r"\\192.168.2.121\BuildingData\AutoGenerte"
        root_p = Path(dir_path).expanduser().resolve()
        if not root_p.is_dir():
            print("路径不存在", file=sys.stderr)
            sys.exit(1)

        main(root_p, ["Africa", "America", "Asia", "Europe", "Oceania", "亚洲_0117", "亚洲_0424"])
    except Exception as e:
            logger.error(f"失败 {e}")