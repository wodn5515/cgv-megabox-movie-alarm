from datetime import datetime
import requests


def notify_console(target_name: str, changes: dict):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[{now}] 예매 오픈! {target_name}")
    print(f"  날짜: {changes['date']}")
    print(f"  영화: {', '.join(changes['movies'])}")
    print(f"  상영관: {', '.join(changes['screens'])}")
    print(f"  시간: {', '.join(changes['times'])}")
    print(f"{'='*60}")


def notify_discord(webhook_url: str, target_name: str, changes: dict):
    if not webhook_url:
        return

    if changes.get("complete"):
        msg = "**모든 타겟 오픈 감지 완료. 모니터링 종료.**"
    else:
        times_str = ", ".join(changes["times"])
        msg = (
            f"@here\n"
            f"**예매 오픈! — {target_name}**\n"
            f"날짜: {changes['date']}\n"
            f"영화: {', '.join(changes['movies'])}\n"
            f"상영관: {', '.join(changes['screens'])}\n"
            f"시간: {times_str}"
        )

    try:
        requests.post(webhook_url, json={"content": msg}, timeout=10)
    except Exception as e:
        print(f"[Discord 알림 실패] {e}")
