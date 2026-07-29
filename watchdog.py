"""모니터와 별개로 도는 감시자(워치독).

monitor.log가 일정 시간 갱신되지 않으면 = 모니터가 죽거나 멈춘 것으로 보고
하트비트 채널로 경보를 보낸다. 복구되면 복구 알림을 한 번 보낸다.
모니터 운영 관련 알림이므로 텔레그램·알림 웹훅으로는 보내지 않는다.
(launchd에서 몇 분마다 한 번씩 실행되는 것을 전제로 함.)
"""
import os
import time
from datetime import datetime

import yaml
import requests

PROJECT = os.path.dirname(os.path.abspath(__file__))

# 로그는 프로젝트 폴더가 아니라 ~/Library/Logs 아래에 둔다.
# 프로젝트가 Desktop/Documents 같은 보호된 위치에 있으면 launchd가
# 그곳의 기존 파일을 stdout으로 열지 못해 EX_CONFIG(78)로 죽는다.
LOG_DIR = os.path.expanduser("~/Library/Logs/cgv-monitor")
LOG = os.path.join(LOG_DIR, "monitor.log")
CONFIG = os.path.join(PROJECT, "config.yaml")
STATE = os.path.join(PROJECT, ".watchdog_alerted")  # 이미 경보 보냈는지 표시
STALE_SEC = 300  # 로그가 이 시간(초) 넘게 안 바뀌면 죽은 것으로 판단


def load_notif() -> dict:
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return c.get("notifications", {}) or {}


def send(notif: dict, dead: bool, detail: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 모니터 상태 관련 알림이므로 하트비트 채널로 보냅니다.
    # 경보에는 @here 멘션이 붙어서 조용한 채널에서도 놓치지 않습니다.
    webhook = (
        notif.get("heartbeat_webhook_url")
        or notif.get("discord_webhook_url")
        or ""
    )

    if not webhook:
        print(f"[{now}] (웹훅 없음) dead={dead} {detail}")
        return
    if dead:
        embed = {
            "title": "🔴 모니터 응답 없음",
            "description": detail,
            "color": 0xE74C3C,
            "footer": {"text": f"워치독 · {now}"},
        }
        content = "@here 예매 모니터가 멈춘 것 같습니다. 확인 필요."
    else:
        embed = {
            "title": "🟢 모니터 복구됨",
            "description": detail,
            "color": 0x2ECC71,
            "footer": {"text": f"워치독 · {now}"},
        }
        content = ""
    try:
        requests.post(
            webhook, json={"content": content, "embeds": [embed]}, timeout=10
        )
    except Exception as e:
        print(f"[{now}] 워치독 전송 실패: {e}")


def main():
    notif = load_notif()

    if not os.path.exists(LOG):
        dead, detail, age = True, "monitor.log 없음 — 모니터가 시작된 적 없음", None
    else:
        age = time.time() - os.path.getmtime(LOG)
        try:
            with open(LOG, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.read()[-500:]
        except Exception:
            tail = ""
        # 임무 완료(모든 타겟 오픈)로 정상 종료된 경우는 경보 대상 아님
        if "모든 타겟 오픈 감지 완료" in tail:
            if os.path.exists(STATE):
                os.remove(STATE)
            return
        dead = age > STALE_SEC
        detail = f"{int(age)}초 동안 폴링 로그 갱신 없음 (임계 {STALE_SEC}초)"

    already = os.path.exists(STATE)
    if dead and not already:
        send(notif, True, detail)
        open(STATE, "w").close()
    elif not dead and already:
        send(notif, False, "폴링 로그가 다시 갱신되고 있습니다.")
        os.remove(STATE)


if __name__ == "__main__":
    main()
