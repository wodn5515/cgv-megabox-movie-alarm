import threading
import time
from datetime import datetime

from src import cgv_client, megabox_client, seat_filter
from src.notifier import (
    notify_cancel,
    notify_console,
    notify_heartbeat,
    notify_open,
)

HEARTBEAT_INTERVAL = 3600  # 1시간
# 상영일 목록 재조회 주기. 새 날짜가 예매 오픈되면 좌석이 크게 열리므로
# 짧게 잡아 빨리 잡습니다 (요청은 바퀴당 최대 1회).
DATE_CACHE_TTL = 600
# 조건에 맞는 회차가 없던 날짜를 건너뛰는 기간.
# 아직 회차가 안 붙은 날짜에 회차가 생기는 순간이 좌석을 잡을 최적기라 짧게 잡습니다.
EMPTY_DATE_TTL = 900
# 같은 회차에 대해 자동 예매를 다시 시도하기까지의 최소 간격.
# 반복 알림이 돌 때마다 브라우저를 다시 몰면 안 되므로 넉넉히 둡니다.
BOOK_COOLDOWN = 600
# 잔여석 수가 그대로여도 이 주기마다는 좌석 배치도를 강제로 다시 받습니다.
# 취소와 매수가 같은 바퀴에 상쇄되면 수가 안 변해 좌석 변화를 놓치는데,
# 이 갱신이 최악의 누락 시간을 이 값으로 묶어줍니다.
SEAT_REFRESH_TTL = 300


def _start_time(schedule: dict, typ: str) -> str:
    """회차 시작시간을 HH:MM으로 통일합니다.

    CGV는 심야 상영을 24:00, 25:00처럼 표기합니다 (25:00 = 다음날 새벽 1시).
    """
    if typ == "megabox":
        return schedule.get("playStartTime", "") or ""
    raw = schedule.get("scnsrtTm", "") or ""
    return f"{raw[:2]}:{raw[2:]}" if len(raw) == 4 else raw


def _is_weekend(ymd: str, holiday: bool) -> bool:
    """토/일 또는 공휴일이면 주말로 봅니다."""
    if holiday:
        return True
    try:
        return datetime.strptime(ymd, "%Y%m%d").weekday() >= 5
    except ValueError:
        return False


def _pretty_date(ymd: str) -> str:
    try:
        dt = datetime.strptime(ymd, "%Y%m%d")
    except ValueError:
        return ymd
    return f"{dt.month:02d}/{dt.day:02d}({'월화수목금토일'[dt.weekday()]})"


class ScheduleMonitor:
    def __init__(self, config: dict):
        self.targets = config["targets"]
        self.max_rpm = config.get("max_requests_per_minute", 2)
        # Discord / 텔레그램 설정 (notifier가 채널별로 알아서 발송)
        self.notif = config.get("notifications", {}) or {}
        # 카톡결제 요청에 쓸 휴대폰번호·생년월일 (개인정보, config.yaml은 gitignore)
        self.kakaopay = config.get("kakaopay") or {}
        self._opened: dict[str, bool] = {}
        # 취소표 모드에서 회차별로 마지막에 본 빈자리 (좌석 라벨 집합 / 잔여 수)
        self._free_seats: dict[str, set[str]] = {}
        self._free_count: dict[str, int] = {}
        # 회차별 마지막 알림 시각 (repeat_alert_sec 주기 계산용)
        self._last_alert: dict[str, float] = {}
        # 회차별 좌석 묶음 표기 ("K17~K18"). 반복 알림에서 재사용합니다.
        self._free_desc: dict[str, list[str]] = {}
        # 회차별 마지막 좌석 배치도 조회 시각 (강제 갱신 주기 계산용)
        self._last_seat_fetch: dict[str, float] = {}
        # 회차별 마지막 자동 예매 시도 시각
        self._last_book: dict[str, float] = {}
        # 사이트별 마지막 요청 시간
        self._last_request: dict[str, float] = {}
        # 영화관별 상영일 목록 캐시 (조회시각, 목록)
        self._date_cache: dict[str, tuple[float, list[dict]]] = {}
        # 조건에 맞는 회차가 없던 (타겟, 날짜) → 확인 시각.
        # 상영을 안 하거나 그 시간대에 회차가 없는 날짜에 매 바퀴 요청하지 않기 위함.
        self._empty_dates: dict[tuple[str, str], float] = {}
        self._start_time = time.time()
        self._last_heartbeat = time.time()
        self._last_check: str | None = None
        self._hits = 0

    def _remaining_targets(self) -> list[dict]:
        """아직 감지되지 않은 오픈 모드 타겟. 취소표 모드는 계속 감시하므로 제외."""
        return [
            t for t in self.targets
            if t.get("mode", "open") == "open" and not self._opened.get(t["name"])
        ]

    def _has_cancel_target(self) -> bool:
        return any(t.get("mode", "open") == "cancel" for t in self.targets)

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
            mode = "취소표" if t.get("mode", "open") == "cancel" else "예매오픈"
            desc = (f"  - [{typ}/{mode}] {t['name']} | {self._date_desc(t)} | "
                    f"{t.get('screen_filter', '전체')} | "
                    f"{t.get('movie_filter', '전체')}")
            if t.get("time_rules"):
                rules = t["time_rules"]
                bits = [
                    f"{ko}={r[0]}~{r[1]}"
                    for key, ko in (("weekday", "평일"), ("weekend", "주말"))
                    if (r := rules.get(key))
                ]
                desc += " | " + " ".join(bits)
            elif t.get("time_range"):
                desc += f" | {t['time_range'][0]}~{t['time_range'][1]}"
            if t.get("seats"):
                desc += f" | 좌석 {self._seat_desc(t['seats'])}"
            print(desc)
        print()

        # 시작 즉시 한 번 상태 전송 (웹훅 정상 여부 확인용)
        notify_heartbeat(self.notif, self._build_status())
        self._last_heartbeat = time.time()

        while True:
            for target in self.targets:
                if self._opened.get(target["name"]):
                    continue

                try:
                    self._poll(target)
                except Exception as e:
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] {target['name']}: 예기치 못한 오류 - {e}")

                if not self._remaining_targets() and not self._has_cancel_target():
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"\n[{now}] 모든 타겟 오픈 감지 완료. 모니터링 종료.")
                    notify_open(
                        self.notif, "모니터링 종료", {"complete": True}
                    )
                    return

            # 1시간마다 살아있음 알림
            if time.time() - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                notify_heartbeat(self.notif, self._build_status())
                self._last_heartbeat = time.time()

    @staticmethod
    def _date_desc(target: dict) -> str:
        spec = target.get("dates", target.get("date"))
        if spec == "all" or spec is None:
            return "상영일 전체"
        if isinstance(spec, dict):
            return f"{spec.get('from') or '처음'}~{spec.get('to') or '끝'}"
        if isinstance(spec, str):
            return spec
        return f"{len(spec)}개 날짜"

    @staticmethod
    def _seat_desc(seats: dict) -> str:
        parts = []
        if seats.get("rows"):
            parts.append(f"{'/'.join(str(r) for r in seats['rows'])}열")
        if seats.get("seat_no"):
            parts.append(f"{seats['seat_no'][0]}~{seats['seat_no'][1]}번")
        if (seats.get("min_consecutive") or 1) > 1:
            parts.append(f"{seats['min_consecutive']}연석")
        return " ".join(parts) or "전체"

    def _build_status(self) -> dict:
        return {
            "targets": [
                {
                    "name": t["name"],
                    "type": t.get("type", "cgv"),
                    "mode": t.get("mode", "open"),
                    "date": self._date_desc(t),
                    "screen": t.get("screen_filter", ""),
                    "movie": t.get("movie_filter", ""),
                    "opened": bool(self._opened.get(t["name"])),
                }
                for t in self.targets
            ],
            "last_check": self._last_check,
            "uptime_sec": time.time() - self._start_time,
            "hits": self._hits,
        }

    def _screening_dates(self, site_no: str) -> list[dict]:
        """상영일 목록을 캐시와 함께 가져옵니다 (1시간마다 갱신)."""
        cached = self._date_cache.get(site_no)
        if cached and time.time() - cached[0] < DATE_CACHE_TTL:
            return cached[1]

        self._wait_for_rate_limit("cgv")
        try:
            days = cgv_client.fetch_screening_dates(site_no)
        except Exception as e:
            print(f"상영일 목록 조회 실패 ({site_no}) - {e}")
            return cached[1] if cached else []

        if days:
            self._date_cache[site_no] = (time.time(), days)
        return days

    def _target_dates(self, target: dict, typ: str) -> list[tuple[str, bool]]:
        """감시할 (날짜, 공휴일여부) 목록을 만듭니다.

        dates: all              → 예매 가능한 상영일 전체 (CGV만 지원)
        dates: {from:, to:}     → 기간으로 지정. 한쪽만 적으면 그쪽만 제한 (CGV만 지원)
        dates: [...]            → 지정한 날짜들
        date: "..."             → 단일 날짜
        """
        spec = target.get("dates", target.get("date"))

        if typ == "megabox":
            # 메가박스는 상영일 목록 API를 쓰지 않으므로 날짜를 명시해야 합니다
            if spec is None or spec == "all" or isinstance(spec, dict):
                print(f"{target['name']}: 메가박스는 date/dates 목록을 명시해야 합니다")
                return []
            dates = [spec] if isinstance(spec, str) else [str(d) for d in spec]
            return [(d, False) for d in dates]

        days = self._screening_dates(target["site_no"])
        holiday = {d["scnYmd"]: d.get("hldyYn") == "Y" for d in days}

        if spec is None or spec == "all":
            wanted = [d["scnYmd"] for d in days]
        elif isinstance(spec, dict):
            lo = str(spec.get("from") or "")
            hi = str(spec.get("to") or "")
            wanted = [
                d["scnYmd"] for d in days
                if (not lo or d["scnYmd"] >= lo) and (not hi or d["scnYmd"] <= hi)
            ]
        elif isinstance(spec, str):
            wanted = [spec]
        else:
            wanted = [str(d) for d in spec]

        return [(d, holiday.get(d, False)) for d in wanted]

    @staticmethod
    def _time_range_for(target: dict, ymd: str, holiday: bool):
        """그 날짜에 적용할 시간대 조건을 고릅니다.

        time_rules가 있으면 평일/주말로 나눠 적용하고,
        해당 요일 규칙이 없으면 시간 제한 없이 봅니다.
        """
        rules = target.get("time_rules")
        if rules:
            key = "weekend" if _is_weekend(ymd, holiday) else "weekday"
            return rules.get(key)
        return target.get("time_range")

    def _poll(self, target: dict):
        typ = target.get("type", "cgv").lower()
        self._last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cancel_mode = target.get("mode", "open") == "cancel"
        report: list[str] = []
        seen_any = False

        skipped = 0
        for ymd, holiday in self._target_dates(target, typ):
            # 조건에 맞는 회차가 없던 날짜는 한동안 건너뜁니다 (취소표 모드 한정).
            # 오픈 감시는 "없다가 생기는 것"을 잡아야 하므로 매번 확인합니다.
            if cancel_mode and self._is_empty_date(target["name"], ymd):
                skipped += 1
                continue

            time_range = self._time_range_for(target, ymd, holiday)

            self._wait_for_rate_limit(typ)
            schedules = self._fetch_filtered(target, typ, ymd, time_range)
            if schedules is None:
                continue
            seen_any = True

            if cancel_mode:
                self._mark_empty_date(target["name"], ymd, not schedules)

            if cancel_mode:
                report.extend(
                    self._poll_cancel(target, schedules, typ, ymd)
                )
            elif schedules:
                self._poll_open(target, schedules, typ, ymd)
                return  # 오픈 감지되면 나머지 날짜는 볼 필요 없음

        now = datetime.now().strftime("%H:%M:%S")
        tail = f" (미상영 {skipped}일 건너뜀)" if skipped else ""
        if cancel_mode and (seen_any or skipped):
            if report:
                head = " | ".join(report[:6])
                more = f" (+{len(report) - 6}개 회차)" if len(report) > 6 else ""
                print(f"[{now}] {target['name']}: 빈자리 {head}{more}{tail}")
            else:
                print(f"[{now}] {target['name']}: 조건에 맞는 빈자리 없음{tail}")
        elif not cancel_mode and seen_any:
            print(f"[{now}] {target['name']}: 미오픈")

    def _is_empty_date(self, name: str, ymd: str) -> bool:
        seen = self._empty_dates.get((name, ymd))
        return seen is not None and time.time() - seen < EMPTY_DATE_TTL

    def _mark_empty_date(self, name: str, ymd: str, empty: bool):
        key = (name, ymd)
        if empty:
            self._empty_dates[key] = time.time()
        else:
            self._empty_dates.pop(key, None)

    def _fetch_filtered(
        self, target: dict, typ: str, date: str, time_range
    ) -> list[dict] | None:
        """타겟 조건(상영관/영화/시간대)에 맞는 회차만 추려서 반환합니다.

        요청 실패 시 None을 돌려줍니다.
        """
        screen_filter = target.get("screen_filter", "")
        movie_filter = target.get("movie_filter", "")
        now = datetime.now().strftime("%H:%M:%S")

        try:
            if typ == "megabox":
                schedules = megabox_client.fetch_schedule(
                    target["branch_no"], date
                )
            else:
                schedules = cgv_client.fetch_schedule(target["site_no"], date)
        except Exception as e:
            print(f"[{now}] {target['name']} {date}: 요청 실패 - {e}")
            return None

        # 상영관 필터링
        if screen_filter:
            if typ == "megabox":
                schedules = megabox_client.filter_screen(
                    schedules, screen_filter
                )
            else:
                schedules = cgv_client.filter_screen(schedules, screen_filter)

        # 영화 필터링
        if movie_filter:
            keyword = movie_filter.upper()
            name_key = "movieNm" if typ == "megabox" else "movNm"
            schedules = [
                s for s in schedules
                if isinstance(s, dict)
                and keyword in (s.get(name_key, "") or "").upper()
            ]

        # 시간대 필터링
        if time_range:
            lo, hi = str(time_range[0]), str(time_range[1])
            schedules = [
                s for s in schedules if lo <= _start_time(s, typ) <= hi
            ]

        return schedules

    def _poll_open(
        self, target: dict, schedules: list[dict], typ: str, ymd: str
    ):
        name = target["name"]
        self._opened[name] = True
        self._hits += 1

        if typ == "megabox":
            movie_names = {s.get("movieNm", "") for s in schedules}
            screen_names = {s.get("theabExpoNm", "") for s in schedules}
        else:
            movie_names = {s.get("movNm", "") for s in schedules}
            screen_names = {s.get("scnsNm", "") for s in schedules}

        changes = {
            "date": ymd,
            "movies": sorted(movie_names),
            "screens": sorted(screen_names),
            "times": [_start_time(s, typ) for s in schedules],
        }
        notify_console(name, changes)
        notify_open(self.notif, name, changes)

    def _poll_cancel(
        self, target: dict, schedules: list[dict], typ: str, ymd: str
    ) -> list[str]:
        """매진된 회차를 지켜보다가 빈자리가 새로 생기면 알립니다.

        콘솔 요약에 쓸 문자열 목록을 돌려줍니다.
        """
        report = []
        for sch in schedules:
            key = self._schedule_key(target["name"], sch, typ, ymd)
            start = _start_time(sch, typ)

            if typ == "megabox":
                free = _int(sch.get("restSeatCnt"))
                total = _int(sch.get("totSeatCnt"))
            else:
                free = _int(sch.get("frSeatCnt"))
                total = _int(sch.get("stcnt"))

            # 좌석 단위 감시는 CGV만 지원 (메가박스는 잔여 수량으로 감시)
            seat_cfg = target.get("seats") if typ != "megabox" else None

            if free <= 0:
                self._free_seats[key] = set()
                self._free_desc.pop(key, None)
                self._free_count[key] = 0
                self._last_alert.pop(key, None)
                self._last_seat_fetch.pop(key, None)
                continue

            report.append(f"{_pretty_date(ymd)} {start} {free}/{total}")
            if seat_cfg:
                # 잔여석 수가 지난 바퀴와 같으면 좌석 구성도 그대로이므로
                # 배치도를 다시 받지 않습니다. 수가 움직일 때만 조회합니다.
                prev_free = self._free_count.get(key)
                self._free_count[key] = free
                if prev_free is None or prev_free != free or self._refresh_due(key):
                    self._check_seats(
                        target, sch, key, start, seat_cfg, typ, ymd
                    )
                else:
                    # 좌석 구성이 그대로라도, 조건에 맞는 자리가 아직 열려
                    # 있으면 캐시된 좌석 목록으로 반복 알림을 보냅니다.
                    self._repeat_alert(target, sch, key, start, typ, ymd)
            else:
                self._check_count(
                    target, sch, key, start, free, total, typ, ymd
                )
        return report

    def _refresh_due(self, key: str) -> bool:
        """잔여석 수가 그대로여도 좌석 배치도를 다시 받을 때가 됐는지 확인합니다.

        취소와 매수가 상쇄되어 수가 그대로인 경우를 잡기 위한 안전장치입니다.
        """
        last = self._last_seat_fetch.get(key)
        return last is None or time.time() - last >= SEAT_REFRESH_TTL

    def _repeat_due(self, target: dict, key: str) -> bool:
        """반복 알림을 보낼 때가 됐는지 확인합니다.

        repeat_alert_sec이 없거나 0이면 반복하지 않습니다.
        """
        try:
            interval = int(target.get("repeat_alert_sec") or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            return False
        last = self._last_alert.get(key)
        return last is None or time.time() - last >= interval

    def _repeat_alert(self, target, sch, key, start, typ, ymd):
        """이미 알린 자리가 아직 열려 있을 때 다시 알립니다."""
        labels = self._free_seats.get(key)
        if not labels or not self._repeat_due(target, key):
            return

        self._last_alert[key] = time.time()
        self._hits += 1
        notify_cancel(self.notif, target["name"], {
            "date": _pretty_date(ymd),
            "time": start,
            "movie": sch.get("movNm", ""),
            "screen": sch.get("scnsNm", ""),
            "free": _int(sch.get("frSeatCnt")),
            "total": _int(sch.get("stcnt")),
            "gained": len(labels),
            "seats": self._free_desc.get(key) or sorted(labels),
            "url": _booking_url(sch, typ),
            "repeat": True,
        })

    def _check_count(
        self, target, sch, key, start, free, total, typ, ymd
    ):
        """빈자리 수가 늘어났을 때 알립니다 (좌석 위치 조건이 없는 경우).

        빈자리가 남아 있는 동안에는 repeat_alert_sec 주기로 다시 알립니다.
        """
        prev = self._free_count.get(key)
        self._free_count[key] = free
        increased = prev is None or free > prev
        repeat = not increased and self._repeat_due(target, key)
        if not increased and not repeat:
            return

        gained = free if prev is None else max(free - prev, 0)
        self._last_alert[key] = time.time()
        self._hits += 1
        notify_cancel(self.notif, target["name"], {
            "date": _pretty_date(ymd),
            "time": start,
            "movie": sch.get("movieNm" if typ == "megabox" else "movNm", ""),
            "screen": sch.get(
                "theabExpoNm" if typ == "megabox" else "scnsNm", ""
            ),
            "free": free,
            "total": total,
            "gained": gained,
            "seats": [],
            "url": _booking_url(sch, typ),
            "repeat": repeat,
        })

    def _check_seats(self, target, sch, key, start, seat_cfg, typ, ymd):
        """조건에 맞는 좌석이 새로 풀렸을 때만 알립니다 (CGV 전용)."""
        self._wait_for_rate_limit(typ)
        try:
            seats = cgv_client.available_seats(cgv_client.fetch_seats(sch))
        except Exception as e:
            print(f"  좌석 조회 실패 ({ymd} {start}) - {e}")
            return
        self._last_seat_fetch[key] = time.time()

        groups = seat_filter.match(seats, seat_cfg)
        labels = {seat_filter.label(s) for g in groups for s in g}
        prev = self._free_seats.get(key, set())
        self._free_seats[key] = labels
        self._free_desc[key] = seat_filter.describe(groups)

        new = labels - prev
        if not new:
            # 새로 풀린 자리는 없지만, 이미 알린 자리가 아직 열려 있으면
            # repeat_alert_sec 주기로 다시 알립니다.
            self._repeat_alert(target, sch, key, start, typ, ymd)
            return

        # 새로 풀린 좌석이 포함된 묶음만 알립니다
        fresh = [g for g in groups if any(seat_filter.label(s) in new for s in g)]
        self._last_alert[key] = time.time()
        self._hits += 1
        # 자동 예매는 '새로 풀린 자리'에만 걸고, 반복 알림에는 걸지 않습니다.
        if target.get("auto_book") and fresh:
            self._launch_booker(target, sch, key, fresh[0])
        notify_cancel(self.notif, target["name"], {
            "date": _pretty_date(ymd),
            "time": start,
            "movie": sch.get("movNm", ""),
            "screen": sch.get("scnsNm", ""),
            "free": _int(sch.get("frSeatCnt")),
            "total": _int(sch.get("stcnt")),
            "gained": len(new),
            "seats": seat_filter.describe(fresh),
            "url": _booking_url(sch, typ),
        })

    def _launch_booker(self, target: dict, sch: dict, key: str,
                       group: list[dict]):
        """크롬에서 좌석 선택까지 진행합니다. 폴링을 막지 않도록 별도 스레드로.

        같은 회차를 반복해서 몰지 않도록 쿨다운을 둡니다.
        """
        last = self._last_book.get(key)
        if last is not None and time.time() - last < BOOK_COOLDOWN:
            return
        self._last_book[key] = time.time()

        # 예매할 인원 수. 명시가 없으면 연석 조건(min_consecutive)을 씁니다.
        # 좌석 묶음이 요청보다 길 수 있으므로(연속 5석 등) 앞에서부터 필요한
        # 만큼만 넘깁니다. group 길이를 그대로 쓰면 5장을 끊게 됩니다.
        seats_cfg = target.get("seats") or {}
        tickets = int(
            target.get("tickets") or seats_cfg.get("min_consecutive") or 1
        )
        seat_locs = [
            s.get("seatLocNo") for s in group[:tickets] if s.get("seatLocNo")
        ]
        if not seat_locs:
            return

        def run():
            try:
                from src import booker
                booker.book(
                    sch, seat_locs,
                    movie_filter=target.get("movie_filter", ""),
                    screen_filter=target.get("screen_filter", ""),
                    count=tickets,
                    pay=bool(target.get("auto_pay")),
                    expect_amount=target.get("expect_amount"),
                    kakao=self.notif.get("kakaopay") or self.kakaopay,
                )
            except Exception as e:
                print(f"  자동 예매 실패 - {e}")

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _schedule_key(name: str, sch: dict, typ: str, ymd: str) -> str:
        if typ == "megabox":
            return f"{name}|{ymd}|{sch.get('playSchdlNo', '')}"
        return (
            f"{name}|{ymd}|{sch.get('scnsNo', '')}|{sch.get('scnSseq', '')}"
        )


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _booking_url(sch: dict, typ: str) -> str:
    if typ == "megabox":
        return "https://www.megabox.co.kr/booking"
    return "https://cgv.co.kr/cnm/movieBook"
