from pathlib import Path

import geopandas as gpd
import pytest

from shp_pipeline_qgis.scale_height import scale_height_path
from shp_pipeline_qgis.test_support import build_rectangles


def test_scale_height_path_scales_directory_outputs(tmp_path: Path):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "scaled"
    source_dir.mkdir()

    first = build_rectangles(count=3, axis="x")
    first["HEIGHT"] = [10.0, None, 20.0]
    first["NAME"] = ["A", "B", "C"]
    first.to_file(source_dir / "beijing_001_001_3857.shp")

    second = build_rectangles(count=2, axis="y")
    second["HEIGHT"] = [5, 15]
    second.to_file(source_dir / "beijing_001_002_3857.shp")

    results = scale_height_path(source_dir, output_dir, field_name="HEIGHT", factor=0.65)

    assert [result.output_path.name for result in results] == [
        "beijing_001_001_3857.shp",
        "beijing_001_002_3857.shp",
    ]

    scaled_first = gpd.read_file(output_dir / "beijing_001_001_3857.shp")
    scaled_second = gpd.read_file(output_dir / "beijing_001_002_3857.shp")

    assert scaled_first["HEIGHT"].tolist()[0] == pytest.approx(6.5)
    assert scaled_first["HEIGHT"].isna().tolist()[1] is True
    assert scaled_first["HEIGHT"].tolist()[2] == pytest.approx(13.0)
    assert scaled_first["NAME"].tolist() == ["A", "B", "C"]
    assert scaled_second["HEIGHT"].tolist() == pytest.approx([3.25, 9.75])


def test_scale_height_path_raises_when_field_is_missing(tmp_path: Path):
    source = tmp_path / "sample.shp"
    build_rectangles(count=1).to_file(source)

    with pytest.raises(ValueError, match="missing field"):
        scale_height_path(source, tmp_path / "scaled", field_name="HEIGHT", factor=0.65)
