"""同一时段汇总、同一温度原生网格及共同有效掩膜的空间关联。"""
from datetime import date
import ee
import city_boundary_api as city
import vegetation_explorer_api as vegetation
from temperature_explorer_api import native_reduce, metadata, composite as temperature_composite
from paired_statistics import summarize_pairs


def paired_image(query, area):
    projection=ee.ImageCollection(city.DATASET).first().select('LST_Day_1km').projection()
    ndvi_projection=ee.ImageCollection(vegetation.DATASET).first().select('NDVI').projection()
    collection=vegetation.images(query,area)
    fine=vegetation.composite(collection,ndvi_projection).clip(area)
    ndvi=(fine.reduceResolution(reducer=ee.Reducer.mean(),maxPixels=256)
          .reproject(crs=projection).rename('NDVI'))
    fraction=(fine.mask().unmask(0).reduceResolution(reducer=ee.Reducer.mean(),maxPixels=256)
              .reproject(crs=projection))
    temperature=temperature_composite(city.images(query.start,query.end,area),projection)
    common=ndvi.mask().And(temperature.mask()).And(fraction.gte(.5))
    return ndvi.addBands(temperature).updateMask(common),projection


def relationship(query,download_map):
    feature,filename=city.city_boundary(query.city,query.district)
    area=(ee.Geometry.Point([query.longitude,query.latitude]).buffer(1000)
          if query.longitude is not None else ee.Geometry(feature['geometry']))
    image,projection=paired_image(query,area)
    samples=image.sample(region=area,projection=projection,numPixels=3000,seed=19,
                         dropNulls=True,geometries=False,tileScale=4).limit(3000)
    values=ee.Dictionary({'samples':samples.toList(3000).map(lambda item:ee.Feature(item).toDictionary()),
                          'count':native_reduce(image.select('NDVI'),area,projection,ee.Reducer.count())}).getInfo()
    result=summarize_pairs(values.get('samples') or [])
    result.update(metadata(query,'relationship'),datasets=[city.DATASET,vegetation.DATASET],
                  scope='point_neighborhood' if query.longitude is not None else 'region',
                  longitude=query.longitude,latitude=query.latitude,
                  grid='MOD11A1 native grid; NDVI area-aggregated, at least 50% valid fine-cell coverage',
                  temporal_alignment='Both summaries contain observations only inside the requested window; observation dates within the window can differ.',
                  note='同一查询时段内汇总，NDVI按面积聚合到温度网格，取共同有效像元；观测日并非逐日一一相同。r仅描述空间关联，不证明植被导致降温。',
                  paired_grid_count=int((values.get('count') or {}).get('NDVI') or 0),
                  boundary_file=filename,map_png_base64=None,bounds=city.raster_bounds(area))
    if result['sample_count']:
        classified=image.select('NDVI').gte(result['ndvi_median']).multiply(2).add(
            image.select('LST_C').gte(result['temperature_median'])).rename('class')
        result['map_png_base64']=download_map(classified,area,palette=['8cacc4','d58666','519d85','b5a363'],minimum=0,maximum=3)
    return result
