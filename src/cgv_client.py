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
    schedules = data.get("data")
    # API가 간헐적으로 data를 리스트가 아닌 형태(문자열/None 등)로 반환할 수 있음
    if not isinstance(schedules, list):
        return []
    return schedules


def fetch_screening_dates(site_no: str) -> list[dict]:
    """예매 가능한 상영일 목록을 반환합니다.

    [{"scnYmd": "20260805", "hldyYn": "N"}, ...] 형태이며
    hldyYn이 "Y"면 공휴일입니다.
    """
    data = _get(
        "/cnm/atkt/searchSiteScnscYmdListBySite",
        {"coCd": "A420", "siteNo": site_no},
    ).get("data")
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("scnYmd")]


def fetch_seats(schedule: dict) -> list[dict]:
    """특정 회차의 좌석 배치도를 평탄화하여 반환합니다.

    schedule은 fetch_schedule이 돌려준 회차 dict를 그대로 넘깁니다.
    """
    data = _get(
        "/cnm/atkt/searchIfSeatData",
        {
            "coCd": schedule.get("coCd", "A420"),
            "siteNo": schedule.get("siteNo", ""),
            "scnYmd": schedule.get("scnYmd", ""),
            "scnsNo": schedule.get("scnsNo", ""),
            "scnSseq": schedule.get("scnSseq", ""),
            "movNo": schedule.get("movNo", ""),
            "prodNo": schedule.get("prodNo", ""),
        },
    ).get("data")
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []

    seats: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        block = item.get("seats")
        if isinstance(block, list):
            seats.extend(s for s in block if isinstance(s, dict))
    return seats


def available_seats(seats: list[dict]) -> list[dict]:
    """판매 가능한(아직 아무도 안 잡은) 좌석만 남깁니다.

    seatStusCd: 00=미정(빈자리), 01=판매됨
    """
    return [
        s for s in seats
        if s.get("seatStusCd") == "00" and s.get("seatSaleYn") == "Y"
    ]


def filter_screen(schedules: list[dict], screen_filter: str) -> list[dict]:
    """특정 상영관 타입으로 필터링합니다."""
    if not screen_filter:
        return schedules
    keyword = screen_filter.upper()
    return [
        s for s in schedules
        if isinstance(s, dict)
        and (
            keyword in (s.get("scnsNm", "") or "").upper()
            or keyword in (s.get("expoScnsNm", "") or "").upper()
        )
    ]
