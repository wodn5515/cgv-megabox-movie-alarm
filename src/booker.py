"""
오픈 감지 시 브라우저를 띄워서 예매 페이지로 자동 이동합니다.
사용자가 좌석 선택 + 결제를 직접 완료합니다.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

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

# 극장 → 지역 탭 매핑 (극장 선택 모달에서 해당 지역 먼저 클릭)
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


async def book_cgv(target: dict, schedule: dict):
    """CGV 예매 페이지를 열고 스케줄 클릭까지 자동 진행합니다."""
    cookies = _load_cookies("cgv")
    if cookies is None:
        return

    site_no = target["site_no"]
    date = target["date"]
    play_time = schedule.get("scnsrtTm", "")
    movie_name = schedule.get("movNm", "")
    screen_name = schedule.get("scnsNm", "")

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

        # 2. 극장 선택
        edit_btn = page.locator(".cnms01510_editBtn__FNDH8")
        if await edit_btn.count() > 0:
            await edit_btn.click()
            await asyncio.sleep(2)

            # 지역 선택 (서울 외 지역이면 해당 지역 탭 클릭)
            region = THEATER_REGION.get(site_no, "서울")
            if region != "서울":
                region_tab = page.locator(f"text={region}").first
                if await region_tab.count() > 0:
                    await region_tab.click()
                    await asyncio.sleep(1)

            # 극장 클릭
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

            # 로그인 필요 팝업이 뜨면 확인
            confirm = page.get_by_role("button", name="확인")
            if await confirm.count() > 0:
                await confirm.click()
                await asyncio.sleep(5)

        print("[CGV] 브라우저에서 인원 선택 → 좌석 선택 → 결제를 진행하세요.")
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


async def book_megabox(target: dict, schedule: dict):
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
