-- =====================================================
-- 多国多城市建筑数据 PostGIS 数据库架构
-- =====================================================

-- 启用 PostGIS 扩展
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE EXTENSION IF NOT EXISTS postgis_tiger_geocoder;

-- =====================================================
-- 1. 地理区域管理表 (按层级组织)
-- =====================================================

-- 创建 world_building 表（英文列名）
CREATE TABLE world_building (
    id SERIAL PRIMARY KEY,
    continent VARCHAR(100) NOT NULL,
    country VARCHAR(100) NOT NULL,
    city VARCHAR(100) NOT NULL,
    shp_name VARCHAR(100) NOT NULL,
    bounding_box GEOMETRY(POLYGON, 3857) NOT NULL,
    coordinate_system VARCHAR(50) DEFAULT 'EPSG:3857',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建唯一索引，确保同一地区同一 shp 只存一条记录
CREATE UNIQUE INDEX uk_4col
    ON world_building (continent, country, city, shp_name);

-- 创建更新时间的触发器函数
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 创建触发器，当更新记录时自动更新时间戳
CREATE TRIGGER update_world_building_modtime
    BEFORE UPDATE ON world_building
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- 添加空间索引以提高查询性能
CREATE INDEX idx_world_building_bounding_box ON world_building USING GIST (bounding_box);