"""알림 발송.

Discord Webhook과 텔레그램 봇으로 동시에 보냅니다.
둘 다 설정에서 비워두면 콘솔 출력만 됩니다.

    notifications:
      discord_webhook_url: "https://discord.com/api/webhooks/..."
      heartbeat_webhook_url: ""        # 비우면 위 웹훅으로 폴백
      telegram:
        bot_token: "123456:ABC..."
        chat_id: "123456789"
"""
from datetime import datetime

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(cfg: dict | None, text: str, silent: bool = False):
    """텔레그램으로 평문 메시지를 보냅니다.

    silent=True면 알림음 없이 조용히 도착합니다 (하트비트용).
    """
    cfg = cfg or {}
    token = cfg.get("bot_token", "")
    chat_id = cfg.get("chat_id", "")
    if not token or not chat_id:
        return

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": str(chat_id),
                "text": text,
                "disable_notification": silent,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[텔레그램 알림 실패] {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[텔레그램 알림 실패] {e}")


def _send_discord(webhook_url: str, payload: dict):
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        print(f"[Discord 알림 실패] {e}")


def _telegram_cfg(notif: dict) -> dict:
    return notif.get("telegram") or {}


def _heartbeat_webhook(notif: dict) -> str:
    """모니터 운영용(하트비트·종료·장애) 채널. 없으면 알림 웹훅으로 폴백."""
    return (
        notif.get("heartbeat_webhook_url")
        or notif.get("discord_webhook_url", "")
    )


def notify_console(target_name: str, changes: dict):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[{now}] 예매 오픈! {target_name}")
    print(f"  날짜: {changes['date']}")
    print(f"  영화: {', '.join(changes['movies'])}")
    print(f"  상영관: {', '.join(changes['screens'])}")
    print(f"  시간: {', '.join(changes['times'])}")
    print(f"{'='*60}")


def notify_open(notif: dict, target_name: str, changes: dict):
    """예매 오픈 감지를 알립니다."""
    if changes.get("complete"):
        # 모니터 운영 관련 메시지이므로 하트비트 채널로만 보냅니다.
        _send_discord(
            _heartbeat_webhook(notif),
            {"content": "**모든 타겟 오픈 감지 완료. 모니터링 종료.**"},
        )
        return

    times_str = ", ".join(changes["times"])
    movies = ", ".join(changes["movies"])
    screens = ", ".join(changes["screens"])

    _send_discord(notif.get("discord_webhook_url", ""), {"content": (
        f"@here\n"
        f"**예매 오픈! — {target_name}**\n"
        f"날짜: {changes['date']}\n"
        f"영화: {movies}\n"
        f"상영관: {screens}\n"
        f"시간: {times_str}"
    )})
    send_telegram(_telegram_cfg(notif), (
        f"🎬 예매 오픈! — {target_name}\n"
        f"날짜: {changes['date']}\n"
        f"영화: {movies}\n"
        f"상영관: {screens}\n"
        f"시간: {times_str}"
    ))


def notify_cancel(notif: dict, target_name: str, info: dict):
    """취소표(빈자리) 발생을 콘솔·Discord·텔레그램으로 알립니다.

    info = {
        "date","time","movie","screen","free","total","gained",
        "seats": ["F7~F8", ...],  # 좌석 조건이 있을 때만
        "url": 예매 페이지 링크,
    }
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seats_str = ", ".join(info.get("seats") or [])
    headline = f"{info['date']} {info['time']} · {info['movie']} · {info['screen']}"
    counts = f"잔여 {info['free']}/{info['total']}석 (신규 {info['gained']}석)"

    print(f"\n{'='*60}")
    print(f"[{now}] 취소표 발생! {target_name}")
    print(f"  {headline}")
    print(f"  {counts}")
    if seats_str:
        print(f"  좌석: {seats_str}")
    print(f"{'='*60}")

    discord_lines = [
        "@here",
        f"🎟️ **취소표 발생! — {target_name}**",
        headline,
        f"잔여 **{info['free']}**/{info['total']}석 (신규 {info['gained']}석)",
    ]
    telegram_lines = [
        f"🎟️ 취소표 발생! — {target_name}",
        headline,
        counts,
    ]
    if seats_str:
        discord_lines.append(f"좌석: **{seats_str}**")
        telegram_lines.append(f"좌석: {seats_str}")
    url = info.get("url", "")
    discord_lines.append(url)
    telegram_lines.append(url)

    _send_discord(
        notif.get("discord_webhook_url", ""),
        {"content": "\n".join(discord_lines)},
    )
    send_telegram(_telegram_cfg(notif), "\n".join(telegram_lines))


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}일")
    if h:
        parts.append(f"{h}시간")
    parts.append(f"{m}분")
    return " ".join(parts)


def _target_icon(t: dict) -> str:
    if t.get("mode") == "cancel":
        return "🎟️ 취소표 감시중"
    return "✅ 오픈 감지됨" if t["opened"] else "⏳ 대기중"


def notify_heartbeat(notif: dict, status: dict):
    """모니터 상태를 Discord 하트비트 채널로만 주기적으로 전송합니다.

    status = {
        "targets": [{"name","type","mode","date","screen","movie","opened"}...],
        "last_check": "YYYY-MM-DD HH:MM:SS" | None,
        "uptime_sec": float,
        "hits": int,
    }
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets = status.get("targets", [])
    remaining = sum(1 for t in targets if not t["opened"])
    last_check = status.get("last_check") or "아직 없음"
    uptime = _format_uptime(status.get("uptime_sec", 0))
    hits = status.get("hits", 0)

    print(f"[{now}] 하트비트: 정상 동작 중 "
          f"(감시 {len(targets)}개 / 대기중 {remaining}개 / 마지막 체크 {last_check})")

    lines = []
    for t in targets:
        icon = _target_icon(t)
        typ = t["type"].upper()
        detail = f"{t['date']} · {t['screen'] or '전체관'} · {t['movie'] or '전체영화'}"
        lines.append(f"{icon}\n`[{typ}]` **{t['name']}**\n　{detail}")
    targets_text = "\n\n".join(lines) if lines else "등록된 타겟 없음"

    webhook_url = _heartbeat_webhook(notif)
    embed = {
        "title": "🎬 예매 모니터 상태",
        "description": "1시간마다 자동으로 살아있음을 알립니다.",
        "color": 0x2ECC71,  # green
        "fields": [
            {"name": "상태", "value": "🟢 정상 동작 중", "inline": True},
            {"name": "가동 시간", "value": uptime, "inline": True},
            {"name": "대기중 타겟", "value": f"{remaining} / {len(targets)}개",
             "inline": True},
            {"name": "누적 알림", "value": f"{hits}회", "inline": True},
            {"name": "마지막 체크", "value": last_check, "inline": False},
            {"name": f"감시 대상 ({len(targets)}개)", "value": targets_text,
             "inline": False},
        ],
        "footer": {"text": f"하트비트 · {now}"},
    }
    # 하트비트는 Discord 하트비트 채널로만 보냅니다.
    # 텔레그램은 취소표·예매 오픈 같은 실제 알림 전용으로 비워둡니다.
    _send_discord(webhook_url, {"embeds": [embed]})
