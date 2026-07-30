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


def _restriction_modal(tab) -> str:
    """좌석 클릭 후 뜨는 예매 제한 안내를 읽습니다. 없으면 빈 문자열.

    휠체어 전용석처럼 목록에는 '판매 가능'으로 나오지만 실제로는 일반
    고객이 살 수 없는 좌석이 있습니다. 좌석 조회 API는 이런 좌석도
    stkndCd=01(일반석)로 내려주므로 화면 안내로만 알 수 있습니다.
    """
    return tab.ev(r"""
      (() => {
        const m = [...document.querySelectorAll('.cgv-modal.active')]
          .filter(e => e.offsetWidth || e.offsetHeight)
          .find(e => /제한|불가|이용하실 수 없/.test(e.innerText || ''));
        return m ? (m.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80) : '';
      })()
    """) or ""


def _dismiss_restriction(tab) -> bool:
    """제한 안내 모달을 닫습니다."""
    return tab.click(r"""
      const norm = s => (s || '').trim().replace(/\s+/g, ' ');
      const m = [...document.querySelectorAll('.cgv-modal.active')]
        .filter(e => e.offsetWidth || e.offsetHeight)
        .find(e => /제한|불가|이용하실 수 없/.test(e.innerText || ''));
      if (!m) return null;
      return [...m.querySelectorAll('button, a')]
        .filter(e => ['확인', '닫기'].includes(norm(e.innerText)));
    """, until="!document.querySelector('.cgv-modal.active')", timeout=4,
        retries=2)


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
        _log(f"{theater} {ymd} {hhmm} {movie} · 좌석 {len(seat_loc_nos)}개")

        tab.goto(BOOK_URL)
        time.sleep(1.5)

        # 극장을 먼저 고릅니다. 극장 모달이 영화 모달을 덮고 있어서
        # 순서를 바꾸면 영화를 누를 수 없습니다.
        #
        # 자주가는 극장에 등록해두어도 이 경로로 들어오면 매번 미선택
        # 상태이므로, 들어올 때마다 골라야 합니다.
        # ('지역별'은 극장 모달에만 있는 문구입니다. '검색'은 영화 모달에도
        #  있어서 모달을 특정하는 마커로 쓸 수 없습니다.)
        if not _theater_chosen(tab):
            if not tab.click(
                _js_modal("지역별", "button, span, li, div, label", theater,
                          exact=True), until=JS_CONFIRM_BTN, timeout=6
            ):
                _log(f"극장 '{theater}' 를 찾지 못했습니다.")
                return False
            if not tab.click(_js_modal("지역별", "button", "극장선택",
                                       exact=True),
                             until=JS_THEATER_OK, timeout=8, retries=2):
                _log(f"극장 '{theater}' 가 선택되지 않았습니다.")
                return False

        # 영화 목록이 안 열려 있으면 '전체보기'로 엽니다.
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

        if not tab.click(_js_date_item(ymd), until=JS_SHOWTIMES, timeout=8):
            _log(f"날짜 {ymd} 를 선택하지 못했습니다.")
            return False

        # URL은 페이지가 조작 가능해지기 전에 바뀝니다. 인원 버튼이 실제로
        # 렌더될 때까지 기다려야 다음 클릭이 먹습니다.
        js_visitor_page = (
            f"location.href.includes({json.dumps(VISITOR_URL_PART)}) && "
            f"[...document.querySelectorAll('button[aria-label$=\"선택\"]')]"
            f".length > 0"
        )
        if not tab.click(_js_showtime(hhmm), until=js_visitor_page, timeout=12):
            _log(f"{hhmm} 회차를 선택하지 못했습니다 (매진되었을 수 있습니다).")
            return False

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

        # 좌석 맵 열기. 반영이 늦을 때가 있어 열릴 때까지 확인합니다.
        for _ in range(6):
            if _seat_map_open(tab):
                break
            tab.click(_js_open_seat_map(), until=JS_SEATMAP_OPEN, timeout=3,
                      retries=1)
        else:
            _log("좌석 맵을 열지 못했습니다.")
            return False

        # 인원이 2명 이상이면 첫 좌석만 누르면 CGV가 옆자리까지 자동으로
        # 잡아줍니다. 그래서 하나 누른 뒤 모자란 만큼만 추가로 누릅니다.
        picked: list[str] = []
        blocked: set[str] = set()   # 예매 제한으로 쓸 수 없는 좌석
        for loc in seat_loc_nos:
            if len(picked) >= count:
                break
            # '아무 좌석이나 선택됨'이 아니라 '이 좌석이 선택됨'을 봐야 합니다.
            # 제한 좌석은 선택 표시가 남아 있어서 전역 조건에 속습니다.
            js_seat_on = (
                f"[...document.querySelectorAll("
                f"'button[data-seatlocno={json.dumps(loc)}]')]"
                f".some(e=>/select|choice|on\\b|active/i.test("
                f"(e.className||'').toString()))"
            )
            if not tab.click(_js_seat(loc), until=js_seat_on, timeout=4,
                             retries=2):
                _log(f"좌석 {loc} 을 누르지 못했습니다 (이미 팔렸을 수 있습니다).")
                continue
            # 휠체어 전용석 등 예매 제한 좌석이면 안내가 뜹니다.
            # 안내를 닫고 다음 후보 좌석으로 넘어갑니다.
            if (msg := _restriction_modal(tab)):
                _log(f"좌석 {loc} 예매 제한 — {msg}")
                _dismiss_restriction(tab)
                blocked.add(loc)
                # 제한 좌석이 선택 상태로 남으면 인원 정원을 먹어서 다음
                # 좌석을 누를 수 없습니다. 다시 눌러 해제합니다.
                if tab.ev(f"!!({js_seat_on})"):
                    tab.click(_js_seat(loc), until=f"!({js_seat_on})",
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
        if not pay:
            _log("결제수단 화면까지 진행했습니다. 결제는 직접 하세요.")
            return True

        return _request_kakao_pay(tab, kakao_identity(kakao), confirm_timeout)

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
