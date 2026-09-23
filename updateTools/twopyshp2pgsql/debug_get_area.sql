-- 获取指定范围内的建筑物数据，返回 GeoJSON 格式
SELECT jsonb_build_object(
    'type',     'FeatureCollection',
	'crs', jsonb_build_object(
	  'type', 'name',
	  'properties', jsonb_build_object(
		'name', 'EPSG:3857'
	  )
	),
    'features', jsonb_agg(
        jsonb_build_object(
            'type',       'Feature',
            'geometry',   ST_AsGeoJSON(bounding_box)::jsonb,
            'properties', to_jsonb(row) - 'bounding_box'  -- 去掉几何字段，避免重复
        )
    )
)
FROM (
	select * from world_building WHERE ST_Intersects(
	    bounding_box,
	    ST_GeomFromText('POLYGON((12650315.360400783 4076037.0977246836, 12693090.79860414 4076037.0977246836, 12693090.79860414 4123291.8893139036, 12650315.360400783 4123291.8893139036, 12650315.360400783 4076037.0977246836))', 3857)
	)
) AS row



SELECT json_build_object(
  'list',
  COALESCE(
    json_agg(
      concat_ws('/', continent, country, city, shp_name)
    ),
    '[]'::json
  )
) AS result
FROM world_building
WHERE ST_Intersects(
  bounding_box,
  ST_GeomFromText(
    'POLYGON((
      12650315.360400783 4076037.0977246836,
      12693090.79860414 4076037.0977246836,
      12693090.79860414 4123291.8893139036,
      12650315.360400783 4123291.8893139036,
      12650315.360400783 4076037.0977246836
    ))',
    3857
  )
);

SELECT json_build_object(
  'list',
  COALESCE(
    json_agg(
      concat_ws('/', continent, country, city, shp_name)
    ),
    '[]'::json
  )
) AS result
FROM world_building
WHERE ST_Intersects(
    bounding_box,
    ST_Transform(
        ST_MakeEnvelope(
            106.418801, 29.467595,   -- minx, miny
            106.659414, 29.662968,   -- maxx, maxy
            4326
        ),
        3857
    )
);

SELECT *
FROM public.world_building
WHERE ST_Intersects(
    bounding_box,
    ST_Transform(
        ST_MakeEnvelope(
            106.418801, 29.467595,   -- minx, miny
            106.659414, 29.662968,   -- maxx, maxy
            4326
        ),
        3857
    )
);
