"""좌석 위치 조건 매칭.

config의 seats 블록을 해석해 원하는 자리만 골라냅니다.

    seats:
      rows: ["F", "G", "H"]   # 감시할 열 (미지정 시 전체)
      seat_no: [4, 15]        # 좌석 번호 범위 [최소, 최대] (미지정 시 전체)
      min_consecutive: 2      # 나란히 붙어있어야 하는 최소 좌석 수 (기본 1)

연속 여부는 같은 열 안에서 좌석 번호가 1씩 이어지는지로 판단합니다.
통로를 사이에 둔 좌석은 번호가 이어져 있으면 연석으로 봅니다.
"""


def label(seat: dict) -> str:
    return f"{seat.get('seatRowNm', '')}{seat.get('seatNo', '')}"


def describe(groups: list[list[dict]]) -> list[str]:
    """알림에 쓸 좌석 묶음 표기를 만듭니다. 예: ['F7~F8', 'H12']"""
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(label(g[0]))
        else:
            out.append(f"{label(g[0])}~{label(g[-1])}")
    return out


def seat_range(seat_no) -> tuple[int | None, int | None]:
    """seat_no 설정을 (하한, 상한)으로 해석합니다.

    [16, 29] → 16번~29번
    [16]     → 16번 이상 (상한 없음)
    []/None  → 제한 없음

    값이 하나만 있는 설정을 쓰다가 터지지 않게 관대하게 받습니다.
    """
    if not seat_no:
        return None, None
    if isinstance(seat_no, (int, str)):
        seat_no = [seat_no]
    try:
        nums = [int(v) for v in seat_no]
    except (TypeError, ValueError):
        return None, None
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], None
    return min(nums[0], nums[1]), max(nums[0], nums[1])


def _seat_num(seat: dict) -> int | None:
    try:
        return int(seat.get("seatNo"))
    except (TypeError, ValueError):
        return None


def match(seats: list[dict], cfg: dict | None) -> list[list[dict]]:
    """조건에 맞는 좌석 묶음 목록을 반환합니다.

    min_consecutive가 1 이하이면 좌석 하나가 곧 하나의 묶음입니다.
    """
    cfg = cfg or {}
    rows = {str(r).upper() for r in (cfg.get("rows") or [])}
    seat_no = cfg.get("seat_no")
    try:
        min_run = int(cfg.get("min_consecutive") or 1)
    except (TypeError, ValueError):
        min_run = 1

    candidates = seats
    if rows:
        candidates = [
            s for s in candidates
            if (s.get("seatRowNm") or "").upper() in rows
        ]
    lo, hi = seat_range(seat_no)
    if lo is not None or hi is not None:
        candidates = [
            s for s in candidates
            if (n := _seat_num(s)) is not None
            and (lo is None or n >= lo)
            and (hi is None or n <= hi)
        ]

    if min_run <= 1:
        return [[s] for s in candidates]

    by_row: dict[str, list[tuple[int, dict]]] = {}
    for s in candidates:
        n = _seat_num(s)
        if n is None:
            continue
        by_row.setdefault(s.get("seatRowNm") or "", []).append((n, s))

    groups: list[list[dict]] = []
    for items in by_row.values():
        items.sort(key=lambda x: x[0])
        run = [items[0]]
        for prev, cur in zip(items, items[1:]):
            if cur[0] == prev[0] + 1:
                run.append(cur)
                continue
            if len(run) >= min_run:
                groups.append([s for _, s in run])
            run = [cur]
        if len(run) >= min_run:
            groups.append([s for _, s in run])
    return groups
