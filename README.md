# CGV IMAX 예매 오픈 모니터

특정 영화의 CGV IMAX 예매가 오픈되면 Discord로 알림을 보내주는 모니터링 도구.

## 동작 방식

1. CGV 내부 API를 주기적으로 폴링 (분당 최대 N회, 설정 가능)
2. 지정한 날짜/영화/상영관 조합의 스케줄이 등장하면 오픈으로 판단
3. Discord Webhook으로 `@here` 멘션과 함께 알림 전송
4. 모든 타겟이 오픈되면 자동 종료

## 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 설정

`config.yaml`을 편집합니다.

```yaml
max_requests_per_minute: 4

notifications:
  discord_webhook_url: "https://discord.com/api/webhooks/..."

targets:
  - name: "용산 IMAX 헤일메리 4/20"
    site_no: "0013"
    date: "20260420"
    screen_filter: "IMAX"
    movie_filter: "헤일메리"
```

| 필드 | 설명 |
|------|------|
| `name` | 알림에 표시될 이름 (자유롭게 작성) |
| `site_no` | CGV 영화관 코드 |
| `date` | 확인할 날짜 (YYYYMMDD) |
| `screen_filter` | 상영관 타입 필터 (IMAX, 4DX, SCREENX 등) |
| `movie_filter` | 영화명 키워드 (일부만 적어도 매칭) |

### 영화관 코드

| 코드 | 영화관 |
|------|--------|
| 0013 | 용산아이파크몰 |
| 0089 | 센텀시티 |
| 0056 | 강남 |
| 0059 | 영등포타임스퀘어 |
| 0074 | 왕십리 |
| 0112 | 여의도 |
| 0005 | 서면 |
| 0229 | 건대입구 |

## 실행

```bash
source venv/bin/activate
python3 main.py
```

## Discord 알림 설정

1. Discord에서 개인 서버 생성
2. 채널 설정 > 연동 > 웹후크 > 새 웹후크 > URL 복사
3. `config.yaml`의 `discord_webhook_url`에 붙여넣기
4. 서버 알림 설정을 "모든 메시지"로 변경하면 폰 푸시도 수신 가능
