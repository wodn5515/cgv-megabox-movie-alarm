"""텔레그램 봇 설정 도우미.

준비:
    1. 텔레그램에서 @BotFather 를 찾아 /newbot 을 보내고 봇을 만듭니다.
       (이름 아무거나 → 아이디는 _bot 으로 끝나야 함)
    2. BotFather가 준 토큰을 복사합니다. 예: 123456789:AAH...
    3. 만든 봇과의 대화창을 열고 아무 메시지나 한 번 보냅니다.
       봇은 먼저 말을 걸 수 없어서 이 과정이 꼭 필요합니다.

사용법:
    python3 telegram_setup.py <봇토큰>

chat_id를 찾아 출력하고, 확인용 테스트 메시지를 보냅니다.
출력된 값을 config.yaml의 notifications.telegram 에 넣으면 됩니다.
"""
import sys

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def main(token: str):
    try:
        resp = requests.get(
            API.format(token=token, method="getUpdates"), timeout=15
        )
    except Exception as e:
        print(f"요청 실패: {e}")
        sys.exit(1)

    if resp.status_code == 401:
        print("토큰이 올바르지 않습니다. BotFather가 준 토큰을 다시 확인하세요.")
        sys.exit(1)
    if resp.status_code != 200:
        print(f"오류 {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)

    updates = resp.json().get("result") or []
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = (
                chat.get("title")
                or " ".join(
                    filter(None, [chat.get("first_name"), chat.get("last_name")])
                )
                or chat.get("username")
                or "이름 없음"
            )
            chats[chat["id"]] = name

    if not chats:
        print("대화 기록을 찾지 못했습니다.")
        print("텔레그램에서 만든 봇을 찾아 아무 메시지나 한 번 보낸 뒤 다시 실행하세요.")
        sys.exit(1)

    print(f"찾은 대화 {len(chats)}개:\n")
    for chat_id, name in chats.items():
        print(f"  chat_id: {chat_id}  ({name})")

    print("\nconfig.yaml에 아래를 넣으세요:\n")
    first = next(iter(chats))
    print("notifications:")
    print("  telegram:")
    print(f'    bot_token: "{token}"')
    print(f'    chat_id: "{first}"')

    for chat_id in chats:
        try:
            r = requests.post(
                API.format(token=token, method="sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": "🎬 예매 모니터 연결 완료. 이 대화로 알림이 옵니다.",
                },
                timeout=10,
            )
            status = "전송 성공" if r.status_code == 200 else f"실패 {r.status_code}"
        except Exception as e:
            status = f"실패 {e}"
        print(f"\n테스트 메시지 → {chat_id}: {status}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 telegram_setup.py <봇토큰>")
        sys.exit(1)
    main(sys.argv[1].strip())
