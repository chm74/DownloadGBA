import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import requests

WFS_URL = "https://tubvsig-so2sat-vm1.srv.mwn.de/geoserver/ows"
VIEWER_SESSION_URL = "https://tubvsig-so2sat-vm1.srv.mwn.de/viewer-session"

MAX_REQUEST_SPAN = 0.09
MIN_REQUEST_SPAN = 0.003
DEFAULT_TIMEOUT = 300
DEFAULT_SLEEP = 0.2
DEFAULT_MIN_REQUEST_INTERVAL = float(os.environ.get("GBA_WFS_MIN_INTERVAL", "1.05"))
SESSION_TOKEN_MARGIN = 60
SESSION_REQUEST_BUDGET = 180
RATE_LIMIT_MAX_RETRIES = 15

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class WfsError(RuntimeError):
    pass


class WfsBboxTooLargeError(WfsError):
    pass


class WfsRateLimitError(WfsError):
    pass


THREAD_LOCAL = threading.local()
PRINT_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
NEXT_ALLOWED_AT = 0.0


def respect_global_rate(min_interval: float) -> None:
    global NEXT_ALLOWED_AT
    if min_interval <= 0:
        return
    while True:
        with RATE_LOCK:
            now = time.monotonic()
            wait = NEXT_ALLOWED_AT - now
            if wait <= 0:
                NEXT_ALLOWED_AT = now + min_interval
                return
        time.sleep(min(wait, 1.0))


def get_thread_client(**kwargs) -> "GbaWfsClient":
    client = getattr(THREAD_LOCAL, "client", None)
    if client is None:
        client = GbaWfsClient(**kwargs)
        THREAD_LOCAL.client = client
    return client


def log_line(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


@dataclass(frozen=True)
class FetchStats:
    requests: int
    splits: int
    max_span: float


class GbaWfsClient:
    def __init__(
        self,
        max_span: float = MAX_REQUEST_SPAN,
        min_span: float = MIN_REQUEST_SPAN,
        sleep_seconds: float = DEFAULT_SLEEP,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = 4,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ):
        self.max_span = float(max_span)
        self.min_span = float(min_span)
        self.sleep_seconds = float(sleep_seconds)
        self.timeout = int(timeout)
        self.retries = int(retries)
        self.min_request_interval = float(min_request_interval)
        self.session = requests.Session()
        self._token: str | None = None
        self._session_expires_at = 0.0
        self._session_request_budget = SESSION_REQUEST_BUDGET
        self._session_requests_used = 0
        self.request_count = 0
        self.split_count = 0

    def _headers(self) -> dict:
        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = "https://tubvsig-so2sat-vm1.srv.mwn.de/"
        headers["Origin"] = "https://tubvsig-so2sat-vm1.srv.mwn.de"
        return headers

    def _refresh_session(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(
                    VIEWER_SESSION_URL,
                    timeout=60,
                    proxies={},
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                token = data.get("viewerSession")
                if not token:
                    raise WfsError(f"viewer-session 未返回令牌: {data}")
                self._token = token
                expires = float(data.get("expiresInSeconds") or 900)
                self._session_expires_at = time.time() + max(expires - SESSION_TOKEN_MARGIN, 60)
                budget = int(data.get("maxWfsRequests") or 200)
                self._session_request_budget = max(min(budget - 20, SESSION_REQUEST_BUDGET), 10)
                self._session_requests_used = 0
                return
            except (requests.RequestException, ValueError, WfsError) as exc:
                last_error = exc
                time.sleep(min(attempt * 2, 10))
        raise WfsError(f"获取 viewer-session 失败: {last_error}")

    def _ensure_session(self) -> None:
        expired = time.time() >= self._session_expires_at
        exhausted = self._session_requests_used >= self._session_request_budget
        if self._token is None or expired or exhausted:
            self._refresh_session()

    def _request_features(self, minx: float, miny: float, maxx: float, maxy: float) -> dict:
        last_error: Exception | None = None
        attempt = 0
        rate_limit_waits = 0
        while attempt < self.retries:
            attempt += 1
            self._ensure_session()
            respect_global_rate(self.min_request_interval)
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": "global3D:lod1_global",
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:4326",
                "viewerSession": self._token,
            }
            try:
                response = self.session.get(
                    WFS_URL,
                    params=params,
                    timeout=self.timeout,
                    proxies={},
                    headers=self._headers(),
                )
                self._session_requests_used += 1
                self.request_count += 1
                if self.sleep_seconds > 0:
                    time.sleep(self.sleep_seconds)

                if response.status_code == 200:
                    payload = response.json()
                    returned = len(payload.get("features") or [])
                    log_line(
                        f"[WFS] {datetime.now().strftime('%H:%M:%S')} req#{self.request_count} "
                        f"bbox=({minx:.4f},{miny:.4f},{maxx:.4f},{maxy:.4f}) "
                        f"returned={returned} matched={payload.get('numberMatched')} "
                        f"session={self._session_requests_used}/{self._session_request_budget}"
                    )
                    return payload

                text = response.text[:300]
                if response.status_code == 429:
                    rate_limit_waits += 1
                    if rate_limit_waits > RATE_LIMIT_MAX_RETRIES:
                        raise WfsRateLimitError(f"持续限流（429）: {text}")
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait_seconds = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        wait_seconds = 0.0
                    wait_seconds = max(wait_seconds, min(5.0 * rate_limit_waits, 30.0))
                    log_line(
                        f"[WFS-RATE] {datetime.now().strftime('%H:%M:%S')} 429 限流，"
                        f"等待 {wait_seconds:.0f} 秒后重试同一请求（第 {rate_limit_waits} 次）"
                    )
                    time.sleep(wait_seconds)
                    attempt -= 1
                    continue
                if response.status_code == 400 and "BBOX_TOO_LARGE" in text:
                    raise WfsBboxTooLargeError(f"bbox 超限: {text}")
                if response.status_code in (401, 403):
                    if any(token in text for token in ("MISSING_VIEWER_SESSION", "SESSION", "viewer session")):
                        self._token = None
                        last_error = WfsError(f"会话失效: {text}")
                        time.sleep(min(attempt * 2, 10))
                        continue
                    raise WfsError(f"WFS 拒绝请求: {response.status_code} {text}")
                last_error = WfsError(f"WFS 返回 {response.status_code}: {text}")
                time.sleep(min(attempt * 3, 15))
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(attempt * 3, 15))
        raise WfsError(f"WFS 请求失败: bbox=({minx},{miny},{maxx},{maxy}) {last_error}")

    def _split_bounds(self, minx: float, miny: float, maxx: float, maxy: float) -> list[tuple[float, float, float, float]]:
        midx = (minx + maxx) / 2.0
        midy = (miny + maxy) / 2.0
        return [
            (minx, miny, midx, midy),
            (midx, miny, maxx, midy),
            (minx, midy, midx, maxy),
            (midx, midy, maxx, maxy),
        ]

    def fetch_bbox_features(
        self,
        minx: float,
        miny: float,
        maxx: float,
        maxy: float,
        max_requests: int = 0,
        on_request=None,
    ) -> tuple[list[dict], FetchStats]:
        features: list[dict] = []
        stats = {"requests": 0, "splits": 0}
        stack: list[tuple[float, float, float, float]] = [(minx, miny, maxx, maxy)]

        while stack:
            if max_requests > 0 and stats["requests"] >= max_requests:
                break
            bounds = stack.pop()
            span_x = bounds[2] - bounds[0]
            span_y = bounds[3] - bounds[1]
            if max(span_x, span_y) > self.max_span:
                if max(span_x, span_y) / 2.0 < self.min_span * 0.5:
                    raise WfsError(f"bbox 无法继续切分: {bounds}")
                stats["splits"] += 1
                stack.extend(self._split_bounds(*bounds))
                continue

            try:
                payload = self._request_features(*bounds)
                stats["requests"] += 1
            except WfsBboxTooLargeError:
                if max(span_x, span_y) / 2.0 < self.min_span:
                    raise
                stats["splits"] += 1
                stack.extend(self._split_bounds(*bounds))
                continue
            except WfsRateLimitError:
                raise
            except WfsError as exc:
                if max(span_x, span_y) / 2.0 >= self.min_span:
                    log_line(
                        f"[WFS-SPLIT] {datetime.now().strftime('%H:%M:%S')} 请求失败自动切分 "
                        f"bbox=({bounds[0]:.4f},{bounds[1]:.4f},{bounds[2]:.4f},{bounds[3]:.4f}): {exc}"
                    )
                    stats["splits"] += 1
                    stack.extend(self._split_bounds(*bounds))
                    continue
                raise WfsError(f"最小格网仍然失败: {bounds} {exc}") from exc

            returned = payload.get("features") or []
            matched = payload.get("numberMatched")
            try:
                matched_count = int(matched)
            except (TypeError, ValueError):
                matched_count = len(returned)

            if matched_count > len(returned):
                if max(span_x, span_y) > self.min_span:
                    log_line(
                        f"[WFS-SPLIT] {datetime.now().strftime('%H:%M:%S')} 返回被截断自动切分 "
                        f"bbox=({bounds[0]:.4f},{bounds[1]:.4f},{bounds[2]:.4f},{bounds[3]:.4f}) "
                        f"returned={len(returned)} matched={matched_count}"
                    )
                    stats["splits"] += 1
                    stack.extend(self._split_bounds(*bounds))
                    continue
                raise WfsError(
                    f"最小格网返回被截断: bbox={bounds} returned={len(returned)} matched={matched_count}"
                )

            if on_request is not None:
                on_request(bounds, len(returned), matched_count)
            features.extend(returned)

        self.split_count += stats["splits"]
        return features, FetchStats(requests=stats["requests"], splits=stats["splits"], max_span=self.max_span)

    def close(self) -> None:
        self.session.close()
