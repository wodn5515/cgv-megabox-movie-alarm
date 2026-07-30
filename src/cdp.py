"""이미 실행 중인 크롬에 CDP로 붙는 최소 클라이언트.

Playwright를 쓰지 않고 크롬 개발자 프로토콜을 직접 말합니다. 브라우저를
직접 띄우지 않으므로 `--enable-automation`이 붙지 않고, 따라서
navigator.webdriver도 false로 유지됩니다.

크롬 실행:
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
      --user-data-dir="$HOME/chrome-booking-profile" \\
      --remote-debugging-port=9222
"""
import json
import re
import time

import requests
import websocket

DEFAULT_PORT = 9222


class ChromeNotRunning(RuntimeError):
    pass


class Tab:
    def __init__(self, port: int = DEFAULT_PORT, url_contains: str | None = None):
        try:
            pages = [
                t for t in requests.get(
                    f"http://localhost:{port}/json", timeout=5
                ).json()
                if t.get("type") == "page"
            ]
        except Exception as e:
            raise ChromeNotRunning(
                f"localhost:{port} 에 붙을 수 없습니다. "
                f"크롬을 --remote-debugging-port={port} 로 띄웠는지 확인하세요. ({e})"
            ) from e
        if not pages:
            raise ChromeNotRunning("열린 탭이 없습니다.")
        if url_contains:
            pages = [p for p in pages if url_contains in p.get("url", "")] or pages

        self.info = pages[0]
        # Origin 헤더를 보내면 크롬이 403으로 막습니다. --remote-allow-origins=*
        # 로 크롬 방어를 푸는 대신 헤더를 보내지 않습니다.
        self.ws = websocket.create_connection(
            self.info["webSocketDebuggerUrl"], timeout=30, suppress_origin=True
        )
        self._id = 0

    def send(self, method: str, **params):
        self._id += 1
        n = self._id
        self.ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def ev(self, expression: str, await_promise: bool = False):
        r = self.send("Runtime.evaluate", expression=expression,
                      returnByValue=True, awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(
                f"JS 오류: {r['exceptionDetails'].get('text')}"
            )
        return r.get("result", {}).get("value")

    def goto(self, url: str, timeout: float = 20.0):
        self.send("Page.enable")
        self.send("Page.navigate", url=url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.1)
            try:
                if self.ev("document.readyState") == "complete":
                    return self.ev("location.href")
            except RuntimeError:
                continue  # 내비게이션 중 컨텍스트 교체
        return self.ev("location.href")

    def widen(self, width: int = 2560, height: int = 1400):
        """뷰포트를 넓힙니다.

        좌석맵이 가로로 넘치면 왼쪽 좌석이 화면 밖(x<0)에 놓여 클릭할 수
        없습니다. 미니맵 좌석은 3px라 눌러도 옆자리가 잡힙니다.
        창 크기와 무관하게 전체 좌석맵이 들어오도록 뷰포트를 키웁니다.
        """
        try:
            self.send("Emulation.setDeviceMetricsOverride", width=width,
                      height=height, deviceScaleFactor=1, mobile=False)
            return True
        except RuntimeError:
            return False

    def front(self):
        try:
            self.send("Page.bringToFront")
        except RuntimeError:
            pass

    def click_point(self, x: float, y: float):
        """실제 입력과 구분되지 않는 신뢰된 마우스 이벤트를 보냅니다."""
        self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for typ in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                      button="left", clickCount=1)

    def locate(self, js_finder: str) -> dict | None:
        """요소를 찾아 히트테스트까지 통과한 클릭 좌표를 돌려줍니다.

        js_finder는 요소(또는 요소 배열)를 반환하는 JS 표현식입니다.
        같은 텍스트를 가진 숨김 중복 요소가 흔해서, elementFromPoint로
        실제 클릭을 받는 요소인지 검증해야 합니다.
        """
        raw = self.ev(f"""
        (() => {{
          let found = (() => {{ {js_finder} }})();
          if (!found) return null;
          const list = Array.isArray(found) ? found : [found];
          for (const el of list) {{
            if (!el || !(el.offsetWidth || el.offsetHeight)) continue;
            // behavior:'instant' 가 없으면 부드러운 스크롤 때문에
            // 아직 도착하지 않은 위치의 좌표를 읽어 엉뚱한 곳을 누릅니다.
            el.scrollIntoView({{block: 'center', inline: 'center',
                                behavior: 'instant'}});
            const b = el.getBoundingClientRect();
            if (!b.width || !b.height) continue;
            const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
            if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) continue;
            const hit = document.elementFromPoint(cx, cy);
            if (hit && (hit === el || el.contains(hit) || hit.contains(el))) {{
              return JSON.stringify({{x: cx, y: cy}});
            }}
          }}
          return null;
        }})()
        """)
        return json.loads(raw) if raw else None

    def wait_for(self, js_bool: str, timeout: float = 8.0,
                 interval: float = 0.08) -> bool:
        """조건이 참이 될 때까지 짧은 간격으로 확인합니다.

        고정 sleep 대신 이것을 쓰면 대부분의 단계가 100~300ms에 끝납니다.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.ev(f"!!({js_bool})"):
                    return True
            except RuntimeError:
                pass  # 내비게이션 중 컨텍스트 교체
            time.sleep(interval)
        return False

    def click(self, js_finder: str, wait: float = 0.0, retries: int = 3,
              until: str | None = None, timeout: float = 8.0) -> bool:
        """요소를 찾아 클릭합니다.

        until에 JS 조건을 주면 그 조건이 참이 될 때까지 기다립니다.
        없으면 wait초만 쉽니다(가급적 until을 쓰세요).
        """
        for attempt in range(retries):
            spot = self.locate(js_finder)
            if spot:
                self.click_point(spot["x"], spot["y"])
                if not until:
                    if wait:
                        time.sleep(wait)
                    return True
                # 조건이 참이 되면 성공. 아니면 클릭이 먹지 않은 것으로 보고
                # 다시 시도합니다 (React가 아직 이벤트를 못 받는 경우가 있음).
                per_try = timeout / retries
                if self.wait_for(until, timeout=per_try):
                    return True
            # 아직 렌더링 안 됐을 수 있으니 짧게 기다렸다 재시도
            if attempt < retries - 1:
                time.sleep(0.25 * (attempt + 1))
        return False

    def type_into(self, css: str, value: str, wait: float = 0.6) -> bool:
        """입력란을 눌러 포커스를 준 뒤 실제 입력 이벤트로 값을 넣습니다.

        React가 관리하는 입력란은 value를 직접 대입해도 상태가 갱신되지
        않습니다. Input.insertText는 실제 타이핑과 같은 이벤트를 만듭니다.
        """
        spot = self.locate(
            f"const el = document.querySelector({json.dumps(css)});"
            " return el ? [el] : null;"
        )
        if not spot:
            return False
        self.click_point(spot["x"], spot["y"])
        time.sleep(0.2)
        # 기존 값 제거 후 입력
        self.ev(f"""
          (() => {{
            const el = document.querySelector({json.dumps(css)});
            if (el) {{ el.focus(); el.setSelectionRange(0, el.value.length); }}
          }})()
        """)
        self.send("Input.insertText", text=value)
        time.sleep(wait)
        # 휴대폰번호처럼 입력 중 자동 서식되는 칸이 있어(010-0000-0000)
        # 원문 비교가 아니라 영숫자만 남겨 비교합니다.
        actual = self.ev(
            f"(() => {{ const el = document.querySelector({json.dumps(css)});"
            f" return el ? el.value : ''; }})()"
        ) or ""
        keep = re.compile(r"[^0-9A-Za-z]")
        return keep.sub("", actual) == keep.sub("", value)

    def text(self) -> str:
        return self.ev("document.body.innerText") or ""

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def js_by_text(text: str, tags: str = "button, a, li, span, div",
               exact: bool = True) -> str:
    """정확히/부분적으로 텍스트가 일치하는 모든 후보를 반환하는 JS."""
    return f"""
      const norm = s => (s || '').trim().replace(/\\s+/g, ' ');
      const want = {json.dumps(text)};
      return [...document.querySelectorAll({json.dumps(tags)})]
        .filter(e => {'norm(e.innerText) === want' if exact
                      else 'norm(e.innerText).includes(want)'});
    """
