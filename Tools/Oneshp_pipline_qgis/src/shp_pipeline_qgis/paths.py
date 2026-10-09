'''
Author: yejinliang
Date: 2026-04-23 00:54:32
LastEditTime: 2026-04-23 01:15:35
LastEditors: yejinliang
Description: 
'''
import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# QGIS 安装目录可通过环境变量 QGIS_DIR 覆盖（看板「配置」会自动注入）；未设置时回退默认。
_QGIS_DIR = os.getenv("QGIS_DIR")
if _QGIS_DIR:
    QGIS_BIN_DIR = Path(_QGIS_DIR) / "bin"
    QGIS_PROCESS_BAT = QGIS_BIN_DIR / "qgis_process-qgis.bat"
    QGIS_PROCESS_EXE = Path(_QGIS_DIR) / "apps" / "qgis" / "bin" / "qgis_process.exe"
else:
    QGIS_BIN_DIR = Path(r"D:\QGIS\bin")
    QGIS_PROCESS_BAT = QGIS_BIN_DIR / "qgis_process-qgis.bat"
    QGIS_PROCESS_EXE = Path(r"D:\QGIS\apps\qgis\bin\qgis_process.exe")
