# 카카오톡 투표 봇 + 관리용 REST API
# uv run 시 아래 의존성을 자동 설치합니다 (pip install 불필요)
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "websockets>=15.0", "fastapi>=0.110", "uvicorn>=0.29"]
# ///
"""
카톡 명령어 (앞의 ! 는 생략 가능, 단체방 멘션도 인식)
  !도움말                  봇 사용법
  !이름 홍길동             내 이름 등록 (카카오가 닉네임을 보내주지 않아 최초 1회 필요)
  !투표 261017 달월        '261017 달월' 참석/불참 투표 생성 (다음주 수요일 23시 마감)
  !참석 / !불참            투표하기
  !멤버                    참석 인원 확인
  !스쿼드                  스쿼드 구성 페이지 링크 안내
  !수정 장소 강남          진행중인 투표 장소 변경
  !수정 날짜 261024        진행중인 투표 날짜 변경 (마감일도 다시 계산)
  !수정 멤버 A B           참석 멤버 A를 B로 변경
  !삭제                    진행중인 투표 삭제

REST API (.env 에 API_KEY 를 넣으면 켜짐, 문서: http://서버:8000/docs)
  모든 요청에 헤더  X-API-Key: <API_KEY>  필요
"""
import asyncio
import json
import os
import secrets
import shlex
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
POLLS_FILE = HERE / "polls.json"
SCHEDULE_FILE = HERE / "schedule.json"  # 자동 투표를 이미 만든 날짜 기록 (재시작해도 중복 방지)

# 매주 토요일 10시에 자동으로 올리는 정기 투표 (날짜는 만드는 날로부터 다음 주 토요일)
WEEKLY_POLL = {
    "room": "bg2_YWwZGRLkPjp4l8ugIkYR8_tJG-zvoVAOew",
    "place": "월곶",
    "desc": "정기풋살",
    "weekday": 5,  # 토요일 (월=0)
    "hour": 10,    # 오전 10시
    "minute": 0,

}
USERS_FILE = HERE / "users.json"
LOG_FILE = HERE / "events.log"
DEBUG = os.environ.get("DEBUG") == "1"

GW_URL = "wss://bot-gateway.kakao.com/gateway"
CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"

HELP_TEXT = """📖 위켄봇 사용법
모든 명령은 @위켄봇 으로 시작해요.

[처음 한 번]
· 이름 홍길동 — 내 이름 등록

[투표]
· 투표 — 진행중인 투표 보기
· 참석 / 불참 — 최근 투표에 응답
· 용병 홍길동 — 최근 투표에 용병 추가 (마감 후에도 가능)
· 멤버 — 최근 투표 참석 멤버 보기 (마감 후)
※ 마감: 다음 주 수요일 23:00

[관리]
· 참석 홍길동 / 불참 홍길동 — 참석·불참 바꾸기 (마감 후에도 가능)
· 장소 달월 — 장소 바꾸기
· 날짜 261031 — 날짜 바꾸기
· 멤버 김기항 김민규 — 김기항 불참, 김민규 참석
· 투표종료 — 지금 마감
· 투표삭제 — 투표 삭제
· 투표조회 — 투표 목록

[스쿼드]
· 스쿼드 — 스쿼드 페이지 링크
· 스쿼드만들기 — 참석자로 팀을 새로 짜서 페이지에 반영
· 명단 — 전체 명단 보기
· 포지션 김기항 ST/LB — 선호 포지션 바꾸기 (GitHub 반영)\n· 등급 김민규 — 등급 조회\n· 등급 김민규 수비 A — 항목 수정\n· 등급 김민규 B B A A B B — 6개 전체 등록 (GitHub 반영)\n· 신규 김민규 BBCCBA ST/CB — 신규 선수 등록 (등급+포지션)"""

VOTE_HINT = "👉 참석 또는 불참 을 입력해주세요."
def vote_buttons(poll: dict) -> list[dict]:
    """이 투표 전용 버튼. 누르면 '참석 #id' 가 전송돼 해당 투표에 기록된다."""
    return [
        {"label": "참석", "action": "message", "messageText": f"참석 #{poll['no']}"},
        {"label": "불참", "action": "message", "messageText": f"불참 #{poll['no']}"},
    ]
def poll_reply(poll: dict, now: datetime | None, text: str) -> tuple[str, list]:
    """진행중이면 문구 + 투표 카드(참석/불참 버튼), 마감됐으면 문구만(스쿼드 버튼은 유지)."""
    if is_closed(poll, now):
        return text, squad_buttons(poll, now)
    return f"{text}\n{poll_summary(poll)}", vote_buttons(poll)


def squad_buttons(poll: dict, now: datetime | None) -> list[dict]:
    """마감된 투표에는 '스쿼드 만들기' 버튼을 붙인다 (누르면 @위켄봇 스쿼드만들기 입력)."""
    if not is_closed(poll, now):
        return []
    return [{"label": "스쿼드 만들기", "action": "message", "messageText": "@위켄봇 스쿼드만들기"}]


SIDES = {"참석": "attend", "불참": "absent"}


# ============================ 저장소 ============================
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 쓰는 도중 꺼져도 파일이 깨지지 않도록


# ============================ 날짜 계산 ============================
def parse_date(s: str) -> datetime | None:
    """'261017' → 2026-10-17"""
    try:
        return datetime.strptime(s, "%y%m%d").replace(tzinfo=KST)
    except (ValueError, TypeError):
        return None


def calc_deadline(base: datetime) -> datetime:
    """투표를 만든 날(base) 기준 다음주 수요일 23:00 (월요일 시작 기준)"""
    next_monday = base - timedelta(days=base.weekday()) + timedelta(days=7)
    return (next_monday + timedelta(days=2)).replace(hour=23, minute=0, second=0, microsecond=0)


def fmt_deadline(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d:%m/%d}({'월화수목금토일'[d.weekday()]}) {d:%H:%M}"


# ============================ 투표 로직 (카톡·API 공용) ============================
# 투표 기록: poll["attend"] / poll["absent"] = {사용자키: 이름}  (입력 순서 유지)
class PollError(Exception):
    """사용자에게 그대로 보여줄 오류 메시지. code 는 API 응답 상태코드."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message, self.code = message, code


NO_POLL = "진행중인 투표가 없어요. 투표 날짜 장소 로 만들어주세요. 예) 투표 261017 달월"


def now_kst() -> datetime:
    return datetime.now(KST)


def is_closed(poll: dict, now: datetime | None = None) -> bool:
    return bool(poll.get("closed_at")) or (now or now_kst()) > datetime.fromisoformat(poll["deadline"])


def new_poll_id(polls: dict, room: str) -> str:
    used = {p.get("id") for p in polls.get(room) or []}
    while True:
        pid = secrets.token_hex(2)
        if pid not in used:
            return pid


def load_polls() -> dict:
    """{카톡방: [투표1, 투표2, ...]} (오래된 순). 예전 형식은 자동 변환하고, 투표마다 짧은 id(#xxxx)를 붙인다."""
    raw = _load(POLLS_FILE)
    polls, changed = {}, False
    for r, v in raw.items():
        lst = v if isinstance(v, list) else [v]
        if not isinstance(v, list):
            changed = True
        for p in lst:
            if not p.get("id"):
                p["id"] = new_poll_id({r: lst}, r)
                changed = True
            if not p.get("no"):  # 투표 번호: 한 번 붙으면 투표를 지워도 바뀌지 않는다
                p["no"] = next_no(lst)
                changed = True
        polls[r] = lst
    if changed:
        _save(POLLS_FILE, polls)
    return polls


def next_no(lst: list) -> int:
    return max([p.get("no") or 0 for p in lst] + [0]) + 1


def find_by_no(polls: dict, room: str, no: int) -> dict:
    for p in polls.get(room) or []:
        if p.get("no") == no:
            return p
    raise PollError(f"{no}번 투표가 없어요. 투표조회 로 번호를 확인해주세요.", 404)


def find_poll_by_id(polls: dict, room: str, pid: str) -> dict:
    """'#3' / '3' (투표 번호) 또는 예전 버튼의 짧은 id 로 투표를 찾는다."""
    key = pid.lower().lstrip("#")
    for p in polls.get(room) or []:
        if p.get("id") == key:
            return p
    if key.isdigit():
        return find_by_no(polls, room, int(key))
    raise PollError(f"#{pid} 투표를 찾을 수 없어요. 투표조회 로 확인해주세요.", 404)


def _require(polls: dict, room: str, now: datetime | None = None) -> dict:
    """현재 투표: 가장 최근 진행중 투표. 진행중인 투표가 없으면 가장 최근 투표(마감된 것)."""
    lst = polls.get(room) or []
    if not lst:
        raise PollError(NO_POLL, 404)
    opened = [p for p in lst if not is_closed(p, now)]
    return opened[-1] if opened else lst[-1]


def _retitle(poll: dict) -> None:
    poll["title"] = f"{poll['date']} {poll['place']}"


def get_poll(room: str) -> dict:
    return _require(load_polls(), room)


def list_polls() -> dict:
    return load_polls()


def register_name(user_key: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise PollError("이름을 입력해주세요.")
    users = _load(USERS_FILE)
    if any(n == name and k != user_key for k, n in users.items()):  # 동명이인 등록 불가
        raise PollError(f"'{name}' 이름은 이미 다른 사람이 쓰고 있어요. 다른 이름을 입력해주세요.", 409)
    users[user_key] = name
    _save(USERS_FILE, users)
    polls = load_polls()  # 이미 투표했다면 명단의 이름도 갱신
    for poll in (p for lst in polls.values() for p in lst):
        for side in ("attend", "absent"):
            if user_key in poll[side]:
                poll[side][user_key] = name
    _save(POLLS_FILE, polls)
    return name


def has_open_poll(polls: dict, room: str, now: datetime | None = None) -> bool:
    return any(not is_closed(p, now) for p in polls.get(room) or [])


def create_poll(room: str, date_str: str, place: str, creator: str,
                now: datetime | None = None, desc: str = "") -> dict:
    date = parse_date(date_str)
    if not date:
        raise PollError("날짜는 YYMMDD 형식으로 입력해주세요. 예) 261017")
    place = (place or "").strip()
    if not place:
        raise PollError("장소를 입력해주세요.")
    polls = load_polls()
    if has_open_poll(polls, room, now):  # 진행중인 투표가 하나라도 있으면 새로 만들지 않는다
        raise PollError("진행중인 투표가 있어요. 먼저 투표를 마감(투표종료)하거나 삭제한 뒤 만들어 주세요.", 409)
    poll = {
        "date": date_str,
        "place": place,
        "id": new_poll_id(polls, room),
        "no": next_no(polls.get(room) or []),
        "desc": (desc or "").strip(),
        "title": "",
        "created": (now or now_kst()).isoformat(),
        "deadline": calc_deadline(now or now_kst()).isoformat(),
        "attend": {},
        "absent": {},
        "creator": creator,
    }
    _retitle(poll)
    polls.setdefault(room, []).append(poll)
    _save(POLLS_FILE, polls)
    return poll


def cast_vote(room: str, user_key: str, name: str, side: str,
              now: datetime | None = None, poll_id: str | None = None) -> dict:
    """side: 'attend' | 'absent'. poll_id 가 없으면 진행중인 가장 최근 투표에 기록한다."""
    polls = load_polls()
    poll = find_poll_by_id(polls, room, poll_id) if poll_id else _require(polls, room, now)
    if is_closed(poll, now):
        raise PollError(f"'{poll['title']}' 투표는 마감되었습니다.", 409)
    other = "absent" if side == "attend" else "attend"
    poll[other].pop(user_key, None)
    poll[side][user_key] = name
    _save(POLLS_FILE, polls)
    return poll


def set_attendance(room: str, name: str, side: str, no: int | None = None) -> dict:
    """투표의 참석/불참을 바꾼다 (번호 생략 시 가장 최근 투표). 마감 후에도 가능하다. 명단에 없으면 새로 넣는다."""
    polls = load_polls()
    if no is None:
        lst = polls.get(room) or []
        if not lst:
            raise PollError(NO_POLL, 404)
        poll = lst[-1]
    else:
        poll = find_by_no(polls, room, no)
    other = "absent" if side == "attend" else "attend"
    key = (next((k for k, n in poll["attend"].items() if n == name), None)
           or next((k for k, n in poll["absent"].items() if n == name), None))
    if key is None:
        users = _load(USERS_FILE)
        key = next((k for k, n in users.items() if n == name), f"manual:{name}")
    poll[other].pop(key, None)
    poll[side][key] = name
    _save(POLLS_FILE, polls)
    return poll


def remove_vote(room: str, name: str) -> dict:
    polls = load_polls()
    poll = _require(polls, room)
    found = False
    for side in ("attend", "absent"):
        for k in [k for k, n in poll[side].items() if n == name]:
            del poll[side][k]
            found = True
    if not found:
        raise PollError(f"명단에 '{name}'님이 없어요.", 404)
    _save(POLLS_FILE, polls)
    return poll


def update_poll(room: str, date_str: str | None = None, place: str | None = None,
                no: int | None = None) -> dict:
    polls = load_polls()
    poll = _require(polls, room) if no is None else find_by_no(polls, room, no)
    if date_str is not None:
        date = parse_date(date_str)
        if not date:
            raise PollError("날짜는 YYMMDD 형식으로 입력해주세요. 예) 261024")
        poll["date"] = date_str
        base = datetime.fromisoformat(poll["created"]) if poll.get("created") else now_kst()
        poll["deadline"] = calc_deadline(base).isoformat()
    if place is not None:
        if not place.strip():
            raise PollError("장소를 입력해주세요.")
        poll["place"] = place.strip()
    _retitle(poll)
    _save(POLLS_FILE, polls)
    return poll


def replace_member(room: str, a: str, b: str, no: int | None = None) -> dict:
    polls = load_polls()
    poll = _require(polls, room) if no is None else find_by_no(polls, room, no)
    key_a = next((k for k, n in poll["attend"].items() if n == a), None)
    if key_a is None:
        raise PollError(f"참석 명단에 '{a}'님이 없어요.", 404)
    users = _load(USERS_FILE)
    # B가 등록된 사용자면 그 키를, 아니면 수동 항목으로 추가
    key_b = next((k for k, n in users.items() if n == b), f"manual:{b}")
    poll["absent"].pop(key_b, None)
    poll["attend"] = {
        (key_b if k == key_a else k): (b if k == key_a else n)
        for k, n in poll["attend"].items()
        if k != key_b
    }
    _save(POLLS_FILE, polls)
    return poll


def close_poll(room: str, now: datetime | None = None, no: int | None = None) -> dict:
    """투표를 지금 바로 마감한다. no 가 없으면 진행중인 가장 최근 투표."""
    polls = load_polls()
    if no is None:
        poll = _require(polls, room, now)
    else:
        poll = find_by_no(polls, room, no)
    if is_closed(poll, now):
        raise PollError(f"'{poll['title']}' 투표는 이미 마감되었어요.", 409)
    poll["closed_at"] = (now or now_kst()).isoformat()
    _save(POLLS_FILE, polls)
    return poll


def room_polls(room: str) -> list[dict]:
    """이 카톡방의 투표 목록 (번호 = 목록 순서 + 1)"""
    return load_polls().get(room, [])


def get_poll_no(room: str, no: int) -> dict:
    return find_by_no(load_polls(), room, no)


def delete_poll(room: str, no: int | None = None) -> str:
    """no 가 없으면 가장 최근 투표를 삭제"""
    polls = load_polls()
    lst = polls.get(room) or []
    if not lst:
        raise PollError(NO_POLL, 404)
    if no is None:
        no = lst[-1]["no"]
    target = find_by_no(polls, room, no)
    lst.remove(target)
    title = target["title"]
    if not lst:
        del polls[room]
    _save(POLLS_FILE, polls)
    return title


def add_mercenaries(room: str, date_str: str | None, names: list[str],
                    now: datetime | None = None) -> tuple[dict, list[str], list[str]]:
    """해당 날짜 투표(날짜 생략 시 최근 투표)의 참석자 명단에 용병을 추가한다. (투표, 추가된 이름, 이미 명단에 있던 이름)"""
    polls = load_polls()
    if date_str is None:  # 날짜 생략 → 가장 최근 투표
        lst = polls.get(room) or []
        if not lst:
            raise PollError(NO_POLL, 404)
        poll = lst[-1]
    else:
        if not parse_date(date_str):
            raise PollError("날짜는 YYMMDD 형식으로 입력해주세요. 예) 용병 261017 홍길동")
        same = [p for p in polls.get(room) or [] if p["date"] == date_str]
        if not same:
            raise PollError(f"{date_str} 날짜의 투표가 없어요. 투표조회 로 확인해주세요.", 404)
        opened = [p for p in same if not is_closed(p, now)]
        poll = (opened or same)[-1]  # 마감 후에도 용병을 추가할 수 있다
    users = _load(USERS_FILE)
    added, skipped = [], []
    for name in names:
        if name in poll["attend"].values():
            skipped.append(name)
            continue
        # 등록된 사용자 이름이면 그 사람의 키를 써서, 나중에 본인이 참석해도 중복되지 않게 한다
        key = next((k for k, n in users.items() if n == name), f"manual:{name}")
        poll["absent"].pop(key, None)
        poll["attend"][key] = name
        added.append(name)
    if added:
        _save(POLLS_FILE, polls)
    return poll, added, skipped


SQUAD_URL = "http://136.111.179.34/"  # 이 VM의 nginx 가 /var/www/squad 를 보여 준다
SQUAD_PUBLISH_DIR = Path(os.environ.get("SQUAD_PUBLISH_DIR", "/var/www/squad"))
# 스쿼드 레포 경로 (팀짜기 결과 index.html 을 push 할 로컬 clone)
SQUAD_REPO = Path(os.environ.get("SQUAD_REPO", str(Path.home() / "weekenders-futsal")))


def _load_squad_module(filename: str):
    """weekenders-futsal 의 데이터 파일(positions.py, grades.py)을 읽어 모듈로 돌려준다."""
    import importlib.util
    path = SQUAD_REPO / filename
    if not path.exists():
        raise PollError(f"명단 파일을 찾을 수 없어요: {path}", 500)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def roster_text() -> str:
    """전체 명단: 이름 · 등급 · 선호 포지션. 등급은 능력치 합산(grades.py) 기준, 높은 순. 조회 전에 레포를 최신으로 받는다."""
    pull_squad()
    positions = _load_squad_module("positions.py").PLAYER_POSITIONS
    grades_mod = _load_squad_module("grades.py")
    abilities = grades_mod.PLAYER_ABILITIES
    max_score = len(grades_mod.ABILITY_KEYS) * 4  # 항목 수 × A(4점)

    def grade_of(n: str) -> str:
        if n not in abilities:
            return "-"  # 등급 미등록
        r = grades_mod.player_score(n) / max_score
        return "A" if r >= 0.80 else "B" if r >= 0.60 else "C" if r >= 0.40 else "D"

    names = list(positions) + [n for n in abilities if n not in positions]
    names.sort(key=lambda n: (-grades_mod.player_score(n), n))
    lines = []
    for i, n in enumerate(names, 1):
        pos = positions.get(n)
        pos_text = " / ".join(pos) if pos else "-"
        lines.append(f"{i}. {n} · {grade_of(n)} · {pos_text}")
    return f"👥 전체 명단 ({len(names)}명)\n(이름 · 등급 · 포지션 1순위/2순위)\n" + "\n".join(lines)


POSITION_CODES = {"ST", "CM", "RB", "LB", "CB"}


def parse_position_args(args: list[str]):
    """포지션 입력을 (이름, 1순위, 2순위)로 바꾼다. 실패하면 None.
    ST/CB, STCB, ST CB, ST · CB, ST / CB, stcb, st cb 를 모두 받는다."""
    tail = []
    i = len(args)
    while i > 0 and re.fullmatch(r"[A-Za-z/·,]+", args[i - 1]):
        tail.insert(0, args[i - 1])
        i -= 1
    name = " ".join(args[:i]).strip()
    letters = re.sub(r"[^A-Za-z]", "", "".join(tail)).upper()
    if not name or len(letters) != 4:
        return None
    first, second = letters[:2], letters[2:]
    if first not in POSITION_CODES or second not in POSITION_CODES:
        return None
    return name, first, second


def update_positions_json(path: Path, name: str, first: str, second: str) -> None:
    """positions.json 에서 선수 포지션을 바꾸거나(없으면 추가) 저장한다. 다른 선수 데이터는 그대로 둔다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data[name] = [first, second]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def position_text(name: str) -> str:
    """선수의 선호 포지션을 positions.json 에서 읽어 보여 준다 (읽기만 함)."""
    pull_squad()
    data = json.loads((SQUAD_REPO / "positions.json").read_text(encoding="utf-8"))
    if name not in data:
        return f"❓ {name} 은(는) 포지션 등록이 없어요. 등록: 포지션 {name} ST/CB"
    first, second = data[name]
    return f"📍 {name} 선호 포지션: {first} / {second}"


def set_player_position(name: str, first: str, second: str) -> str:
    """선호 포지션을 positions.json 에 저장하고 GitHub 에 커밋·푸시한다. 없던 선수는 새로 추가한다."""
    import subprocess
    path = SQUAD_REPO / "positions.json"
    if not path.exists():
        raise PollError(f"포지션 파일을 찾을 수 없어요: {path}", 500)
    with SQUAD_LOCK:
        pull_squad()
        update_positions_json(path, name, first, second)

        def run(*args):
            return subprocess.run(["git", *args], cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)

        run("add", "positions.json")
        r = run("commit", "-m", f"선호 포지션 변경: {name} {first}/{second}")
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            raise PollError(f"커밋 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
        r = run("push", "origin", "HEAD")
        if r.returncode != 0:
            raise PollError(f"GitHub 푸시 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
    return f"✅ {name} 선호 포지션을 {first}/{second}(으)로 바꾸고 GitHub에 올렸어요."


ABILITY_KEYS = ["패스", "리턴", "수비", "드리블", "볼소유", "골결정력"]  # weekenders-futsal grades.py 와 같아야 함
GRADE_CODES = {"A", "B", "C", "D"}
GRADE_USAGE = ("사용법\n"
               "· 등급 김민규 — 현재 등급 조회\n"
               "· 등급 김민규 수비 A — 항목 하나만 수정\n"
               "· 등급 김민규 B B A A B B — 6개 항목 전체 등록\n"
               "  (순서: 패스 리턴 수비 드리블 볼소유 골결정력 / 등급: A B C D)")


def _valid_grade_name(name: str) -> bool:
    """이름은 한글/영문 1~3단어, 12자 이하만 허용. 기호·구분자가 섞이면 저장하지 않는다."""
    import re
    if any(k in name for k in ABILITY_KEYS):
        return False  # 항목 이름이 섞인 입력 (예: 김기항패스A)
    return bool(name) and len(name) <= 12 and re.fullmatch(r"[가-힣A-Za-z]+( [가-힣A-Za-z]+){0,2}", name) is not None


def parse_grade_args(args: list[str]) -> tuple[str, str, dict | None]:
    """반환 (이름, 모드, 값). 모드: view(조회) / item(항목 수정) / all(전체 등록) / error.
    구분자(·,)는 무시. 뒤 6개가 등급이면 전체 등록. 뒤에서부터 '항목 등급' 또는 '항목등급' 쌍을 모아 항목 수정.
    아무 항목도 없으면 이름만으로 조회."""
    def is_grade(tok: str) -> bool:
        return tok.upper() in GRADE_CODES
    def is_glued(tok: str) -> bool:
        return len(tok) >= 2 and tok[:-1] in ABILITY_KEYS and is_grade(tok[-1:])
    args = [a for a in args if a not in ("·", ",", "/")]
    if not args:
        return "", "error", None
    # 붙여 쓴 6글자 등급 (예: BBCCBA) → 공백으로 풀어서 전체 등록으로 처리
    if len(args) >= 2 and re.fullmatch(r"[ABCDabcd]{6}", args[-1]):
        args = args[:-1] + list(args[-1].upper())
    # A~D 글자만 여러 개 붙은 토큰(예: BBCC)은 형식 오류
    if any(re.fullmatch(r"[A-Z]{2,}", a) for a in args):  # 영문 대문자만 붙은 토큰은 등급 형식 오류
        return "", "error", None
    if len(args) >= 7 and all(is_grade(a) for a in args[-6:]):
        return " ".join(args[:-6]), "all", dict(zip(ABILITY_KEYS, [a.upper() for a in args[-6:]]))
    changes, i = {}, len(args)
    while i > 0:
        tok = args[i - 1]
        if is_glued(tok):
            changes.setdefault(tok[:-1], tok[-1:].upper()); i -= 1
        elif i >= 2 and args[i - 2] in ABILITY_KEYS and is_grade(tok):
            changes.setdefault(args[i - 2], tok.upper()); i -= 2
        else:
            break
    name_tokens = args[:i]
    if changes:
        # 이름 쪽에 항목/등급 토큰이 섞여 있으면 잘못된 입력 (예: '김기항 패스A 리턴A')
        if not name_tokens or any(t in ABILITY_KEYS or is_grade(t) or is_glued(t) for t in name_tokens):
            return "", "error", None
        return " ".join(name_tokens), "item", changes
    if any(is_grade(a) or a in ABILITY_KEYS or is_glued(a) for a in args[-6:]):
        return "", "error", None  # 등급/항목 토큰이 있는데 형식이 안 맞음
    return " ".join(args), "view", None


def grade_text(name: str, abilities: dict) -> str:
    parts = []
    for k in ABILITY_KEYS:
        parts.append(f"{k}{abilities[k]}" if k in abilities else f"{k}-(미입력·B기본)")
    return f"⭐ {name} 등급\n" + " · ".join(parts)


def set_player_grades(name: str, mode: str, changes: dict | None) -> str:
    """grades.json 을 고치고 GitHub 에 커밋·푸시한다. view 는 조회만, item 은 입력 항목만, all 은 6개 전체를 바꾼다."""
    import subprocess
    path = SQUAD_REPO / "grades.json"
    if not path.exists():
        raise PollError(f"등급 파일을 찾을 수 없어요: {path}", 500)
    with SQUAD_LOCK:
        pull_squad()
        data = json.loads(path.read_text(encoding="utf-8"))
        if mode == "view":
            if name not in data:
                return f"❓ {name} 은(는) 등급 파일에 없어요. 등록: 등급 {name} B B A A B B"
            return grade_text(name, data[name])
        abilities = {} if mode == "all" else dict(data.get(name, {}))
        abilities.update(changes or {})
        data[name] = {k: abilities[k] for k in ABILITY_KEYS if k in abilities}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        def run(*args):
            return subprocess.run(["git", *args], cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)

        run("add", "grades.json")
        r = run("commit", "-m", f"등급 변경: {name}")
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            raise PollError(f"커밋 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
        r = run("push", "origin", "HEAD")
        if r.returncode != 0:
            raise PollError(f"GitHub 푸시 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
    what = "전체 등록" if mode == "all" else "항목 수정"
    return f"✅ {name} {what} 완료 (GitHub 반영)\n" + grade_text(name, data[name])


NEW_USAGE = ("사용법: 신규 이름 등급6개 포지션\n"
             "예) 신규 김기항 BBCCBA ST/CB\n"
             "   신규 김기항 B B C C B A ST CB\n"
             "(등급: A B C D 6개 순서 패스 리턴 수비 드리블 볼소유 골결정력 / 포지션: ST CM RB LB CB)")


def parse_new_player_args(args: list[str]):
    """신규 이름 등급6개 포지션 → (이름, {항목:등급}, 1순위, 2순위). 형식이 안 맞으면 None.
    구분자(/ · ,)는 토큰 사이 기호로 보고 나눈다. 뒤에서 포지션 2개(4글자), 그 앞에서 등급 6글자를 찾는다."""
    toks = []
    for a in args:
        toks += [t for t in re.split(r"[/·,]", a) if t]
    i = len(toks)
    pos = ""
    while i > 0 and len(pos) < 4 and re.fullmatch(r"[A-Za-z]+", toks[i - 1]):
        pos = toks[i - 1] + pos
        i -= 1
    if len(pos) != 4:
        return None
    j = i
    grade = ""
    while j > 0 and len(grade) < 6 and re.fullmatch(r"[A-Za-z]+", toks[j - 1]):
        grade = toks[j - 1] + grade
        j -= 1
    grade = grade.upper()
    if len(grade) != 6 or any(c not in GRADE_CODES for c in grade):
        return None
    first, second = pos[:2].upper(), pos[2:].upper()
    if first not in POSITION_CODES or second not in POSITION_CODES:
        return None
    name = " ".join(toks[:j]).strip()
    if not _valid_grade_name(name):
        return None
    return name, dict(zip(ABILITY_KEYS, list(grade))), first, second


def add_new_player(name: str, grades6: dict, first: str, second: str) -> str:
    """신규 선수를 grades.json 과 positions.json 에 함께 추가하고 GitHub 에 커밋·푸시한다. 이미 있는 이름은 막는다."""
    import subprocess
    gpath = SQUAD_REPO / "grades.json"
    ppath = SQUAD_REPO / "positions.json"
    if not gpath.exists() or not ppath.exists():
        raise PollError("등급/포지션 파일을 찾을 수 없어요", 500)
    with SQUAD_LOCK:
        pull_squad()
        gdata = json.loads(gpath.read_text(encoding="utf-8"))
        pdata = json.loads(ppath.read_text(encoding="utf-8"))
        if name in gdata or name in pdata:
            return f"❓ {name} 은(는) 이미 등록돼 있어요. 등급은 '등급', 포지션은 '포지션' 명령으로 바꿔 주세요."
        gdata[name] = grades6
        pdata[name] = [first, second]
        gpath.write_text(json.dumps(gdata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ppath.write_text(json.dumps(pdata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        def run(*args):
            return subprocess.run(["git", *args], cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)

        run("add", "grades.json", "positions.json")
        r = run("commit", "-m", f"신규 선수 등록: {name}")
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            raise PollError(f"커밋 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
        r = run("push", "origin", "HEAD")
        if r.returncode != 0:
            raise PollError(f"GitHub 푸시 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
    return f"✅ 신규 {name} 등록 완료 (GitHub 반영)\n" + grade_text(name, grades6) + f"\n포지션: {first}/{second}"


def make_squad(room: str) -> str:
    """스쿼드 구성은 외부 페이지에서 한다. 봇은 그 링크를 안내한다."""
    return f"스쿼드 구성 페이지\n{SQUAD_URL}"


def build_squad_html(members: list[str], game_mode: str = "auto") -> None:
    """weekenders-futsal 레포의 export_html.generate_html 로 index.html 생성."""
    import importlib.util

    export_path = SQUAD_REPO / "export_html.py"
    if not export_path.exists():
        raise PollError(f"스쿼드 레포를 찾을 수 없어요: {SQUAD_REPO}", 500)

    # export_html.py 를 동적 로드 (team_maker/grades/positions 등 같은 폴더에서 import 됨)
    spec = importlib.util.spec_from_file_location("squad_export", export_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SQUAD_REPO))  # team_maker 등 import 가능하게
    spec.loader.exec_module(mod)
    mod.generate_html(players=members, game_mode=game_mode, open_browser=False)


def push_squad(commit_msg: str) -> bool:
    """weekenders-futsal 레포에 index.html 커밋·push. 성공하면 True."""
    import subprocess

    def run(*args):
        return subprocess.run(["git", *args], cwd=SQUAD_REPO,
                              capture_output=True, text=True, timeout=60)

    run("add", "index.html")
    if not run("status", "--porcelain").stdout.strip():
        return True  # 변경 없음
    r = run("commit", "-m", commit_msg)
    if r.returncode != 0:
        print("squad commit 실패:", r.stderr)
        return False
    r = run("push", "origin", "HEAD")
    if r.returncode != 0:
        print("squad push 실패:", r.stderr)
        return False
    return True


def pull_squad() -> None:
    """팀배정 로직 최신화: export_html 호출 전에 weekenders-futsal 을 pull."""
    import subprocess
    try:
        subprocess.run(["git", "pull", "--ff-only", "origin", "HEAD"],
                       cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("squad pull 경고 (무시하고 진행):", repr(e))


SQUAD_LOCK = threading.Lock()  # 팀 구성은 한 번에 하나씩


def publish_squad() -> bool:
    """만든 index.html 을 웹 서버 폴더로 복사한다 (GitHub push 없음). 성공하면 True."""
    import shutil
    try:
        SQUAD_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SQUAD_PUBLISH_DIR / "index.html.tmp"
        shutil.copyfile(SQUAD_REPO / "index.html", tmp)
        tmp.replace(SQUAD_PUBLISH_DIR / "index.html")  # 복사 도중 페이지가 깨지지 않도록 교체
        return True
    except OSError as e:
        print("squad 복사 실패:", repr(e))
        return False


def _squad_job(title: str, members: list[str], game_mode: str) -> None:
    if not members:
        print(f"팀짜기 {title}: 참석자가 없어 건너뜀")
        return
    with SQUAD_LOCK:
        try:
            pull_squad()
            build_squad_html(members, game_mode)
            pushed = push_squad(f"팀 구성 업데이트: {title}")  # GitHub 에 커밋·푸시
            ok = publish_squad()  # VM 웹 페이지 갱신
            print(f"팀짜기 {title}: {'완료' if ok else '실패'}" + ("" if pushed else " (GitHub push 실패)"))
        except Exception as e:
            print(f"팀짜기 {title} 오류:", repr(e))


def start_squad(poll: dict, game_mode: str = "auto") -> None:
    """팀 구성을 백그라운드에서 시작한다 (봇 응답이 멈추지 않도록)."""
    members = list(poll["attend"].values())
    threading.Thread(target=_squad_job, args=(poll["title"], members, game_mode), daemon=True).start()


def mark_squad_started(poll_id: str, now: datetime) -> dict | None:
    """이 투표의 팀 구성을 아직 안 했다면 표시하고 그 투표를 돌려준다 (중복 방지)."""
    polls = load_polls()
    for lst in polls.values():
        for p in lst:
            if p.get("id") == poll_id:
                if p.get("squad_at"):
                    return None
                p["squad_at"] = now.isoformat()
                _save(POLLS_FILE, polls)
                return p
    return None


def close_due_polls(now: datetime) -> int:
    """마감 시각이 막 지난(10분 이내) 투표를 마감 처리하고 팀을 짠다. 예전 투표는 건드리지 않는다."""
    polls = load_polls()
    started = []
    for lst in polls.values():
        for p in lst:
            if p.get("closed_at") or p.get("squad_at"):
                continue
            passed = now - datetime.fromisoformat(p["deadline"])
            if timedelta(0) <= passed < timedelta(minutes=10):
                p["closed_at"] = p["deadline"]
                p["squad_at"] = now.isoformat()
                started.append(p)
    if started:
        _save(POLLS_FILE, polls)
        for p in started:
            start_squad(p)
    return len(started)


def fix_open_deadlines(now: datetime) -> int:
    """생성일 기록이 없던 진행중 투표(업데이트 전에 만든 투표)는 지금을 생성일로 보고 마감을 다시 계산한다."""
    polls = load_polls()
    fixed = 0
    for lst in polls.values():
        for p in lst:
            if p.get("created") or p.get("closed_at") or is_closed(p, now):
                continue
            p["created"] = now.isoformat()
            p["deadline"] = calc_deadline(now).isoformat()
            fixed += 1
    if fixed:
        _save(POLLS_FILE, polls)
    return fixed


def make_team(room: str, no: int | None = None, game_mode: str = "auto") -> str:
    """수동 팀짜기: 최근(또는 지정) 투표 참석자로 팀을 짠다. 결과는 잠시 뒤 링크에 반영된다."""
    poll = get_poll(room) if no is None else get_poll_no(room, no)
    members = list(poll["attend"].values())
    if not members:
        raise PollError("참석자가 없어요. 참석 투표부터 받아주세요.")
    start_squad(poll, game_mode)
    return (f"⚽ 팀 구성을 시작했어요. ({poll['title']}, {len(members)}명)\n"
            f"👉 {SQUAD_URL}\n(바로 반영됩니다)")


# ============================ 카톡 명령 처리 ============================
def poll_summary(p: dict) -> str:
    att, absn = list(p["attend"].values()), list(p["absent"].values())
    return "\n".join([
        f"🗳 {p.get('no', '')}번 · {p['title']}" + (f"\n📝 {p['desc']}" if p.get("desc") else ""),
        f"마감: {fmt_deadline(p['closed_at'] if p.get('closed_at') else p['deadline'])}"
        + (" (마감됨)" if p.get("closed_at") else ""),
        f"참석 {len(att)}명: {', '.join(att) if att else '-'}",
        f"불참 {len(absn)}명: {', '.join(absn) if absn else '-'}",
    ])


COMMANDS = {"도움말", "이름", "투표", "참석", "불참", "멤버", "스쿼드", "스쿼드만들기", "명단", "포지션", "등급", "신규", "장소", "날짜", "투표종료", "투표완료", "투표조회", "투표삭제", "용병", "팀짜기"}
ALIASES = {"투표완료": "투표종료"}  # 마감 전에 투표를 끝내는 말 두 가지 모두 같은 명령


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """메시지에서 (명령, 인자들) 을 꺼낸다. 명령이 아니면 None.
    - 맨 앞 단어가 명령이면 인식 ('!' 는 있어도 없어도 됨): "참석", "!투표 261017 달월"
    - 단체방 멘션이 앞에 붙은 경우도 인식: "@위켄봇 도움말", "@위켄 봇 !멤버"
    - 문장 중간의 단어는 무시: "나 내일 참석" → 명령 아님
    """
    words = (text or "").split()
    if not words:
        return None
    start = 0
    if words[0].startswith("@"):  # 멘션: 봇 이름에 띄어쓰기가 있어도 3단어까지 건너뜀
        start = next((i for i in range(1, min(len(words), 4)) if words[i].lstrip("!") in COMMANDS), None)
        if start is None:
            return None
    cmd, args = words[start].lstrip("!"), words[start + 1:]
    if cmd not in COMMANDS:
        return None
    if cmd == "투표" and args[:1] in (["종료"], ["완료"], ["조회"]):  # "투표 종료/완료/조회" 도 허용
        cmd, args = "투표" + args[0], args[1:]
        if cmd == "투표완료":
            cmd = "투표종료"
    cmd = ALIASES.get(cmd, cmd)
    return cmd, args


BOT_MENTION = "@위켄"  # 단체방에서 봇을 부르는 이름 (@위켄봇, @위켄 봇 모두)
UNKNOWN_HINT = "❓ 알 수 없는 명령이에요.\n사용법은 @위켄봇 도움말 을 입력해주세요."


def handle(text: str, user_key: str, room: str, now: datetime | None = None) -> tuple[str, list] | None:
    """카톡 명령어를 처리하고 (답장 텍스트, 버튼 목록) 을 돌려준다. 명령어가 아니면 None."""
    parsed = parse_command(text)
    if not parsed:
        # 봇을 불렀는데 명령을 모르면 도움말 안내 (일반 대화에는 답하지 않는다)
        if (text or "").startswith(BOT_MENTION) and len((text or "").split()) >= 2:
            return UNKNOWN_HINT, []
        return None
    cmd, args = parsed
    try:
        return _dispatch(cmd, args, user_key, room, now)
    except PollError as e:
        return e.message, []


def _parse_no(s: str) -> int:
    s = s.rstrip("번")
    if not s.isdigit():
        raise PollError("번호는 숫자로 입력해주세요. 예) 투표삭제 1")
    return int(s)


ALLOWED_NAMES = {"김민규", "이성학", "김기항"}  # 나머지 명령어를 쓸 수 있는 닉네임
OPEN_COMMANDS = {"이름", "참석", "불참"}  # 이름 등록과 참석/불참은 누구나 쓸 수 있다


def _dispatch(cmd: str, args: list[str], user_key: str, room: str, now: datetime | None) -> tuple[str, list]:
    if cmd in ("장소", "날짜") or (cmd == "멤버" and args):  # 수정 없이: 장소 달월 / 날짜 261031 / 멤버 김기항 김민규
        args, cmd = [cmd] + args, "수정"
    if cmd not in OPEN_COMMANDS:  # 이름·참석·불참 외에는 지정된 멤버만 쓸 수 있다 (도움말 포함)
        my_name = _load(USERS_FILE).get(user_key)
        if not my_name:
            return "먼저 '@위켄봇 이름 [닉네임]'으로 이름을 등록해 주세요.", []
        if my_name not in ALLOWED_NAMES:
            return "이 명령은 지정된 멤버만 쓸 수 있어요.", []

    if cmd == "도움말":
        return HELP_TEXT, []

    if cmd == "이름":
        if not args:
            current = _load(USERS_FILE).get(user_key, "없음")
            return f"사용법: @위켄봇 이름 [닉네임]\n현재 등록된 이름: {current}", []
        name = register_name(user_key, " ".join(args))
        return f"이름을 '{name}'(으)로 등록했어요.", []

    if cmd == "투표" and not args:  # "@위켄봇 투표" → 진행중인 최근 투표 카드 (버튼으로 참석·불참)
        opened = [p for p in room_polls(room) if not is_closed(p, now)]
        if not opened:
            raise PollError("진행중인 투표가 없어요.\n새로 만들려면: @위켄봇 투표 261017 달월")
        poll = opened[-1]
        return f"{poll_summary(poll)}\n\n{VOTE_HINT}", vote_buttons(poll)

    if cmd == "투표":
        try:  # 따옴표로 묶은 장소("강남역 2번출구")를 한 덩어리로 읽는다
            tokens = shlex.split(" ".join(args))
        except ValueError:
            tokens = args
        if len(tokens) < 2:
            return ("사용법: 투표 날짜 장소 [설명]\n예) 투표 261017 달월\n예) 투표 261017 달월 1차 회식\n"
                    "예) 투표 261017 \"강남역 2번출구\" 회식"), []
        poll = create_poll(room, tokens[0], tokens[1], user_key, now, desc=" ".join(tokens[2:]))
        return f"투표가 생성되었습니다!\n{poll_summary(poll)}\n\n{VOTE_HINT}", vote_buttons(poll)

    if cmd in SIDES:
        if args and not (args[0].startswith("#") or args[0].isdigit()) \
                and _load(USERS_FILE).get(user_key) in ALLOWED_NAMES:
            # 관리자: 참석 김기항 / 불참 김기항 → 그 사람의 참석·불참을 바꾼다 (마감 후에도 가능)
            target = " ".join(args).strip('"')
            poll = set_attendance(room, target, SIDES[cmd])
            return poll_reply(poll, now, f"{target}님을 {cmd}으로 바꿨어요.")
        polls = load_polls()
        pid = None
        if args and (args[0].startswith("#") or args[0].isdigit()):  # "참석 2" 또는 버튼의 "참석 #2"
            pid = args[0].lstrip("#")
            args = args[1:]
            if not pid:
                raise PollError("투표 번호 형식이 잘못됐어요. 예) 참석 2")
            find_poll_by_id(polls, room, pid)  # 없는 투표면 여기서 안내
        else:
            _require(polls, room, now)
        if args:  # "참석 홍길동" → 이름 등록도 함께
            register_name(user_key, " ".join(args))
        name = _load(USERS_FILE).get(user_key)
        if not name:
            return "먼저 이름을 알려주세요.\n예) 이름 홍길동\n(본인 이름을 최초 등록해주세요.)", []
        target = find_poll_by_id(polls, room, pid) if pid else _require(polls, room, now)
        if user_key in target[SIDES[cmd]]:  # 이미 같은 상태(참석중 또는 불참중)면 아무 응답도 하지 않는다
            return None
        poll = cast_vote(room, user_key, name, SIDES[cmd], now, poll_id=pid)
        return f"{name}님 {cmd}으로 기록했어요.\n{poll_summary(poll)}", vote_buttons(poll)

    if cmd == "멤버":  # 멤버 → 최근 투표, 멤버 2 → 2번 투표
        if args:
            poll = get_poll_no(room, _parse_no(args[0]))
        else:
            poll = get_poll(room)
        if not is_closed(poll, now):  # 진행중에는 명단을 보여주지 않는다 (마감 또는 투표완료 후에만)
            return (f"🗳 {poll['no']}번 · {poll['title']}\n"
                    "아직 투표가 진행중이에요. 마감 후 또는 투표완료 후에 멤버를 볼 수 있어요."), []
        att = list(poll["attend"].values())
        body = "\n".join(f"{i}. {n}" for i, n in enumerate(att, 1)) or "참석자가 없어요."
        return f"🗳 {poll['no']}번 · {poll['title']} (마감)\n참석 {len(att)}명\n{body}", []

    if cmd == "포지션":  # 포지션 김기항 ST/CB (ST CB, STCB, ST·CB, stcb 등 모두 가능)
        usage = ("사용법: 포지션 이름 1순위/2순위\n"
                 "예) 포지션 김기항 ST/CB\n"
                 "(띄어쓰기·구분자 없이 STCB 도 가능 / 포지션: ST, CM, RB, LB, CB)")
        parsed = parse_position_args(args)
        if not parsed:
            name = " ".join(args).strip()  # 이름만 쓰면 조회 (예: 포지션 김기항)
            if args and not any(re.search(r"[A-Za-z]", a) for a in args) and _valid_grade_name(name):
                return position_text(name), []
            return usage, []
        name, first, second = parsed
        return set_player_position(name, first, second), []

    if cmd == "신규":  # 신규 김기항 BBCCBA ST/CB → 신규 선수를 등급·포지션과 함께 등록
        parsed = parse_new_player_args(args)
        if not parsed:
            return NEW_USAGE, []
        name, grades6, first, second = parsed
        return add_new_player(name, grades6, first, second), []

    if cmd == "명단":  # weekenders-futsal 의 전체 명단
        return roster_text(), []

    if cmd == "스쿼드":  # 스쿼드 페이지 주소만 안내
        return make_squad(room), []

    if cmd == "스쿼드만들기":  # 최근 투표 참석자로 팀을 새로 짜서 페이지에 반영
        return make_team(room), []

    if cmd == "팀짜기":
        # 팀짜기 → 최근 투표 참석자로 자동 모드 / 팀짜기 2 → 2번 투표
        # 팀짜기 매칭 / 자체 → 모드 지정 (매칭=외부 상대, 자체=자체전)
        no = None
        game_mode = "auto"
        for a in args:
            t = a.lstrip("#").rstrip("번")
            if t.isdigit():
                no = int(t)
            elif a in ("매칭", "외부"):
                game_mode = "match"
            elif a in ("자체", "자체전"):
                game_mode = "self"
        return make_team(room, no, game_mode), []

    if cmd == "수정":
        usage = ("사용법: 장소 [새 장소] / 날짜 [YYMMDD] / 멤버 [불참할 이름] [참석할 이름]\n"
                 "가장 최근 투표를 고칩니다.")
        no = None  # 맨 앞의 숫자 = 수정할 투표 번호 (예: 수정 2 장소 강남역)
        if args and args[0].lstrip("#").isdigit():
            no, args = int(args[0].lstrip("#")), args[1:]
        if not args:
            get_poll(room) if no is None else get_poll_no(room, no)
            return usage, []
        sub, sub_args = args[0], args[1:]
        if sub == "장소":
            if not sub_args:
                return "사용법: 장소 [새 장소]", []
            poll = update_poll(room, place=" ".join(sub_args), no=no)
        elif sub == "날짜":
            if not sub_args or not parse_date(sub_args[0]):
                get_poll(room)
                return "사용법: 날짜 [YYMMDD]  예) 날짜 261031", []
            poll = update_poll(room, date_str=sub_args[0], no=no)
        elif sub == "멤버":  # 수정 멤버 김기항 김민규 → 김기항 불참, 김민규 참석
            if len(sub_args) != 2:
                return "사용법: 멤버 [불참할 이름] [참석할 이름]\n예) 멤버 김기항 김민규", []
            set_attendance(room, sub_args[0], SIDES["불참"], no=no)
            poll = set_attendance(room, sub_args[1], SIDES["참석"], no=no)
            return poll_reply(poll, now, f"{sub_args[0]}님을 불참, {sub_args[1]}님을 참석으로 바꿨어요.")
        else:
            return "수정할 항목은 장소 / 날짜 / 멤버 중 하나예요.", []
        return f"{poll['no']}번 투표를 수정했어요.\n{poll_summary(poll)}", squad_buttons(poll, now)

    if cmd == "용병":
        try:  # 띄어쓰기가 있는 닉네임은 따옴표로 묶는다: 용병 261017 "홍 길동" 김철수
            tokens = shlex.split(" ".join(args))
        except ValueError:
            return "따옴표가 짝이 맞지 않아요. 예) 용병 261017 \"홍 길동\" 김철수", []
        if not tokens:
            return "사용법: 용병 이름 [이름2 ...]  예) 용병 홍길동 (최근 투표에 추가)", []
        date_str, names = None, tokens  # 항상 가장 최근 투표에 추가
        poll, added, skipped = add_mercenaries(room, date_str, names, now)
        lines = []
        if added:
            lines.append(f"용병 {', '.join(added)}님을 '{poll['title']}' 참석자에 추가했어요.")
        if skipped:
            lines.append(f"이미 명단에 있어요: {', '.join(skipped)}")
        return poll_reply(poll, now, "\n".join(lines))

    if cmd == "투표종료":
        no = _parse_no(args[0]) if args else None
        poll = close_poll(room, now, no)
        att = list(poll["attend"].values())
        # 카드 설명은 400자까지만 보이므로 짧게 쓴다. 버튼을 누르면 '@위켄봇 스쿼드만들기'가 입력된다.
        text = (f"🔒 '{poll['title']}' 투표를 마감했습니다.\n"
                f"참석 {len(att)}명: {', '.join(att) if att else '-'}")
        if not att:
            return text, []
        text += "\n\n⚽ 아래 버튼을 눌러 스쿼드를 만드세요."
        btn = [{"label": "스쿼드 만들기", "action": "message", "messageText": "@위켄봇 스쿼드만들기"}]
        return text, btn

    if cmd == "투표조회":
        if args:  # 투표조회 2 → 2번 투표 상세
            no = _parse_no(args[0])
            poll = get_poll_no(room, no)
            status = "🔒 마감" if is_closed(poll, now) else "🟢 진행중"
            return f"[{no}번 · {status}]\n{poll_summary(poll)}", []
        lst = room_polls(room)
        if not lst:
            raise PollError(NO_POLL, 404)
        lines = [
            f"{p.get('no')}번 {'🔒' if is_closed(p, now) else '🟢'} {p['title']} (참석 {len(p['attend'])}명)"
            for p in lst
        ]
        return "[투표 목록] 🟢진행중 🔒마감\n" + "\n".join(lines) + "\n\n상세: 투표조회 번호 / 삭제: 투표삭제 번호", []

    if cmd == "투표삭제":
        no = _parse_no(args[0]) if args else None
        return f"'{delete_poll(room, no)}' 투표를 삭제했습니다.", []

    return "모르는 명령어예요. 도움말 을 입력해보세요.", []


# ============================ REST API ============================
def poll_view(room: str, p: dict) -> dict:
    """API 응답: 날짜, 장소, 참석 멤버"""
    return {
        "date": p["date"],
        "place": p["place"],
        "members": list(p["attend"].values()),
    }


def make_api(api_key: str):
    from typing import Literal

    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field

    def auth(x_api_key: str = Header(..., description="`.env` 의 API_KEY")):
        if x_api_key != api_key:
            raise HTTPException(401, "API 키가 올바르지 않아요.")

    app = FastAPI(
        title="위켄 봇 API",
        description="카카오톡 투표 봇의 투표 데이터를 조회·관리합니다. 모든 요청에 `X-API-Key` 헤더가 필요합니다.\n\n"
        "`room` 은 카톡방 키입니다. `GET /polls` 로 확인하세요.",
        version="1.0",
        dependencies=[Depends(auth)],
    )

    @app.exception_handler(PollError)
    async def _poll_error(_, e: PollError):
        return JSONResponse(status_code=e.code, content={"detail": e.message})

    class PollCreate(BaseModel):
        date: str = Field(..., examples=["261017"], description="YYMMDD")
        place: str = Field(..., examples=["달월"])
        desc: str = Field("", examples=["1차 회식"], description="설명 (선택)")

    class PollUpdate(BaseModel):
        no: int | None = Field(None, examples=[2], description="투표 번호 (생략 시 진행중인 최근 투표)")
        date: str | None = Field(None, examples=["261024"], description="YYMMDD")
        place: str | None = Field(None, examples=["강남"])

    class CloseBody(BaseModel):
        no: int | None = Field(None, examples=[2], description="투표 번호 (생략 시 진행중인 최근 투표)")

    class Vote(BaseModel):
        name: str = Field(..., examples=["홍길동"])
        status: Literal["참석", "불참"] = "참석"
        poll: str | None = Field(None, examples=["2"], description="투표 번호 (예: 2). 없으면 진행중인 최근 투표")

    class Mercenary(BaseModel):
        date: str = Field(..., examples=["261017"], description="YYMMDD")
        names: list[str] = Field(..., examples=[["홍길동", "김철수"]])

    class MemberChange(BaseModel):
        no: int | None = Field(None, examples=[2], description="투표 번호 (생략 시 진행중인 최근 투표)")
        old: str = Field(..., examples=["홍길동"], description="바꿀 참석자 이름")
        new: str = Field(..., examples=["김철수"], description="새 이름")

    @app.get("/rooms", summary="카톡방 키 목록 (다른 API의 {room} 에 사용)")
    def rooms():
        return [{"room": r, "title": lst[-1]["title"]} for r, lst in list_polls().items()]

    @app.get("/polls", summary="모든 카톡방의 투표 목록")
    def all_polls():
        return [
            {"no": p["no"], **poll_view(r, p)}
            for r, lst in list_polls().items()
            for p in lst
        ]

    @app.get("/polls/{room}", summary="투표 상세")
    def one_poll(room: str):
        return poll_view(room, get_poll(room))

    @app.get("/polls/{room}/members", summary="참석 인원 (!멤버 / !멤버 번호)")
    def members(room: str, no: int | None = None):
        p = get_poll(room) if no is None else get_poll_no(room, no)
        return {"title": p["title"], "count": len(p["attend"]), "members": list(p["attend"].values())}

    @app.post("/polls/{room}", status_code=201, summary="투표 생성 (!투표)")
    def create(room: str, body: PollCreate):
        return poll_view(room, create_poll(room, body.date, body.place, "api", desc=body.desc))

    @app.patch("/polls/{room}", summary="날짜·장소 수정 (!수정 날짜 / !수정 장소)")
    def update(room: str, body: PollUpdate):
        if body.date is None and body.place is None:
            raise PollError("date 또는 place 중 하나는 있어야 해요.")
        return poll_view(room, update_poll(room, body.date, body.place, no=body.no))

    @app.post("/polls/{room}/votes", summary="이름으로 참석/불참 기록")
    def vote(room: str, body: Vote):
        users = _load(USERS_FILE)
        key = next((k for k, n in users.items() if n == body.name), f"manual:{body.name}")
        return poll_view(room, cast_vote(room, key, body.name, SIDES[body.status], poll_id=body.poll))

    @app.delete("/polls/{room}/votes/{name}", summary="명단에서 사람 빼기")
    def unvote(room: str, name: str):
        return poll_view(room, remove_vote(room, name))

    @app.put("/polls/{room}/members", summary="참석 멤버 변경 (!수정 번호 멤버 A B)")
    def change_member(room: str, body: MemberChange):
        return poll_view(room, replace_member(room, body.old, body.new, no=body.no))

    @app.post("/polls/{room}/squad", summary="스쿼드 구성 (!스쿼드)")
    def squad(room: str):
        return {"result": make_squad(room)}

    @app.post("/polls/{room}/mercenaries", summary="용병 추가 (용병 날짜 이름)")
    def mercenaries(room: str, body: Mercenary):
        poll, _, _ = add_mercenaries(room, body.date, body.names)
        return poll_view(room, poll)

    @app.post("/polls/{room}/close", summary="지금 바로 투표 마감 (투표종료 / 투표완료)")
    def close(room: str, body: CloseBody | None = None):
        return poll_view(room, close_poll(room, no=body.no if body else None))

    @app.get("/polls/{room}/list", summary="카톡방의 투표 목록 (투표조회)")
    def room_list(room: str):
        return [{"no": p["no"], **poll_view(room, p)} for p in room_polls(room)]

    @app.get("/polls/{room}/{no}", summary="번호로 투표 조회 (투표조회 번호)")
    def by_no(room: str, no: int):
        return {"no": no, **poll_view(room, get_poll_no(room, no))}

    @app.delete("/polls/{room}/{no}", summary="번호로 투표 삭제 (투표삭제 번호)")
    def delete_no(room: str, no: int):
        return {"deleted": delete_poll(room, no)}

    @app.delete("/polls/{room}", summary="최근 투표 삭제 (삭제)")
    def delete(room: str):
        return {"deleted": delete_poll(room)}

    @app.get("/users", summary="등록된 이름 목록")
    def users():
        return sorted(set(_load(USERS_FILE).values()))

    return app


async def serve_api(stop: asyncio.Event) -> None:
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("API 꺼짐 (.env 에 API_KEY 를 넣으면 켜집니다)")
        return
    import uvicorn

    port = int(os.environ.get("API_PORT", "8000"))
    config = uvicorn.Config(make_api(api_key), host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # Ctrl+C 는 봇 본체가 처리
    print(f"API 켜짐: http://localhost:{port}/docs")
    task = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await task


# ============================ 게이트웨이 ============================
def log(line: str) -> None:
    if DEBUG:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


UPDATE_EXIT_CODE = 3          # run_bot.sh 가 이 코드를 받으면 즉시 재시작
UPDATE_CHECK_SEC = int(os.environ.get("UPDATE_CHECK_SEC", "30"))  # GitHub 새 커밋 확인 주기(초). 서버는 300 권장


def git(*args: str) -> str:
    """봇 폴더에서 git 명령을 실행하고 출력을 돌려준다. 실패하면 빈 문자열."""
    import subprocess

    try:
        r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


async def watch_updates(start_head: str, stop: asyncio.Event) -> None:
    """GitHub에 새 커밋이 올라오면 받아오고, 코드가 바뀌었으면 재시작을 요청한다."""
    auto_push = os.environ.get("AUTO_PUSH") == "1" and (HERE / "auto_push.sh").exists()
    while not stop.is_set():
        await asyncio.sleep(UPDATE_CHECK_SEC)
        # 개발 맥: 폴더에서 바뀐 파일이 있으면 자동 커밋·푸시 (.env 에 AUTO_PUSH=1)
        if auto_push and await asyncio.to_thread(git, "status", "--porcelain"):
            import subprocess

            print("변경된 파일 감지 → GitHub에 커밋·푸시")
            await asyncio.to_thread(subprocess.run, ["bash", str(HERE / "auto_push.sh")], cwd=HERE)
        await asyncio.to_thread(git, "fetch", "-q", "origin")
        behind = await asyncio.to_thread(git, "rev-list", "--count", "HEAD..@{u}")
        if behind and behind != "0":
            print(f"GitHub에 새 커밋 {behind}개 → 받아오는 중")
            await asyncio.to_thread(git, "pull", "--ff-only", "-q")
        head = await asyncio.to_thread(git, "rev-parse", "HEAD")
        if head and head != start_head:
            print(f"새 버전 감지 ({start_head[:7]} → {head[:7]}) → 재시작합니다")
            stop.set()


def make_weekly_poll(now: datetime) -> dict | None:
    """토요일 10시대에 정기 투표를 한 번만 만든다. 만들었으면 그 투표, 아니면 None."""
    cfg = WEEKLY_POLL
    if now.weekday() != cfg["weekday"] or now.hour != cfg["hour"] or now.minute != cfg["minute"]:
        return None
    today = now.date().isoformat()
    marks = _load(SCHEDULE_FILE)
    if marks.get("weekly") == today:  # 오늘 이미 만듦
        return None
    # 다음 주 토요일: 오늘 이후 가장 가까운 토요일 (토요일에 만들면 +7일, 일요일에 만들면 +6일)
    ahead = (5 - now.weekday()) % 7 or 7
    poll_date = (now.date() + timedelta(days=ahead)).strftime("%y%m%d")
    polls = load_polls()
    if any(p["date"] == poll_date for p in polls.get(cfg["room"]) or []):
        marks["weekly"] = today
        _save(SCHEDULE_FILE, marks)
        return None
    if has_open_poll(polls, cfg["room"], now):  # 진행중인 투표가 있으면 이번 주는 건너뛴다
        return None
    poll = create_poll(cfg["room"], poll_date, cfg["place"], "auto", now, desc=cfg["desc"])
    marks["weekly"] = today
    _save(SCHEDULE_FILE, marks)
    print(f"정기 투표 생성: {poll['title']} #{poll['no']}")
    return poll


async def weekly_poll_loop(stop: asyncio.Event) -> None:
    """30초마다 시간을 확인해서 정기 투표를 만든다 (1분에 최소 두 번 확인하므로 놓치지 않는다)."""
    while not stop.is_set():
        try:
            make_weekly_poll(now_kst())
        except Exception as e:
            print("정기 투표 오류:", repr(e))
        try:
            close_due_polls(now_kst())  # 마감 시각이 지나면 자동 마감 + 팀 구성
        except Exception as e:
            print("자동 마감 오류:", repr(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def main() -> int:
    import websockets

    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit('BOT_TOKEN 환경변수가 없습니다. .env 파일이나 export BOT_TOKEN="봇 인증 토큰" 으로 설정하세요.')

    start_head = git("rev-parse", "HEAD")
    fix_open_deadlines(now_kst())  # 생성일 기준 마감 (진행중 투표 한 번 보정)
    pull_squad()  # 봇을 켤 때마다 스쿼드 레포를 최신으로
    print(f"봇 시작 (버전 {start_head[:7] or '알수없음'})")
    stop = asyncio.Event()
    if start_head:
        asyncio.create_task(watch_updates(start_head, stop))
    api_task = asyncio.create_task(serve_api(stop))
    weekly_task = asyncio.create_task(weekly_poll_loop(stop))

    retry = 1
    while not stop.is_set():
        try:
            await run_gateway(websockets, token, stop)
            retry = 1
        except Exception as e:  # 연결 끊김 등 → 잠시 후 재접속
            if stop.is_set():
                break
            print(f"연결 끊김: {e!r} → {retry}초 후 재접속")
            await asyncio.sleep(retry)
            retry = min(retry * 2, 60)
    await api_task
    await weekly_task
    return UPDATE_EXIT_CODE


async def run_gateway(websockets, token: str, stop: asyncio.Event) -> None:
    async with websockets.connect(GW_URL) as ws:
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"op": 2, "d": {"token": token}}))
        await ws.recv()
        print("게이트웨이 연결 성공")

        interval = hello["d"]["heartbeat_interval"] / 1000
        hb = asyncio.create_task(heartbeat(ws, interval))
        closer = asyncio.create_task(close_on(stop, ws))
        try:
            async for raw in ws:
                event = json.loads(raw)
                if event.get("t") != "MESSAGE_CREATE":
                    continue
                d = event["d"]
                log("RAW " + json.dumps(event, ensure_ascii=False))
                user_key = d.get("botUserKey", "unknown")
                room = d.get("botGroupKey", "default")
                content = d.get("content", "")
                print(f"RECV {user_key[-6:]}: {content}")
                result = None
                try:
                    # 팀짜기처럼 오래 걸리는 작업(git push 등)이 연결 유지를 막지 않도록 별도 스레드에서 처리
                    result = await asyncio.to_thread(handle, content or "", user_key, room)
                except Exception as e:  # 명령 처리 오류가 봇 전체를 멈추지 않도록
                    print("ERROR:", repr(e))
                    result = ("처리 중 오류가 발생했어요.", [])
                if result and d.get("callbackToken"):
                    await reply(token, d["callbackToken"], *result)
        finally:
            hb.cancel()
            closer.cancel()
        if not stop.is_set():
            raise ConnectionError("게이트웨이가 연결을 닫음")


async def close_on(stop: asyncio.Event, ws) -> None:
    await stop.wait()
    await ws.close()


async def heartbeat(ws, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1}))


async def reply(token: str, callback_token: str, text: str, buttons: list) -> None:
    """답장을 보낸다. 버튼이 있으면 카드로 먼저 시도하고, 거부되면 텍스트로 다시 보낸다."""
    import httpx

    headers = {"Authorization": f"KakaoAK {token}", "X-Bot-Callback-Token": callback_token}

    def body_of(outputs: list) -> dict:
        return {"skillResponse": {"version": "2.0", "template": {"outputs": outputs}}}

    bodies = [body_of([{"simpleText": {"text": text}}])]
    if buttons:
        card = {"textCard": {"description": text[:400], "buttons": buttons}}
        bodies.insert(0, body_of([card]))

    async with httpx.AsyncClient() as client:
        for body in bodies:
            resp = await client.post(CALLBACK_URL, json=body, headers=headers)
            print("SENT:", resp.status_code, resp.text)
            log("SENT " + json.dumps({"status": resp.status_code, "resp": resp.text, "body": body}, ensure_ascii=False))
            try:
                ok = resp.status_code == 200 and resp.json().get("errorCount", 0) == 0
            except ValueError:
                ok = False
            if ok:
                break


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n봇을 종료합니다.")
