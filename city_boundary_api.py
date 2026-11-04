"""研究2的城市边界取数接口；与原有半径查询接口并存。"""

import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal

import ee
import requests
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field


DATASET = "MODIS/061/MOD11A1"
SCHEMA = "city-boundary-temperature-v1"
ROOT = "https://raw.githubusercontent.com/junwang23/geoCN/master/geojson/"
ADMIN_ROOT = "https://raw.githubusercontent.com/modood/Administrative-divisions-of-China/master/dist/"
DISTRICT_ROOT = "https://raw.githubusercontent.com/pamaforce/geojson/main/geojson_wgs84/100000/"
PALETTE = ["173f96", "53b8e8", "9fe3cb", "ffe68a", "ef754e", "bb2847"]
MUNICIPALITIES = {"北京": "Beijing", "天津": "Tianjin", "上海": "Shanghai", "重庆": "Chongqing"}
grid_counts = {}


class CityName(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    city: str = Field(min_length=2, max_length=40)
    district: str | None = Field(default=None, min_length=2, max_length=40)


class CityQuery(CityName):
    start: date
    end: date
    operation: Literal["summary", "daily", "history", "map"] = "summary"


def validate_period(query):
    if query.start < date(2000, 2, 24) or query.end < query.start:
        raise HTTPException(422, detail="请选择 2000-02-24 之后、起止顺序正确的日期。")
    if (query.end - query.start).days > 92:
        raise HTTPException(422, detail="一次最多查询 93 天。")
    if query.start > datetime.now(timezone.utc).date():
        raise HTTPException(422, detail="不能查询尚未发生的日期。")


@lru_cache(maxsize=2)
def boundary_file(filename):
    response = requests.get(ROOT + filename, timeout=(8, 40))
    response.raise_for_status()
    return response.json()["features"]


@lru_cache(maxsize=1)
def administrative_index():
    cities = requests.get(ADMIN_ROOT + "cities.json", timeout=(8, 40))
    areas = requests.get(ADMIN_ROOT + "areas.json", timeout=(8, 40))
    cities.raise_for_status()
    areas.raise_for_status()
    return cities.json(), areas.json()


@lru_cache(maxsize=256)
def district_geometry(code):
    province = code[:2] + "0000"
    city = code[:4] + "00"
    parts = [province, code] if code[:2] in {"11", "12", "31", "50"} else [province, city, code]
    filename = "geojson_wgs84/100000/" + "/".join(parts) + "/" + code + ".json"
    response = requests.get(DISTRICT_ROOT + "/".join(parts) + "/" + code + ".json",
                            timeout=(8, 40))
    response.raise_for_status()
    features = response.json().get("features", [])
    if len(features) != 1 or str(features[0]["properties"].get("adcode")) != code:
        raise HTTPException(422, detail="区界文件与所选行政区代码不一致。")
    return features[0], filename


def district_boundary(city, district):
    name = city.strip().removesuffix("市")
    district = district.strip()
    if not district.endswith("区"):
        raise HTTPException(422, detail="目前支持市辖区查询，请输入完整区名，例如“蜀山区”。")
    cities, areas = administrative_index()
    if name in MUNICIPALITIES:
        codes = {str(item["code"]) for item in cities
                 if str(item["provinceCode"]) == {"北京":"11","天津":"12","上海":"31","重庆":"50"}[name]
                 and item["name"] == "市辖区"}
    else:
        codes = {str(item["code"]) for item in cities if item["name"].removesuffix("市") == name}
    matches = [item for item in areas if str(item["cityCode"]) in codes and item["name"] == district]
    if len(matches) != 1:
        raise HTTPException(422, detail=f"未找到“{name}{district}”的唯一区界，请核对城市与区名。")
    code = str(matches[0]["code"])
    try:
        feature, filename = district_geometry(code)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            raise HTTPException(422, detail=f"{name}{district}暂缺可用区界，未返回全市数据。") from None
        raise
    feature = {**feature, "properties": {**feature["properties"], "name": district}}
    return feature, filename


def city_boundary(city, district=None):
    if district:
        return district_boundary(city, district)
    name = city.strip().removesuffix("市")
    if name.endswith(("区", "县")):
        raise HTTPException(422, detail="当前接口只支持城市和直辖市边界。")
    if name in MUNICIPALITIES:
        filename = "china_provinces.json"
        matches = [item for item in boundary_file(filename)
                   if item["properties"]["name"] == MUNICIPALITIES[name]]
    else:
        filename = "china_cities.json"
        matches = [item for item in boundary_file(filename)
                   if item["properties"]["name"].removesuffix("市") == name]
    if len(matches) != 1:
        raise HTTPException(422, detail=f"没有找到唯一的{name}城市边界，请核对城市全称。")
    return matches[0], filename


def prepare(image):
    qa = image.select("QC_Day")
    good = qa.bitwiseAnd(3).eq(0).And(qa.rightShift(6).bitwiseAnd(3).lte(1))
    return (image.select("LST_Day_1km").multiply(0.02).subtract(273.15)
            .rename("LST_C").updateMask(good)
            .copyProperties(image, ["system:time_start"]))


def images(start, end, area):
    return (ee.ImageCollection(DATASET)
            .filterDate(start.isoformat(), (end + timedelta(days=1)).isoformat())
            .filterBounds(area).map(prepare).sort("system:time_start"))


def reduce_image(image, area, projection):
    reducer = (ee.Reducer.mean().combine(ee.Reducer.minMax(), sharedInputs=True)
               .combine(ee.Reducer.count(), sharedInputs=True))
    return image.reduceRegion(reducer=reducer, geometry=area, crs=projection,
                              scale=1000, maxPixels=1e9, tileScale=4)


def context(query):
    feature, filename = city_boundary(query.city, query.district)
    area = ee.Geometry(feature["geometry"])
    projection = ee.ImageCollection(DATASET).first().select("LST_Day_1km").projection()
    key = query.city + "/" + (query.district or "")
    cached = grid_counts.get(key)
    if cached and cached[0] > time.monotonic():
        total = cached[1]
    else:
        raw = reduce_image(ee.Image.constant(1).rename("LST_C"), area, projection).getInfo()
        total = int(raw.get("LST_C_count") or 0)
        if total <= 0:
            raise HTTPException(422, detail="城市边界在卫星网格上没有有效像元。")
        if len(grid_counts) >= 64:
            grid_counts.clear()
        grid_counts[key] = (time.monotonic() + 1800, total)
    return area, projection, total, filename


def statistics(raw, total):
    raw = raw or {}
    count = int(raw.get("LST_C_count") or 0)
    if count < 0 or count > total:
        raise RuntimeError("City valid-pixel count exceeds the same-grid denominator")
    return {"count": count, "coverage": round(100 * count / total, 1) if total else 0,
            **{key: float(raw["LST_C_" + key])
               if count and raw.get("LST_C_" + key) is not None else None
               for key in ("min", "mean", "max")}}


def daily_rows(query, received, total):
    by_day = {row["date"]: row for row in received}
    return [{"date": (query.start + timedelta(days=i)).isoformat(),
             **statistics(by_day.get((query.start + timedelta(days=i)).isoformat()), total)}
            for i in range((query.end - query.start).days + 1)]


def historical_rows(query, area, projection, total):
    years = list(range(query.start.year - 9, query.start.year + 1))
    expressions = []
    for year in years:
        try:
            start = query.start.replace(year=year)
            end = query.end.replace(year=year + query.end.year - query.start.year)
        except ValueError:
            continue
        if start < date(2000, 2, 24):
            continue
        collection = images(start, end, area)
        empty = ee.Image.constant(0).rename("LST_C").updateMask(ee.Image.constant(0))
        composite = ee.Image(ee.Algorithms.If(collection.size().gt(0), collection.mean(), empty))
        expressions.append(reduce_image(composite, area, projection).set("year", year))
    received = ee.List(expressions).getInfo() if expressions else []
    found = {int(row["year"]): row for row in received}
    return [{"year": year, **statistics(found.get(year), total)} for year in years]


def raster_bounds(area):
    ring = area.bounds(maxError=100, proj="EPSG:3857").coordinates().getInfo()[0]
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    radius = 6378137.0
    to_lat = lambda value: math.degrees(2 * math.atan(math.exp(value / radius)) - math.pi / 2)
    return [[to_lat(min(ys)), math.degrees(min(xs) / radius)],
            [to_lat(max(ys)), math.degrees(max(xs) / radius)]]


def calculate(query, download_map):
    validate_period(query)
    area, projection, total, filename = context(query)
    result = {"schema": SCHEMA, "dataset": DATASET, "operation": query.operation,
              "city": query.city, "district": query.district,
              "start": query.start.isoformat(), "end": query.end.isoformat(),
              "unit": "degC", "scale_m": 1000, "total": total, "boundary_file": filename,
              "quality": "QC_Day mandatory QA=0; LST error<=2K"}
    collection = images(query.start, query.end, area)
    if query.operation == "history":
        return {**result, "history": historical_rows(query, area, projection, total)}
    if query.operation == "daily":
        def attach(image):
            return image.set("city_daily", reduce_image(image, area, projection)
                             .set("date", image.date().format("YYYY-MM-dd")))
        received = collection.map(attach).aggregate_array("city_daily").getInfo() or []
        return {**result, "daily": daily_rows(query, received, total)}
    count = int(collection.size().getInfo())
    composite = collection.mean().clip(area) if count else None
    if query.operation == "summary":
        summary = statistics(reduce_image(composite, area, projection).getInfo() if count else {}, total)
        return {**result, "image_count": count, "summary": summary,
                "daily": ([{"date": query.start.isoformat(), **summary}]
                          if query.start == query.end else [])}
    if not count:
        return {**result, "map_png_base64": None, "bounds": None}
    return {**result, "map_png_base64": download_map(composite, area, palette=PALETTE,
                                                    white_background=True),
            "bounds": raster_bounds(area)}


def register_city_api(app, check_token, run_gee, download_map):
    @app.post("/research2/boundary", dependencies=[Depends(check_token)])
    def boundary(query: CityName):
        try:
            feature, filename = city_boundary(query.city, query.district)
            return {"schema": SCHEMA, "feature": feature, "boundary_file": filename}
        except requests.RequestException:
            raise HTTPException(502, detail="暂时无法读取城市边界数据。") from None

    @app.post("/research2/query", dependencies=[Depends(check_token)])
    def query_city(query: CityQuery):
        validate_period(query)
        return run_gee(lambda: calculate(query, download_map))

    @app.get("/research2/install.sh", include_in_schema=False)
    def installer():
        return FileResponse(Path(__file__).with_name("research2_install.sh"), media_type="text/plain")

    @app.get("/research2/bundle.zip", include_in_schema=False)
    def bundle():
        return FileResponse(Path(__file__).with_name("research2_bundle.zip"),
                            media_type="application/zip", filename="research2-city.zip")
