import threading
import time
from datetime import datetime, timedelta

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
# 잔여석 수가 그대로여도 이 주기마다는 좌석 배치도를 강제로 다시 받습니다.
# 취소와 매수가 같은 바퀴에 상쇄되면 수가 안 변해 좌석 변화를 놓치는데,
# 이 갱신이 최악의 누락 시간을 이 값으로 묶어줍니다.
SEAT_REFRESH_TTL = 300
# 오픈 감시가 한 바퀴에 건너뛸 수 있는 '이미 열린 날짜' 수의 상한.
# 지정한 날짜 대부분이 이미 열려 있으면 커서를 미오픈 지점까지 밀어야 하는데,
# 한 바퀴에 하나씩 밀면 수십 바퀴가 걸린다. 그렇다고 무제한으로 밀면 첫 바퀴에
# 요청이 몰린다. 몇 바퀴에 걸쳐 나눠 미는 타협값이다. 지평선에 닿고 나면
# 그 뒤로는 계속 바퀴당 1회다.
OPEN_SKIP_PER_CYCLE = 8
# 감시할 타겟이 하나도 없을 때 한 바퀴 쉬는 시간.
IDLE_SLEEP = 5
# 유휴 상태에서도 이 주기로는 로그를 한 줄 남깁니다.
# 워치독의 STALE_SEC(300초)보다 충분히 짧아야 오탐 경보가 안 뜹니다.
IDLE_LOG_INTERVAL = 60


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


def _calendar_range(lo: str, hi: str) -> list[str]:
    """YYYYMMDD lo~hi 사이의 달력상 날짜를 모두 전개합니다.

    오픈 감시에서 아직 예매가 안 열린(상영일 목록에 없는) 날짜까지 봐야 할 때
    씁니다. 취소표 감시는 예매 가능한 날짜만 보므로 이 함수를 쓰지 않습니다.
    """
    try:
        d0 = datetime.strptime(lo, "%Y%m%d")
        d1 = datetime.strptime(hi, "%Y%m%d")
    except ValueError:
        return []
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y%m%d"))
        d0 += timedelta(days=1)
    return out


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
        # 결제까지 완료된 타겟. 표를 이미 샀으므로 감시와 자동 예매를
        # 모두 멈춥니다. 선점만 하고 결제하지 않은 경우는 포함하지 않습니다.
        self._booked: set[str] = set()
        # 예매는 크롬 탭 하나를 몰기 때문에 동시에 두 건이 돌면 서로를
        # 망칩니다. 날짜가 여러 개면 8/10 예매 중에 8/11이 걸릴 수 있어
        # 한 번에 한 건만 진행하도록 잠금을 둡니다.
        self._book_lock = threading.Lock()
        # 오픈 감시 커서. 타겟별로 "지금 붙잡고 있는 날짜" 하나입니다.
        self._open_cursor: dict[str, str] = {}
        # 그 날짜를 직전 바퀴에 봤을 때 회차가 있었는지.
        # 키가 없으면 아직 한 번도 안 본 날짜라는 뜻입니다.
        self._open_prev: dict[str, bool] = {}
        # 확인이 끝나 더 볼 필요가 없어진 날짜들 (타겟별).
        self._open_done: dict[str, set[str]] = {}
        # 볼 날짜가 다 떨어졌다고 이미 알린 타겟 (같은 줄 반복 방지).
        self._open_exhausted: set[str] = set()

        # 사이트별 마지막 요청 시간
        self._last_request: dict[str, float] = {}
        # 영화관별 상영일 목록 캐시 (조회시각, 목록)
        self._date_cache: dict[str, tuple[float, list[dict]]] = {}
        # 조건에 맞는 회차가 없던 (타겟, 날짜) → 확인 시각.
        # 상영을 안 하거나 그 시간대에 회차가 없는 날짜에 매 바퀴 요청하지 않기 위함.
        self._empty_dates: dict[tuple[str, str], float] = {}
        self._start_time = time.time()
        self._last_heartbeat = time.time()
        # 마지막 유휴 로그 시각. 0이면 유휴에 들어가자마자 한 줄 남깁니다.
        self._last_idle_log = 0.0
        self._last_check: str | None = None
        self._hits = 0

    def _remaining_targets(self) -> list[dict]:
        """아직 '완료되지 않은' 오픈 모드 타겟. 취소표 모드는 계속 감시하므로 제외.

        - 알림만: 오픈 감지되면 완료
        - auto_book: 결제 완료(_booked) 전까지 완료 아님 (그 사이 종료하면
          예매 스레드가 죽습니다)
        """
        rem = []
        for t in self.targets:
            if t.get("mode", "open") != "open":
                continue
            name = t["name"]
            if t.get("auto_book"):
                if name not in self._booked:
                    rem.append(t)
            elif not self._opened.get(name):
                rem.append(t)
        return rem

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
            # 시작 요약은 장식입니다. 설정 오타 때문에 여기서 죽으면
            # launchd가 30초마다 재시작하는 크래시 루프가 됩니다.
            try:
                print(self._target_desc(t))
            except Exception as e:
                print(f"  - {t.get('name', '(이름 없음)')}: 설정 요약 실패 - {e}")
        print()

        # 시작 즉시 한 번 상태 전송 (웹훅 정상 여부 확인용)
        notify_heartbeat(self.notif, self._build_status())
        self._last_heartbeat = time.time()

        while True:
            # 바퀴 도중 targets가 바뀌어도 안전하도록 스냅샷을 뜹니다.
            targets = list(self.targets)
            polled = False
            for target in targets:
                # 결제까지 끝난 타겟은 더 보지 않습니다.
                if target["name"] in self._booked:
                    continue
                # 오픈 감지된 타겟: 알림만이면 완료. auto_book이면 예매가
                # 끝날 때까지(위 _booked) 계속 폴링해서 좌석을 잡습니다.
                if (self._opened.get(target["name"])
                        and not target.get("auto_book")):
                    continue

                try:
                    self._poll(target)
                    polled = True
                except Exception as e:
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] {target['name']}: 예기치 못한 오류 - {e}")

            # 종료 조건은 for 밖에서 봅니다. 안에 두면 모든 타겟이 위에서
            # 걸러졌을 때 이 줄에 도달조차 하지 않습니다.
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

            if not polled:
                # 폴링할 타겟이 하나도 없는 유휴 상태. 대기는 _poll 안의
                # RPM 제한뿐이라 이때 sleep이 없으면 CPU를 태웁니다.
                # 주기 로그는 로그 파일 mtime을 갱신해서, 워치독에게
                # "죽은 게 아니라 쉬는 중"임을 알리는 유일한 신호입니다.
                time.sleep(IDLE_SLEEP)
                if time.time() - self._last_idle_log >= IDLE_LOG_INTERVAL:
                    now = datetime.now().strftime("%H:%M:%S")
                    print(f"[{now}] 유휴 — 감시 중인 타겟 없음")
                    self._last_idle_log = time.time()

    def _target_desc(self, t: dict) -> str:
        """시작할 때 보여줄 타겟 한 줄 요약."""
        typ = t.get("type", "cgv").upper()
        mode = "취소표" if t.get("mode", "open") == "cancel" else "예매오픈"
        desc = (f"  - [{typ}/{mode}] {t.get('name', '(이름 없음)')} | "
                f"{self._date_desc(t)} | "
                f"{t.get('screen_filter', '전체')} | "
                f"{t.get('movie_filter', '전체')}")
        if t.get("time_rules"):
            rules = t["time_rules"]
            bits = [
                f"{ko}={r[0]}~{r[1]}"
                for key, ko in (("weekday", "평일"), ("weekend", "주말"))
                if (r := rules.get(key)) and len(r) >= 2
            ]
            if bits:
                desc += " | " + " ".join(bits)
        elif (tr := t.get("time_range")) and len(tr) >= 2:
            desc += f" | {tr[0]}~{tr[1]}"
        if t.get("seats"):
            desc += f" | 좌석 {self._seat_desc(t['seats'])}"
        if t.get("auto_book"):
            desc += " | 자동예매" + ("+결제" if t.get("auto_pay") else "")
        return desc

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
        lo, hi = seat_filter.seat_range(seats.get("seat_no"))
        if lo is not None and hi is not None:
            parts.append(f"{lo}~{hi}번")
        elif lo is not None:
            parts.append(f"{lo}번 이상")
        elif hi is not None:
            parts.append(f"{hi}번 이하")
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
            cancel_mode = target.get("mode", "open") == "cancel"
            if not cancel_mode and lo and hi:
                # 오픈 감시는 "아직 예매가 안 열린 날짜"의 오픈을 잡아야 하므로,
                # 예매 가능 목록과 교집합하지 않고 달력상 날짜를 그대로 봅니다.
                wanted = _calendar_range(lo, hi)
            else:
                wanted = [
                    d["scnYmd"] for d in days
                    if (not lo or d["scnYmd"] >= lo)
                    and (not hi or d["scnYmd"] <= hi)
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

        if target.get("mode", "open") != "cancel":
            self._poll_open_watch(target, typ)
            return

        report: list[str] = []
        seen_any = False

        skipped = 0
        for ymd, holiday in self._target_dates(target, typ):
            # 조건에 맞는 회차가 없던 날짜는 한동안 건너뜁니다.
            if self._is_empty_date(target["name"], ymd):
                skipped += 1
                continue

            time_range = self._time_range_for(target, ymd, holiday)

            self._wait_for_rate_limit(typ)
            schedules = self._fetch_filtered(target, typ, ymd, time_range)
            if schedules is None:
                continue
            seen_any = True

            self._mark_empty_date(target["name"], ymd, not schedules)
            report.extend(self._poll_cancel(target, schedules, typ, ymd))

        now = datetime.now().strftime("%H:%M:%S")
        tail = f" (미상영 {skipped}일 건너뜀)" if skipped else ""
        if seen_any or skipped:
            if report:
                head = " | ".join(report[:6])
                more = f" (+{len(report) - 6}개 회차)" if len(report) > 6 else ""
                print(f"[{now}] {target['name']}: 빈자리 {head}{more}{tail}")
            else:
                print(f"[{now}] {target['name']}: 조건에 맞는 빈자리 없음{tail}")

    def _poll_open_watch(self, target: dict, typ: str):
        """오픈 감시: 아직 안 열린 가장 빠른 날짜 '하나'만 붙잡고 봅니다.

        날짜를 전부 훑으면 미오픈 날짜 수만큼 요청이 나가고, 그만큼 한 바퀴가
        길어져 정작 열리는 순간을 늦게 잡습니다. 오픈은 빠른 날짜부터 순서대로
        열리므로 가장 빠른 미오픈 날짜 하나만 보면 충분하고, 바퀴당 요청도
        날짜 수와 무관하게 1회로 고정됩니다.

        오픈 판정은 "직전 바퀴엔 조건에 맞는 회차가 없었는데 이번 바퀴엔
        있다"는 상태 전이로 합니다. 있다는 사실만으로 판정하면 재시작할 때마다
        이미 열려 있던 날짜를 오픈으로 오인해 표를 사버립니다.

        판정 기준은 반드시 **필터를 통과한 회차**여야 합니다. 그 날짜에 스케줄이
        하나라도 있으면 열린 것으로 보면 안 됩니다. CGV는 날짜를 통째로 열지
        않고 특별 상영(GV·콘서트 실황 등)만 먼저 걸어두는 부분 오픈을 하기
        때문입니다. 실제로 영등포 08/26은 정규 시간표가 열리기 전인데도 7·8관에
        8회차가 잡혀 있었습니다. 그걸 오픈으로 보면 그 날짜를 건너뛰어
        정작 IMAX 회차가 열리는 순간을 놓칩니다.
        """
        name = target["name"]
        today = datetime.now().strftime("%Y%m%d")
        done = self._open_done.setdefault(name, set())
        dates = self._target_dates(target, typ)

        # 이미 열린 날짜는 그 자리에서 건너뛰고 미오픈 지점까지 밀어붙입니다.
        # 미오픈에 닿거나 상한에 걸리면 멈춥니다.
        for _ in range(OPEN_SKIP_PER_CYCLE):
            now = datetime.now().strftime("%H:%M:%S")

            cursor = cursor_holiday = None
            for ymd, holiday in dates:
                if ymd < today or ymd in done:
                    continue
                cursor, cursor_holiday = ymd, holiday
                break

            if cursor is None:
                if name not in self._open_exhausted:
                    self._open_exhausted.add(name)
                    print(f"[{now}] {name}: 감시할 날짜가 없습니다 "
                          f"(모두 지났거나 확인이 끝났습니다)")
                return
            self._open_exhausted.discard(name)

            # 커서가 다른 날짜로 옮겨갔으면 직전 관측 상태는 그 날짜 것이므로
            # 버립니다. 새 날짜는 "아직 안 본 날짜"에서 다시 시작합니다.
            if self._open_cursor.get(name) != cursor:
                self._open_cursor[name] = cursor
                self._open_prev.pop(name, None)

            time_range = self._time_range_for(target, cursor, cursor_holiday)

            self._wait_for_rate_limit(typ)
            raw = self._fetch_raw(target, typ, cursor)
            if raw is None:
                # 요청 실패. 상태를 건드리면 실패가 "미오픈"으로 기록되어
                # 다음 바퀴에 가짜 전이가 잡힙니다. 그대로 두고 다시 봅니다.
                return
            matched = self._apply_filters(target, typ, raw, time_range)

            was_open = self._open_prev.get(name)  # None이면 아직 안 본 날짜
            self._open_prev[name] = bool(matched)

            if not matched:
                # raw가 비어있지 않아도 미오픈입니다. 부분 오픈(특별 상영만
                # 먼저 걸린 상태)일 수 있어 몇 회차가 잡혀 있는지 남깁니다.
                tail = (f" (그날 전체 {len(raw)}회차 있으나 조건 불일치)"
                        if raw else "")
                print(f"[{now}] {name}: {_pretty_date(cursor)} 미오픈{tail}")
                return

            if was_open is None:
                # 처음 본 순간 이미 열려 있었습니다. 오픈 '순간'이 아니므로
                # 예매하지 않습니다. 그 빈자리는 취소표 타겟의 몫입니다.
                done.add(cursor)
                print(f"[{now}] {name}: {_pretty_date(cursor)} 이미 오픈됨 "
                      f"— 건너뜁니다")
                continue

            # 미오픈 → 오픈. 여기가 오픈런입니다.
            done.add(cursor)
            self._fire_open(target, matched, typ, cursor)
            return

    def _is_empty_date(self, name: str, ymd: str) -> bool:
        seen = self._empty_dates.get((name, ymd))
        return seen is not None and time.time() - seen < EMPTY_DATE_TTL

    def _mark_empty_date(self, name: str, ymd: str, empty: bool):
        key = (name, ymd)
        if empty:
            self._empty_dates[key] = time.time()
        else:
            self._empty_dates.pop(key, None)

    def _fetch_raw(
        self, target: dict, typ: str, date: str
    ) -> list[dict] | None:
        """그 날짜의 전체 회차를 거르지 않고 반환합니다.

        오픈 감시는 "날짜 자체가 열렸는가"와 "조건에 맞는 회차가 있는가"를
        나눠 봐야 해서, 필터 전 원본이 필요합니다.
        요청 실패 시 None을 돌려줍니다(빈 목록과 구분해야 합니다).
        """
        now = datetime.now().strftime("%H:%M:%S")
        try:
            if typ == "megabox":
                return megabox_client.fetch_schedule(target["branch_no"], date)
            return cgv_client.fetch_schedule(target["site_no"], date)
        except Exception as e:
            print(f"[{now}] {target['name']} {date}: 요청 실패 - {e}")
            return None

    def _apply_filters(
        self, target: dict, typ: str, schedules: list[dict], time_range
    ) -> list[dict]:
        """타겟 조건(상영관/영화/시간대)에 맞는 회차만 추립니다."""
        screen_filter = target.get("screen_filter", "")
        movie_filter = target.get("movie_filter", "")

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

    def _fetch_filtered(
        self, target: dict, typ: str, date: str, time_range
    ) -> list[dict] | None:
        """조회 + 필터링. 요청 실패 시 None을 돌려줍니다."""
        schedules = self._fetch_raw(target, typ, date)
        if schedules is None:
            return None
        return self._apply_filters(target, typ, schedules, time_range)

    def _fire_open(
        self, target: dict, schedules: list[dict], typ: str, ymd: str
    ):
        """미오픈 → 오픈 전이를 처리합니다. 알리고, 자동예매를 겁니다.

        커서가 날짜마다 한 번씩만 여기 도달하므로(_poll_open_watch에서 곧바로
        done 처리) 알림 중복 걱정 없이 매 전이를 그대로 알립니다.
        """
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

        # 오픈 자동예매: 조건에 맞는 좌석을 잡습니다(취소표와 동일한 예매
        # 흐름). 오픈 직후엔 좌석이 넉넉하므로 '새로 풀린 것'을 따지지 않고
        # 조건에 맞는 자리를 바로 노립니다.
        if target.get("auto_book") and target.get("seats"):
            self._book_open(target, schedules, typ, ymd)

    def _book_open(self, target, schedules, typ, ymd):
        """오픈된 회차에서 조건에 맞는 좌석을 잡습니다 (한 바퀴에 한 건)."""
        seat_cfg = target.get("seats") or {}
        for sch in schedules:
            key = self._schedule_key(target["name"], sch, typ, ymd)
            self._wait_for_rate_limit(typ)
            try:
                seats = cgv_client.available_seats(cgv_client.fetch_seats(sch))
            except Exception as e:
                print(f"  좌석 조회 실패 ({ymd} {_start_time(sch, typ)}) - {e}")
                continue
            groups = seat_filter.match(seats, seat_cfg)
            if groups:
                self._launch_booker(target, sch, key, groups[0])
                return  # 예매는 한 번에 한 건만

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

        동시 실행은 _book_lock 이 막습니다. 쿨다운은 두지 않습니다(아래 참고).
        """
        name = target["name"]
        # 결제까지 끝났으면 더 잡지 않습니다. 표는 이미 있는데 조건에 맞는
        # 자리가 남아 있으면 계속 사게 되고, 남의 자리를 5분씩 묶습니다.
        # 반복 예매를 원하면 auto_book_once: false 로 둡니다.
        once = target.get("auto_book_once", True)
        if once and name in self._booked:
            return

        # 쿨다운은 두지 않습니다. 동시 실행은 아래 잠금이 막고, 좌석은
        # 약 5분이면 만료되므로 실패했으면 바로 다시 시도하는 편이 낫습니다.
        # 이미 다른 예매가 브라우저를 쓰고 있으면 건너뜁니다.
        # 놓쳐도 다음 바퀴에 다시 잡힙니다.
        if not self._book_lock.acquire(blocking=False):
            print(f"  자동 예매 대기 — 다른 예매가 진행 중입니다 ({name})")
            return

        # 예매할 인원 수. 명시가 없으면 연석 조건(min_consecutive)을 씁니다.
        # 좌석 묶음이 요청보다 길 수 있으므로(연속 5석 등) 앞에서부터 필요한
        # 만큼만 넘깁니다. group 길이를 그대로 쓰면 5장을 끊게 됩니다.
        seats_cfg = target.get("seats") or {}
        tickets = int(
            target.get("tickets") or seats_cfg.get("min_consecutive") or 1
        )
        # 필요한 수보다 넉넉히 후보를 넘깁니다. 휠체어 전용석처럼 목록에는
        # 판매 가능으로 보이지만 실제로 못 사는 좌석이 있어서, booker가
        # 다음 후보로 넘어갈 수 있어야 합니다.
        seat_locs = [
            s.get("seatLocNo") for s in group[:tickets + 5]
            if s.get("seatLocNo")
        ]
        if not seat_locs:
            return

        def run():
            try:
                from src import booker
                from src.notifier import notify_pay_request
                typ = target.get("type", "cgv").lower()
                pay_info = {
                    "date": _pretty_date(sch.get("scnYmd", "")),
                    "time": _start_time(sch, typ),
                    "movie": sch.get("movNm", ""),
                    "screen": sch.get("scnsNm", ""),
                    "seats": seat_filter.describe([group]),
                }

                def on_pay_request(method):
                    # QR/카톡 결제요청이 실제로 나간 순간, 결제 마무리를 재촉합니다.
                    notify_pay_request(self.notif, name, method, pay_info)

                ok = booker.book(
                    sch, seat_locs,
                    movie_filter=target.get("movie_filter", ""),
                    screen_filter=target.get("screen_filter", ""),
                    count=tickets,
                    pay=bool(target.get("auto_pay")),
                    # 타겟에 지정된 것만 씁니다. 없으면 QR로 진행합니다.
                    kakao=target.get("kakaopay"),
                    on_pay_request=on_pay_request,
                    # 오픈런 대기열을 버티도록 첫 페이지 로드 대기시간을
                    # 늘릴 수 있습니다(기본 10초). 대기열이 이 안에 통과되면
                    # 그대로 예매가 이어집니다.
                    stage_timeout=int(target.get("queue_wait_sec", 10)),
                    **({"confirm_timeout": int(target["pay_timeout_sec"])}
                       if target.get("pay_timeout_sec") else {}),
                )
                # 'paid' = 결제 완료, 'held' = 선점만. 선점만으로는
                # 멈추지 않습니다. 사용자가 결제하지 않았을 수 있습니다.
                if ok == "paid" and once:
                    self._booked.add(name)
                    print(f"  결제 완료 — '{name}' 감시를 중단합니다. "
                          f"다시 감시하려면 재시작하세요.")
            except Exception as e:
                print(f"  자동 예매 실패 - {e}")
            finally:
                self._book_lock.release()

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
