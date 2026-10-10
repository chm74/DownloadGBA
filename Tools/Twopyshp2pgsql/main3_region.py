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

数据库连接与表操作统一由 world_building.py 提供：
优先级 CLI 覆盖参数 > 环境变量(PG_*) > 目录内 .env > 内置默认。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from world_building import (
    DEFAULT_DATA_ROOT,
    TargetRegion,
    refresh_region_data,
    sanitize_filename,
)

DEFAULT_CONTINENT = "亚洲"
DEFAULT_COUNTRY = "中国"
DEFAULT_LOG_DIR = Path(".")
DATA_ROOT = Path(os.getenv("DATA_ROOT", DEFAULT_DATA_ROOT))


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


def refresh_targets(root: Path, targets: list[TargetRegion], log_dir: Path) -> int:
    failed_targets: list[str] = []

    for target in targets:
        logger = setup_logger(build_log_path(log_dir, target))
        try:
            summary = refresh_region_data(root=root, target=target, logger=logger)
        except Exception:
            logger.exception(
                "执行失败: continent=%s, country=%s, region=%s",
                target.continent,
                target.country,
                target.region,
            )
            failed_targets.append(target.region)
            continue
        print(
            "__IMPORT_SUMMARY__ " + json.dumps(summary, ensure_ascii=False),
            flush=True,
        )

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
