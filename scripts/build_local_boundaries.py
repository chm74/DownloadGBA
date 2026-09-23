import argparse
import sys
import time
import zipfile
from pathlib import Path

import requests

NE_S3_BASE = "https://naturalearth.s3.amazonaws.com/10m_cultural"
NE_FILES = {
    "ne_10m_admin_0_countries": "ne_admin0.gpkg",
    "ne_10m_admin_1_states_provinces": "ne_admin1.gpkg",
}


def remote_size(url: str) -> int | None:
    try:
        response = requests.head(url, timeout=45, proxies={}, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        if response.status_code == 200 and response.headers.get("content-length"):
            return int(response.headers["content-length"])
    except requests.RequestException:
        return None
    return None


def download_resumable(url: str, target: Path, retries: int = 30, timeout: int = 180) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    expected = remote_size(url)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        existing = part.stat().st_size if part.exists() else 0
        if expected is not None and existing == expected:
            break
        if expected is not None and existing > expected:
            print(f"  断点文件异常（{existing}/{expected} 字节），重新下载")
            part.unlink(missing_ok=True)
            existing = 0
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout, proxies={}) as response:
                if response.status_code == 416:
                    break
                response.raise_for_status()
                mode = "ab" if existing and response.status_code == 206 else "wb"
                if mode == "wb":
                    existing = 0
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            handle.write(chunk)
            size = part.stat().st_size
            if expected is None or size >= expected:
                break
            print(f"  续传中（第 {attempt}/{retries} 次）：{size}/{expected} 字节")
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            print(f"  下载中断（第 {attempt}/{retries} 次）: {exc}")
            time.sleep(min(attempt * 2, 15))
    else:
        raise RuntimeError(f"下载失败: {target.name} last_error={last_error}")

    if expected is not None and part.stat().st_size != expected:
        raise RuntimeError(f"下载不完整: {target.name} {part.stat().st_size}/{expected} 字节")

    target.unlink(missing_ok=True)
    part.replace(target)


def convert_zip_to_gpkg(zip_path: Path, gpkg_path: Path, extract_dir: Path) -> int:
    import geopandas as gpd

    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    shapefiles = sorted(extract_dir.rglob("*.shp"))
    if not shapefiles:
        raise RuntimeError(f"压缩包中未找到 shapefile: {zip_path}")
    gdf = gpd.read_file(shapefiles[0])
    gpkg_path.unlink(missing_ok=True)
    gdf.to_file(gpkg_path, driver="GPKG", layer=gpkg_path.stem)
    return len(gdf)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载 Natural Earth 行政边界并转换为本地 GPKG 索引。")
    parser.add_argument("--data-dir", default="data/boundaries", help="边界数据目录。")
    parser.add_argument("--force", action="store_true", help="存在时也重新下载与转换。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = repo_root / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    for dataset_name, gpkg_name in NE_FILES.items():
        zip_path = data_dir / f"{dataset_name}.zip"
        gpkg_path = data_dir / gpkg_name
        if gpkg_path.exists() and not args.force:
            print(f"已存在，跳过: {gpkg_path}")
            continue
        if not zip_path.exists() or args.force:
            url = f"{NE_S3_BASE}/{dataset_name}.zip"
            print(f"下载 {dataset_name}.zip ...")
            download_resumable(url, zip_path)
        print(f"转换 {dataset_name}.zip -> {gpkg_name} ...")
        count = convert_zip_to_gpkg(zip_path, gpkg_path, data_dir / f"{dataset_name}_extract")
        print(f"完成 {gpkg_name}，要素数 {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
