import geopandas as gpd
from shapely.geometry import box


def build_rectangles(count: int, axis: str = "x", crs: str = "EPSG:4326") -> gpd.GeoDataFrame:
    records = []
    for index in range(count):
        x = float(index) if axis == "x" else 0.0
        y = float(index) if axis == "y" else 0.0
        records.append({"feature_id": index, "geometry": box(x, y, x + 0.8, y + 0.8)})
    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
