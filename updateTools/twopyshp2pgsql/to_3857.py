'''
Author: yejinliang
Date: 2025-12-09 15:32:45
LastEditTime: 2025-12-09 15:32:49
LastEditors: yejinliang
Description: 
'''
import geopandas as gpd
import time


time_start = time.time()
# 1. 读入 4326 的 Shapefile
gdf = gpd.read_file(r'D:\SceneAutomationV3\Tools\pyshp2pgsql\shp\buildings_merged.shp')          # 此时 gdf.crs 一般是 EPSG:4326

# 2. 转到 3857
gdf_3857 = gdf.to_crs(epsg=3857)          # 或 .to_crs('EPSG:3857')

# 3. 保存
gdf_3857.to_file('output_3857.shp')
time_end = time.time()
print(f'运行时间: {time_end - time_start} 秒')
print('已生成 output_3857.shp')