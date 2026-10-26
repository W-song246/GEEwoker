"""GEE 取数服务：由国内的 Gradio 界面调用，不在这里保存任何密钥。"""

import base64
import calendar
import hmac
import hashlib
import json
import logging
import math
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import ee
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field


@asynccontextmanager
async def lifespan(application):
    """服务上线后自动检查一次 GEE；不阻塞网页和健康检查。"""
    global gee_connection
    if os.getenv("GEE_SERVICE_ACCOUNT_JSON", "").strip():
        gee_connection = "checking"
        threading.Thread(target=startup_gee_check, daemon=True).start()
    else:
        gee_connection = "not_configured"
    yield


app = FastAPI(title="GEE temperature worker", docs_url=None, redoc_url=None,
              lifespan=lifespan)
logger = logging.getLogger("uvicorn.error")
gee_lock = threading.Lock()
gee_ready = False
gee_connection = "not_started"
map_check = {"status": "not_started"}
history_check = {"status": "not_started"}
DATASET = "MODIS/061/MOD11A1"


def check_token(authorization: str | None = Header(default=None)):
    """只有持有相同口令的国内服务器才能发起取数请求。"""
    expected = os.getenv("WORKER_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, detail="WORKER_TOKEN is not configured")
    supplied = (authorization or "").partition(" ")
    if (supplied[0].lower() != "bearer" or not supplied[2]
            or not hmac.compare_digest(supplied[2].encode(), expected.encode())):
        raise HTTPException(401, detail="Invalid access token")


def initialize_gee():
    """首次请求时才初始化；环境变量未填写时，健康检查仍然能打开。"""
    global gee_ready
    if gee_ready:
        return
    raw_key = os.getenv("GEE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_key:
        raise HTTPException(503, detail="GEE_SERVICE_ACCOUNT_JSON is not configured")
    try:
        info = json.loads(raw_key)
        project = os.getenv("GEE_PROJECT_ID", "").strip() or info["project_id"]
        credentials = ee.ServiceAccountCredentials(
            info["client_email"], key_data=raw_key
        )
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, detail="Invalid service account configuration") from None
    ee.Initialize(credentials, project=project)
    ee.data.setDeadline(90000)
    gee_ready = True


def safe_error_message(error):
    """显示 Google 返回的具体原因，同时移除口令、私钥和访问令牌。"""
    message = str(error)
    sensitive = [os.getenv("WORKER_TOKEN", ""),
                 os.getenv("GEE_SERVICE_ACCOUNT_JSON", "")]
    try:
        info = json.loads(os.getenv("GEE_SERVICE_ACCOUNT_JSON", ""))
        if isinstance(info, dict):
            sensitive.extend(info.get(name, "") for name in
                             ("private_key", "private_key_id", "client_secret"))
    except (ValueError, TypeError):
        pass
    for value in sensitive:
        if isinstance(value, str) and value:
            message = message.replace(value, "[redacted]")
            message = message.replace(json.dumps(value)[1:-1], "[redacted]")
    message = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?"
                     r"(?:-----END [^-]*PRIVATE KEY-----|$)",
                     "[redacted private key]", message, flags=re.S)
    message = re.sub(r"(?i)Bearer\s+[^\s\"',<>]+", "Bearer [redacted]", message)
    message = re.sub(r"\bya29\.[A-Za-z0-9._-]+", "[redacted access token]", message)
    message = re.sub(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
                     r"[A-Za-z0-9_-]{12,}\b", "[redacted JWT]", message)
    message = re.sub(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]+",
                     r"\1?[redacted query]", message)
    return message[:1500] or type(error).__name__


def run_gee(operation):
    """免费实例先一次处理一个 GEE 查询，避免同时占满配额和内存。"""
    if not gee_lock.acquire(blocking=False):
        raise HTTPException(429, detail="A query is running. Try again later.",
                            headers={"Retry-After": "15"})
    stage = "initialize_gee"
    try:
        initialize_gee()
        stage = "gee_query"
        return operation()
    except HTTPException:
        raise
    except Exception as error:
        message = safe_error_message(error)
        logger.error("GEE failed at %s (%s): %s", stage, type(error).__name__, message)
        raise HTTPException(502, detail={
            "stage": stage, "error_type": type(error).__name__, "message": message,
        }) from None
    finally:
        gee_lock.release()


@app.head("/")
@app.head("/health")
@app.get("/")
@app.get("/health")
def health():
    # status 只说明进程在运行；gee_connection 才表示实际的 GEE 检测结果。
    return {"status": "ok", "service": "gee-worker", "api_version": 8,
            "capabilities": ["center-temperature-30km-v1", "center-history-30km-v1"],
            "gee_initialized": gee_ready, "gee_connection": gee_connection,
            "map_check": map_check, "history_check": history_check}


@app.get("/platform/install.sh", include_in_schema=False)
def platform_installer():
    path = Path(__file__).resolve().with_name("platform_install.sh")
    if not path.is_file():
        raise HTTPException(404, detail="Installer is not available")
    return FileResponse(path, media_type="text/plain")


@app.get("/platform/bundle.zip", include_in_schema=False)
def platform_bundle():
    path = Path(__file__).resolve().with_name("platform_bundle.zip")
    if not path.is_file():
        raise HTTPException(404, detail="Package is not available")
    return FileResponse(path, media_type="application/zip", filename="temperature-platform.zip")


def check_gee_connection():
    global gee_connection

    def operation():
        value = ee.Number(1).getInfo()
        if value != 1:
            raise RuntimeError("GEE returned an unexpected check value")
        return {"status": "ok", "gee_value": value,
                "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        result = run_gee(operation)
    except HTTPException as error:
        if error.status_code != 429:
            gee_connection = "failed"
        raise
    gee_connection = "ok"
    return result


def startup_gee_check():
    try:
        check_gee_connection()
    except HTTPException as error:
        logger.error("GEE_AUTO_CHECK_FAILED: %s", json.dumps(error.detail, ensure_ascii=False))
    else:
        logger.info("GEE_AUTO_CHECK_OK: connected to Google Earth Engine")
        map_check.clear()
        map_check.update(status="checking")
        try:
            result = run_gee(check_map_connection)
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
            # 测试只用公开的北京 MODIS 数据；公开状态中隐藏完整下载链接。
            message = re.sub(r"https?://[^\s\"'<>]+", "[redacted URL]",
                             safe_error_message(detail.get("message", "Map check failed")))
            map_check.update(status="failed", error_type=detail.get("error_type", "HTTPException"),
                             message=message)
            logger.error("GEE_MAP_CHECK_FAILED: %s", json.dumps(map_check, ensure_ascii=False))
        else:
            map_check.update(result)
            logger.info("GEE_MAP_CHECK_OK: generated %s bytes", result["png_bytes"])
        history_check.update(status="checking")
        try:
            result = run_gee(check_history_connection)
        except HTTPException as error:
            history_check.update(status="failed", message=safe_error_message(error.detail))
            logger.error("GEE_HISTORY_CHECK_FAILED: %s", json.dumps(history_check, ensure_ascii=False))
        else:
            history_check.update(status="ok", sample=result)
            logger.info("GEE_HISTORY_CHECK_OK: %s annual records", len(result["years"]))


@app.get("/gee-check", dependencies=[Depends(check_token)])
def gee_check():
    return check_gee_connection()


class TemperatureQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85, le=85)
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    include_map: bool = True


def to_celsius(image):
    # 与现有程序一致：保留 MODIS 原有掩膜，没有额外进行 QC_Day 严格筛选。
    return (image.select("LST_Day_1km").multiply(0.02).subtract(273.15)
            .rename("LST_C").copyProperties(image, ["system:time_start"]))


def make_daily_rows(year, month, observations):
    """每个日历日都有一行；无值用 None，绝不补成 0 度。"""
    by_date = {item["date"]: item for item in observations}
    today = datetime.now(timezone.utc).date()
    rows = []
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        current = date(year, month, day)
        item = by_date.get(current.isoformat())
        if item is None:
            status = "future" if current > today else "no_image"
            rows.append({"date": current.isoformat(), "center": None, "outer": None,
                         "center_valid_pixels": 0, "outer_valid_pixels": 0,
                         "status": status})
        else:
            rows.append({"date": current.isoformat(),
                         "center": item.get("center"), "outer": item.get("outer"),
                         "center_valid_pixels": item.get("center_valid_pixels") or 0,
                         "outer_valid_pixels": item.get("outer_valid_pixels") or 0,
                         "status": "available" if item.get("center") is not None
                         and item.get("outer") is not None else "missing_pixels"})
    return rows


def download_map(image, area, palette=None, white_background=False):
    """通过已有的计算权限直接取得 PNG，不创建额外的缩略图资源。"""
    coordinates = area.bounds(maxError=100, proj="EPSG:3857").coordinates().getInfo()[0]
    xs = [point[0] for point in coordinates]
    ys = [point[1] for point in coordinates]
    left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
    span_x, span_y = right - left, top - bottom
    if (not all(math.isfinite(value) for value in (left, right, bottom, top))
            or span_x <= 0 or span_y <= 0):
        raise ValueError("Invalid map bounds")
    pixel_size = max(span_x, span_y) / 700
    width = max(1, min(700, math.ceil(span_x / pixel_size)))
    height = max(1, min(700, math.ceil(span_y / pixel_size)))
    visual = image.clip(area).visualize(
        min=-20, max=50, forceRgbOutput=True,
        palette=palette or ["0000ff", "00ffff", "00ff00", "ffff00", "ff0000"],
    )
    if white_background:
        # 只填显示颜色，不修改任何温度统计或温度影像的掩膜。
        visual = visual.unmask(255)
    # Earth Engine SDK 会携带服务账号认证；PNG 字节由本服务交给国内服务器。
    content = ee.data.computePixels({
        "expression": visual, "fileFormat": "PNG",
        "grid": {
            "dimensions": {"width": width, "height": height},
            "affineTransform": {
                "scaleX": span_x / width, "shearX": 0, "translateX": left,
                "shearY": 0, "scaleY": -span_y / height, "translateY": top,
            },
            "crsCode": "EPSG:3857",
        },
    })
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("Temperature map exceeds size limit")
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Temperature map is not a PNG")
    return base64.b64encode(content).decode("ascii")


def check_map_connection():
    """用上月合肥公开影像核对月、日范围与真实PNG生成。"""
    today = datetime.now(timezone.utc).date()
    year = today.year if today.month > 1 else today.year - 1
    month = today.month - 1 if today.month > 1 else 12
    monthly = calculate_center_temperature(CenterTemperatureQuery(
        longitude=117.22, latitude=31.82, year=year, month=month, include_map=True))
    monthly_png = monthly.get("map_png_base64")
    if not monthly_png:
        raise RuntimeError("Center30 monthly map was not generated")
    available = next((row for row in monthly["daily"] if row["status"] == "available"
                      and (row.get("coverage_fraction") or 0) >= .2), None)
    if available is None:
        raise RuntimeError("No day with sufficient pixels for the public center30 map check")
    chosen = date.fromisoformat(available["date"])
    daily = calculate_center_temperature(CenterTemperatureQuery(
        longitude=117.22, latitude=31.82, year=year, month=month, day=chosen.day, include_map=True))
    encoded = daily.get("map_png_base64")
    if not encoded:
        raise RuntimeError("Center30 daily map was not generated")
    content = base64.b64decode(encoded)
    return {"status": "ok", "schema": daily["schema"], "mode": "day", "date": chosen.isoformat(),
            "city_reference": "Hefei", "radius_m": CENTER_RADIUS_M, "unit": "degC",
            "mean": daily["statistics"]["mean"], "valid_pixels": daily["statistics"]["valid_pixels"],
            "png_bytes": len(content), "png_sha256": hashlib.sha256(content).hexdigest(),
            "month": f"{year:04d}-{month:02d}", "month_total_pixels": monthly["total_pixels"],
            "month_valid_pixels": monthly["statistics"]["valid_pixels"],
            "month_max_daily_valid_pixels": max(row["valid_pixels"] for row in monthly["daily"]),
            "month_png_bytes": len(base64.b64decode(monthly_png))}


def calculate_temperature(query):
    point = ee.Geometry.Point([query.longitude, query.latitude])
    center_area = point.buffer(50000)
    full_area = point.buffer(100000)
    outer_area = full_area.difference(center_area)
    start = ee.Date.fromYMD(query.year, query.month, 1)
    end = start.advance(1, "month")
    collection = (ee.ImageCollection(DATASET).filterDate(start, end)
                  .filterBounds(full_area).map(to_celsius).sort("system:time_start"))
    count = collection.size().getInfo()
    result = {
        "status": "ok" if count else "no_images", "dataset": DATASET,
        "band": "LST_Day_1km", "unit": "degC", "year": query.year,
        "month": query.month, "longitude": query.longitude, "latitude": query.latitude,
        "center_radius_m": 50000, "outer_radius_m": 100000, "scale_m": 1000,
        "image_count": count, "statistics": {}, "daily": [],
        "map_png_base64": None, "warnings": [],
        "notes": {
            "temperature": "Daytime land surface temperature, not air temperature.",
            "quality": "Original valid-pixel mask; no additional QC_Day screening.",
            "monthly": "Regional statistics of the monthly mean image.",
            "missing": "null means no usable value. No interpolation or zero filling.",
            "daily_status": {"no_image": "No image returned for this date; the cause is not determined.",
                             "future": "This date has not occurred yet (UTC).",
                             "missing_pixels": "An image exists; at least one region has no valid value.",
                             "available": "Both regions have valid mean values."},
        },
    }
    if not count:
        result["daily"] = make_daily_rows(query.year, query.month, [])
        return result

    mean_image = collection.mean()
    mean_count = ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True)
    all_stats = (mean_count.combine(ee.Reducer.max(), sharedInputs=True)
                 .combine(ee.Reducer.min(), sharedInputs=True))

    def daily_statistics(image):
        center = image.reduceRegion(mean_count, center_area, 1000, maxPixels=1e8)
        outer = image.reduceRegion(mean_count, outer_area, 1000, maxPixels=1e8)
        return image.set("daily", ee.Dictionary({
            "date": image.date().format("YYYY-MM-dd"),
            "center": center.get("LST_C_mean"), "outer": outer.get("LST_C_mean"),
            "center_valid_pixels": center.get("LST_C_count"),
            "outer_valid_pixels": outer.get("LST_C_count"),
        }))

    # 合并为一次取回，减少服务器和 GEE 之间的往返。
    data = ee.Dictionary({
        "center": mean_image.reduceRegion(all_stats, center_area, 1000, maxPixels=1e8),
        "outer": mean_image.reduceRegion(mean_count, outer_area, 1000, maxPixels=1e8),
        "daily": collection.map(daily_statistics).aggregate_array("daily"),
    }).getInfo()
    center = data["center"]
    outer = data["outer"]
    center_mean = center.get("LST_C_mean")
    outer_mean = outer.get("LST_C_mean")
    result["statistics"] = {
        "center_mean": center_mean, "center_max": center.get("LST_C_max"),
        "center_min": center.get("LST_C_min"), "outer_mean": outer_mean,
        "difference": center_mean - outer_mean
        if center_mean is not None and outer_mean is not None else None,
        "center_valid_pixels": center.get("LST_C_count") or 0,
        "outer_valid_pixels": outer.get("LST_C_count") or 0,
    }
    result["daily"] = make_daily_rows(query.year, query.month, data.get("daily") or [])
    if center_mean is None or outer_mean is None:
        result["status"] = "missing_pixels"
    if query.include_map:
        try:
            result["map_png_base64"] = download_map(mean_image, full_area.bounds())
        except Exception as error:
            logger.warning("Map download failed (%s): %s", type(error).__name__,
                           safe_error_message(error))
            result["warnings"].append("map_unavailable")
    return result


@app.post("/temperature", dependencies=[Depends(check_token)])
def temperature(query: TemperatureQuery):
    today = datetime.now(timezone.utc).date()
    if (query.year, query.month) > (today.year, today.month):
        raise HTTPException(422, detail="A future month cannot be queried")
    return run_gee(lambda: calculate_temperature(query))


# 新工作台只查询中心30公里；原 /temperature 保留给已经部署的旧站点。
CENTER_RADIUS_M = 30000
CENTER_PALETTE = ["313695", "4575b4", "74add1", "abd9e9", "e0f3f8", "ffffbf",
                  "fee090", "fdae61", "f46d43", "d73027", "a50026"]


class CenterTemperatureQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85, le=85)
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    include_map: bool = True


def center_query_dates(query, today=None):
    """日请求的结束为次日；月请求的结束为下月1日，均为右开区间。"""
    today = today or datetime.now(timezone.utc).date()
    try:
        first = date(query.year, query.month, query.day or 1)
    except ValueError:
        raise HTTPException(422, detail="Invalid calendar date") from None
    if first > today:
        raise HTTPException(422, detail="A future date or month cannot be queried")
    if query.day is not None:
        return first, first + timedelta(days=1)
    return first, (first.replace(day=28) + timedelta(days=4)).replace(day=1)


def center_daily_rows(first, end, observations, total_pixels, today=None):
    today = today or datetime.now(timezone.utc).date()
    by_date = {item["date"]: item for item in observations}
    rows = []
    current = first
    while current < end:
        item = by_date.get(current.isoformat())
        pixels = int((item or {}).get("valid_pixels") or 0)
        valid = item is not None and item.get("mean") is not None and pixels > 0
        rows.append({"date": current.isoformat(), "mean": item.get("mean") if valid else None,
                     "min": item.get("min") if valid else None, "max": item.get("max") if valid else None,
                     "valid_pixels": pixels, "coverage_fraction": pixels / total_pixels if total_pixels else None,
                     "status": "available" if valid else "future" if current > today else "no_image" if item is None else "missing_pixels"})
        current += timedelta(days=1)
    return rows


def calculate_center_temperature(query):
    first, end = center_query_dates(query)
    area = ee.Geometry.Point([query.longitude, query.latitude]).buffer(CENTER_RADIUS_M)
    collection = (ee.ImageCollection(DATASET).filterDate(first.isoformat(), end.isoformat())
                  .filterBounds(area).map(to_celsius).sort("system:time_start"))
    count = collection.size().getInfo()
    result = {
        "schema": "center-temperature-v1", "status": "ok" if count else "no_images",
        "dataset": DATASET, "band": "LST_Day_1km", "unit": "degC", "scale_m": 1000,
        "longitude": query.longitude, "latitude": query.latitude, "center_radius_m": CENTER_RADIUS_M,
        "year": query.year, "month": query.month, "day": query.day,
        "period": {"mode": "day" if query.day is not None else "month", "start": first.isoformat(), "end_exclusive": end.isoformat()},
        "image_count": count, "total_pixels": 0,
        "statistics": {"mean": None, "min": None, "max": None, "valid_pixels": 0, "coverage_fraction": None},
        "daily": [], "map_png_base64": None,
        "visualization": {"min": -20, "max": 50, "unit": "degC", "palette": CENTER_PALETTE,
                          "crs": "EPSG:3857", "clip": "center_circle", "radius_m": CENTER_RADIUS_M},
        "warnings": [],
        "notes": {"temperature": "Daytime satellite land surface temperature; not 2m air temperature or a full-day average.",
                  "region": "A 30km radius around the requested reference point, not an administrative boundary.",
                  "quality": "Original product mask; no additional QC_Day screening.",
                  "monthly": "Statistics of the monthly mean image; not an arithmetic mean of daily region means.",
                  "missing": "Missing pixels are not zero-filled or interpolated; coverage uses the same native grid."},
    }
    if not count:
        result["daily"] = center_daily_rows(first, end, [], 0)
        return result
    projection = ee.Image(collection.first()).projection()
    reducer = (ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True)
               .combine(ee.Reducer.min(), sharedInputs=True).combine(ee.Reducer.max(), sharedInputs=True))

    def reduce_image(image):
        return image.reduceRegion(reducer=reducer, geometry=area, scale=1000, crs=projection, maxPixels=1e7)

    def daily_statistics(image):
        values = reduce_image(image)
        return image.set("center_daily", ee.Dictionary({
            "date": image.date().format("YYYY-MM-dd"), "mean": values.get("LST_C_mean"),
            "min": values.get("LST_C_min"), "max": values.get("LST_C_max"),
            "valid_pixels": values.get("LST_C_count"),
        }))

    mean_image = collection.mean()
    # count 与 mean/min/max 组合后使用相同的区域取样口径；总像元数也必须
    # 通过同一 reducer、同一网格计算。单独的 count reducer 对圆边缘像元
    # 采用不同的纳入规则，可能比组合 reducer 的有效像元数少。
    full_grid = ee.Image.constant(1).rename("LST_C")
    values = ee.Dictionary({
        "summary": reduce_image(mean_image),
        "total": reduce_image(full_grid),
        "daily": collection.map(daily_statistics).aggregate_array("center_daily"),
    }).getInfo()
    total = int(values["total"].get("LST_C_count") or 0)
    summary = values["summary"]
    pixels = int(summary.get("LST_C_count") or 0)
    daily = values.get("daily") or []
    if pixels > total or any(int(row.get("valid_pixels") or 0) > total for row in daily):
        raise RuntimeError("Center30 valid pixel count exceeds total pixel count on the same grid")
    result["total_pixels"] = total
    result["statistics"] = {"mean": summary.get("LST_C_mean"), "min": summary.get("LST_C_min"),
                            "max": summary.get("LST_C_max"), "valid_pixels": pixels,
                            "coverage_fraction": pixels / total if total else None}
    result["daily"] = center_daily_rows(first, end, daily, total)
    if pixels == 0 or summary.get("LST_C_mean") is None:
        result["status"] = "missing_pixels"
    elif query.include_map:
        try:
            # area 是圆，而非其包围矩形；圆外与缺测保持掩膜。
            result["map_png_base64"] = download_map(mean_image, area, palette=CENTER_PALETTE, white_background=True)
        except Exception as error:
            logger.warning("Center map generation failed (%s): %s", type(error).__name__, safe_error_message(error))
            result["warnings"].append("map_unavailable")
    return result


@app.post("/temperature/center", dependencies=[Depends(check_token)])
def center_temperature(query: CenterTemperatureQuery):
    center_query_dates(query)
    return run_gee(lambda: calculate_center_temperature(query))


class CenterHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85, le=85)
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


def history_years(query, today=None):
    today = today or datetime.now(timezone.utc).date()
    if date(query.year, query.month, 1) > today:
        raise HTTPException(422, detail="A future month cannot be queried")
    return list(range(max(2000, query.year - 9), query.year + 1))


def calculate_center_history(query):
    """一次批量计算十个同月影像，不下载十张PNG、不累加日均温。"""
    years = history_years(query)
    area = ee.Geometry.Point([query.longitude, query.latitude]).buffer(CENTER_RADIUS_M)
    last_first = date(query.year, query.month, 1)
    last_end = (last_first.replace(day=28) + timedelta(days=4)).replace(day=1)
    base = (ee.ImageCollection(DATASET).filterBounds(area)
            .filterDate(f"{years[0]}-01-01", last_end.isoformat()).map(to_celsius))
    result = {"schema": "center-history-v1", "dataset": DATASET, "band": "LST_Day_1km",
              "unit": "degC", "scale_m": 1000, "center_radius_m": CENTER_RADIUS_M,
              "longitude": query.longitude, "latitude": query.latitude,
              "end_year": query.year, "start_year": years[0], "month": query.month,
              "total_pixels": 0, "years": [],
              "statistic": "Regional statistics of each year's monthly pixel-mean image; original mask, no extra QC."}
    observations = []
    if base.size().getInfo():
        projection = ee.Image(base.first()).projection()
        reducer = (ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True)
                   .combine(ee.Reducer.min(), sharedInputs=True).combine(ee.Reducer.max(), sharedInputs=True))

        def reduce_image(image):
            return image.reduceRegion(reducer=reducer, geometry=area, scale=1000, crs=projection, maxPixels=1e7)

        def annual(year):
            start = ee.Date.fromYMD(ee.Number(year), query.month, 1)
            subset = base.filterDate(start, start.advance(1, "month"))
            count = subset.size()
            empty = {"LST_C_mean": None, "LST_C_min": None, "LST_C_max": None, "LST_C_count": 0}
            values = ee.Dictionary(ee.Algorithms.If(count.gt(0), reduce_image(subset.mean()), empty))
            return values.set("year", year).set("image_count", count)

        values = ee.Dictionary({"total": reduce_image(ee.Image.constant(1).rename("LST_C")),
                                "years": ee.List(years).map(annual)}).getInfo()
        result["total_pixels"] = int(values["total"].get("LST_C_count") or 0)
        observations = values.get("years") or []
    by_year = {int(item["year"]): item for item in observations}
    today = datetime.now(timezone.utc).date()
    for year in years:
        item = by_year.get(year, {})
        pixels, total = int(item.get("LST_C_count") or 0), result["total_pixels"]
        if pixels > total:
            raise RuntimeError("Historical valid pixel count exceeds total on the same grid")
        first = date(year, query.month, 1)
        end = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        count = int(item.get("image_count") or 0)
        valid = pixels > 0 and item.get("LST_C_mean") is not None
        result["years"].append({"year": year, "month": query.month, "start": first.isoformat(),
                                "end_exclusive": end.isoformat(), "complete_month": end <= today,
                                "image_count": count, "mean": item.get("LST_C_mean") if valid else None,
                                "min": item.get("LST_C_min") if valid else None,
                                "max": item.get("LST_C_max") if valid else None,
                                "valid_pixels": pixels, "coverage_fraction": pixels / total if total else None,
                                "status": "available" if valid else "no_images" if count == 0 else "missing_pixels"})
    return result


@app.post("/temperature/center/history", dependencies=[Depends(check_token)])
def center_history(query: CenterHistoryQuery):
    history_years(query)
    return run_gee(lambda: calculate_center_history(query))


def check_history_connection():
    today = datetime.now(timezone.utc).date()
    previous = today.replace(day=1) - timedelta(days=1)
    result = calculate_center_history(CenterHistoryQuery(longitude=117.22, latitude=31.82,
                                                       year=previous.year, month=previous.month))
    if len(result["years"]) != min(10, previous.year - 1999) or not any(row["mean"] is not None for row in result["years"]):
        raise RuntimeError("Historical same-month check did not return valid observations")
    return result
