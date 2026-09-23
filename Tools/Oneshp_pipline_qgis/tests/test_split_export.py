from pathlib import Path

from shp_pipeline_qgis.splitter import export_split_shapefiles
from shp_pipeline_qgis.test_support import build_rectangles


def test_export_split_shapefiles_writes_expected_names(tmp_path: Path):
    source = tmp_path / "sample.shp"
    build_rectangles(count=120000, axis="x").to_file(source)

    outputs = export_split_shapefiles(source, tmp_path / "split")

    assert [path.stem for path in outputs] == [
        "sample_001_001",
        "sample_001_002",
        "sample_001_003",
    ]
