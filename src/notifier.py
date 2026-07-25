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


def notify_heartbeat(webhook_url: str, status: dict):
    """모니터 상태를 보기 좋은 임베드로 주기적으로 전송합니다.

    status = {
        "targets": [{"name","type","date","screen","movie","opened"}...],
        "last_check": "YYYY-MM-DD HH:MM:SS" | None,
        "uptime_sec": float,
    }
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets = status.get("targets", [])
    remaining = sum(1 for t in targets if not t["opened"])
    last_check = status.get("last_check") or "아직 없음"
    uptime = _format_uptime(status.get("uptime_sec", 0))

    print(f"[{now}] 하트비트: 정상 동작 중 "
          f"(감시 {len(targets)}개 / 대기중 {remaining}개 / 마지막 체크 {last_check})")

    if not webhook_url:
        return

    lines = []
    for t in targets:
        icon = "✅ 오픈 감지됨" if t["opened"] else "⏳ 대기중"
        typ = t["type"].upper()
        lines.append(
            f"{icon}\n"
            f"`[{typ}]` **{t['name']}**\n"
            f"　{t['date']} · {t['screen'] or '전체관'} · {t['movie'] or '전체영화'}"
        )
    targets_text = "\n\n".join(lines) if lines else "등록된 타겟 없음"

    embed = {
        "title": "🎬 예매 모니터 상태",
        "description": "1시간마다 자동으로 살아있음을 알립니다.",
        "color": 0x2ECC71,  # green
        "fields": [
            {"name": "상태", "value": "🟢 정상 동작 중", "inline": True},
            {"name": "가동 시간", "value": uptime, "inline": True},
            {"name": "대기중 타겟", "value": f"{remaining} / {len(targets)}개",
             "inline": True},
            {"name": "마지막 체크", "value": last_check, "inline": False},
            {"name": f"감시 대상 ({len(targets)}개)", "value": targets_text,
             "inline": False},
        ],
        "footer": {"text": f"하트비트 · {now}"},
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"[Discord 하트비트 실패] {e}")
