import asyncio
import time
from datetime import datetime

from src import cgv_client, megabox_client
from src.booker import book_cgv, book_megabox
from src.notifier import notify_console, notify_discord


def _parse_time_to_minutes(time_str: str) -> int:
    """시간 문자열을 분으로 변환. "1840"→1120, "18:40"→1120"""
    t = time_str.replace(":", "")
    return int(t[:2]) * 60 + int(t[2:4])


def _in_time_range(minutes: int, time_range: str) -> bool:
    """시간(분)이 범위 안에 있는지 확인. "18:00-22:00" """
    start, end = time_range.split("-")
    start_m = _parse_time_to_minutes(start.strip())
    end_m = _parse_time_to_minutes(end.strip())
    return start_m <= minutes <= end_m


def _pick_schedule(schedules: list[dict], booking: dict, typ: str) -> dict:
    """preferred_times 우선순위에 따라 최적의 스케줄을 선택합니다."""
    preferred_times = booking.get("preferred_times", [])
    if not preferred_times:
        return schedules[0]

    time_key = "playStartTime" if typ == "megabox" else "scnsrtTm"

    for time_range in preferred_times:
        for s in schedules:
            try:
                m = _parse_time_to_minutes(s.get(time_key, ""))
                if _in_time_range(m, time_range):
                    return s
            except (ValueError, IndexError):
                continue

    # 매칭되는 시간대가 없으면 첫 번째
    return schedules[0]


class ScheduleMonitor:
    def __init__(self, config: dict):
        self.targets = config["targets"]
        self.max_rpm = config.get("max_requests_per_minute", 2)
        self.discord_webhook = (
            config.get("notifications", {}).get("discord_webhook_url", "")
        )
        self.auto_book = config.get("auto_book", False)
        self.booking = config.get("booking", {})
        self._opened: dict[str, bool] = {}

    def _remaining_targets(self) -> list[dict]:
        return [t for t in self.targets if not self._opened.get(t["name"])]

    def _sleep_interval(self) -> float:
        if not self._remaining_targets():
            return 0
        return 60 / self.max_rpm

    def run(self):
        n = len(self.targets)
        interval = max(30, 60 * n / self.max_rpm)
        print(f"모니터링 시작: {n}개 타겟, 요청 간격 {interval:.0f}초 "
              f"(분당 최대 {self.max_rpm}회)")
        for t in self.targets:
            typ = t.get("type", "cgv").upper()
            print(f"  - [{typ}] {t['name']} | {t['date']} | "
                  f"{t.get('screen_filter', '전체')} | "
                  f"{t.get('movie_filter', '전체')}")
        print()

        while True:
            for target in self.targets:
                if self._opened.get(target["name"]):
                    continue

                self._poll(target)

                if not self._remaining_targets():
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{now}] 모든 타겟 오픈 감지 완료. 모니터링 종료.")
                    notify_discord(
                        self.discord_webhook,
                        "모니터링 종료",
                        {"complete": True},
                    )
                    return

                time.sleep(self._sleep_interval())

    def _poll(self, target: dict):
        name = target["name"]
        date = target["date"]
        screen_filter = target.get("screen_filter", "")
        movie_filter = target.get("movie_filter", "")
        typ = target.get("type", "cgv").lower()
        now = datetime.now().strftime("%H:%M:%S")

        try:
            if typ == "megabox":
                schedules = megabox_client.fetch_schedule(
                    target["branch_no"], date
                )
            else:
                schedules = cgv_client.fetch_schedule(
                    target["site_no"], date
                )
        except Exception as e:
            print(f"[{now}] {name}: 요청 실패 - {e}")
            return

        # 상영관 필터링
        if screen_filter:
            if typ == "megabox":
                schedules = megabox_client.filter_screen(
                    schedules, screen_filter
                )
            else:
                schedules = cgv_client.filter_screen(
                    schedules, screen_filter
                )

        # 영화 필터링
        if movie_filter:
            keyword = movie_filter.upper()
            if typ == "megabox":
                schedules = [
                    s for s in schedules
                    if keyword in (s.get("movieNm", "") or "").upper()
                ]
            else:
                schedules = [
                    s for s in schedules
                    if keyword in (s.get("movNm", "") or "").upper()
                ]

        if schedules:
            self._opened[name] = True

            if typ == "megabox":
                times = [s.get("playStartTime", "") for s in schedules]
                movie_names = {s.get("movieNm", "") for s in schedules}
                screen_names = {s.get("theabExpoNm", "") for s in schedules}
            else:
                times = [s.get("scnsrtTm", "") for s in schedules]
                movie_names = {s.get("movNm", "") for s in schedules}
                screen_names = {s.get("scnsNm", "") for s in schedules}

            changes = {
                "date": date,
                "movies": sorted(movie_names),
                "screens": sorted(screen_names),
                "times": times,
            }
            notify_console(name, changes)
            notify_discord(self.discord_webhook, name, changes)

            # 자동 예매 (브라우저 열기)
            if self.auto_book:
                best = _pick_schedule(schedules, self.booking, typ)
                if typ == "megabox":
                    asyncio.run(book_megabox(target, best, self.booking))
                else:
                    asyncio.run(book_cgv(target, best, self.booking))
        else:
            print(f"[{now}] {name}: 미오픈")
