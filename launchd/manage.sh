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
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$HOME/Library/Logs/cgv-monitor"
LABELS=(local.cgv-monitor local.cgv-watchdog)

boot_out() {
    launchctl bootout "gui/$UID/$1" 2>/dev/null || true
}

boot_in() {
    launchctl bootstrap "gui/$UID" "$AGENTS/$1.plist"
}

case "${1:-}" in
install)
    mkdir -p "$AGENTS" "$LOGDIR"
    for l in "${LABELS[@]}"; do
        boot_out "$l"
        cp "$HERE/$l.plist" "$AGENTS/$l.plist"
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
