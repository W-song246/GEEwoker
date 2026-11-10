"""独立温度交互接口；不改变旧城市网页的接口和统计结果。"""
import hashlib
import json
import math
import threading
import time
from datetime import date
from pathlib import Path
from typing import Literal

import ee
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

import city_boundary_api as city

SCHEMA = "temperature-explorer-v1"
SAMPLE_LIMIT = 3000
_cache = {}
_cache_lock = threading.Lock()


class ExplorerQuery(city.CityName):
    start: date
    end: date
    metric: Literal['temperature', 'ndvi'] = 'temperature'


class PointQuery(ExplorerQuery):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85, le=85)
    radius_m: int = Field(default=1000, ge=1000, le=1000)


class RelationshipQuery(ExplorerQuery):
    longitude: float | None = Field(default=None,ge=-180,le=180)
    latitude: float | None = Field(default=None,ge=-85,le=85)


def native_reduce(image, area, projection, reducer=None):
    reducer = reducer or (ee.Reducer.mean().combine(ee.Reducer.minMax(), sharedInputs=True)
                          .combine(ee.Reducer.count(), sharedInputs=True))
    # Projection already contains its native affine grid. transform() returns
    # a WKT String, whereas reduceRegion.crsTransform requires six numbers.
    # Keep the full Projection and omit scale so its native grid is retained.
    return image.reduceRegion(reducer=reducer, geometry=area, crs=projection,
                              maxPixels=1e9, tileScale=4)


def composite(collection, projection):
    empty = ee.Image.constant(0).rename("LST_C").updateMask(ee.Image.constant(0))
    return ee.Image(ee.Algorithms.If(collection.size().gt(0), collection.mean(), empty)).setDefaultProjection(projection)


def window_data(query, area, projection):
    collection = city.images(query.start, query.end, area)
    image = composite(collection, projection).clip(area)
    def attach(daily_image):
        return daily_image.set("explorer_daily", native_reduce(daily_image, area, projection)
                               .set("date", daily_image.date().format("YYYY-MM-dd")))
    values = ee.Dictionary({
        "total": native_reduce(ee.Image.constant(1).rename("LST_C"), area, projection),
        "summary": native_reduce(image, area, projection),
        "daily": collection.map(attach).aggregate_array("explorer_daily"),
        "samples": image.sample(region=area, projection=projection, numPixels=SAMPLE_LIMIT,
                                seed=19, dropNulls=True, geometries=False, tileScale=4)
                       .limit(SAMPLE_LIMIT).aggregate_array("LST_C"),
        "image_count": collection.size(),
    }).getInfo()
    total = int((values.get("total") or {}).get("LST_C_count") or 0)
    samples = [float(v) for v in values.get("samples", []) if v is not None and math.isfinite(v)]
    return image, collection, {
        "summary": city.statistics(values.get("summary"), total), "total": total,
        "daily": city.daily_rows(query, values.get("daily") or [], total),
        "samples": samples, "sample_count": len(samples), "sample_limit": SAMPLE_LIMIT,
        "sample_basis": "Period pixel-mean image; seeded sample of valid native-grid pixels",
        "image_count": int(values.get("image_count") or 0),
    }


def metadata(query, operation):
    return {"schema": SCHEMA, "dataset": city.DATASET, "operation": operation,
            "metric": "temperature",
            "city": query.city, "district": query.district,
            "start": query.start.isoformat(), "end": query.end.isoformat(),
            "unit": "degC", "nominal_resolution_m": 1000,
            "quality": "QC_Day mandatory QA=0; LST error category<=2K",
            "statistic": "Spatial statistics of QC-filtered daily pixel-mean composite"}


def image_output(image, area, stats, download_map):
    bounds = city.raster_bounds(area)
    if stats["mean"] is None:
        return {"map_png_base64": None, "bounds": bounds}
    return {"map_png_base64": download_map(image, area, palette=city.PALETTE,
                                          white_background=False), "bounds": bounds}


def region(query, download_map):
    city.validate_period(query)
    feature, filename = city.city_boundary(query.city, query.district)
    area = ee.Geometry(feature["geometry"])
    projection = ee.ImageCollection(city.DATASET).first().select("LST_Day_1km").projection()
    image, _, data = window_data(query, area, projection)
    return {**metadata(query, "region"), **data, "feature": feature,
            "boundary_file": filename, "native_projection": projection.getInfo(),
            **image_output(image, area, data["summary"], download_map)}


def pixel_polygon(column, row, transform, crs):
    """由MODIS原生网格的仿射变换构造像元边框，不用近似经纬度方框。"""
    a, b, c, d, e, f = transform
    ring = [[a*x+b*y+c, d*x+e*y+f] for x, y in
            [(column,row), (column+1,row), (column+1,row+1), (column,row+1), (column,row)]]
    return ee.Geometry.Polygon([ring], proj=crs, geodesic=False).transform("EPSG:4326", 1)


def point(query, download_map):
    city.validate_period(query)
    location = ee.Geometry.Point([query.longitude, query.latitude])
    area = location.buffer(query.radius_m)
    projection = ee.ImageCollection(city.DATASET).first().select("LST_Day_1km").projection()
    image, collection, data = window_data(query, area, projection)
    grid = projection.getInfo()
    raw = native_reduce(ee.Image.pixelCoordinates(projection).addBands(image), location,
                        projection, ee.Reducer.first()).getInfo() or {}
    value = raw.get("LST_C")
    footprint = None
    if "x" in raw and "y" in raw:
        footprint = pixel_polygon(raw["x"], raw["y"], grid["transform"], grid["crs"]).getInfo()
    def attach(daily_image):
        result = native_reduce(daily_image, location, projection, ee.Reducer.first())
        return daily_image.set("point_daily", result.set("date", daily_image.date().format("YYYY-MM-dd")))
    observed = collection.map(attach).aggregate_array("point_daily").getInfo() or []
    by_date = {r["date"]: r.get("LST_C") for r in observed}
    point_rows = [{"date": r["date"], "value": by_date.get(r["date"])} for r in data["daily"]]
    feature, filename = city.city_boundary(query.city, query.district)
    return {**metadata(query, "point"), "longitude": query.longitude, "latitude": query.latitude,
            "feature": feature, "boundary_file": filename,
            "radius_m": query.radius_m,
            "point": {"value": value, "pixel_geometry": footprint, "daily": point_rows,
                      "statistic": "Native pixel containing selected point; no nearest-valid replacement"},
            "neighborhood": {**data, **image_output(image, area, data["summary"], download_map)},
            "native_projection": grid}


def history(query, download_map=None):
    city.validate_period(query)
    feature,filename=city.city_boundary(query.city,query.district)
    area=ee.Geometry(feature['geometry'])
    projection=ee.ImageCollection(city.DATASET).first().select('LST_Day_1km').projection()
    total=int(native_reduce(ee.Image.constant(1).rename('LST_C'),area,projection).getInfo().get('LST_C_count') or 0)
    expressions=[]
    for year in range(query.start.year-9,query.start.year+1):
        try:
            first=query.start.replace(year=year)
            last=query.end.replace(year=year+query.end.year-query.start.year)
        except ValueError:continue
        if first<date(2000,2,24):continue
        image=composite(city.images(first,last,area),projection)
        expressions.append(native_reduce(image,area,projection).set('year',year))
    records=ee.List(expressions).getInfo() if expressions else []
    by_year={int(row['year']):row for row in records}
    rows=[{'year':year,**city.statistics(by_year.get(year),total)}
          for year in range(query.start.year-9,query.start.year+1)]
    return {**metadata(query,'history'),'history':rows,'boundary_file':filename}


def cached(query, operation, run_gee, download_map):
    key = hashlib.sha256((operation + query.model_dump_json()).encode()).hexdigest()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
    if operation == 'relationship':
        from relationship_explorer_api import relationship
        operation_function=relationship
    elif query.metric == 'ndvi':
        import vegetation_explorer_api as vegetation
        operation_function = getattr(vegetation, operation)
    else:
        operation_function={'region':region,'point':point,'history':history}[operation]
    value = run_gee(lambda: operation_function(query, download_map))
    with _cache_lock:
        if len(_cache) >= 24:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + 900, value)
    return value


def register_explorer_api(app, check_token, run_gee, download_map):
    @app.post('/research2/explorer/relationship',dependencies=[Depends(check_token)])
    def query_relationship(query:RelationshipQuery):
        city.validate_period(query)
        if (query.longitude is None)!=(query.latitude is None):raise HTTPException(422,'经纬度需要同时提供。')
        return cached(query,'relationship',run_gee,download_map)
    @app.post("/research2/explorer/region", dependencies=[Depends(check_token)])
    def query_region(query: ExplorerQuery):
        city.validate_period(query)
        return cached(query, "region", run_gee, download_map)

    @app.post("/research2/explorer/point", dependencies=[Depends(check_token)])
    def query_point(query: PointQuery):
        city.validate_period(query)
        return cached(query, "point", run_gee, download_map)

    @app.post("/research2/explorer/history", dependencies=[Depends(check_token)])
    def query_history(query: ExplorerQuery):
        city.validate_period(query)
        return cached(query,"history",run_gee,download_map)

    @app.get("/research2/explorer/install.sh", include_in_schema=False)
    def installer():
        return FileResponse(Path(__file__).with_name("research2_explorer_install.sh"),media_type="text/plain")

    @app.get("/research2/explorer/bundle.zip", include_in_schema=False)
    def bundle():
        return FileResponse(Path(__file__).with_name("research2_explorer_bundle.zip"),
                            media_type="application/zip",filename="research2_explorer_bundle.zip")
