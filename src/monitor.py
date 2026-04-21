import time
from datetime import datetime

from src import cgv_client, megabox_client
from src.notifier import notify_console, notify_discord


class ScheduleMonitor:
    def __init__(self, config: dict):
        self.targets = config["targets"]
        self.max_rpm = config.get("max_requests_per_minute", 2)
        self.discord_webhook = (
            config.get("notifications", {}).get("discord_webhook_url", "")
        )
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
        else:
            print(f"[{now}] {name}: 미오픈")
