import importlib.util
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "gba_grid.py"
    spec = importlib.util.spec_from_file_location("gba_grid", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def boundary(geometry):
    return gpd.GeoDataFrame([{"geometry": geometry}], geometry="geometry", crs=4326)


class NationalGridTests(unittest.TestCase):
    def test_fujian_bbox_maps_to_full_canonical_half_degree_cell(self):
        module = load_module()

        grid = module.build_national_grid(
            boundary(box(119.34238, 26.01709, 119.37546, 26.04441)),
            0.5,
        )

        self.assertEqual(grid["GRID_ID"].tolist(), ["GBA_050_I0598_J0232"])
        self.assertEqual(tuple(grid.geometry.iloc[0].bounds), (119.0, 26.0, 119.5, 26.5))

    def test_adjacent_regions_share_the_same_canonical_cell_id(self):
        module = load_module()

        west = module.build_national_grid(boundary(box(0.1, 0.1, 0.49, 0.9)), 0.5)
        east = module.build_national_grid(boundary(box(0.49, 0.1, 0.9, 0.9)), 0.5)

        shared = set(west["GRID_ID"]) & set(east["GRID_ID"])
        self.assertEqual(shared, {"GBA_050_I0360_J0180", "GBA_050_I0360_J0181"})

    def test_negative_world_origin_cell_has_stable_non_negative_indices(self):
        module = load_module()

        grid = module.build_national_grid(boundary(box(-179.9, -89.9, -179.6, -89.6)), 0.5)

        self.assertEqual(grid["GRID_ID"].tolist(), ["GBA_050_I0000_J0000"])
        self.assertEqual(tuple(grid.geometry.iloc[0].bounds), (-180.0, -90.0, -179.5, -89.5))

    def test_invalid_grid_size_is_rejected(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "grid_size"):
            module.build_national_grid(boundary(box(0, 0, 1, 1)), 0)


if __name__ == "__main__":
    unittest.main()
