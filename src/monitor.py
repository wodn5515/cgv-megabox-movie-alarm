import time
from datetime import datetime

from src import cgv_client, megabox_client
from src.notifier import notify_console, notify_discord, notify_heartbeat

HEARTBEAT_INTERVAL = 3600  # 1시간


class ScheduleMonitor:
    def __init__(self, config: dict):
        self.targets = config["targets"]
        self.max_rpm = config.get("max_requests_per_minute", 2)
        notif = config.get("notifications", {})
        self.discord_webhook = notif.get("discord_webhook_url", "")
        # 하트비트 전용 웹훅 (없으면 알림 웹훅으로 폴백)
        self.heartbeat_webhook = (
            notif.get("heartbeat_webhook_url") or self.discord_webhook
        )
        self._opened: dict[str, bool] = {}
        # 사이트별 마지막 요청 시간
        self._last_request: dict[str, float] = {}
        self._start_time = time.time()
        self._last_heartbeat = time.time()
        self._last_check: str | None = None

    def _remaining_targets(self) -> list[dict]:
        return [t for t in self.targets if not self._opened.get(t["name"])]

    def _wait_for_rate_limit(self, typ: str):
        """사이트별 RPM 제한을 지키도록 대기합니다."""
        min_interval = 60 / self.max_rpm
        last = self._last_request.get(typ, 0)
        elapsed = time.time() - last
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request[typ] = time.time()

    def run(self):
        n = len(self.targets)
        cgv_count = sum(1 for t in self.targets if t.get("type", "cgv") == "cgv")
        mega_count = sum(1 for t in self.targets if t.get("type") == "megabox")
        print(f"모니터링 시작: {n}개 타겟 "
              f"(CGV {cgv_count}개, 메가박스 {mega_count}개), "
              f"사이트별 분당 최대 {self.max_rpm}회")
        for t in self.targets:
            typ = t.get("type", "cgv").upper()
            print(f"  - [{typ}] {t['name']} | {t['date']} | "
                  f"{t.get('screen_filter', '전체')} | "
                  f"{t.get('movie_filter', '전체')}")
        print()

        # 시작 즉시 한 번 상태 전송 (웹훅 정상 여부 확인용)
        notify_heartbeat(self.heartbeat_webhook, self._build_status())
        self._last_heartbeat = time.time()

        while True:
            for target in self.targets:
                if self._opened.get(target["name"]):
                    continue

                typ = target.get("type", "cgv").lower()
                self._wait_for_rate_limit(typ)
                try:
                    self._poll(target)
                except Exception as e:
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] {target['name']}: 예기치 못한 오류 - {e}")

                if not self._remaining_targets():
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{now}] 모든 타겟 오픈 감지 완료. 모니터링 종료.")
                    notify_discord(
                        self.discord_webhook,
                        "모니터링 종료",
                        {"complete": True},
                    )
                    return

            # 1시간마다 살아있음 알림
            if time.time() - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                notify_heartbeat(self.heartbeat_webhook, self._build_status())
                self._last_heartbeat = time.time()

    def _build_status(self) -> dict:
        return {
            "targets": [
                {
                    "name": t["name"],
                    "type": t.get("type", "cgv"),
                    "date": t["date"],
                    "screen": t.get("screen_filter", ""),
                    "movie": t.get("movie_filter", ""),
                    "opened": bool(self._opened.get(t["name"])),
                }
                for t in self.targets
            ],
            "last_check": self._last_check,
            "uptime_sec": time.time() - self._start_time,
        }

    def _poll(self, target: dict):
        name = target["name"]
        date = target["date"]
        screen_filter = target.get("screen_filter", "")
        movie_filter = target.get("movie_filter", "")
        typ = target.get("type", "cgv").lower()
        now = datetime.now().strftime("%H:%M:%S")
        self._last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                    if isinstance(s, dict)
                    and keyword in (s.get("movieNm", "") or "").upper()
                ]
            else:
                schedules = [
                    s for s in schedules
                    if isinstance(s, dict)
                    and keyword in (s.get("movNm", "") or "").upper()
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
