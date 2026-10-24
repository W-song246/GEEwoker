"""GEE 取数服务：由国内的 Gradio 界面调用，不在这里保存任何密钥。"""

import base64
import calendar
import hmac
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import ee
import requests
from fastapi import Depends, FastAPI, Header, HTTPException
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


@app.get("/")
@app.get("/health")
def health():
    # status 只说明进程在运行；gee_connection 才表示实际的 GEE 检测结果。
    return {"status": "ok", "service": "gee-worker", "api_version": 3,
            "gee_initialized": gee_ready, "gee_connection": gee_connection}


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


def download_map(image, area):
    url = image.clip(area).getThumbURL({
        "region": area, "dimensions": 700, "format": "png", "crs": "EPSG:3857",
        "min": -20, "max": 50,
        "palette": ["0000ff", "00ffff", "00ff00", "ffff00", "ff0000"],
    })
    # 图像由本服务下载后交给国内服务器，浏览器不需要直连 Google。
    with requests.get(url, timeout=(10, 90), stream=True) as response:
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 4 * 1024 * 1024:
                raise ValueError("Thumbnail exceeds size limit")
            chunks.append(chunk)
    content = b"".join(chunks)
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Thumbnail is not a PNG")
    return base64.b64encode(content).decode("ascii")


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
            logger.warning("Map download failed (%s)", type(error).__name__)
            result["warnings"].append("map_unavailable")
    return result


@app.post("/temperature", dependencies=[Depends(check_token)])
def temperature(query: TemperatureQuery):
    today = datetime.now(timezone.utc).date()
    if (query.year, query.month) > (today.year, today.month):
        raise HTTPException(422, detail="A future month cannot be queried")
    return run_gee(lambda: calculate_temperature(query))

