from dataclasses import dataclass
from pathlib import Path
import argparse

import geopandas as gpd


TARGET_TILE_FEATURES = 200_000
MIN_TILE_FEATURES = 50_000
MAX_MERGED_TILE_FEATURES = 250_000
MERGE_DISTANCE_TOLERANCE = 500.0
MAX_MERGED_BOUNDS_EXPANSION_RATIO = 1.25
MAX_SPLIT_TRIGGER = TARGET_TILE_FEATURES
MAX_RECURSION_DEPTH = 32


Bounds = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class ChunkPlan:
    indices: tuple[int, ...]
    bounds: Bounds

    @property
    def size(self) -> int:
        return len(self.indices)

    @property
    def start(self) -> int:
        return min(self.indices) if self.indices else 0

    @property
    def end(self) -> int:
        return max(self.indices) + 1 if self.indices else 0


@dataclass(frozen=True)
class SplitPlan:
    chunks: list[ChunkPlan]


def build_split_plan(gdf: gpd.GeoDataFrame) -> SplitPlan:
    indexed_gdf = gdf.reset_index(drop=True)
    total = len(indexed_gdf)
    if total <= TARGET_TILE_FEATURES:
        return SplitPlan(chunks=[_build_chunk(indexed_gdf, tuple(range(total)))])

    working_gdf = _to_split_crs(indexed_gdf)
    points = _representative_points(working_gdf)
    leaf_groups = _split_indices(tuple(range(total)), points)
    chunks = [_build_chunk(working_gdf, indices) for indices in leaf_groups]
    chunks = _merge_small_chunks(chunks, working_gdf)
    return SplitPlan(chunks=_sort_chunks_spatially(chunks))


def assign_boundary_to_current_chunk(gdf: gpd.GeoDataFrame, split_value: float, axis: str) -> list[int]:
    bounds = gdf.geometry.bounds
    min_col = "minx" if axis == "x" else "miny"
    selected: list[int] = []
    for index, row in bounds.iterrows():
        if row[min_col] <= split_value:
            selected.append(index)
    return selected


def export_split_shapefiles(source_path: Path, output_dir: Path) -> list[Path]:
    gdf = gpd.read_file(source_path)
    plan = build_split_plan(gdf)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for chunk_index, chunk in enumerate(plan.chunks, start=1):
        chunk_gdf = gdf.iloc[list(chunk.indices)].copy()
        target = output_dir / f"{source_path.stem}_001_{chunk_index:03d}.shp"
        chunk_gdf.to_file(target)
        outputs.append(target)
    return outputs


def _to_split_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None or gdf.crs.to_epsg() == 3857:
        return gdf
    return gdf.to_crs(epsg=3857)


def _representative_points(gdf: gpd.GeoDataFrame) -> list[Point]:
    points = gdf.geometry.representative_point()
    bounds = gdf.geometry.bounds
    result: list[Point] = []
    for index, point in enumerate(points):
        if point.is_empty:
            row = bounds.iloc[index]
            result.append(((float(row.minx) + float(row.maxx)) / 2.0, (float(row.miny) + float(row.maxy)) / 2.0))
        else:
            result.append((float(point.x), float(point.y)))
    return result


def _split_indices(indices: tuple[int, ...], points: list[Point], depth: int = 0) -> list[tuple[int, ...]]:
    if len(indices) <= TARGET_TILE_FEATURES or depth >= MAX_RECURSION_DEPTH:
        return [indices]

    minx, miny, maxx, maxy = _point_bounds(indices, points)
    if minx == maxx and miny == maxy:
        return _split_by_spatial_order(indices, points)

    midx = (minx + maxx) / 2.0
    midy = (miny + maxy) / 2.0
    quadrants: list[list[int]] = [[], [], [], []]
    for index in indices:
        x, y = points[index]
        right = x >= midx
        top = y >= midy
        quadrant_index = (1 if right else 0) + (2 if top else 0)
        quadrants[quadrant_index].append(index)

    non_empty = [tuple(group) for group in quadrants if group]
    if len(non_empty) <= 1:
        return _split_by_spatial_order(indices, points)

    leaves: list[tuple[int, ...]] = []
    for group in non_empty:
        leaves.extend(_split_indices(group, points, depth + 1))
    return leaves


def _split_by_spatial_order(indices: tuple[int, ...], points: list[Point]) -> list[tuple[int, ...]]:
    ordered = tuple(sorted(indices, key=lambda index: (points[index][1], points[index][0], index)))
    return [
        ordered[start:start + TARGET_TILE_FEATURES]
        for start in range(0, len(ordered), TARGET_TILE_FEATURES)
    ]


def _point_bounds(indices: tuple[int, ...], points: list[Point]) -> Bounds:
    xs = [points[index][0] for index in indices]
    ys = [points[index][1] for index in indices]
    return min(xs), min(ys), max(xs), max(ys)


def _build_chunk(gdf: gpd.GeoDataFrame, indices: tuple[int, ...]) -> ChunkPlan:
    return ChunkPlan(indices=indices, bounds=_geometry_bounds(gdf, indices))


def _geometry_bounds(gdf: gpd.GeoDataFrame, indices: tuple[int, ...]) -> Bounds:
    if not indices:
        return 0.0, 0.0, 0.0, 0.0
    bounds = gdf.iloc[list(indices)].total_bounds
    return tuple(float(value) for value in bounds)


def _merge_small_chunks(chunks: list[ChunkPlan], gdf: gpd.GeoDataFrame) -> list[ChunkPlan]:
    merged = chunks[:]
    while True:
        best_pair = _find_best_small_chunk_merge(merged)
        if best_pair is None:
            return _sort_chunks_spatially(merged)

        left_index, right_index = best_pair
        left = merged[left_index]
        right = merged[right_index]
        replacement = _merge_chunk_pair(left, right, gdf)
        merged = [
            chunk
            for index, chunk in enumerate(merged)
            if index not in {left_index, right_index}
        ]
        merged.append(replacement)


def _find_best_small_chunk_merge(chunks: list[ChunkPlan]) -> tuple[int, int] | None:
    best: tuple[float, float, int, int] | None = None
    for left_index, left in enumerate(chunks):
        if left.size >= MIN_TILE_FEATURES:
            continue

        for right_index, right in enumerate(chunks):
            if left_index == right_index:
                continue
            if left.size + right.size > MAX_MERGED_TILE_FEATURES:
                continue
            distance = _bounds_distance(left.bounds, right.bounds)
            merged_bounds = _merge_bounds(left.bounds, right.bounds)
            merged_area = _bounds_area(merged_bounds)
            if not _can_merge_bounds(left.bounds, right.bounds, distance, merged_area):
                continue

            candidate = (distance, merged_area, left_index, right_index)
            if best is None or candidate < best:
                best = candidate

    if best is None:
        return None

    left_index, right_index = best[2], best[3]
    return (min(left_index, right_index), max(left_index, right_index))


def _merge_chunk_pair(left: ChunkPlan, right: ChunkPlan, gdf: gpd.GeoDataFrame) -> ChunkPlan:
    indices = tuple(sorted(left.indices + right.indices))
    return _build_chunk(gdf, indices)


def _can_merge_bounds(left: Bounds, right: Bounds, distance: float, merged_area: float) -> bool:
    if distance <= MERGE_DISTANCE_TOLERANCE:
        return True

    source_area = _bounds_area(left) + _bounds_area(right)
    if source_area <= 0.0:
        return False
    return (merged_area / source_area) <= MAX_MERGED_BOUNDS_EXPANSION_RATIO


def _bounds_distance(left: Bounds, right: Bounds) -> float:
    x_gap = max(left[0] - right[2], right[0] - left[2], 0.0)
    y_gap = max(left[1] - right[3], right[1] - left[3], 0.0)
    return (x_gap * x_gap + y_gap * y_gap) ** 0.5


def _merge_bounds(left: Bounds, right: Bounds) -> Bounds:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _bounds_area(bounds: Bounds) -> float:
    return max(bounds[2] - bounds[0], 0.0) * max(bounds[3] - bounds[1], 0.0)


def _sort_chunks_spatially(chunks: list[ChunkPlan]) -> list[ChunkPlan]:
    return sorted(chunks, key=lambda chunk: (chunk.bounds[1], chunk.bounds[0], chunk.bounds[3], chunk.bounds[2], chunk.start))


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a shapefile into adaptive spatial tiles.")
    parser.add_argument("input_shp", type=Path, help="Input shapefile path.")
    parser.add_argument("output_dir", type=Path, help="Output directory for split shapefiles.")
    args = parser.parse_args()
    export_split_shapefiles(args.input_shp, args.output_dir)


if __name__ == "__main__":
    main()
