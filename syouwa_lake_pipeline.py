#!/usr/bin/env python3
"""昭和池の Sentinel-2 データを処理し、CSV・NDCI画像を生成する。

Google Earth Engine をサービスアカウントで初期化し、採用条件を満たす
観測データをローカルへ出力する。Supabase 設定がある場合は Storage と
Postgres にも同じ結果を反映する。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import ee
import requests
from google.oauth2 import service_account
from PIL import Image
from supabase import Client, create_client


LOGGER = logging.getLogger("syouwa_lake_pipeline")
EARTH_ENGINE_SCOPES = (
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform",
)
CSV_COLUMNS = (
    "scene_id",
    "source_product_id",
    "satellite_collection",
    "captured_at",
    "observation_date",
    "ndci_mean",
    "ndti_mean",
    "fai_mean",
    "area_valid_m2",
    "cloud_over_water",
    "valid_ratio",
    "image_bucket",
    "image_path",
    "csv_bucket",
    "csv_path",
)


class ConfigurationError(RuntimeError):
    """必須設定が不足または不正な場合の例外。"""


@dataclass(frozen=True)
class Settings:
    gee_project_id: str
    gee_service_account_json: str
    supabase_url: str | None
    supabase_secret_key: str | None
    image_bucket: str
    csv_bucket: str
    observations_table: str
    ingestion_runs_table: str
    output_dir: Path
    center_lat: float
    center_lon: float
    lat_span: float
    lon_span: float
    cloud_over_water_threshold: float
    min_valid_ratio: float
    cloud_probability_threshold: float
    mndwi_threshold: float
    min_connected_pixels: int
    thumbnail_scale: int
    image_retention_days: int
    water_mask_start_date: date
    water_mask_end_date: date

    @classmethod
    def from_environment(
        cls,
        *,
        output_dir: Path,
        water_mask_end_date: date,
        require_supabase: bool,
    ) -> "Settings":
        gee_project_id = require_env("GEE_PROJECT_ID")
        gee_service_account_json = require_env("GEE_SERVICE_ACCOUNT_JSON")
        supabase_url = clean_env("SUPABASE_URL")
        supabase_secret_key = clean_env("SUPABASE_SECRET_KEY") or clean_env(
            "SUPABASE_SERVICE_ROLE_KEY"
        )

        if require_supabase and (not supabase_url or not supabase_secret_key):
            raise ConfigurationError(
                "SUPABASE_URL と SUPABASE_SECRET_KEY が必要です。"
            )

        return cls(
            gee_project_id=gee_project_id,
            gee_service_account_json=gee_service_account_json,
            supabase_url=supabase_url,
            supabase_secret_key=supabase_secret_key,
            image_bucket=os.getenv("SUPABASE_IMAGE_BUCKET", "satellite-images"),
            csv_bucket=os.getenv("SUPABASE_CSV_BUCKET", "satellite-csv"),
            observations_table=os.getenv(
                "SUPABASE_OBSERVATIONS_TABLE", "satellite_observations"
            ),
            ingestion_runs_table=os.getenv(
                "SUPABASE_INGESTION_RUNS_TABLE", "satellite_ingestion_runs"
            ),
            output_dir=output_dir,
            center_lat=float(os.getenv("ROI_CENTER_LAT", "34.73126")),
            center_lon=float(os.getenv("ROI_CENTER_LON", "137.37958")),
            lat_span=float(os.getenv("ROI_LAT_SPAN", "0.0023")),
            lon_span=float(os.getenv("ROI_LON_SPAN", "0.0028")),
            cloud_over_water_threshold=float(
                os.getenv("CLOUD_OVER_WATER_THRESHOLD", "30.0")
            ),
            min_valid_ratio=float(os.getenv("MIN_VALID_RATIO", "0.3")),
            cloud_probability_threshold=float(
                os.getenv("CLOUD_PROBABILITY_THRESHOLD", "40.0")
            ),
            mndwi_threshold=float(os.getenv("MNDWI_THRESHOLD", "0.05")),
            min_connected_pixels=int(os.getenv("MIN_CONNECTED_PIXELS", "100")),
            thumbnail_scale=int(os.getenv("THUMBNAIL_SCALE_METERS", "10")),
            image_retention_days=int(os.getenv("IMAGE_RETENTION_DAYS", "365")),
            water_mask_start_date=parse_date(
                os.getenv("WATER_MASK_START_DATE", "2021-01-01"),
                "WATER_MASK_START_DATE",
            ),
            water_mask_end_date=parse_date(
                os.getenv(
                    "WATER_MASK_END_DATE", water_mask_end_date.isoformat()
                ),
                "WATER_MASK_END_DATE",
            ),
        )


def clean_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def require_env(name: str) -> str:
    value = clean_env(name)
    if not value:
        raise ConfigurationError(f"環境変数 {name} が必要です。")
    return value


def parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} は YYYY-MM-DD 形式で指定してください。") from exc


def resolve_date_window(args: argparse.Namespace) -> tuple[date, date]:
    end_value = args.end_date or clean_env("END_DATE")
    end_date = parse_date(end_value, "end-date") if end_value else datetime.now(
        timezone.utc
    ).date()

    start_value = args.start_date or clean_env("START_DATE")
    if start_value:
        start_date = parse_date(start_value, "start-date")
    else:
        lookback_days = args.lookback_days or int(os.getenv("LOOKBACK_DAYS", "21"))
        if lookback_days < 1:
            raise ConfigurationError("lookback-days は1以上にしてください。")
        start_date = end_date - timedelta(days=lookback_days - 1)

    if start_date > end_date:
        raise ConfigurationError("start-date は end-date 以前にしてください。")
    return start_date, end_date


def gee_end_exclusive(value: date) -> str:
    return (value + timedelta(days=1)).isoformat()


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "scene"


def build_roi(settings: Settings) -> ee.Geometry:
    half_lat = settings.lat_span / 2
    half_lon = settings.lon_span / 2
    bottom = settings.center_lat - half_lat
    top = settings.center_lat + half_lat
    left = settings.center_lon - half_lon
    right = settings.center_lon + half_lon
    return ee.Geometry.Polygon(
        [[[left, top], [left, bottom], [right, bottom], [right, top]]]
    )


def initialize_earth_engine(settings: Settings) -> None:
    try:
        account_info = json.loads(settings.gee_service_account_json)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "GEE_SERVICE_ACCOUNT_JSON が有効なJSONではありません。"
        ) from exc

    client_email = account_info.get("client_email")
    private_key = account_info.get("private_key")
    if not client_email or not private_key:
        raise ConfigurationError(
            "GEE_SERVICE_ACCOUNT_JSON に client_email/private_key がありません。"
        )

    credentials = service_account.Credentials.from_service_account_info(
        account_info,
        scopes=list(EARTH_ENGINE_SCOPES),
    )
    ee.Initialize(credentials=credentials, project=settings.gee_project_id)
    LOGGER.info("Google Earth Engineを初期化しました: project=%s", settings.gee_project_id)


def joined_sentinel_collection(
    roi: ee.Geometry,
    start_date: date,
    end_date: date,
) -> ee.ImageCollection:
    end_exclusive = gee_end_exclusive(end_date)
    surface_reflectance = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi)
        .filterDate(start_date.isoformat(), end_exclusive)
    )
    cloud_probability = (
        ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
        .filterBounds(roi)
        .filterDate(start_date.isoformat(), end_exclusive)
    )
    joined = ee.ImageCollection(
        ee.Join.saveFirst("cloud_probability_image").apply(
            primary=surface_reflectance,
            secondary=cloud_probability,
            condition=ee.Filter.equals(
                leftField="system:index", rightField="system:index"
            ),
        )
    )
    return joined.filter(ee.Filter.notNull(["cloud_probability_image"]))


def mask_clouds(
    collection: ee.ImageCollection,
    probability_threshold: float,
) -> ee.ImageCollection:
    def apply_mask(image: ee.Image) -> ee.Image:
        cloud_probability = ee.Image(image.get("cloud_probability_image")).select(
            "probability"
        )
        scl = image.select("SCL")
        cloud_mask = (
            cloud_probability.gte(probability_threshold)
            .Or(scl.eq(3))
            .Or(scl.eq(8))
            .Or(scl.eq(9))
            .Or(scl.eq(10))
            .Or(scl.eq(11))
        )
        reflectance = (
            image.select(["B3", "B4", "B5", "B8", "B11"])
            .updateMask(cloud_mask.Not())
            .multiply(0.0001)
            .copyProperties(
                image,
                [
                    "system:time_start",
                    "system:index",
                    "PRODUCT_ID",
                    "MGRS_TILE",
                    "CLOUDY_PIXEL_PERCENTAGE",
                ],
            )
        )
        return reflectance.set("scene_id", image.get("system:index"))

    return collection.map(apply_mask)


def build_water_mask(settings: Settings, roi: ee.Geometry) -> ee.Image:
    if settings.water_mask_start_date > settings.water_mask_end_date:
        raise ConfigurationError(
            "WATER_MASK_START_DATE は WATER_MASK_END_DATE 以前にしてください。"
        )
    reference_joined = joined_sentinel_collection(
        roi,
        settings.water_mask_start_date,
        settings.water_mask_end_date,
    )
    reference_masked = mask_clouds(
        reference_joined, settings.cloud_probability_threshold
    )
    composite = (
        reference_masked.filter(
            ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", 30)
        )
        .median()
        .clip(roi)
    )
    mndwi = composite.normalizedDifference(["B3", "B11"]).rename("MNDWI")
    raw_mask = mndwi.gt(settings.mndwi_threshold).selfMask()
    smooth_mask = (
        raw_mask.focal_max(radius=1, units="pixels")
        .focal_min(radius=1, units="pixels")
        .selfMask()
    )
    connected = smooth_mask.connectedPixelCount(150, True)
    water_mask = smooth_mask.updateMask(
        connected.gte(settings.min_connected_pixels)
    ).selfMask()

    pixel_count = water_mask.reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=roi,
        scale=10,
        maxPixels=1_000_000_000,
    ).get("MNDWI")
    count_value = pixel_count.getInfo()
    if not count_value:
        raise RuntimeError(
            "水域マスクが空です。ROIまたはMNDWI_THRESHOLDを確認してください。"
        )
    LOGGER.info("水域マスクのピクセル数: %s", count_value)
    return water_mask


def score_images(
    collection: ee.ImageCollection,
    water_mask: ee.Image,
    roi: ee.Geometry,
    settings: Settings,
) -> ee.ImageCollection:
    raw_water_count = water_mask.reduceRegion(
        reducer=ee.Reducer.count(),
        geometry=roi,
        scale=10,
        maxPixels=1_000_000_000,
    ).get("MNDWI")
    water_pixel_count = ee.Number(raw_water_count)

    def add_quality(image: ee.Image) -> ee.Image:
        raw_valid_count = (
            image.select("B3")
            .updateMask(water_mask)
            .reduceRegion(
                reducer=ee.Reducer.count(),
                geometry=roi,
                scale=10,
                maxPixels=1_000_000_000,
            )
            .get("B3")
        )
        valid_count = ee.Number(
            ee.Algorithms.If(raw_valid_count, raw_valid_count, 0)
        )
        valid_ratio = ee.Number(
            ee.Algorithms.If(
                water_pixel_count.gt(0),
                valid_count.divide(water_pixel_count),
                0,
            )
        )
        cloud_over_water = ee.Number(1).subtract(valid_ratio).multiply(100)
        return image.set(
            {
                "water_valid_px": valid_count,
                "valid_ratio": valid_ratio,
                "cloud_over_water": cloud_over_water,
            }
        )

    return (
        collection.map(add_quality)
        .filter(
            ee.Filter.lte(
                "cloud_over_water", settings.cloud_over_water_threshold
            )
        )
        .filter(ee.Filter.gte("valid_ratio", settings.min_valid_ratio))
        .filter(ee.Filter.gt("water_valid_px", 0))
        .sort("system:time_start")
    )


def add_driver_bands(
    collection: ee.ImageCollection,
    water_mask: ee.Image,
) -> ee.ImageCollection:
    def add_drivers(image: ee.Image) -> ee.Image:
        ndci = (
            image.normalizedDifference(["B5", "B4"])
            .rename("NDCI")
            .updateMask(water_mask)
        )
        ndti = (
            image.normalizedDifference(["B4", "B3"])
            .rename("NDTI")
            .updateMask(water_mask)
        )
        red = image.select("B4")
        nir = image.select("B8")
        swir = image.select("B11")
        interpolation_ratio = (842 - 665) / (1610 - 665)
        fai = (
            nir.subtract(
                red.add(swir.subtract(red).multiply(interpolation_ratio))
            )
            .rename("FAI")
            .updateMask(water_mask)
        )
        return image.addBands([ndci, ndti, fai])

    return collection.map(add_drivers)


def collection_to_features(
    collection: ee.ImageCollection,
    water_mask: ee.Image,
    roi: ee.Geometry,
) -> ee.FeatureCollection:
    pixel_area = ee.Image.pixelArea().rename("area")

    def to_feature(image: ee.Image) -> ee.Feature:
        means = image.select(["NDCI", "NDTI", "FAI"]).reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=roi,
            scale=10,
            maxPixels=1_000_000_000,
            bestEffort=True,
            tileScale=4,
        )
        valid_area = (
            pixel_area.updateMask(image.select("B3").mask())
            .updateMask(water_mask)
            .reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=roi,
                scale=10,
                maxPixels=1_000_000_000,
                bestEffort=True,
                tileScale=4,
            )
            .get("area")
        )
        captured_at = ee.Date(image.get("system:time_start")).format(
            "YYYY-MM-dd'T'HH:mm:ss'Z'"
        )
        observation_date = ee.Date(image.get("system:time_start")).format(
            "YYYY-MM-dd"
        )
        return ee.Feature(
            None,
            {
                "scene_id": image.get("scene_id"),
                "source_product_id": image.get("PRODUCT_ID"),
                "captured_at": captured_at,
                "observation_date": observation_date,
                "ndci_mean": means.get("NDCI"),
                "ndti_mean": means.get("NDTI"),
                "fai_mean": means.get("FAI"),
                "area_valid_m2": valid_area,
                "cloud_over_water": image.get("cloud_over_water"),
                "valid_ratio": image.get("valid_ratio"),
            },
        )

    # ImageCollection.map() must return images, not features. Convert to a
    # server-side list first so each image can become a Feature.
    images = collection.toList(collection.size())
    return ee.FeatureCollection(
        images.map(lambda image: to_feature(ee.Image(image)))
    ).sort("captured_at")


def fetch_feature_rows(
    feature_collection: ee.FeatureCollection,
    *,
    page_size: int = 40,
) -> list[dict[str, Any]]:
    count = int(feature_collection.size().getInfo())
    rows: list[dict[str, Any]] = []
    for offset in range(0, count, page_size):
        page = ee.FeatureCollection(
            feature_collection.toList(page_size, offset)
        ).getInfo()
        rows.extend(feature["properties"] for feature in page["features"])
    return rows


def normalize_rows(
    raw_rows: Iterable[dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if settings.image_retention_days < 1:
        raise ConfigurationError("IMAGE_RETENTION_DAYS は1以上にしてください。")
    image_cutoff = datetime.now(timezone.utc).date() - timedelta(
        days=settings.image_retention_days
    )
    numeric_columns = (
        "ndci_mean",
        "ndti_mean",
        "fai_mean",
        "area_valid_m2",
        "cloud_over_water",
        "valid_ratio",
    )
    for raw in raw_rows:
        if raw.get("ndci_mean") is None or not raw.get("scene_id"):
            continue
        row = dict(raw)
        for column in numeric_columns:
            row[column] = (
                float(row[column]) if row.get(column) is not None else None
            )
        scene_name = safe_filename(str(row["scene_id"]))
        observed = parse_date(str(row["observation_date"]), "observation_date")
        row["satellite_collection"] = "COPERNICUS/S2_SR_HARMONIZED"
        row["image_bucket"] = settings.image_bucket
        row["image_path"] = (
            f"ndci/{observed:%Y/%m/%d}/NDCI_{observed.isoformat()}_{scene_name}.png"
            if observed >= image_cutoff
            else None
        )
        row["csv_bucket"] = settings.csv_bucket
        row["csv_path"] = ""
        rows.append(row)
    return rows


def download_ndci_image(
    image: ee.Image,
    roi: ee.Geometry,
    destination: Path,
    scale: int,
) -> None:
    url = image.select("NDCI").visualize(
        min=-0.1,
        max=0.4,
        palette=["ffffff", "d9f7d9", "b7f7b7", "2ca02c", "006400"],
    ).getThumbURL(
        {
            "region": roi,
            "scale": scale,
            "format": "png",
        }
    )
    response = requests.get(url, timeout=(15, 180))
    response.raise_for_status()
    image_file = Image.open(io.BytesIO(response.content)).convert("RGBA")
    destination.parent.mkdir(parents=True, exist_ok=True)
    image_file.save(destination, format="PNG", optimize=True)


def write_csv(rows: list[dict[str, Any]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_supabase_client(settings: Settings) -> Client:
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise ConfigurationError("Supabaseの接続設定がありません。")
    return create_client(settings.supabase_url, settings.supabase_secret_key)


def upload_file(
    client: Client,
    *,
    bucket: str,
    storage_path: str,
    local_path: Path,
    content_type: str,
    cache_control: str,
) -> None:
    with local_path.open("rb") as file:
        client.storage.from_(bucket).upload(
            path=storage_path,
            file=file,
            file_options={
                "content-type": content_type,
                "cache-control": cache_control,
                "upsert": "true",
            },
        )


def upsert_observations(
    client: Client,
    table_name: str,
    rows: list[dict[str, Any]],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = [{**row, "updated_at": now} for row in rows]
    for offset in range(0, len(payload), 100):
        client.table(table_name).upsert(
            payload[offset : offset + 100],
            on_conflict="scene_id",
        ).execute()


def log_ingestion_run(
    client: Client,
    table_name: str,
    *,
    started_at: datetime,
    start_date: date,
    end_date: date,
    status: str,
    accepted_count: int,
    message: str,
) -> None:
    client.table(table_name).insert(
        {
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
            "status": status,
            "accepted_count": accepted_count,
            "message": message[:2000],
        }
    ).execute()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="取得開始日（YYYY-MM-DD、両端を含む）")
    parser.add_argument("--end-date", help="取得終了日（YYYY-MM-DD、両端を含む）")
    parser.add_argument(
        "--lookback-days",
        type=int,
        help="開始日未指定時に遡る日数（既定: 21日）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.getenv("OUTPUT_DIR", "output")),
        help="CSV・画像のローカル出力先",
    )
    parser.add_argument(
        "--skip-supabase",
        action="store_true",
        help="Supabaseへ送らずローカル出力だけを行う",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=int(os.getenv("MAX_IMAGES_PER_RUN", "0")),
        help="1回に処理する画像数。0は無制限",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    started_at = datetime.now(timezone.utc)
    start_date, end_date = resolve_date_window(args)
    settings = Settings.from_environment(
        output_dir=args.output_dir.resolve(),
        water_mask_end_date=end_date,
        require_supabase=not args.skip_supabase,
    )
    LOGGER.info("取得期間: %s ～ %s", start_date, end_date)
    initialize_earth_engine(settings)
    roi = build_roi(settings)
    water_mask = build_water_mask(settings, roi)

    joined = joined_sentinel_collection(roi, start_date, end_date)
    masked = mask_clouds(joined, settings.cloud_probability_threshold)
    scored = score_images(masked, water_mask, roi, settings)
    drivers = add_driver_bands(scored, water_mask)
    features = collection_to_features(drivers, water_mask, roi)
    rows = normalize_rows(fetch_feature_rows(features), settings)
    if args.max_images > 0:
        rows = rows[-args.max_images :]

    supabase_client: Client | None = None
    if not args.skip_supabase:
        supabase_client = make_supabase_client(settings)

    if not rows:
        message = "採用条件を満たす新しい衛星データはありません。"
        LOGGER.info(message)
        if supabase_client:
            log_ingestion_run(
                supabase_client,
                settings.ingestion_runs_table,
                started_at=started_at,
                start_date=start_date,
                end_date=end_date,
                status="no_data",
                accepted_count=0,
                message=message,
            )
        return 0

    image_files: list[tuple[dict[str, Any], Path]] = []
    for row in rows:
        if not row["image_path"]:
            continue
        image = ee.Image(
            drivers.filter(ee.Filter.eq("scene_id", row["scene_id"])).first()
        )
        local_image_path = settings.output_dir / row["image_path"]
        LOGGER.info("NDCI画像を生成します: %s", row["scene_id"])
        download_ndci_image(
            image,
            roi,
            local_image_path,
            settings.thumbnail_scale,
        )
        image_files.append((row, local_image_path))

    csv_storage_path = (
        f"observations/{end_date:%Y/%m}/"
        f"syouwa_lake_{start_date.isoformat()}_{end_date.isoformat()}.csv"
    )
    for row in rows:
        row["csv_path"] = csv_storage_path
    local_csv_path = settings.output_dir / csv_storage_path
    write_csv(rows, local_csv_path)
    LOGGER.info("CSVを生成しました: %s（%d件）", local_csv_path, len(rows))

    if supabase_client:
        for row, local_image_path in image_files:
            upload_file(
                supabase_client,
                bucket=settings.image_bucket,
                storage_path=row["image_path"],
                local_path=local_image_path,
                content_type="image/png",
                cache_control="31536000",
            )
        upload_file(
            supabase_client,
            bucket=settings.csv_bucket,
            storage_path=csv_storage_path,
            local_path=local_csv_path,
            content_type="text/csv; charset=utf-8",
            cache_control="3600",
        )
        upsert_observations(
            supabase_client,
            settings.observations_table,
            rows,
        )
        log_ingestion_run(
            supabase_client,
            settings.ingestion_runs_table,
            started_at=started_at,
            start_date=start_date,
            end_date=end_date,
            status="success",
            accepted_count=len(rows),
            message="CSV・画像・観測値の保存が完了しました。",
        )
        LOGGER.info("Supabaseへの反映が完了しました。")
    else:
        LOGGER.info("--skip-supabase のため、ローカル出力のみ完了しました。")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return run(parse_args(argv or sys.argv[1:]))
    except ConfigurationError as exc:
        LOGGER.error("設定エラー: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("衛星データ処理に失敗しました。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
