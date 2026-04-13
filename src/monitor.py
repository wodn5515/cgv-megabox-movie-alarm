import time
from datetime import datetime

from src.cgv_client import fetch_schedule, filter_screen
from src.notifier import notify_console, notify_discord


class ScheduleMonitor:
    def __init__(self, config: dict):
        self.targets = config["targets"]
        self.max_rpm = config.get("max_requests_per_minute", 2)
        self.discord_webhook = (
            config.get("notifications", {}).get("discord_webhook_url", "")
        )
        n = len(self.targets)
        self.interval = max(30, 60 * n / self.max_rpm)
        # 각 타겟의 오픈 여부
        self._opened: dict[str, bool] = {}

    def run(self):
        n = len(self.targets)
        print(f"모니터링 시작: {n}개 타겟, 요청 간격 {self.interval:.0f}초 "
              f"(분당 최대 {self.max_rpm}회)")
        for t in self.targets:
            print(f"  - {t['name']} | {t['date']} | "
                  f"{t.get('screen_filter', '전체')} | "
                  f"{t.get('movie_filter', '전체')}")
        print()

        while True:
            for target in self.targets:
                self._poll(target)

                # 전부 오픈 감지되면 종료
                if all(self._opened.get(t["name"]) for t in self.targets):
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{now}] 모든 타겟 오픈 감지 완료. 모니터링 종료.")
                    notify_discord(
                        self.discord_webhook,
                        "모니터링 종료",
                        {"complete": True},
                    )
                    return

                time.sleep(self.interval / n)

    def _poll(self, target: dict):
        name = target["name"]
        site_no = target["site_no"]
        date = target["date"]
        screen_filter = target.get("screen_filter", "")
        movie_filter = target.get("movie_filter", "")
        now = datetime.now().strftime("%H:%M:%S")

        # 이미 오픈 감지된 타겟은 스킵
        if self._opened.get(name):
            return

        try:
            schedules = fetch_schedule(site_no, date)
        except Exception as e:
            print(f"[{now}] {name}: 요청 실패 - {e}")
            return

        # 필터링
        if screen_filter:
            schedules = filter_screen(schedules, screen_filter)
        if movie_filter:
            keyword = movie_filter.upper()
            schedules = [
                s for s in schedules
                if keyword in (s.get("movNm", "") or "").upper()
            ]

        if schedules:
            self._opened[name] = True
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
