"""
브라우저를 띄워서 로그인 후 쿠키를 저장합니다.

사용법:
    python3 login.py cgv
    python3 login.py megabox
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = Path(__file__).parent / "profiles"

SITES = {
    "cgv": {
        "url": "https://cgv.co.kr/mem/login",
        "check": "https://cgv.co.kr",
    },
    "megabox": {
        "url": "https://www.megabox.co.kr/member/login",
        "check": "https://www.megabox.co.kr",
    },
}


async def main(site: str):
    if site not in SITES:
        print(f"지원: {', '.join(SITES.keys())}")
        sys.exit(1)

    profile_path = PROFILE_DIR / site
    profile_path.mkdir(parents=True, exist_ok=True)

    info = SITES[site]
    print(f"[{site.upper()}] 브라우저가 열립니다. 로그인 후 이 터미널에서 Enter를 누르세요.")

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(profile_path),
        headless=False,
        viewport={"width": 430, "height": 932},
        locale="ko-KR",
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(info["url"], wait_until="networkidle", timeout=30000)

    # 사용자가 로그인 완료할 때까지 대기
    input("\n로그인 완료 후 Enter를 누르세요... ")

    # 로그인 확인
    await page.goto(info["check"], wait_until="networkidle", timeout=15000)
    await asyncio.sleep(2)
    print(f"쿠키 저장 완료: {profile_path}")

    await context.close()
    await pw.stop()


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else ""
    if not site:
        print("사용법: python3 login.py [cgv|megabox]")
        sys.exit(1)
    asyncio.run(main(site))
