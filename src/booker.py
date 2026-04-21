"""
오픈 감지 시 브라우저를 띄워서 예매 페이지로 자동 이동합니다.
인원 선택과 좌석 선택까지 자동화하고, 결제는 사용자가 직접 진행합니다.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, Page

COOKIE_DIR = Path(__file__).parent.parent / "profiles"

THEATER_NAMES = {
    "0013": "용산아이파크몰",
    "0089": "센텀시티",
    "0056": "강남",
    "0059": "영등포",
    "0074": "왕십리",
    "0112": "여의도",
    "0005": "서면",
    "0229": "건대입구",
}

THEATER_REGION = {
    "0013": "서울",
    "0056": "서울",
    "0059": "서울",
    "0074": "서울",
    "0112": "서울",
    "0229": "서울",
    "0089": "부산/울산",
    "0005": "부산/울산",
}


def _load_cookies(site: str) -> list[dict] | None:
    cookie_file = COOKIE_DIR / f"{site}_cookies.json"
    if not cookie_file.exists():
        print(f"[{site.upper()}] 쿠키 파일이 없습니다. "
              f"먼저 python3 login.py {site} 를 실행하세요.")
        return None
    return json.loads(cookie_file.read_text())


async def _select_visitors(page: Page, booking: dict):
    """인원 선택 화면에서 인원수를 설정합니다."""
    adults = booking.get("adults", 1)
    teens = booking.get("teens", 0)
    children = booking.get("children", 0)

    # 인원선택 페이지 로드 대기
    await page.wait_for_selector('[id="number-choice-label"]', timeout=10000)
    await asyncio.sleep(1)

    # 일반(성인) 인원 설정 — 기본 0에서 + 버튼을 adults번 클릭
    # 첫 번째 NumberChoice가 일반
    number_sections = page.locator('[id="number-choice-label"]')
    section_count = await number_sections.count()

    if section_count > 0 and adults > 0:
        # 일반 섹션의 + 버튼
        first_section = number_sections.first.locator("xpath=..")
        plus_btn = first_section.locator('button[aria-label*="증가"], button:has-text("+")')
        for _ in range(adults):
            if await plus_btn.count() > 0:
                await plus_btn.first.click()
                await asyncio.sleep(0.3)

    # 청소년 (두 번째 섹션이 있으면)
    if teens > 0 and section_count > 1:
        teen_section = number_sections.nth(1).locator("xpath=..")
        plus_btn = teen_section.locator('button[aria-label*="증가"], button:has-text("+")')
        for _ in range(teens):
            if await plus_btn.count() > 0:
                await plus_btn.first.click()
                await asyncio.sleep(0.3)

    # 인원선택 확인 버튼
    await asyncio.sleep(1)
    confirm_btn = page.locator('button:has-text("인원선택")')
    if await confirm_btn.count() > 0:
        await confirm_btn.first.click()
        await asyncio.sleep(2)

    total = adults + teens + children
    print(f"[CGV] 인원 선택: 일반 {adults}명, 청소년 {teens}명 (총 {total}명)")


async def _select_seats(page: Page, booking: dict, total_count: int):
    """좌석 선택 화면에서 선호 좌석을 선택합니다."""
    preferred = booking.get("preferred_seats", [])
    if not preferred:
        print("[CGV] 선호 좌석 미설정 — 직접 선택하세요.")
        return

    # 좌석 맵 로드 대기
    await asyncio.sleep(3)

    selected = 0
    for seat_id in preferred:
        if selected >= total_count:
            break

        # 좌석 ID 파싱: "H12" → row="H", num="12"
        row = seat_id[0].upper()
        num = seat_id[1:]

        # 좌석 버튼 찾기 — aria-label이나 data 속성으로 매칭
        # CGV 좌석은 seatRowNm + seatNo로 식별
        seat = page.locator(
            f'[aria-label*="{row}열"][aria-label*="{num}"], '
            f'[data-seat-row="{row}"][data-seat-no="{num}"]'
        )
        if await seat.count() > 0:
            first = seat.first
            # 이미 선택됐거나 예매 불가인지 확인
            cls = await first.get_attribute("class") or ""
            if "disabled" in cls or "sold" in cls or "isDisabled" in cls:
                print(f"[CGV] 좌석 {seat_id}: 선택 불가 (이미 판매됨)")
                continue
            await first.click()
            selected += 1
            print(f"[CGV] 좌석 선택: {seat_id}")
            await asyncio.sleep(0.5)
        else:
            print(f"[CGV] 좌석 {seat_id}: 찾을 수 없음")

    if selected < total_count:
        print(f"[CGV] {total_count - selected}석 추가 선택 필요 — 직접 선택하세요.")


async def book_cgv(target: dict, schedule: dict, booking: dict | None = None):
    """CGV 예매: 스케줄 클릭 → 인원 선택 → 좌석 선택까지 자동 진행합니다."""
    cookies = _load_cookies("cgv")
    if cookies is None:
        return

    site_no = target["site_no"]
    date = target["date"]
    play_time = schedule.get("scnsrtTm", "")
    movie_name = schedule.get("movNm", "")
    screen_name = schedule.get("scnsNm", "")
    booking = booking or {}

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 430, "height": 932},
        locale="ko-KR",
    )
    await context.add_cookies(cookies)
    page = await context.new_page()

    try:
        # 1. 극장별 예매 페이지
        print(f"[CGV] 예매 페이지 로드: {site_no}, {date}")
        await page.goto(
            f"https://cgv.co.kr/cnm/movieBook/cinema?siteNo={site_no}",
            wait_until="networkidle",
            timeout=30000,
        )
        await asyncio.sleep(2)

        # 2. 극장 선택 (미선택 상태인 경우)
        edit_btn = page.locator(".cnms01510_editBtn__FNDH8")
        if await edit_btn.count() > 0:
            await edit_btn.click()
            await asyncio.sleep(2)

            region = THEATER_REGION.get(site_no, "서울")
            if region != "서울":
                region_tab = page.locator(f"text={region}").first
                if await region_tab.count() > 0:
                    await region_tab.click()
                    await asyncio.sleep(1)

            theater_name = THEATER_NAMES.get(site_no, "")
            if theater_name:
                theater = page.locator(f"text={theater_name}").first
                await theater.click(timeout=5000)
                await asyncio.sleep(5)

        # 3. 날짜 선택
        day = str(int(date[6:8]))
        date_btns = page.locator(f'button:has-text("{day}")')
        for i in range(await date_btns.count()):
            btn_text = await date_btns.nth(i).inner_text()
            if day in btn_text.strip():
                await date_btns.nth(i).click()
                await asyncio.sleep(3)
                break

        # 4. 해당 시간 클릭
        time_display = f"{play_time[:2]}:{play_time[2:]}"
        print(f"[CGV] 스케줄 클릭: {movie_name} {time_display} {screen_name}")

        time_btns = page.locator(f'button:has-text("{time_display}")')
        if await time_btns.count() > 0:
            await time_btns.first.click()
            await asyncio.sleep(3)

            # 로그인 필요 팝업
            confirm = page.get_by_role("button", name="확인")
            if await confirm.count() > 0:
                await confirm.click()
                await asyncio.sleep(5)

        # 5. 인원 선택
        if booking.get("adults") or booking.get("teens"):
            try:
                await _select_visitors(page, booking)
            except Exception as e:
                print(f"[CGV] 인원 선택 자동화 실패: {e}")

            # 6. 좌석 선택
            total = booking.get("adults", 0) + booking.get("teens", 0) + booking.get("children", 0)
            if booking.get("preferred_seats") and total > 0:
                try:
                    await _select_seats(page, booking, total)
                except Exception as e:
                    print(f"[CGV] 좌석 선택 자동화 실패: {e}")

        print("[CGV] 브라우저에서 남은 단계를 진행하세요.")
        print("[CGV] 브라우저를 닫으면 종료됩니다.")

        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    except Exception as e:
        print(f"[CGV] 자동화 오류: {e}")
        print("[CGV] 브라우저를 닫으면 종료됩니다.")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    finally:
        await browser.close()
        await pw.stop()


async def book_megabox(target: dict, schedule: dict, booking: dict | None = None):
    """메가박스 예매 페이지를 브라우저로 엽니다."""
    cookies = _load_cookies("megabox")
    if cookies is None:
        return

    branch_no = target["branch_no"]
    date = target["date"]
    movie_name = schedule.get("movieNm", "")
    play_time = schedule.get("playStartTime", "")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    await context.add_cookies(cookies)
    page = await context.new_page()

    try:
        url = (f"https://www.megabox.co.kr/booking"
               f"?brchNo1={branch_no}&playDe={date}")
        print(f"[메가박스] 예매 페이지: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(5)

        print(f"[메가박스] {movie_name} {play_time} 예매를 브라우저에서 진행하세요.")
        print("[메가박스] 브라우저를 닫으면 종료됩니다.")

        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    except Exception as e:
        print(f"[메가박스] 자동화 오류: {e}")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    finally:
        await browser.close()
        await pw.stop()
