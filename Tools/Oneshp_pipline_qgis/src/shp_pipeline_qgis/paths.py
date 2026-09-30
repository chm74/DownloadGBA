'''
Author: yejinliang
Date: 2026-04-23 00:54:32
LastEditTime: 2026-04-23 01:15:35
LastEditors: yejinliang
Description: 
'''
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
QGIS_BIN_DIR = Path(r"D:\QGIS\bin")
QGIS_PROCESS_BAT = QGIS_BIN_DIR / "qgis_process-qgis.bat"
QGIS_PROCESS_EXE = Path(r"D:\QGIS\apps\qgis\bin\qgis_process.exe")
