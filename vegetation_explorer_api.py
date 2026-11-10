"""NDVI取数：原生250m网格、真实16天产品期，绝不复制成每日观测。"""
import base64
from datetime import date, timedelta

import ee

import city_boundary_api as city
from temperature_explorer_api import native_reduce, pixel_polygon

DATASET = 'MODIS/061/MOD13Q1'
PALETTE = ['d6c2a0', 'f4edcc', 'c1da9a', '7db86b', '3c884b', '155d36']


def period_end(image):
    start = image.date()
    next_year = ee.Date.fromYMD(ee.Number(start.get('year')).add(1), 1, 1)
    return ee.Date(start.advance(16, 'day').millis().min(next_year.millis()))


def prepare(image):
    raw = image.select('NDVI')
    land = image.select('DetailedQA').rightShift(11).bitwiseAnd(7).eq(1)
    good = image.select('SummaryQA').eq(0).And(land).And(raw.gte(-2000)).And(raw.lte(10000))
    return (raw.multiply(.0001).rename('NDVI').updateMask(good)
            .copyProperties(image, ['system:time_start'])
            .set('period_end_exclusive', period_end(image).millis()))


def selected_observation(image, query):
    start = ee.Date.fromYMD(image.date().get('year'), 1, 1).millis()
    observed = image.select('DayOfYear').subtract(1).multiply(86400000).add(start)
    inside = observed.gte(ee.Date(query.start.isoformat()).millis()).And(
        observed.lt(ee.Date((query.end+timedelta(days=1)).isoformat()).millis()))
    inside=inside.And(image.select('DayOfYear').gte(1)).And(image.select('DayOfYear').lte(366))
    return (ee.Image(prepare(image)).updateMask(inside)
            .set('source_period_start',image.date().format('YYYY-MM-dd'))
            .set('source_period_end',period_end(image).advance(-1,'day').format('YYYY-MM-dd'))
            .set('selection_start',ee.Date(image.date().millis().max(ee.Date(query.start.isoformat()).millis())).format('YYYY-MM-dd'))
            .set('selection_end',ee.Date(period_end(image).advance(-1,'day').millis().min(ee.Date(query.end.isoformat()).millis())).format('YYYY-MM-dd')))


def images(query, area):
    first = query.start - timedelta(days=15)
    collection = (ee.ImageCollection(DATASET)
                  .filterDate(first.isoformat(), (query.end + timedelta(days=1)).isoformat())
                  .filterBounds(area).map(lambda image:selected_observation(image,query)))
    collection = collection.filter(ee.Filter.gt('period_end_exclusive', ee.Date(query.start.isoformat()).millis()))
    return collection.sort('system:time_start')


def statistics(raw, total):
    raw = raw or {}
    count = int(raw.get('NDVI_count') or 0)
    if count < 0 or count > total:
        raise RuntimeError('NDVI valid-pixel count exceeds native-grid denominator')
    return {'count': count, 'coverage': round(100 * count / total, 1) if total else 0,
            **{key: float(raw['NDVI_' + key]) if count and raw.get('NDVI_' + key) is not None else None
               for key in ('min', 'mean', 'max')}}


def composite(collection, projection):
    empty = ee.Image.constant(0).rename('NDVI').updateMask(ee.Image.constant(0))
    return ee.Image(ee.Algorithms.If(collection.size().gt(0), collection.mean(), empty)).setDefaultProjection(projection)


def rows(collection, area, projection, point=False):
    def attach(image):
        reducer = ee.Reducer.first() if point else None
        return image.set('observation', native_reduce(image, area, projection, reducer)
                         .set('date', image.get('selection_start'))
                         .set('period_start', image.get('selection_start'))
                         .set('period_end', image.get('selection_end'))
                         .set('source_period_start',image.get('source_period_start'))
                         .set('source_period_end',image.get('source_period_end')))
    return collection.map(attach).aggregate_array('observation')


def window_data(query, area, projection):
    collection = images(query, area)
    image = composite(collection, projection).clip(area)
    values = ee.Dictionary({
        'total': native_reduce(ee.Image.constant(1).rename('NDVI'), area, projection),
        'summary': native_reduce(image, area, projection),
        'observations': rows(collection, area, projection),
        'samples': image.sample(region=area, projection=projection, numPixels=3000,
                                seed=19, dropNulls=True, geometries=False, tileScale=4)
                       .limit(3000).aggregate_array('NDVI'),
        'image_count': collection.size(),
    }).getInfo()
    total = int((values.get('total') or {}).get('NDVI_count') or 0)
    observations = [{**{key: row[key] for key in ('date', 'period_start', 'period_end','source_period_start','source_period_end') if key in row},
                     **statistics(row, total)} for row in values.get('observations', [])]
    samples = [float(value) for value in values.get('samples', []) if value is not None]
    return image, collection, {
        'metric': 'ndvi',
        'summary': statistics(values.get('summary'), total), 'total': total,
        'daily': [], 'observations': observations, 'samples': samples,
        'sample_count': len(samples), 'sample_limit': 3000,
        'sample_basis': 'Seeded valid native NDVI pixels; selected pixel observation dates strictly inside requested window',
        'image_count': int(values.get('image_count') or 0),
        'actual_periods': [{'start': row['period_start'], 'end': row['period_end']} for row in observations],
    }


def metadata(query, operation):
    return {'schema': 'temperature-explorer-v1', 'dataset': DATASET, 'metric': 'ndvi', 'operation': operation,
            'city': query.city, 'district': query.district, 'start': query.start.isoformat(), 'end': query.end.isoformat(),
            'unit': 'NDVI', 'nominal_resolution_m': 250, 'cadence': '16-day composite',
            'quality': 'SummaryQA=0; DetailedQA land/water=1 (land); valid raw NDVI -2000..10000; scale=0.0001',
            'time_basis': 'actual_pixel_observation_date_inside_requested_range',
            'temporal_note': '16天合成源产品，按DayOfYear逐像元筛选，只纳入所选日期范围内被选中的观测；区间外观测已排除。没有观测的日期不补值。',
            'statistic': 'Spatial statistics of QC-valid product-period pixel-mean NDVI; NDVI is not vegetation coverage %'}


def image_output(image, area, stats, download_map):
    return {'bounds': city.raster_bounds(area),
            'map_png_base64': download_map(image, area, palette=PALETTE, white_background=False,
                                          minimum=-.2, maximum=1) if stats['mean'] is not None else None}


def region(query, download_map):
    feature, filename = city.city_boundary(query.city, query.district)
    area = ee.Geometry(feature['geometry'])
    projection = ee.ImageCollection(DATASET).first().select('NDVI').projection()
    image, _, data = window_data(query, area, projection)
    return {**metadata(query, 'region'), **data, 'feature': feature, 'boundary_file': filename,
            'native_projection': projection.getInfo(), **image_output(image, area, data['summary'], download_map)}


def point(query, download_map):
    location = ee.Geometry.Point([query.longitude, query.latitude])
    area = location.buffer(query.radius_m)
    projection = ee.ImageCollection(DATASET).first().select('NDVI').projection()
    image, collection, data = window_data(query, area, projection)
    grid = projection.getInfo()
    raw = native_reduce(ee.Image.pixelCoordinates(projection).addBands(image), location,
                        projection, ee.Reducer.first()).getInfo() or {}
    footprint = (pixel_polygon(raw['x'], raw['y'], grid['transform'], grid['crs']).getInfo()
                 if raw.get('x') is not None and raw.get('y') is not None else None)
    point_rows = [{**{key: row[key] for key in ('date', 'period_start', 'period_end','source_period_start','source_period_end') if key in row}, 'value': row.get('NDVI')}
                  for row in (rows(collection, location, projection, point=True).getInfo() or [])]
    feature, filename = city.city_boundary(query.city, query.district)
    return {**metadata(query, 'point'), 'longitude': query.longitude, 'latitude': query.latitude,
            'radius_m': query.radius_m, 'feature': feature, 'boundary_file': filename, 'native_projection': grid,
            'point': {'value': raw.get('NDVI'), 'pixel_geometry': footprint, 'metric': 'ndvi', 'daily': [], 'observations': point_rows,
                      'statistic': 'Selected native NDVI pixel; no nearest-valid replacement'},
            'neighborhood': {**data, **image_output(image, area, data['summary'], download_map)}}


def history(query, download_map=None):
    feature, filename = city.city_boundary(query.city, query.district)
    area = ee.Geometry(feature['geometry'])
    projection = ee.ImageCollection(DATASET).first().select('NDVI').projection()
    total = int(native_reduce(ee.Image.constant(1).rename('NDVI'), area, projection).getInfo().get('NDVI_count') or 0)
    expressions = []
    for year in range(max(2000, query.start.year - 9), query.start.year + 1):
        try:
            period = query.model_copy(update={'start': query.start.replace(year=year),
                                             'end': query.end.replace(year=year + query.end.year-query.start.year)})
        except ValueError:
            continue
        image = composite(images(period, area), projection)
        expressions.append(native_reduce(image, area, projection).set('year', year))
    records = ee.List(expressions).getInfo() if expressions else []
    found = {int(row['year']): row for row in records}
    return {**metadata(query, 'history'), 'boundary_file': filename,
            'history': [{'year': year, **statistics(found.get(year), total)}
                        for year in range(query.start.year-9, query.start.year+1)]}


def check_connection(download_map):
    from temperature_explorer_api import ExplorerQuery, PointQuery
    query = ExplorerQuery(city='合肥', district='蜀山区', start=date(2025, 7, 1), end=date(2025, 7, 31), metric='ndvi')
    result = region(query, download_map)
    selected = point(PointQuery(**query.model_dump(), longitude=117.2, latitude=31.85), download_map)
    from relationship_explorer_api import relationship
    from temperature_explorer_api import RelationshipQuery
    relation=relationship(RelationshipQuery(**{**query.model_dump(),'metric':'temperature'}),download_map)
    if not result['summary']['count'] or not result['map_png_base64'] or not selected['point']['pixel_geometry']:
        raise RuntimeError('NDVI self-check did not return valid native-grid observations and image')
    return {'dataset': DATASET, 'metric': 'ndvi', 'query': query.model_dump(mode='json'),
            'summary': result['summary'], 'periods': result['actual_periods'],
            'png_bytes': len(base64.b64decode(result['map_png_base64'])),
            'point_value': selected['point']['value'], 'neighborhood': selected['neighborhood']['summary'],
            'point_pixel_geometry': bool(selected['point']['pixel_geometry']),
            'relationship':{key:relation[key] for key in ('status','sample_count','pearson_r','note')},
            'time_basis':metadata(query,'region')['time_basis']}
