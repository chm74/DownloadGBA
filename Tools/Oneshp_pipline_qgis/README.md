# shp_pipeline_qgis

Isolated workspace for the SHP split, QGIS validation, and reprojection pipeline.

The splitter uses adaptive spatial tiles calculated in EPSG:3857. Features are
assigned whole to one tile by representative point, so output files have tighter
bounding boxes without clipping or duplicating buildings.

## Run

From the repository root:

```powershell
uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\pipeline.py data out_data
```

## Single-Step Scripts

- Split only:
  `uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\splitter.py <input_shp> <output_dir>`
- Validate only:
  `uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\validate_with_qgis.py <input_shp> <output_dir>`
- Reproject only:
  `uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\reproject.py <input_shp> <output_dir>`
- Scale height field only:
  `uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\scale_height.py <input_path> <output_dir> <factor> --field HEIGHT`

Example for Beijing final tiles:

```powershell
uv run --project shp_pipeline_qgis --python shp_pipeline_qgis\.venv\Scripts\python.exe python shp_pipeline_qgis\scale_height.py out_data\beijing_pipeline\final out_data\beijing_pipeline\final_height_065 0.65 --field HEIGHT
```

## Output

- `out_data/final`: EPSG:3857 shapefiles
- `out_data/logs/pipeline.log`: processing summary

`split` 和 `valid` 现在只作为运行期临时目录使用，任务结束后会自动清理，不会保留在输出目录中。

## Split Policy

- Target tile size: about 80,000 features
- Small neighboring tiles: merged when possible
- Maximum merged tile size: about 100,000 features
- Output names remain compatible, for example `tianjin_001_001.shp`
