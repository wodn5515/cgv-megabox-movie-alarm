import hmac
import hashlib
import base64
import time

import requests

HMAC_KEY = "ydqXY0ocnFLmJGHr_zNzFcpjwAsXq_8JcBNURAkRscg"
API_BASE = "https://api.cgv.co.kr"

HEADERS_BASE = {
    "Accept": "application/json",
    "Accept-Language": "ko-KR",
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Origin": "https://cgv.co.kr",
    "Referer": "https://cgv.co.kr/",
}


def _sign(pathname: str, body: str = "") -> tuple[str, str]:
    ts = str(int(time.time()))
    message = f"{ts}|{pathname}|{body}"
    sig = base64.b64encode(
        hmac.new(HMAC_KEY.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return ts, sig


def _get(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}"
    ts, sig = _sign(path)
    headers = {**HEADERS_BASE, "X-TIMESTAMP": ts, "X-SIGNATURE": sig}
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_schedule(site_no: str, date: str) -> list[dict]:
    """특정 영화관/날짜의 전체 스케줄을 반환합니다."""
    data = _get(
        "/cnm/atkt/searchMovScnInfo",
        {"coCd": "A420", "siteNo": site_no, "scnYmd": date, "rtctlScopCd": "08"},
    )
    return data.get("data", [])


def filter_screen(schedules: list[dict], screen_filter: str) -> list[dict]:
    """특정 상영관 타입으로 필터링합니다."""
    if not screen_filter:
        return schedules
    keyword = screen_filter.upper()
    return [
        s for s in schedules
        if keyword in (s.get("scnsNm", "") or "").upper()
        or keyword in (s.get("expoScnsNm", "") or "").upper()
    ]
