import sys
import yaml

from src.monitor import ScheduleMonitor


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config.get("targets"):
        print("config.yaml에 모니터링 대상이 없습니다.")
        sys.exit(1)

    monitor = ScheduleMonitor(config)
    try:
        monitor.run()
    except KeyboardInterrupt:
        print("\n모니터링 종료")


if __name__ == "__main__":
    main()
