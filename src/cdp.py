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
            time.sleep(0.4)
            try:
                if self.ev("document.readyState") == "complete":
                    return self.ev("location.href")
            except RuntimeError:
                continue  # 내비게이션 중 컨텍스트 교체
        return self.ev("location.href")

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
            el.scrollIntoView({{block: 'center', inline: 'center'}});
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

    def click(self, js_finder: str, wait: float = 2.0,
              retries: int = 3) -> bool:
        """요소를 찾아 클릭합니다. 렌더링을 기다리며 재시도합니다."""
        for attempt in range(retries):
            spot = self.locate(js_finder)
            if spot:
                self.click_point(spot["x"], spot["y"])
                time.sleep(wait)
                return True
            time.sleep(1.0 + attempt)
        return False

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
