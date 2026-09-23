from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import geopandas as gpd
from shapely.geometry import box

from gba_contract import GRID_ALGORITHM_VERSION

WORLD_MIN_X = Decimal("-180")
WORLD_MIN_Y = Decimal("-90")
WORLD_MAX_X = Decimal("180")
WORLD_MAX_Y = Decimal("90")


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _validated_size(grid_size: float) -> Decimal:
    size = _decimal(grid_size)
    if size <= 0:
        raise ValueError("grid_size must be greater than zero")
    x_cells = (WORLD_MAX_X - WORLD_MIN_X) / size
    y_cells = (WORLD_MAX_Y - WORLD_MIN_Y) / size
    if x_cells != x_cells.to_integral_value() or y_cells != y_cells.to_integral_value():
        raise ValueError("grid_size must divide both 360 and 180 degrees exactly")
    return size


def _minimum_index(value: float, origin: Decimal, size: Decimal) -> int:
    return int(((_decimal(value) - origin) / size).to_integral_value(rounding=ROUND_FLOOR))


def _maximum_index(value: float, origin: Decimal, size: Decimal) -> int:
    upper = ((_decimal(value) - origin) / size).to_integral_value(rounding=ROUND_CEILING)
    return int(upper) - 1


def _grid_size_code(size: Decimal) -> str:
    hundredths = size * Decimal("100")
    if hundredths != hundredths.to_integral_value():
        raise ValueError("grid_size must use no more than two decimal places")
    return f"{int(hundredths):03d}"


def grid_id(ix: int, iy: int, grid_size: float) -> str:
    size = _validated_size(grid_size)
    return f"GBA_{_grid_size_code(size)}_I{ix:04d}_J{iy:04d}"


def grid_bounds(ix: int, iy: int, grid_size: float) -> tuple[float, float, float, float]:
    size = _validated_size(grid_size)
    minx = WORLD_MIN_X + Decimal(ix) * size
    miny = WORLD_MIN_Y + Decimal(iy) * size
    return float(minx), float(miny), float(minx + size), float(miny + size)


def build_national_grid(boundary: gpd.GeoDataFrame, grid_size: float) -> gpd.GeoDataFrame:
    size = _validated_size(grid_size)
    if boundary.empty:
        return gpd.GeoDataFrame(
            columns=["GRID_ID", "GRID_I", "GRID_J", "GRID_SIZE", "geometry"],
            geometry="geometry",
            crs=4326,
        )

    normalized = boundary.set_crs(4326) if boundary.crs is None else boundary.to_crs(4326)
    geom = normalized.union_all() if hasattr(normalized, "union_all") else normalized.unary_union
    minx, miny, maxx, maxy = normalized.total_bounds
    x_count = int((WORLD_MAX_X - WORLD_MIN_X) / size)
    y_count = int((WORLD_MAX_Y - WORLD_MIN_Y) / size)
    start_x = max(0, _minimum_index(minx, WORLD_MIN_X, size))
    end_x = min(x_count - 1, _maximum_index(maxx, WORLD_MIN_X, size))
    start_y = max(0, _minimum_index(miny, WORLD_MIN_Y, size))
    end_y = min(y_count - 1, _maximum_index(maxy, WORLD_MIN_Y, size))

    cells: list[dict] = []
    size_float = float(size)
    for ix in range(start_x, end_x + 1):
        for iy in range(start_y, end_y + 1):
            bounds = grid_bounds(ix, iy, size_float)
            cell = box(*bounds)
            if cell.intersects(geom):
                cells.append(
                    {
                        "GRID_ID": grid_id(ix, iy, size_float),
                        "GRID_I": ix,
                        "GRID_J": iy,
                        "GRID_SIZE": size_float,
                        "geometry": cell,
                    }
                )

    return gpd.GeoDataFrame(cells, geometry="geometry", crs=4326)
