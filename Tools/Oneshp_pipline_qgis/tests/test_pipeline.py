from pathlib import Path

import geopandas as gpd

from shp_pipeline_qgis.pipeline import run_pipeline
from shp_pipeline_qgis.validate_with_qgis import ValidationResult
from shp_pipeline_qgis.test_support import build_rectangles


def test_run_pipeline_creates_final_outputs(tmp_path: Path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    build_rectangles(count=3, axis="x", crs="EPSG:4326").to_file(input_dir / "demo.shp")

    summary = run_pipeline(input_dir=input_dir, output_dir=output_dir)

    assert len(summary.items) == 1
    assert summary.items[0].final_outputs[0].exists()
    assert not (output_dir / "split").exists()
    assert not (output_dir / "valid").exists()
    assert (output_dir / "final").exists()
    assert (output_dir / "logs").exists()


def test_run_pipeline_uses_one_temp_directory_per_source_file(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    build_rectangles(count=3, axis="x", crs="EPSG:4326").to_file(input_dir / "demo_a.shp")
    build_rectangles(count=3, axis="x", crs="EPSG:4326").to_file(input_dir / "demo_b.shp")

    from shp_pipeline_qgis import pipeline as pipeline_module

    call_count = 0
    original_factory = pipeline_module.tempfile.TemporaryDirectory

    def counting_factory(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_factory(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.tempfile, "TemporaryDirectory", counting_factory)
    run_pipeline(input_dir=input_dir, output_dir=output_dir)

    assert call_count == 2


def test_run_pipeline_logs_zero_height_removed(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    gdf = build_rectangles(count=2, axis="x", crs="EPSG:4326")
    gdf["HEIGHT"] = [0.0, 8.0]
    gdf.to_file(input_dir / "demo.shp")

    from shp_pipeline_qgis import pipeline as pipeline_module

    def fake_validate(source_path: Path, output_dir: Path) -> ValidationResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        valid_output = output_dir / f"{source_path.stem}_valid.shp"
        gpd.read_file(source_path).to_file(valid_output)
        return ValidationResult(
            output_path=valid_output,
            valid_count=2,
            invalid_count=0,
            error_count=0,
        )

    monkeypatch.setattr(pipeline_module, "validate_shapefile_with_qgis", fake_validate)

    summary = run_pipeline(input_dir=input_dir, output_dir=output_dir)
    final_gdf = gpd.read_file(summary.items[0].final_outputs[0])
    log_text = (output_dir / "logs" / "pipeline.log").read_text(encoding="utf-8")

    assert len(final_gdf) == 1
    assert "zero_height_removed=1" in log_text
