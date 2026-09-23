#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量把“洲/国家/省份”目录结构的 shp 外包矩形写入 PostGIS
python insert_shp_extent.py  /your/data/root
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

import geopandas as gpd
import psycopg2
import rarfile
import subprocess
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 设置log路径
log_path = "log.log"
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

def insert_one(continent: str, country: str, city: str, wkt_geom: str):
    sql = f"""
    INSERT INTO {TABLE} (continent, country, city, bounding_box)
    VALUES (%s, %s, %s, ST_GeomFromText(%s, 3857))
    ON CONFLICT DO NOTHING;
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (continent, country, city, wkt_geom))
        conn.commit()


def extract_rar_with_7z(rar_path: Path, out_dir: Path):
    """调用 7z 解压，支持 .rar 格式"""
    # 7z 安装路径，若已加入 PATH 可直接写 '7z'
    seven_z = r"C:\Program Files\7-Zip\7z.exe"
    cmd = [seven_z, 'x', '-y', '-o' + str(out_dir), str(rar_path)]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f'7z 解压失败: {completed.stderr}')

def process_rar(rar_path: Path, continent: str, country: str):
    """解压一个 rar 并处理其中所有 shp"""
    # 1. 以“洲/国家”为子目录，方便人工查看，保持原始层级结构
    sub = WORK_TEMP / f"{continent}" / f"{country}"
    if sub.exists():                       # 重复运行先清掉
        shutil.rmtree(sub)
    sub.mkdir(parents=True, exist_ok=True)

    # 2. 解压到固定目录
    extract_rar_with_7z(rar_path, sub)     # 或你原来的 rf.extractall(sub)

    # 3. 遍历所有 shp，取外包矩形写入数据库
    for shp in sub.rglob("*.shp"):
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
            city = shp.stem
            insert_one(continent, country, city, bbox_wkt)
            logger.info(f"添加数据成功 {continent} / {country} / {city}")
        except Exception as e:
            logger.error(f"添加数据失败 {shp} : {e}")

def main(root: Path):
    create_table_if_not_exists()
    for cont_dir in sorted([d for d in root.iterdir() if d.is_dir()]):
        continent = cont_dir.name
        for rar in sorted(cont_dir.glob("*.rar")):
            country = rar.stem
            process_rar(rar, continent, country)

if __name__ == "__main__":
    # if len(sys.argv) != 2:
    #     print("用法: python insert_shp_extent.py  /your/data/root")
    #     sys.exit(1)
    try:
        dir_path = r"F:\BuildingSHP\2024_Asia"
        root_p = Path(dir_path).expanduser().resolve()
        if not root_p.is_dir():
            print("路径不存在", file=sys.stderr)
            sys.exit(1)

        WORK_TEMP = Path(r"F:\BuildingSHP\AGISDataTemp")   # ← 你想放的地方
        WORK_TEMP.mkdir(parents=True, exist_ok=True)
        main(root_p)
    except Exception as e:
            logger.error(f"失败 {e}")