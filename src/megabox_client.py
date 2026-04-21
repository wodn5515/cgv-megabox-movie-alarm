import requests

API_URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.megabox.co.kr",
    "Referer": "https://www.megabox.co.kr/booking",
}

# 지점별 지역 코드 매핑
BRANCH_AREA = {
    # DOLBY CINEMA
    "0019": "30",  # 남양주현대아울렛스페이스원 (경기)
    "7011": "55",  # 대구신세계(동대구)
    "0028": "45",  # 대전신세계아트앤사이언스
    "4062": "35",  # 송도(트리플스트리트) (인천)
    "0052": "30",  # 수원AK플라자(수원역) (경기)
    "0020": "30",  # 안성스타필드 (경기)
    "1351": "10",  # 코엑스 (서울)
    "4651": "30",  # 하남스타필드 (경기)
    # 일반
    "1372": "10",  # 강남 (서울)
}


def fetch_schedule(branch_no: str, date: str) -> list[dict]:
    """특정 지점/날짜의 전체 스케줄을 반환합니다."""
    area_cd = BRANCH_AREA.get(branch_no, "10")

    resp = requests.post(
        API_URL,
        headers=HEADERS,
        json={
            "arrMovieNo": "",
            "playDe": date,
            "brchNoListCnt": 1,
            "brchNo1": branch_no,
            "brchNo2": "", "brchNo3": "", "brchNo4": "", "brchNo5": "",
            "areaCd1": area_cd,
            "areaCd2": "", "areaCd3": "", "areaCd4": "", "areaCd5": "",
            "spclbYn1": "N",
            "spclbYn2": "", "spclbYn3": "", "spclbYn4": "", "spclbYn5": "",
            "theabKindCd1": area_cd,
            "theabKindCd2": "", "theabKindCd3": "", "theabKindCd4": "", "theabKindCd5": "",
            "brchAll": area_cd,
            "brchSpcl": "",
            "movieNo1": "", "movieNo2": "", "movieNo3": "",
            "sellChnlCd": "",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("movieFormList") or []


def filter_screen(schedules: list[dict], screen_filter: str) -> list[dict]:
    """특정 상영관 타입으로 필터링합니다."""
    if not screen_filter:
        return schedules
    keyword = screen_filter.upper()
    return [
        s for s in schedules
        if keyword in (s.get("theabExpoNm", "") or "").upper()
    ]
