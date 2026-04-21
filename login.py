"""
브라우저를 띄워서 로그인 후 쿠키를 저장합니다.

사용법:
    python3 login.py cgv
    python3 login.py megabox
"""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

COOKIE_DIR = Path(__file__).parent / "profiles"

SITES = {
    "cgv": {
        "login_url": "https://cgv.co.kr/mem/login",
        "check_url": "https://cgv.co.kr",
    },
    "megabox": {
        "login_url": "https://www.megabox.co.kr/member/login",
        "check_url": "https://www.megabox.co.kr",
    },
}


async def main(site: str):
    if site not in SITES:
        print(f"지원: {', '.join(SITES.keys())}")
        sys.exit(1)

    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = COOKIE_DIR / f"{site}_cookies.json"
    info = SITES[site]

    print(f"[{site.upper()}] 브라우저가 열립니다. 로그인 후 이 터미널에서 Enter를 누르세요.")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 430, "height": 932},
        locale="ko-KR",
    )
    page = await context.new_page()
    await page.goto(info["login_url"], wait_until="networkidle", timeout=30000)

    input("\n로그인 완료 후 Enter를 누르세요... ")

    # 쿠키 저장
    cookies = await context.cookies()
    cookie_file.write_text(json.dumps(cookies, indent=2, ensure_ascii=False))
    print(f"쿠키 저장 완료: {cookie_file} ({len(cookies)}개)")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else ""
    if not site:
        print("사용법: python3 login.py [cgv|megabox]")
        sys.exit(1)
    asyncio.run(main(site))
