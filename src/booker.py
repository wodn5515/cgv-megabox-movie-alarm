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
import sys
import time

from src.cdp import ChromeNotRunning, Tab, js_by_text

BOOK_URL = "https://cgv.co.kr/cnm/movieBook/movie"
VISITOR_URL_PART = "selectVisitorCnt"


def _log(msg: str):
    print(f"  [예매] {msg}", flush=True)


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


def _js_showtime(hhmm: str) -> str:
    """상영 시작시간으로 회차 항목을 찾는 JS (매진 항목은 제외)."""
    return f"""
      const want = {json.dumps(hhmm)};
      const items = [...document.querySelectorAll('[class*="screenInfo_timeItem"]')]
        .filter(e => !/disabled/i.test((e.className || '').toString()))
        .filter(e => (e.innerText || '').trim().startsWith(want));
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

    이 값은 좌석 조회 API의 seatLocNo와 동일합니다. 같은 좌석이
    메인맵/미니맵에 중복 렌더링되므로 후보를 모두 넘깁니다.
    좌석 맵이 열린 뒤에만 보이므로 가시성도 확인합니다.
    """
    return f"""
      return [...document.querySelectorAll(
        'button[data-seatlocno={json.dumps(seat_loc_no)}]')]
        .filter(e => !e.disabled)
        .filter(e => getComputedStyle(e).visibility !== 'hidden');
    """


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


def _to_payment_qr(tab, schedule: dict, seats: int,
                   expect_amount: int | None) -> bool:
    """결제 페이지로 넘어가 카카오페이 QR까지 띄우고 멈춥니다.

    QR은 사용자가 폰으로 스캔해 승인해야 결제가 완료됩니다.
    QR이 뜬 뒤에는 아무것도 누르지 않습니다.
    """
    if not tab.click(js_by_text("선택완료", tags="button"), wait=5.0):
        _log("'선택완료'를 누르지 못했습니다.")
        return False
    _dismiss_timer(tab)

    pay_btn = """
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      return [...document.querySelectorAll('button')]
        .filter(e => (e.offsetWidth || e.offsetHeight))
        .filter(e => /원 결제하기$/.test(norm(e.innerText)));
    """
    if not tab.click(pay_btn, wait=6.0):
        _log("결제하기 버튼을 누르지 못했습니다.")
        return False

    # '결제 전 확인해 주세요' 안내 모달
    tab.click("""
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      const m = [...document.querySelectorAll('.cgv-modal.active')].pop();
      if (!m) return null;
      return [...m.querySelectorAll('button')].filter(e => norm(e.innerText) === '결제하기');
    """, wait=8.0, retries=2)

    for _ in range(12):
        if PAY_URL_PART in (tab.ev("location.href") or ""):
            break
        time.sleep(1.0)
    else:
        _log("결제 페이지로 넘어가지 못했습니다.")
        return False

    amount = _final_amount(tab)
    if expect_amount is not None and amount != expect_amount:
        _log(f"최종결제금액이 예상과 다릅니다 (예상 {expect_amount:,}원, "
             f"화면 {amount:,}원 이라면 확인 필요). 결제를 진행하지 않습니다.")
        return False
    _log(f"결제 페이지 · 최종결제금액 {amount:,}원" if amount
         else "결제 페이지 · 금액 확인 실패")

    # 결제수단을 먼저 고릅니다. 수단을 고르면 약관 체크가 초기화되므로
    # 순서를 바꾸면 '전체 약관에 동의해주세요'에서 막힙니다.
    if not tab.click(_js_pay_method("카카오페이"), wait=3.5):
        _log("카카오페이를 선택하지 못했습니다.")
        return False
    if not _pay_method_active(tab, "카카오페이"):
        _log("카카오페이가 선택 상태로 바뀌지 않았습니다.")
        return False

    if not tab.click(_js_checkbox_label("chkAll"), wait=2.0):
        _log("약관 전체 동의를 체크하지 못했습니다.")
        return False
    if not tab.ev("(() => { const e = document.getElementById('chkAll');"
                  " return e ? e.checked : false; })()"):
        _log("약관 동의가 반영되지 않았습니다.")
        return False

    _dismiss_timer(tab)
    if not tab.click(pay_btn, wait=10.0):
        _log("결제 요청을 보내지 못했습니다.")
        return False

    for _ in range(15):
        url = tab.ev("location.href") or ""
        if KAKAO_HOST in url:
            _log("카카오페이 QR이 표시되었습니다. 폰으로 스캔해 승인하세요.")
            _log("(여기서 멈춥니다. 결제 승인은 진행하지 않습니다.)")
            return True
        time.sleep(1.0)

    _log("카카오페이 화면으로 넘어가지 못했습니다. 브라우저를 확인하세요.")
    return False


def book(schedule: dict, seat_loc_nos: list[str], movie_filter: str = "",
         screen_filter: str = "", count: int | None = None,
         port: int = 9222, pay: bool = False,
         expect_amount: int | None = None) -> bool:
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
        _log(f"{theater} {ymd} {hhmm} {movie} · 좌석 {len(seat_loc_nos)}개")

        tab.goto(BOOK_URL)
        time.sleep(1.5)

        # 극장 모달이 영화 모달을 덮고 있으므로 극장을 먼저 처리합니다.
        if _modal_open(tab, "지역별"):
            if not tab.click(
                _js_modal("지역별", "button, span, li, div, label", theater,
                          exact=True), wait=2.0
            ):
                _log(f"극장 '{theater}' 를 찾지 못했습니다.")
                return False
            tab.click(_js_modal("지역별", "button", "극장선택", exact=True),
                      wait=3.0, retries=2)

        # 영화 모달이 안 열려 있으면 '전체보기'로 엽니다.
        if not _modal_open(tab, "검색"):
            tab.click(js_by_text("전체보기", tags="button, a"), wait=2.0,
                      retries=1)

        if _modal_open(tab, "검색"):
            if not tab.click(
                _js_modal("검색", 'ul[class*="mvList"] button', movie),
                wait=3.5
            ):
                _log(f"영화 '{movie}' 를 목록에서 찾지 못했습니다.")
                return False

        if not tab.click(_js_date_item(ymd), wait=3.0):
            _log(f"날짜 {ymd} 를 선택하지 못했습니다.")
            return False

        if not tab.click(_js_showtime(hhmm), wait=4.5):
            _log(f"{hhmm} 회차를 선택하지 못했습니다 (매진되었을 수 있습니다).")
            return False

        for _ in range(10):
            if VISITOR_URL_PART in (tab.ev("location.href") or ""):
                break
            time.sleep(1.0)
        else:
            _log("인원 선택 화면으로 넘어가지 못했습니다.")
            return False

        if not tab.click(_js_visitor_count(count), wait=2.5):
            _log(f"인원 {count}명을 선택하지 못했습니다.")
            return False

        # 좌석 맵 열기. 반영이 늦을 때가 있어 열릴 때까지 확인합니다.
        for _ in range(6):
            if _seat_map_open(tab):
                break
            tab.click(_js_open_seat_map(), wait=2.5, retries=1)
            time.sleep(1.5)
        else:
            _log("좌석 맵을 열지 못했습니다.")
            return False

        # 인원이 2명 이상이면 첫 좌석만 누르면 CGV가 옆자리까지 자동으로
        # 잡아줍니다. 그래서 하나 누른 뒤 모자란 만큼만 추가로 누릅니다.
        picked: list[str] = []
        for loc in seat_loc_nos:
            if len(picked) >= count:
                break
            if not tab.click(_js_seat(loc), wait=1.8, retries=2):
                _log(f"좌석 {loc} 을 누르지 못했습니다 (이미 팔렸을 수 있습니다).")
                continue
            picked = _selected_locnos(tab)
            if len(picked) >= count:
                break

        if not picked:
            _log("좌석을 하나도 선택하지 못했습니다.")
            return False
        if len(picked) != count:
            _log(f"좌석 {len(picked)}/{count}석만 선택됐습니다. 화면에서 확인하세요.")

        ok, summary = verify_summary(tab, schedule, len(picked))
        _log(f"검증: {summary}")
        if not ok:
            _log("예매 내용이 요청과 달라 여기서 멈춥니다. 화면을 확인하세요.")
            return False

        if not pay:
            _log(f"좌석 {len(picked)}석 선택 완료 ({', '.join(picked)}). "
                 f"결제는 직접 진행하세요.")
            return True

        return _to_payment_qr(tab, schedule, len(picked), expect_amount)

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
