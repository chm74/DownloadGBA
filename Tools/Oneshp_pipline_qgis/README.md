# shp_pipeline_qgis

Isolated workspace for the SHP split, QGIS validation, clean, and EPSG:3857 reprojection pipeline.

The splitter uses adaptive spatial tiles calculated in EPSG:3857. Features are
assigned whole to one tile by representative point, so output files have tighter
bounding boxes without clipping or duplicating buildings.

This tool is normally invoked by `scripts/run_shp_process_tasks.py`; the commands
below are for standalone runs from the repository root.

## Run

```powershell
uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\pipeline.py data out_data
```

Pipeline steps: split → QGIS validation → clean (empty / zero-height / duplicate) → reproject to EPSG:3857.

## Single-Step Scripts

- Split only:
  `uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\splitter.py <input_shp> <output_dir>`
- Validate only:
  `uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\validate_with_qgis.py <input_shp> <output_dir>`
- Reproject only:
  `uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\reproject.py <input_shp> <output_dir>`
- Scale height field only:
  `uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\scale_height.py <input_path> <output_dir> <factor> --field HEIGHT`

Example for Beijing final tiles:

```powershell
uv run --project Tools\Oneshp_pipline_qgis --python Tools\Oneshp_pipline_qgis\.venv\Scripts\python.exe python Tools\Oneshp_pipline_qgis\scale_height.py out_data\beijing_pipeline\final out_data\beijing_pipeline\final_height_065 0.65 --field HEIGHT
```

## Output

- `<output_dir>/final`: EPSG:3857 shapefiles
- `<output_dir>/logs/pipeline.log`: processing summary

`split` and `valid` are runtime temp directories and are cleaned up after each task.

## Split Policy

- Target tile size: about 200,000 features
- Minimum tile size: about 50,000 features (smaller neighboring tiles merged when possible)
- Maximum merged tile size: about 250,000 features
- Merge tolerance: 500 m, with max bounds expansion ratio 1.25x
- Output names remain compatible, for example `tianjin_001_001.shp`
