from pathlib import Path


def test_project_layout_exists():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "shp_pipeline_qgis").exists()
    assert (root / "tests").exists()
