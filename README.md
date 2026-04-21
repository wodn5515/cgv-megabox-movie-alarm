# 영화 예매 오픈 모니터

CGV / 메가박스의 특정 영화 예매 오픈을 감지하여 Discord로 알림을 보내주는 도구.

## 동작 방식

1. CGV / 메가박스 API를 주기적으로 폴링 (분당 최대 N회, 설정 가능)
2. 지정한 날짜/영화/상영관 조합의 스케줄이 등장하면 오픈으로 판단
3. Discord Webhook으로 `@here` 멘션과 함께 알림 전송
4. 모든 타겟이 오픈되면 자동 종료

## 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

## 설정

`config.yaml`을 편집합니다.

```yaml
max_requests_per_minute: 4

notifications:
  discord_webhook_url: "https://discord.com/api/webhooks/..."

targets:
  # CGV
  - name: "용산 IMAX 헤일메리 4/20"
    type: cgv
    site_no: "0013"
    date: "20260420"
    screen_filter: "IMAX"
    movie_filter: "헤일메리"

  # 메가박스
  - name: "코엑스 DOLBY 헤일메리 5/1"
    type: megabox
    branch_no: "1351"
    date: "20260501"
    screen_filter: "DOLBY"
    movie_filter: "헤일메리"
```

| 필드 | 설명 |
|------|------|
| `name` | 알림에 표시될 이름 (자유롭게 작성) |
| `type` | `cgv` 또는 `megabox` |
| `site_no` | CGV 영화관 코드 |
| `branch_no` | 메가박스 지점 코드 |
| `date` | 확인할 날짜 (YYYYMMDD) |
| `screen_filter` | 상영관 타입 (IMAX, DOLBY, 4DX, SCREENX 등) |
| `movie_filter` | 영화명 키워드 (일부만 적어도 매칭) |

### CGV 영화관 코드

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

### 메가박스 지점 코드 (DOLBY CINEMA)

| 코드 | 지점 |
|------|------|
| 1351 | 코엑스 |
| 0019 | 남양주현대아울렛스페이스원 |
| 7011 | 대구신세계(동대구) |
| 0028 | 대전신세계아트앤사이언스 |
| 4062 | 송도(트리플스트리트) |
| 0052 | 수원AK플라자(수원역) |
| 0020 | 안성스타필드 |
| 4651 | 하남스타필드 |

## 실행

```bash
source venv/bin/activate
python3 main.py
```

## 자동 예매 (선택)

오픈 감지 시 브라우저를 자동으로 띄워서 예매 페이지로 이동합니다.
좌석 선택과 결제는 직접 진행해야 합니다.

### 1. 로그인

```bash
# CGV 로그인 (브라우저가 열림, 로그인 후 Enter)
python3 login.py cgv

# 메가박스 로그인
python3 login.py megabox
```

### 2. Playwright 설치

```bash
playwright install chromium
```

### 3. auto_book 활성화

```yaml
auto_book: true
```

오픈 감지 → Discord 알림 + 브라우저 자동 오픈 → 사용자가 좌석 선택 + 결제 완료

## Discord 알림 설정

1. Discord에서 개인 서버 생성
2. 채널 설정 > 연동 > 웹후크 > 새 웹후크 > URL 복사
3. `config.yaml`의 `discord_webhook_url`에 붙여넣기
4. 서버 알림 설정을 "모든 메시지"로 변경하면 폰 푸시도 수신 가능
