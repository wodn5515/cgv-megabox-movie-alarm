#!/bin/bash
# 예매 모니터 launchd 관리 스크립트
#
#   ./launchd/manage.sh install     plist 설치 + 시작 (모니터 + 워치독)
#   ./launchd/manage.sh uninstall   중지 + plist 제거
#   ./launchd/manage.sh start       시작
#   ./launchd/manage.sh stop        중지
#   ./launchd/manage.sh restart     재시작 (config.yaml 수정 후 반영용)
#   ./launchd/manage.sh status      동작 상태 확인
#   ./launchd/manage.sh logs        모니터 로그 실시간 보기
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 프로젝트 루트/파이썬 경로를 체크아웃 위치에서 자동으로 잡습니다.
# 덕분에 plist를 머신마다 고칠 필요 없이 어디서 받아도 그대로 돕니다.
PROJECT="$(cd "$HERE/.." && pwd)"
PYTHON="$PROJECT/venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs/cgv-monitor"
LABELS=(local.cgv-monitor local.cgv-watchdog)

loaded() {
    launchctl print "gui/$UID/$1" >/dev/null 2>&1
}

# bootout은 비동기입니다. 언로드가 끝나기 전에 bootstrap을 호출하면
# 라벨이 아직 남아 있어 "Bootstrap failed: 5: Input/output error"가 납니다.
boot_out() {
    launchctl bootout "gui/$UID/$1" 2>/dev/null || true
    for _ in $(seq 1 30); do
        loaded "$1" || return 0
        sleep 0.3
    done
    echo "경고: $1 언로드가 끝나지 않았습니다" >&2
    return 1
}

boot_in() {
    if loaded "$1"; then
        echo "이미 등록됨: $1 (restart를 쓰세요)" >&2
        return 1
    fi
    launchctl bootstrap "gui/$UID" "$AGENTS/$1.plist"
}

case "${1:-}" in
install)
    if [ ! -x "$PYTHON" ]; then
        echo "venv 파이썬이 없습니다: $PYTHON" >&2
        echo "먼저 'python3 -m venv venv && ./venv/bin/pip install -r requirements.txt'" >&2
        exit 1
    fi
    mkdir -p "$AGENTS" "$LOGDIR"
    for l in "${LABELS[@]}"; do
        boot_out "$l"
        # plist의 placeholder를 이 체크아웃의 실제 경로로 치환해 설치합니다.
        sed -e "s#__PYTHON__#$PYTHON#g" \
            -e "s#__WORKDIR__#$PROJECT#g" \
            -e "s#__LOGDIR__#$LOGDIR#g" \
            "$HERE/$l.plist" > "$AGENTS/$l.plist"
        boot_in "$l"
        echo "설치됨: $l"
    done
    echo
    echo "로그: $0 logs"
    ;;
logs)
    tail -f "$LOGDIR/monitor.log"
    ;;
uninstall)
    for l in "${LABELS[@]}"; do
        boot_out "$l"
        rm -f "$AGENTS/$l.plist"
        echo "제거됨: $l"
    done
    ;;
start)
    for l in "${LABELS[@]}"; do boot_in "$l" && echo "시작됨: $l"; done
    ;;
stop)
    for l in "${LABELS[@]}"; do boot_out "$l"; echo "중지됨: $l"; done
    ;;
restart)
    for l in "${LABELS[@]}"; do
        boot_out "$l"
        boot_in "$l"
        echo "재시작됨: $l"
    done
    ;;
status)
    for l in "${LABELS[@]}"; do
        if launchctl print "gui/$UID/$l" >/dev/null 2>&1; then
            pid=$(launchctl print "gui/$UID/$l" | awk '/^\tpid = /{print $3}')
            echo "$l: 등록됨 (pid ${pid:-없음 — 대기중이거나 주기 실행})"
        else
            echo "$l: 미등록"
        fi
    done
    ;;
*)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
