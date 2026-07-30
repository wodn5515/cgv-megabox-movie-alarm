"""취소표 발견 시 크롬에서 좌석 선택까지 진행합니다.

결제는 하지 않습니다. 좌석을 고른 상태로 멈추고 사람이 이어받습니다.

준비:
    1. 크롬을 디버깅 포트로 띄웁니다 (평소 쓰는 크롬은 그대로 둬도 됩니다).

        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
          --user-data-dir="$HOME/chrome-booking-profile" \\
          --remote-debugging-port=9222

    2. 그 창에서 CGV에 한 번 로그인합니다. 프로필이 디스크에 남아 유지됩니다.

수동 테스트:
    python3 -m src.booker 0013 20260815 1800 오디세이 IMAX 00100100090001
"""
import json
import os
import re
import sys
import time

from src.cdp import ChromeNotRunning, Tab, js_by_text

BOOK_URL = "https://cgv.co.kr/cnm/movieBook/movie"
HOME_URL = "https://cgv.co.kr/"
VISITOR_URL_PART = "selectVisitorCnt"


# 각 단계가 끝났는지 판정하는 JS 조건. 고정 sleep 대신 이것을 기다립니다.
JS_THEATER_MODAL = ("[...document.querySelectorAll('.cgv-modal')]"
                    ".filter(m=>m.offsetWidth||m.offsetHeight)"
                    ".some(m=>(m.innerText||'').includes('지역별'))")
JS_CONFIRM_BTN = ("[...document.querySelectorAll('.cgv-modal button')]"
                  ".some(b=>(b.innerText||'').trim()==='극장선택')")
JS_THEATER_OK = "!document.body.innerText.includes('선택 된 극장이 없습니다')"
JS_MOVIE_LIST = ("[...document.querySelectorAll('.cgv-modal')]"
                 ".filter(m=>m.offsetWidth||m.offsetHeight)"
                 ".some(m=>m.querySelector('ul[class*=\"mvList\"] button'))")
JS_DATES_READY = ("[...document.querySelectorAll('[class*=\"dayScroll_scrollItem\"]')]"
                  ".some(e=>!/disabled/i.test((e.className||'').toString()))")
JS_SHOWTIMES = "document.querySelectorAll('[class*=\"screenInfo_timeItem\"]').length>0"
JS_SEATMAP_OPEN = ("(()=>{const c=document.querySelector('[class*=\"seatMap_container\"]');"
                   "if(!c||getComputedStyle(c).visibility!=='visible')return false;"
                   "return [...document.querySelectorAll('button[data-seatlocno]')]"
                   ".some(e=>getComputedStyle(e).visibility!=='hidden');})()")


def _log(msg: str):
    print(f"  [예매] {msg}", flush=True)


# 단계별 소요 시간 측정용. BOOK_TRACE=1 이면 각 단계를 찍습니다.
_TRACE = os.environ.get("BOOK_TRACE") == "1"
_trace_t0 = [0.0]


def _mark(step: str):
    if not _TRACE:
        return
    now = time.time()
    if _trace_t0[0]:
        print(f"    ⏱ {step}: +{now - _trace_t0[0]:.2f}s", flush=True)
    _trace_t0[0] = now


def _theater_name(schedule: dict) -> str:
    """'CGV 용산아이파크몰' → '용산아이파크몰'"""
    return (schedule.get("siteNm") or "").replace("CGV", "").strip()


def _js_modal(marker: str, inner_selector: str, want: str,
              exact: bool = False) -> str:
    """특정 모달 안에서만 요소를 찾는 JS.

    CGV는 영화 모달과 극장 모달을 같은 z-index(300)로 동시에 띄웁니다.
    DOM 뒤쪽 모달이 앞쪽을 덮으므로, 페이지 전체를 훑으면 가려진 모달의
    요소를 잡아 클릭이 먹지 않습니다. 대상 모달로 범위를 한정합니다.
    """
    cmp = ("t === want" if exact else "t.includes(want)")
    return f"""
      const marker = {json.dumps(marker)}, want = {json.dumps(want)};
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      const modal = [...document.querySelectorAll('.cgv-modal')]
        .filter(m => (m.offsetWidth || m.offsetHeight))
        .filter(m => (m.innerText || '').includes(marker))
        .pop();
      if (!modal) return null;
      return [...modal.querySelectorAll({json.dumps(inner_selector)})]
        .filter(e => {{ const t = norm(e.innerText); return {cmp}; }});
    """


def _js_movie_in_list(movie: str) -> str:
    """영화 목록 모달 안에서 해당 영화 버튼을 찾는 JS.

    텍스트 마커로 모달을 고르면 안 됩니다. '검색'은 영화 모달과 극장 모달
    양쪽에 있어서 잘못된 모달을 잡습니다. mvList를 품은 모달로 특정합니다.
    """
    return f"""
      const want = {json.dumps(movie)};
      const modal = [...document.querySelectorAll('.cgv-modal')]
        .filter(m => (m.offsetWidth || m.offsetHeight))
        .filter(m => m.querySelector('ul[class*="mvList"]'))
        .pop();
      if (!modal) return null;
      return [...modal.querySelectorAll('ul[class*="mvList"] button')]
        .filter(e => (e.innerText || '').includes(want));
    """


def _movie_list_open(tab) -> bool:
    return bool(tab.ev("""
      [...document.querySelectorAll('.cgv-modal')]
        .filter(m => (m.offsetWidth || m.offsetHeight))
        .some(m => m.querySelector('ul[class*="mvList"] button'))
    """))


def _staged(tab, movie: str) -> bool:
    """그 영화의 회차 목록이 떠 있는 상태인지 확인합니다.

    날짜는 보지 않습니다. 비싼 것은 첫 페이지 로드(약 4초)이고 날짜
    클릭은 0.13초라, 날짜별로 예열하면 브라우저만 계속 헤집게 됩니다.
    """
    return bool(tab.ev(rf"""
      (() => {{
        if (!location.href.includes('/cnm/movieBook/movie')) return false;
        if (!document.body.innerText.includes({json.dumps(movie)})) return false;
        return document.querySelectorAll('[class*="screenInfo_timeItem"]').length > 0;
      }})()
    """))


def is_staged(schedule: dict, movie_filter: str = "",
              port: int = 9222) -> bool:
    """예열되어 있는지만 가볍게 확인합니다 (브라우저를 건드리지 않음)."""
    movie = movie_filter or schedule.get("movNm", "")
    try:
        tab = Tab(port=port)
    except ChromeNotRunning:
        return False
    try:
        return _staged(tab, movie)
    except Exception:
        return False
    finally:
        tab.close()


def prepare(schedule: dict, movie_filter: str = "", screen_filter: str = "",
            port: int = 9222) -> bool:
    """예매 화면을 회차 목록까지 미리 열어둡니다 (예열).

    취소표가 뜬 뒤 첫 페이지 로드에 4초 가까이 쓰이므로, 미리 이 상태로
    대기해두면 회차 클릭부터 시작할 수 있습니다.
    """
    try:
        tab = Tab(port=port)
    except ChromeNotRunning:
        return False
    try:
        return _stage(tab, schedule, movie_filter, screen_filter)
    except Exception:
        return False
    finally:
        tab.close()


def _stage(tab, schedule: dict, movie: str, screen_filter: str) -> bool:
    """극장 → 영화 → 날짜까지 진행해 회차 목록을 띄웁니다."""
    ymd = schedule.get("scnYmd", "")
    theater = _theater_name(schedule)
    movie = movie or schedule.get("movNm", "")

    if _staged(tab, movie):
        _mark("이미 준비됨")
        return True

    tab.goto(BOOK_URL)
    tab.wait_for(f"({JS_THEATER_MODAL})||({JS_MOVIE_LIST})", timeout=10)
    _mark("① 페이지 로드")

    # 예전엔 여기서 특별관(IMAX) 필터를 먼저 눌렀는데, 그러면 극장 목록이
    # 다시 그려져서 목록이 준비되기 전에 극장을 클릭하게 되고 cold-start에서
    # 극장 선택이 간헐적으로 실패했습니다. 회차는 뒤에서 상영관 이름으로
    # 고르므로 이 필터는 필요 없어 제거했습니다. (screen_filter 인자는
    # 호출부 호환을 위해 남겨둡니다.)

    if not _theater_chosen(tab):
        # 극장 목록이 실제로 그려질 때까지 기다린 뒤 딱 한 번 클릭합니다.
        # 준비 전에 누르면 빗나가고, until 실패로 재클릭하면 선택이 토글로
        # 풀립니다.
        theater_js = _js_modal("지역별", "button, span, li, div, label",
                               theater, exact=True)
        if not tab.wait_for(
            f"(()=>{{const r=(()=>{{{theater_js}}})();"
            f"return Array.isArray(r)&&r.length>0;}})()", timeout=8
        ):
            _log(f"극장 '{theater}' 목록이 나타나지 않았습니다.")
            return False
        if not tab.click(theater_js, until=JS_CONFIRM_BTN, timeout=8):
            _log(f"극장 '{theater}' 를 찾지 못했습니다.")
            return False
        _mark("③ 극장 클릭")
        if not tab.click(_js_modal("지역별", "button", "극장선택", exact=True),
                         until=JS_THEATER_OK, timeout=8, retries=2):
            _log(f"극장 '{theater}' 가 선택되지 않았습니다.")
            return False
        _mark("④ 극장선택 확정")

    if not _movie_list_open(tab):
        tab.click(js_by_text("전체보기", tags="button, a"),
                  until=JS_MOVIE_LIST, timeout=5, retries=1)
    if _movie_list_open(tab):
        if not tab.click(_js_movie_in_list(movie),
                         until=JS_DATES_READY, timeout=8):
            _log(f"영화 '{movie}' 를 목록에서 찾지 못했습니다.")
            return False
    else:
        _log("영화 목록을 열지 못했습니다.")
        return False

    _mark("⑤ 영화 클릭")
    # 날짜는 고르지 않습니다. 영화를 고르면 CGV가 첫 상영일을 자동 선택해
    # 회차 목록이 뜨고, 실제 예매 시 book()이 목표 날짜를 클릭합니다.
    return tab.wait_for(JS_SHOWTIMES, timeout=8)


def _theater_chosen(tab) -> bool:
    """극장이 실제로 선택됐는지 확인합니다."""
    return not bool(tab.ev("""
      document.body.innerText.includes('선택 된 극장이 없습니다')
    """))


def _js_modal_present(marker: str) -> str:
    return f"""
      const marker = {json.dumps(marker)};
      const m = [...document.querySelectorAll('.cgv-modal')]
        .filter(x => (x.offsetWidth || x.offsetHeight))
        .filter(x => (x.innerText || '').includes(marker));
      return m.length ? [m.pop()] : null;
    """


def _modal_open(tab, marker: str) -> bool:
    """해당 모달이 화면에 떠 있는지 확인합니다 (클릭 가능 여부와 무관)."""
    return bool(tab.ev(f"""
      [...document.querySelectorAll('.cgv-modal')]
        .filter(x => (x.offsetWidth || x.offsetHeight))
        .some(x => (x.innerText || '').includes({json.dumps(marker)}))
    """))


def _js_date_item(ymd: str) -> str:
    """날짜 스트립에서 해당 날짜 항목을 찾는 JS.

    항목 텍스트는 '오늘 30' / '토 15' / '토 8.1' 처럼 나오고,
    달이 바뀌는 날은 'M.D' 형태가 됩니다.
    """
    month, day = int(ymd[4:6]), int(ymd[6:8])
    return f"""
      const month = {month}, day = {day};
      return [...document.querySelectorAll('[class*="dayScroll_scrollItem"]')]
        .filter(e => !/disabled/i.test((e.className || '').toString()))
        .filter(e => {{
          const t = (e.innerText || '').trim().replace(/\\s+/g, ' ');
          const m = t.match(/(?:(\\d{{1,2}})\\.)?(\\d{{1,2}})$/);
          if (!m) return false;
          if (m[1] !== undefined && Number(m[1]) !== month) return false;
          return Number(m[2]) === day;
        }});
    """


def js_date_active(ymd: str) -> str:
    """날짜 스트립의 활성 항목이 목표 날짜인지 판정하는 JS 조건."""
    month, day = int(ymd[4:6]), int(ymd[6:8])
    return rf"""
      (() => {{
        const act = [...document.querySelectorAll('[class*="dayScroll_scrollItem"]')]
          .filter(e => /itemActive/.test((e.className || '').toString()))
          .map(e => (e.innerText || '').trim().replace(/\s+/g, ' '));
        return act.some(t => {{
          const m = t.match(/(?:(\d{{1,2}})\.)?(\d{{1,2}})$/);
          if (!m) return false;
          if (m[1] !== undefined && Number(m[1]) !== {month}) return false;
          return Number(m[2]) === {day};
        }});
      }})()
    """


def js_showtime_ready(hhmm: str, screen: str, free: int, total: int) -> str:
    """목표 회차가 '그 날짜의' 목록으로 갱신됐는지 판정하는 JS 조건.

    활성 날짜만 보면 안 됩니다. 날짜 칩은 즉시 갱신되지만 회차 목록은
    비동기로 받아오므로, 이전 날짜 목록에 같은 시간대가 있으면 그대로
    통과해 엉뚱한 회차를 누릅니다.

    회차 항목 텍스트에 '24:00-27:02 100/624석' 처럼 잔여석이 들어 있어서,
    API 값과 대조하면 그 날짜의 목록인지 확실히 알 수 있습니다.
    잔여석은 몇 초 사이 변하므로 여유를 둡니다.
    """
    tol = max(5, int(free * 0.3)) if free else 10 ** 9
    return rf"""
      (() => {{
        const want = {json.dumps(hhmm)}, screen = {json.dumps(screen)};
        const norm = s => (s || '').trim().replace(/\s+/g, ' ');
        const blocks = [...document.querySelectorAll('div, section, li')]
          .filter(e => (e.innerText || '').includes(screen))
          .filter(e => e.querySelectorAll('[class*="screenInfo_timeItem"]').length);
        const scope = blocks.length ? blocks[blocks.length - 1] : document;
        return [...scope.querySelectorAll('[class*="screenInfo_timeItem"]')]
          .filter(e => norm(e.innerText).startsWith(want))
          .some(e => {{
            const m = norm(e.innerText).match(/(\d+)\s*\/\s*{total}\s*석/);
            if (!m) return false;
            return Math.abs(Number(m[1]) - {free}) <= {tol};
          }});
      }})()
    """


def js_showtime_present(hhmm: str, screen: str) -> str:
    """목표 상영관·회차가 화면 목록에 실제로 있는지 판정하는 JS 조건.

    날짜를 클릭한 뒤 '회차 항목이 있으면 통과'로 기다리면, 이전 날짜의
    낡은 목록에서 그대로 클릭하게 됩니다. 목표 회차가 나타날 때까지
    기다려야 갱신이 끝난 것을 보장할 수 있습니다.
    """
    return f"""
      (() => {{
        const want = {json.dumps(hhmm)}, screen = {json.dumps(screen)};
        const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
        const blocks = [...document.querySelectorAll('div, section, li')]
          .filter(e => (e.innerText || '').includes(screen))
          .filter(e => e.querySelectorAll('[class*="screenInfo_timeItem"]').length);
        const scope = blocks.length ? blocks[blocks.length - 1] : document;
        return [...scope.querySelectorAll('[class*="screenInfo_timeItem"]')]
          .some(e => norm(e.innerText).startsWith(want));
      }})()
    """


def _js_showtime(hhmm: str, screen: str) -> str:
    """상영관 + 시작시간으로 회차 항목을 찾는 JS (매진 항목은 제외).

    시작시간만 보면 안 됩니다. 같은 극장의 다른 상영관에 같은 시간대가
    있어서 엉뚱한 관을 고릅니다. 실제로 IMAX관에 없는 25:00 이 1관에
    있어서 1관을 골랐고, 좌석 locNo가 맞지 않아 실패했습니다.
    화면의 특별관 필터에 의존하지 않고 상영관 이름으로 직접 좁힙니다.
    """
    return f"""
      const want = {json.dumps(hhmm)}, screen = {json.dumps(screen)};
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      // 상영관 블록: 제목에 상영관 이름이 있고 회차 항목을 품은 가장 안쪽 요소
      const blocks = [...document.querySelectorAll('div, section, li')]
        .filter(e => (e.innerText || '').includes(screen))
        .filter(e => e.querySelectorAll('[class*="screenInfo_timeItem"]').length);
      const scope = blocks.length ? blocks[blocks.length - 1] : document;
      const items = [...scope.querySelectorAll('[class*="screenInfo_timeItem"]')]
        .filter(e => !/disabled/i.test((e.className || '').toString()))
        .filter(e => norm(e.innerText).startsWith(want));
      return items.map(e => e.querySelector('[class*="timeLink"]') || e);
    """


def _js_visitor_count(count: int) -> str:
    """관람인원 '일반' 그룹에서 해당 숫자 버튼을 찾는 JS.

    일반/청소년 두 그룹에 같은 숫자 버튼이 있어서 앞쪽(일반)을 씁니다.
    """
    return f"""
      const n = {count};
      const btns = [...document.querySelectorAll('button[aria-label$="선택"]')]
        .filter(e => e.getAttribute('aria-label') === n + ' 선택');
      return btns.slice(0, 1);
    """


def _js_open_seat_map() -> str:
    """좌석 맵을 여는 '선택' 버튼을 찾는 JS.

    인원을 고르면 좌석 미리보기만 보이고, 실제 좌석 맵
    (seatMap_container)은 숨겨져 있습니다. '좌석을 선택해 주세요' 영역의
    '선택' 버튼을 눌러야 열립니다. 이미 열려 있으면 null을 돌려줍니다.
    """
    return """
      const c = document.querySelector('[class*="seatMap_container"]');
      if (c && getComputedStyle(c).visibility === 'visible') return null;
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      // 안내문과 버튼을 함께 담은 가장 안쪽 div를 찾습니다.
      const wraps = [...document.querySelectorAll('div')]
        .filter(e => (e.innerText || '').includes('좌석을 선택해 주세요'))
        .filter(e => {
          const n = e.querySelectorAll('button').length;
          return n >= 1 && n <= 3;
        });
      const wrap = wraps[wraps.length - 1];
      const scoped = wrap
        ? [...wrap.querySelectorAll('button')].filter(b => norm(b.innerText) === '선택')
        : [];
      if (scoped.length) return scoped;
      // 못 찾으면 페이지에서 '선택' 버튼을 직접 찾습니다.
      return [...document.querySelectorAll('button')]
        .filter(b => (b.offsetWidth || b.offsetHeight) && norm(b.innerText) === '선택');
    """


def _seat_map_open(tab) -> bool:
    return bool(tab.ev("""
      (() => {
        const c = document.querySelector('[class*="seatMap_container"]');
        if (!c || getComputedStyle(c).visibility !== 'visible') return false;
        return [...document.querySelectorAll('button[data-seatlocno]')]
          .some(e => getComputedStyle(e).visibility !== 'hidden');
      })()
    """))


def _js_seat(seat_loc_no: str) -> str:
    """data-seatlocno로 좌석 버튼을 찾는 JS.

    같은 좌석이 메인맵(38px)과 미니맵(3px)에 중복 렌더링됩니다.
    큰 것부터 시도하되 미니맵도 후보로 남겨둡니다. 좌석맵이 가로로 넘치면
    메인맵 좌석이 화면 밖(x<0)에 놓여 클릭할 수 없고, 그때는 미니맵이
    유일한 경로입니다. 미니맵을 후보에서 빼면 아예 못 누릅니다.

    엉뚱한 좌석이 눌리는 경우는 호출부의 per-seat 조건이 잡아냅니다.
    """
    return f"""
      return [...document.querySelectorAll(
        'button[data-seatlocno={json.dumps(seat_loc_no)}]')]
        .filter(e => !e.disabled)
        .filter(e => getComputedStyle(e).visibility !== 'hidden')
        .sort((a, b) => {{
          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
          return (rb.width * rb.height) - (ra.width * ra.height);
        }});
    """


# 좌석 클릭 후 뜰 수 있는 차단 모달들. 특정 문구를 나열하면 새로운 안내를
# 놓치고, 놓치면 모달이 화면을 덮은 채 이후 클릭이 전부 막힙니다.
# 그래서 '확인/닫기 버튼만 있는 알림성 모달'을 통째로 잡습니다.
JS_ALERT_MODAL = r"""
  [...document.querySelectorAll('.cgv-modal.active')]
    .filter(e => e.offsetWidth || e.offsetHeight)
    .filter(e => {
      const btns = [...e.querySelectorAll('button, a')]
        .map(b => (b.innerText || '').trim());
      return btns.length && btns.length <= 3 &&
             btns.every(t => ['확인', '닫기', '취소'].includes(t));
    })
"""


def _blocking_alert(tab) -> str:
    """좌석 클릭을 막는 알림 모달의 문구를 읽습니다. 없으면 빈 문자열."""
    return tab.ev(rf"""
      (() => {{
        const m = ({JS_ALERT_MODAL}).pop();
        return m ? (m.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80) : '';
      }})()
    """) or ""


def _dismiss_alert(tab) -> bool:
    """알림 모달을 닫습니다."""
    return tab.click(rf"""
      const norm = s => (s || '').trim().replace(/\s+/g, ' ');
      const m = ({JS_ALERT_MODAL}).pop();
      if (!m) return null;
      return [...m.querySelectorAll('button, a')]
        .filter(e => ['확인', '닫기'].includes(norm(e.innerText)));
    """, until=f"!(({JS_ALERT_MODAL}).length)", timeout=4, retries=2)


def _seatmap_matches(tab, schedule: dict) -> tuple[bool, str]:
    """화면의 좌석맵이 이 회차의 것인지 확인합니다.

    날짜를 바꿔도 좌석맵이 이전 회차의 것으로 남는 경우가 있습니다.
    그 상태로 진행하면 실제로는 빈 좌석인데 화면에서 '판매됨'으로 보여
    아무것도 잡지 못합니다. 총 좌석 수와 잔여 수로 대조합니다.
    """
    total = _int(schedule.get("stcnt"))
    free = _int(schedule.get("frSeatCnt"))
    raw = tab.ev("""
      (() => {
        const b = [...document.querySelectorAll('button[data-seatlocno]')]
          .filter(e => getComputedStyle(e).visibility !== 'hidden');
        const uniq = new Set(b.map(e => e.getAttribute('data-seatlocno')));
        const open = new Set(b.filter(e => !e.disabled)
          .map(e => e.getAttribute('data-seatlocno')));
        return JSON.stringify({total: uniq.size, free: open.size});
      })()
    """)
    if not raw:
        return False, "좌석맵을 읽지 못했습니다"
    seen = json.loads(raw)
    if total and seen["total"] != total:
        return False, f"총 좌석 불일치 (API {total} / 화면 {seen['total']})"
    # 잔여석은 몇 초 사이 변하므로 여유를 둡니다.
    if free and abs(seen["free"] - free) > max(5, free * 0.3):
        return False, f"잔여석 불일치 (API {free} / 화면 {seen['free']})"
    return True, f"좌석맵 확인 (총 {seen['total']} · 잔여 {seen['free']})"


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _click_seat(tab, loc: str, until: str, timeout: float = 2.4,
                retries: int = 4) -> bool:
    """좌석을 HTML(el.click)로 직접 눌러 조건(until)이 참이 될 때까지 재시도.

    좌석맵은 react-zoom-pan-pinch 로 감싸인 600px 컨테이너인데, 콘텐츠가
    ~1790px라 좌석 대부분이 화면 밖(x<0)에 놓입니다. 컨테이너는 뷰포트를
    넓혀도 600px 고정이고 줌아웃도 minScale 0.75에서 막혀 전체를 담을 수
    없습니다. 좌표 기반 클릭(locate)은 화면 밖 요소를 못 누르므로, 선택할
    seatLocNo로 요소를 찾아 el.click()으로 위치와 무관하게 누릅니다.
    (React onClick 이라 합성 클릭으로도 선택이 잡히는 것을 실측 확인.)
    """
    for _ in range(retries):
        if tab.ev(f"!!({until})"):
            return True
        tab.js_click(_js_seat(loc))
        if tab.wait_for(until, timeout=timeout / retries):
            return True
    return bool(tab.ev(f"!!({until})"))


def _seat_state(tab, seat_loc_no: str) -> str:
    """좌석 버튼의 실제 상태를 진단용으로 읽습니다.

    '누르지 못했다'가 팔린 것인지, 맵이 안 열린 것인지, 셀렉터가 어긋난
    것인지 구분하려면 이 정보가 필요합니다.
    """
    return tab.ev(f"""
      (() => {{
        const all = [...document.querySelectorAll(
          'button[data-seatlocno={json.dumps(seat_loc_no)}]')];
        if (!all.length) return '좌석 요소 없음';
        const vis = all.filter(e => getComputedStyle(e).visibility !== 'hidden');
        if (!vis.length) return '좌석 맵 미개방';
        if (vis.every(e => e.disabled)) return '판매됨(disabled)';
        return '클릭 가능하나 반응 없음';
      }})()
    """) or "확인 불가"


def _selected_locnos(tab) -> list[str]:
    """현재 선택된 좌석의 seatLocNo 목록 (성공 확인용)."""
    raw = tab.ev("""
      JSON.stringify([...new Set(
        [...document.querySelectorAll('button[data-seatlocno]')]
          .filter(e => /select|choice|on\\b|active/i.test(
            (e.className || '').toString()))
          .map(e => e.getAttribute('data-seatlocno')))])
    """)
    return json.loads(raw) if raw else []


def _js_checkbox_label(checkbox_id: str) -> str:
    """체크박스를 실제로 토글하는 라벨을 찾는 JS.

    input 자체는 SVG 아이콘 라벨에 가려져 있어 input 좌표를 눌러도
    토글되지 않습니다. 그 좌표에서 히트되는 요소의 label을 눌러야 합니다.
    """
    return f"""
      const i = document.getElementById({json.dumps(checkbox_id)});
      if (!i) return null;
      i.scrollIntoView({{block: 'center'}});
      const b = i.getBoundingClientRect();
      const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
      const lab = hit ? hit.closest('label') : null;
      return lab ? [lab] : (hit ? [hit] : null);
    """


def _js_pay_method(alt: str) -> str:
    """결제수단 아이콘(img[alt])으로 해당 항목을 찾는 JS."""
    return f"""
      return [...document.querySelectorAll('img[alt={json.dumps(alt)}]')]
        .map(i => i.closest('button, label, li') || i);
    """


def _pay_method_active(tab, alt: str) -> bool:
    return bool(tab.ev(f"""
      (() => {{
        const i = document.querySelector('img[alt={json.dumps(alt)}]');
        const li = i ? i.closest('li') : null;
        return li ? /active/.test((li.className || '').toString()) : false;
      }})()
    """))


def _final_amount(tab) -> int | None:
    """화면의 최종결제금액을 숫자로 읽습니다."""
    raw = tab.ev(
        r"""(() => {
          const m = document.body.innerText.match(/최종결제금액\s*([\d,]+)\s*원/);
          return m ? m[1] : null;
        })()"""
    )
    if not raw:
        return None
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return None


def _dismiss_timer(tab, extend: bool = True):
    """'결제 가능 시간이 N분 남았습니다' 알림을 처리합니다.

    결제 직전 단계에서는 연장(확인)하는 편이 낫습니다. QR만 띄워두고
    시간이 만료되면 사용자가 승인할 틈이 없습니다.
    """
    text = tab.ev("""
      (() => {
        const m = document.querySelector('.cgv-modal.modal-alert.active');
        return m ? (m.innerText || '').replace(/\\s+/g, ' ') : '';
      })()
    """) or ""
    if "연장" not in text:
        return
    want = "확인" if extend else "취소"
    tab.click(f"""
      const m = document.querySelector('.cgv-modal.modal-alert.active');
      if (!m) return null;
      return [...m.querySelectorAll('button, a')]
        .filter(e => (e.innerText || '').trim() === {json.dumps(want)});
    """, wait=1.5)
    _log(f"결제 가능 시간 알림 → {want}")


def verify_summary(tab, schedule: dict, expect_seats: int) -> tuple[bool, str]:
    """결제로 넘어가기 전에 화면의 예매 내용이 요청과 맞는지 확인합니다.

    셀렉터가 어긋나 엉뚱한 회차·상영관을 잡았을 때 결제로 진입하지
    않도록 막는 안전장치입니다.
    """
    text = tab.text()
    raw = schedule.get("scnsrtTm", "") or ""
    hhmm = f"{raw[:2]}:{raw[2:]}" if len(raw) == 4 else raw
    ymd = schedule.get("scnYmd", "")
    checks = {
        "영화": schedule.get("movNm", ""),
        "상영관": schedule.get("scnsNm", ""),
        "회차": hhmm,
        "날짜": f"{ymd[:4]}.{ymd[4:6]}.{ymd[6:8]}" if len(ymd) == 8 else ymd,
    }
    missing = [k for k, v in checks.items() if v and v not in text]
    if missing:
        return False, f"화면에서 확인 실패: {', '.join(missing)}"

    picked = _selected_locnos(tab)
    if len(picked) != expect_seats:
        return False, f"선택 좌석 수 불일치 (기대 {expect_seats}, 실제 {len(picked)})"
    return True, f"{checks['영화']} {checks['날짜']} {hhmm} · 좌석 {len(picked)}석"


PAY_URL_PART = "mpy/main"
KAKAO_HOST = "kakaopay.com"


def kakao_identity(spec) -> dict:
    """카톡결제 요청에 넣을 휴대폰번호·생년월일을 읽습니다.

    **타겟에 지정된 것만 씁니다. 전역 폴백은 없습니다.**
    지정하지 않은 타겟은 QR결제 화면을 띄웁니다. 전역 기본값을 두면
    의도하지 않은 사람에게 결제 요청이 갈 수 있어 명시를 요구합니다.

        targets:
          - name: "내가 결제할 타겟"
            kakaopay:
              phone: "01012345678"
              birth: "900101"      # YYMMDD 6자리

          - name: "환경변수로 받을 타겟"
            kakaopay: true         # CGV_KAKAO_PHONE / CGV_KAKAO_BIRTH 사용

          - name: "QR로 볼 타겟"
            # kakaopay 없음 → QR결제 탭

    생년월일은 그 번호의 카카오페이 계정 본인 확인용입니다. 다른 사람에게
    보내려면 그 사람의 생년월일이어야 하고, 결제도 그 사람이 하게 됩니다.

    개인정보이므로 config.yaml은 .gitignore에 있어야 합니다.
    """
    none = {"phone": "", "birth": ""}
    if spec is True:
        phone = os.environ.get("CGV_KAKAO_PHONE", "")
        birth = os.environ.get("CGV_KAKAO_BIRTH", "")
    elif isinstance(spec, dict):
        phone = str(spec.get("phone") or "")
        birth = str(spec.get("birth") or "")
    else:
        return none

    phone = re.sub(r"\D", "", phone)
    birth = re.sub(r"\D", "", birth)
    # 둘 중 하나라도 비어 있으면 카톡결제를 시도하지 않고 QR로 갑니다.
    # 반쯤 채워진 설정으로 엉뚱한 요청을 보내지 않기 위함입니다.
    if not (phone and birth):
        return none
    return {"phone": phone, "birth": birth}


def _mask(value: str) -> str:
    """로그에 개인정보를 그대로 남기지 않습니다."""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


CONFIRM_POLL_SEC = 10
# 좌석 선점 시간은 실측 약 5분입니다 (경고 모달이 2분 남을 때 뜨고,
# 그 뒤 좌석 선택 화면으로 돌아가며 해제됩니다).
# 카카오페이 화면에 있는 동안은 CGV의 연장 알림을 볼 수 없으므로
# 만료 직전에 스스로 빠져나오도록 그보다 짧게 잡습니다.
SEAT_HOLD_SEC = 300
DEFAULT_CONFIRM_TIMEOUT = 240


def _js_kakao_page_confirm() -> str:
    """카카오페이 대기 화면의 '확인' 버튼 (모달의 확인과 구분)."""
    return """
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      const btns = [...document.querySelectorAll('button')]
        .filter(e => (e.offsetWidth || e.offsetHeight) && norm(e.innerText) === '확인');
      const page = btns.filter(e => /confirm-btn/.test((e.className || '').toString()));
      return page.length ? page : btns;
    """


def _js_kakao_modal_confirm() -> str:
    """'결제가 진행 중이에요' 안내 모달의 '확인' 버튼."""
    return """
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      const modal = [...document.querySelectorAll('[class*="modallayout"]')]
        .filter(e => (e.offsetWidth || e.offsetHeight))
        .filter(e => /isShow/.test((e.className || '').toString()))
        .pop();
      if (!modal) return null;
      return [...modal.querySelectorAll('button')]
        .filter(e => norm(e.innerText) === '확인');
    """


def _kakao_modal_open(tab) -> bool:
    return bool(tab.ev("""
      [...document.querySelectorAll('[class*="modallayout"]')]
        .filter(e => (e.offsetWidth || e.offsetHeight))
        .some(e => /isShow/.test((e.className || '').toString()))
    """))


def _wait_and_confirm(tab, timeout: int = DEFAULT_CONFIRM_TIMEOUT) -> bool:
    """폰에서 승인을 마치면 브라우저의 '확인'을 눌러 예매를 마무리합니다.

    카톡결제는 폰에서 승인해도 브라우저에서 '확인'을 눌러야 예매가 끝납니다.
    승인 전에 누르면 '결제가 진행 중이에요' 안내가 뜨므로, 안내를 닫고
    주기적으로 다시 누릅니다.

    이 클릭은 사용자가 이미 결제를 승인한 뒤의 마무리 단계입니다.
    승인 없이는 아무리 눌러도 예매가 되지 않습니다.
    """
    _log(f"승인 대기 중... 최대 {timeout}초 "
         f"(좌석 선점 약 {SEAT_HOLD_SEC // 60}분 중 남은 시간), "
         f"{CONFIRM_POLL_SEC}초마다 확인합니다.")
    _log("(승인은 폰에서 직접 하셔야 합니다. 승인 없이는 예매되지 않습니다.)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = tab.ev("location.href") or ""
        if KAKAO_HOST not in url:
            _log(f"결제 승인 확인 · 예매 완료 화면으로 이동했습니다 ({url[:60]})")
            return True

        if _kakao_modal_open(tab):
            tab.click(_js_kakao_modal_confirm(), wait=1.0, retries=1)

        tab.click(_js_kakao_page_confirm(), wait=2.5, retries=1)
        time.sleep(CONFIRM_POLL_SEC)

    # 시간 안에 승인되지 않으면 놓친 것으로 보고 초기 화면으로 돌아갑니다.
    # 결제 페이지에 머물러 있으면 좌석이 계속 묶여 다른 사람도, 모니터도
    # 그 자리를 볼 수 없습니다.
    _log(f"{timeout}초 안에 승인되지 않았습니다. 놓친 것으로 보고 빠져나갑니다.")
    try:
        tab.goto(HOME_URL)
        _log("초기 화면으로 돌아갔습니다 (좌석 선점 해제).")
    except Exception as e:
        _log(f"초기 화면 이동 실패 - {e}")
    return False


def _kakao_handoff(tab, identity: dict,
                   confirm_timeout: int = DEFAULT_CONFIRM_TIMEOUT) -> bool:
    """카카오페이 화면에서 승인 수단을 준비하고 멈춥니다.

    휴대폰번호·생년월일이 설정돼 있으면 '카톡결제'로 결제요청을 보냅니다.
    카카오톡으로 요청이 도착하므로 컴퓨터 앞에 없어도 승인할 수 있습니다.
    없으면 'QR결제' 탭을 띄워 스캔하도록 둡니다.

    어느 쪽이든 최종 승인은 사용자가 폰에서 합니다.
    """
    phone, birth = identity.get("phone", ""), identity.get("birth", "")

    if not (phone and birth):
        if tab.click(js_by_text("QR결제", tags="button, a, li, span, div"),
                     wait=1.5, retries=1):
            _log("카카오페이 QR이 표시되었습니다. 폰으로 스캔해 승인하세요.")
        else:
            _log("카카오페이 결제 화면입니다 (QR결제/카톡결제를 직접 고르세요).")
        # QR 화면에는 '확인' 버튼이 보이지 않지만, 스캔 후 마무리 클릭이
        # 필요한지 확인되지 않았습니다. 같은 대기 루프를 돌립니다.
        # 버튼이 없으면 URL 변화만 감시하므로 무해합니다.
        return _wait_and_confirm(tab, confirm_timeout)

    if not tab.click(js_by_text("카톡결제", tags="button, a, li, span, div"),
                     wait=2.5):
        _log("카톡결제 탭을 열지 못했습니다. 화면에서 직접 진행하세요.")
        return True

    ok_phone = tab.type_into("#phoneNumber", phone)
    ok_birth = tab.type_into("#dateOfBirth", birth)
    if not (ok_phone and ok_birth):
        _log(f"입력 실패 (휴대폰 {ok_phone}, 생년월일 {ok_birth}). "
             f"화면에서 직접 입력하세요.")
        return True
    _log(f"카톡결제 정보 입력: {_mask(phone)} / {_mask(birth)}")

    if not tab.click(js_by_text("결제요청", tags="button"), wait=4.0):
        _log("결제요청을 보내지 못했습니다. 화면에서 직접 눌러주세요.")
        return True

    _log("카카오톡으로 결제요청을 보냈습니다. 카톡에서 승인하세요.")
    return _wait_and_confirm(tab, confirm_timeout)


# 버튼 innerText가 "21,000원\n결제하기" 처럼 줄바꿈을 포함하므로
# 반드시 공백을 정규화한 뒤 비교해야 합니다.
# 좌석 맵 모달이 열려 있는지. 이 모달 안에 '선택완료'가 있고, 그 뒤에
# 가려진 'N원 결제하기'가 계속 존재하므로 결제 버튼 유무로는 판정할 수 없습니다.
JS_SEATMAP_MODAL = (
    "[...document.querySelectorAll('.cgv-modal.active')]"
    ".filter(e=>e.offsetWidth||e.offsetHeight)"
    ".some(e=>[...e.querySelectorAll('button')]"
    ".some(b=>(b.innerText||'').trim()==='선택완료'))"
)

JS_PAY_BTN_EXISTS = (
    "[...document.querySelectorAll('button')]"
    ".filter(e=>e.offsetWidth||e.offsetHeight)"
    ".some(e=>/원 결제하기$/.test((e.innerText||'').trim().replace(/\\s+/g,' ')))"
)

JS_PAY_BTN = """
  const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
  return [...document.querySelectorAll('button')]
    .filter(e => (e.offsetWidth || e.offsetHeight))
    .filter(e => /원 결제하기$/.test(norm(e.innerText)));
"""


def _to_payment_page(tab) -> bool:
    """좌석 선택 상태에서 결제수단 화면(/mpy/main)까지 밀어두고 멈춥니다.

    좌석은 고른 순간 서버가 선점하고 제한시간이 있으므로, 결제수단만
    고르면 되는 상태로 세워두는 편이 그 시간을 알차게 씁니다.
    결제수단을 고르고 확정하지 않으면 결제되지 않습니다.
    """
    # 좌석 맵 모달이 열려 있으면 '선택완료'로 닫습니다.
    if tab.ev(f"!!({JS_SEATMAP_MODAL})"):
        if not tab.click(js_by_text("선택완료", tags="button"),
                         until=f"!({JS_SEATMAP_MODAL})", timeout=8):
            _log("'선택완료'를 누르지 못했습니다.")
            return False
    _dismiss_timer(tab)

    # 결제하기 → '결제 전 확인해 주세요' 안내 모달이 뜹니다.
    if not tab.click(JS_PAY_BTN,
                     until="!!document.querySelector('.cgv-modal.active')",
                     timeout=8):
        _log("결제하기 버튼을 누르지 못했습니다.")
        return False

    js_on_pay_page = f"location.href.includes({json.dumps(PAY_URL_PART)})"
    if not tab.click(r"""
      const norm = s => (s || '').trim().replace(/\s+/g, ' ');
      const m = [...document.querySelectorAll('.cgv-modal.active')].pop();
      if (!m) return null;
      return [...m.querySelectorAll('button')].filter(e => norm(e.innerText) === '결제하기');
    """, until=js_on_pay_page, timeout=12, retries=2):
        _log("결제 페이지로 넘어가지 못했습니다.")
        return False

    # 결제수단 목록이 실제로 렌더될 때까지 기다립니다.
    tab.wait_for("!!document.querySelector('img[alt=\"카카오페이\"]')",
                 timeout=8)

    # 금액은 막지 않고 기록만 합니다. 승인 화면에 금액이 표시되고
    # 사용자가 그것을 보고 승인하므로 고정값과 비교할 필요가 없습니다.
    amount = _final_amount(tab)
    _log(f"결제수단 화면 · 최종결제금액 {amount:,}원" if amount
         else "결제수단 화면 · 금액을 읽지 못했습니다")
    return True


def _request_kakao_pay(tab, identity: dict, confirm_timeout: int) -> bool:
    """결제수단 화면에서 카카오페이를 골라 결제요청까지 보냅니다."""
    # 결제수단을 먼저 고릅니다. 수단을 고르면 약관 체크가 초기화되므로
    # 순서를 바꾸면 '전체 약관에 동의해주세요'에서 막힙니다.
    if not tab.click(_js_pay_method("카카오페이"),
                     until=("(()=>{const i=document.querySelector("
                            "'img[alt=\"카카오페이\"]');const li=i?i.closest('li'):null;"
                            "return li?/active/.test((li.className||'').toString()):false;})()"),
                     timeout=6):
        _log("카카오페이를 선택하지 못했습니다.")
        return False

    js_terms_ok = ("(()=>{const e=document.getElementById('chkAll');"
                   "return e?e.checked:false;})()")
    if not tab.click(_js_checkbox_label("chkAll"), until=js_terms_ok,
                     timeout=6):
        _log("약관 전체 동의를 체크하지 못했습니다.")
        return False

    _dismiss_timer(tab)
    js_on_kakao = f"location.href.includes({json.dumps(KAKAO_HOST)})"
    if not tab.click(JS_PAY_BTN, until=js_on_kakao, timeout=15):
        _log("카카오페이 화면으로 넘어가지 못했습니다. 브라우저를 확인하세요.")
        return False

    time.sleep(1.0)
    return _kakao_handoff(tab, identity, confirm_timeout)


def book(schedule: dict, seat_loc_nos: list[str], movie_filter: str = "",
         screen_filter: str = "", count: int | None = None,
         port: int = 9222, pay: bool = False, kakao=None,
         confirm_timeout: int = DEFAULT_CONFIRM_TIMEOUT) -> bool:
    """예매 화면을 열어 좌석 선택까지 진행합니다. 결제는 하지 않습니다.

    schedule: 모니터가 받은 회차 dict (siteNm, scnYmd, scnsrtTm, movNm 등)
    seat_loc_nos: 선택할 좌석의 seatLocNo 목록
    """
    if not seat_loc_nos:
        _log("선택할 좌석이 없어 중단합니다.")
        return False

    ymd = schedule.get("scnYmd", "")
    raw_time = schedule.get("scnsrtTm", "")
    hhmm = f"{raw_time[:2]}:{raw_time[2:]}" if len(raw_time) == 4 else raw_time
    theater = _theater_name(schedule)
    movie = movie_filter or schedule.get("movNm", "")
    count = count or len(seat_loc_nos)

    try:
        tab = Tab(port=port)
    except ChromeNotRunning as e:
        _log(f"크롬 연결 실패 — {e}")
        return False

    try:
        tab.front()
        _log(f"{theater} {ymd} {hhmm} {schedule.get('scnsNm', '')} {movie} · "
             f"{count}장 (후보 좌석 {len(seat_loc_nos)}개)")

        _mark("시작")
        # 극장 → 영화 → 날짜까지. 이미 그 상태면(예열) 즉시 지나갑니다.
        # 첫 페이지 로드가 전체 시간의 대부분이라 여기가 관건입니다.
        if not _stage(tab, schedule, movie, screen_filter):
            return False
        _mark("⑥ 회차목록 대기")

        # 목표 회차가 목록에 나타날 때까지 기다립니다. 단순히 '회차가 있다'로
        # 기다리면 이전 날짜의 낡은 목록에서 그대로 클릭하게 됩니다.
        # 영화를 고르면 CGV가 기본 날짜를 자동 선택합니다. 그 날짜 목록에도
        # 같은 시간대 회차가 있을 수 있어, '회차가 보인다'만으로 기다리면
        # 날짜 반영 전에 엉뚱한 날짜의 회차를 누릅니다.
        # 활성 날짜가 목표 날짜인지까지 확인해야 합니다.
        screen = schedule.get("scnsNm", "")
        until_date = (
            f"({js_date_active(ymd)}) && "
            f"({js_showtime_ready(hhmm, screen, _int(schedule.get('frSeatCnt')), _int(schedule.get('stcnt')))})"
        )
        if not tab.click(_js_date_item(ymd), until=until_date, timeout=10):
            _log(f"{ymd} 의 {screen} {hhmm} 회차가 목록에 없습니다 "
                 f"(날짜 선택 실패 또는 매진).")
            return False
        _mark("⑦ 날짜 클릭")

        # URL은 페이지가 조작 가능해지기 전에 바뀝니다. 인원 버튼이 실제로
        # 렌더될 때까지 기다려야 다음 클릭이 먹습니다.
        js_visitor_page = (
            f"location.href.includes({json.dumps(VISITOR_URL_PART)}) && "
            f"[...document.querySelectorAll('button[aria-label$=\"선택\"]')]"
            f".length > 0"
        )
        if not tab.click(_js_showtime(hhmm, screen), until=js_visitor_page,
                         timeout=12):
            _log(f"{screen} {hhmm} 회차를 선택하지 못했습니다 "
                 f"(매진되었을 수 있습니다).")
            return False

        _mark("⑧ 회차 클릭 → 인원페이지")
        js_count_on = (
            f"[...document.querySelectorAll('button[aria-label$=\"선택\"]')]"
            f".some(e=>e.getAttribute('aria-label')==='{count} 선택'"
            f" && e.getAttribute('aria-pressed')==='true')"
        )
        if not tab.ev(f"!!({js_count_on})") and not tab.click(
            _js_visitor_count(count), until=js_count_on, timeout=9, retries=3
        ):
            _log(f"인원 {count}명을 선택하지 못했습니다.")
            return False

        _mark("⑨ 인원 클릭")
        # 좌석 맵 열기. 반영이 늦을 때가 있어 열릴 때까지 확인합니다.
        for _ in range(6):
            if _seat_map_open(tab):
                # 맵이 보이자마자 클릭하면 겉도는 경우가 있어 아주 짧게 둡니다.
                time.sleep(0.15)
                break
            tab.click(_js_open_seat_map(), until=JS_SEATMAP_OPEN, timeout=3,
                      retries=1)
        else:
            _log("좌석 맵을 열지 못했습니다.")
            return False

        # 인원이 2명 이상이면 첫 좌석만 누르면 CGV가 옆자리까지 자동으로
        # 잡아줍니다. 그래서 하나 누른 뒤 모자란 만큼만 추가로 누릅니다.
        _mark("⑩ 좌석맵 열기")
        ok_map, map_msg = _seatmap_matches(tab, schedule)
        if not ok_map:
            _log(f"좌석맵이 이 회차의 것이 아닙니다 — {map_msg}. 중단합니다.")
            return False

        picked: list[str] = []
        blocked: set[str] = set()   # 예매 제한으로 쓸 수 없는 좌석
        clicks = 0                  # 실제로 누른 횟수 (건너뛴 좌석은 제외)
        for loc in seat_loc_nos:
            # 인원 수보다 많이 누르지 않습니다. 첫 클릭이 요청과 다른 좌석을
            # 잡았더라도 정원은 이미 찼고, 더 누르면 CGV가 "선택하신 관람인원은
            # N명입니다" 팝업을 띄웁니다.
            if clicks >= count:
                break
            # 매 반복마다 화면의 실제 선택 상태를 봅니다. 로컬 변수만 믿으면,
            # 첫 클릭이 '요청한 좌석'을 못 잡았을 때(다른 좌석이 잡힌 경우)
            # 실패로 처리되어 이미 정원이 찼는데도 계속 누릅니다.
            picked = [x for x in _selected_locnos(tab) if x not in blocked]
            if len(picked) >= count:
                break

            # 누르기 전에 상태를 확인합니다. 팔린 좌석을 눌러봐야 팝업만
            # 뜨고 재시도로 시간만 씁니다. 목록 조회와 클릭 사이 몇 초
            # 동안 팔릴 수 있어 화면 기준으로 다시 봅니다.
            state = _seat_state(tab, loc)
            if state != "클릭 가능하나 반응 없음":
                _log(f"좌석 {loc} 건너뜀 — {state}")
                continue

            clicks += 1
            # '아무 좌석이나 선택됨'이 아니라 '이 좌석이 선택됨'을 봐야 합니다.
            # 제한 좌석은 선택 표시가 남아 있어서 전역 조건에 속습니다.
            js_seat_on = (
                f"[...document.querySelectorAll("
                f"'button[data-seatlocno={json.dumps(loc)}]')]"
                f".some(e=>/select|choice|on\\b|active/i.test("
                f"(e.className||'').toString()))"
            )
            # 좌표 대신 el.click()으로 직접 눌러 화면 밖 좌석도 선택합니다.
            # 좌석 맵이 막 열린 직후엔 한 번 겉돌 수 있어 짧게 여러 번 시도합니다.
            if not _click_seat(tab, loc, js_seat_on, timeout=2.4, retries=4):
                # 의도한 좌석이 아니어도 이미 정원만큼 잡혔을 수 있습니다.
                # 이걸 확인하지 않으면 잡힌 자리를 두고 계속 다른 자리를
                # 누릅니다.
                already = [x for x in _selected_locnos(tab) if x not in blocked]
                if len(already) >= count:
                    picked = already
                    _log(f"좌석 {len(picked)}석 이미 선택됨 — 추가 클릭 중단")
                    break
                if (msg := _blocking_alert(tab)):
                    _dismiss_alert(tab)
                    _log(f"좌석 {loc} 실패 — {msg}")
                else:
                    _log(f"좌석 {loc} 실패 — {_seat_state(tab, loc)}")
                continue
            # 휠체어 전용석 제한, 인원 초과 등 안내가 뜨면 닫고 판단합니다.
            # 닫지 않으면 모달이 화면을 덮어 이후 클릭이 전부 막힙니다.
            if (msg := _blocking_alert(tab)):
                _dismiss_alert(tab)
                already = _selected_locnos(tab)
                if len(already) >= count:
                    # 이미 정원만큼 잡혀 있습니다 ("관람인원은 N명입니다" 등)
                    _log(f"안내 — {msg}")
                    picked = [x for x in already if x not in blocked]
                    break
                _log(f"좌석 {loc} 실패 — {msg}")
                blocked.add(loc)
                # 선택 상태로 남으면 정원을 먹어 다음 좌석을 못 누릅니다.
                if tab.ev(f"!!({js_seat_on})"):
                    _click_seat(tab, loc, f"!({js_seat_on})",
                                timeout=3, retries=2)
                continue
            picked = [x for x in _selected_locnos(tab) if x not in blocked]
            if len(picked) >= count:
                break

        if not picked:
            _log("좌석을 하나도 선택하지 못했습니다.")
            return False
        if len(picked) != count:
            _log(f"좌석 {len(picked)}/{count}석만 선택됐습니다. 화면에서 확인하세요.")

        _mark("⑪ 좌석 클릭")
        ok, summary = verify_summary(tab, schedule, len(picked))
        _log(f"검증: {summary}")
        if not ok:
            _log("예매 내용이 요청과 달라 여기서 멈춥니다. 화면을 확인하세요.")
            return False

        _log(f"좌석 {len(picked)}석 선점 ({', '.join(picked)})")

        # 좌석은 이미 선점됐고 제한시간이 있으므로, 결제수단만 고르면 되는
        # 화면까지 밀어둡니다. auto_pay가 없으면 여기서 멈춥니다.
        if not _to_payment_page(tab):
            return False
        _mark("⑫ 결제수단 화면")
        if not pay:
            _log("결제수단 화면까지 진행했습니다. 결제는 직접 하세요.")
            return "held"   # 선점만 됨 (결제 안 함)

        return ("paid" if _request_kakao_pay(
            tab, kakao_identity(kakao), confirm_timeout) else "held")

    except Exception as e:
        _log(f"진행 중 오류 - {e}")
        return False
    finally:
        tab.close()


def main(argv: list[str]):
    if len(argv) < 6:
        print(__doc__)
        print("사용법: python3 -m src.booker <siteNo> <YYYYMMDD> <HHMM> "
              "<영화키워드> <상영관키워드> <seatLocNo> [seatLocNo ...]")
        return 1

    site_no, ymd, hhmm, movie, screen, *seats = argv
    from src import cgv_client

    schedules = [
        s for s in cgv_client.fetch_schedule(site_no, ymd)
        if s.get("scnsrtTm") == hhmm
        and screen.upper() in (s.get("scnsNm", "") or "").upper()
        and movie in (s.get("movNm", "") or "")
    ]
    if not schedules:
        print("해당 회차를 찾지 못했습니다.")
        return 1

    ok = book(schedules[0], seats, movie_filter=movie, screen_filter=screen)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
