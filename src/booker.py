"""
오픈 감지 시 브라우저를 띄워서 좌석 선택 직전까지 자동 이동합니다.
사용자가 좌석 선택 + 결제를 직접 완료합니다.
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = Path(__file__).parent.parent / "profiles"


async def book_cgv(target: dict, schedule: dict):
    """CGV 예매 페이지를 열고 스케줄 클릭 → 인원 선택까지 자동 진행합니다."""
    site_no = target["site_no"]
    date = target["date"]
    screen_filter = target.get("screen_filter", "")
    movie_filter = target.get("movie_filter", "")

    # 상영 시간 (API 응답에서 가져온 첫 타임)
    play_time = schedule.get("scnsrtTm", "")
    movie_name = schedule.get("movNm", "")
    screen_name = schedule.get("scnsNm", "")

    profile_path = PROFILE_DIR / "cgv"
    if not profile_path.exists():
        print("[CGV] 로그인 프로필이 없습니다. 먼저 python3 login.py cgv 를 실행하세요.")
        return

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(profile_path),
        headless=False,
        viewport={"width": 430, "height": 932},
        locale="ko-KR",
    )
    page = context.pages[0] if context.pages else await context.new_page()

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

            # 극장 이름으로 매칭 (site_no → 이름은 config name에서 추출)
            # 일단 첫 번째 검색 결과 클릭
            theater_names = {
                "0013": "용산아이파크몰",
                "0089": "센텀시티",
                "0056": "강남",
                "0059": "영등포",
                "0074": "왕십리",
                "0112": "여의도",
            }
            theater_name = theater_names.get(site_no, "")
            if theater_name:
                theater = page.locator(f"text={theater_name}").first
                await theater.click(timeout=5000)
                await asyncio.sleep(5)

        # 3. 날짜 선택 (해당 날짜 탭 클릭)
        date_display = f"{int(date[6:8])}"  # "21" from "20260421"
        date_btns = page.locator(f'button:has-text("{date_display}")')
        date_count = await date_btns.count()
        for i in range(date_count):
            btn_text = await date_btns.nth(i).inner_text()
            if date_display in btn_text.strip():
                await date_btns.nth(i).click()
                await asyncio.sleep(3)
                break

        # 4. 해당 영화의 해당 시간 클릭
        time_display = f"{play_time[:2]}:{play_time[2:]}"  # "1840" → "18:40"
        print(f"[CGV] 스케줄 클릭: {movie_name} {time_display} {screen_name}")

        time_btns = page.locator(f'button:has-text("{time_display}")')
        count = await time_btns.count()
        if count > 0:
            await time_btns.first.click()
            await asyncio.sleep(3)

            # 로그인 필요 팝업 → 확인
            confirm = page.get_by_role("button", name="확인")
            if await confirm.count() > 0:
                await confirm.click()
                await asyncio.sleep(5)

        print(f"[CGV] 브라우저에서 인원 선택 → 좌석 선택 → 결제를 진행하세요.")
        print(f"[CGV] 브라우저를 닫으면 자동화가 종료됩니다.")

        # 브라우저가 닫힐 때까지 대기
        await page.wait_for_event("close", timeout=0)
    except Exception as e:
        print(f"[CGV] 자동화 오류: {e}")
        print(f"[CGV] 브라우저를 닫으면 종료됩니다.")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    finally:
        await context.close()
        await pw.stop()


async def book_megabox(target: dict, schedule: dict):
    """메가박스 예매 페이지를 브라우저로 엽니다."""
    branch_no = target["branch_no"]
    date = target["date"]
    movie_name = schedule.get("movieNm", "")
    play_time = schedule.get("playStartTime", "")

    profile_path = PROFILE_DIR / "megabox"
    if not profile_path.exists():
        print("[메가박스] 로그인 프로필이 없습니다. 먼저 python3 login.py megabox 를 실행하세요.")
        return

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(profile_path),
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        url = f"https://www.megabox.co.kr/booking?brchNo1={branch_no}&playDe={date}"
        print(f"[메가박스] 예매 페이지: {url}")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(5)

        print(f"[메가박스] {movie_name} {play_time} 예매를 브라우저에서 진행하세요.")
        print(f"[메가박스] 브라우저를 닫으면 자동화가 종료됩니다.")

        await page.wait_for_event("close", timeout=0)
    except Exception as e:
        print(f"[메가박스] 자동화 오류: {e}")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
    finally:
        await context.close()
        await pw.stop()
