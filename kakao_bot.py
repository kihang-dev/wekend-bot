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
    "room": "bg2_aT-gVOURKGGR8EANLPFAp3utsa0xQOu1Mw",
    "place": "월곶",
    "desc": "정기풋살",
    "weekday": 5,  # 토요일 (월=0)
    "hour": 10,    # 오전 10시
    "minute": 0,

}
USERS_FILE = HERE / "users.json"
ROOM_MEMBERS_FILE = HERE / "room_members.json"
DORMANT_FILE = HERE / "dormant.json"  # 휴면 인원 (미응답 명단에서 제외)  # 방별 botUserKey -> 이름 (이 방에서 실제로 쓰인 키만)
SETTINGS_FILE = HERE / "settings.json"  # 경기 시간 같은 설정 (깃에 올리지 않음)
LINK_FILE = HERE / "link_codes.json"  # 일반/오픈채팅 연결용 임시 코드 (깃에 올리지 않음)
LINKS_FILE = HERE / "links.json"  # 같은 사람으로 묶인 카톡 키 목록 {묶음번호: [키, ...]} (깃에 올리지 않음)
DEFAULT_START_HOUR = 8
LOG_FILE = HERE / "events.log"
DEBUG = os.environ.get("DEBUG") == "1"

GW_URL = "wss://bot-gateway.kakao.com/gateway"
CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"

HELP_TEXT = """📖 위켄봇 사용법
모든 명령은 @위켄봇 으로 시작해요.

[처음 한 번]
· 이름 홍길동 — 내 이름 등록
· 연결 — 일반·오픈채팅방의 같은 사람 묶기 (코드 발급 후 다른 방에서 연결 코드 입력)

[투표 참여]
· 투표 — 진행중인 투표 보기
· 참석 / 불참 — 최근 투표에 응답
· 용병 홍길동 — 최근 투표에 용병 추가 (마감 후면 팀 자동 재구성)
· 멤버 — 최근 투표 참석·불참 멤버 보기 (진행중·마감 모두)\n· 미응답 — 아직 응답하지 않은 사람 보기
※ 마감: 다음 주 수요일 23:00

[투표 관리]
· 참석 홍길동 김민규 / 불참 홍길동 — 참석·불참 바꾸기, 여러 명 가능 (마감 후면 팀 자동 재구성)
· 교체 김기항 김민규 — 김기항 불참, 김민규 참석 (마감 후면 팀 자동 재구성)
· 장소 달월 — 장소 바꾸기
· 날짜 261031 — 날짜 바꾸기
· 시간 7 — 경기 시간 바꾸기 (7 → 07:00~09:00, 지각 체크 07시 01분)
· 투표종료 — 지금 마감
· 투표삭제 — 투표 삭제
· 투표조회 — 투표 목록

[팀 짜기]
· 스쿼드 — 스쿼드 페이지 링크
· 팀짜기 — 참석자로 팀 구성
· 팀다시짜기 — 현재 참석자로 팀을 다시 구성
· 팀짜기 매칭 — 무조건 매칭 (6명 이상)
· 팀짜기 자체 — 무조건 자체 (10명 이상)

[팀 고정] (마감 후에만)
· 같은팀 [이름1] [이름2] — 두 사람을 같은 팀으로 고정
· 다른팀 [이름1] [이름2] — 두 사람을 다른 팀으로 고정
· 팀삭제 [이름1] [이름2] — 두 사람의 같은팀/다른팀 고정 삭제
· 팀삭제 — 고정 전체 초기화

[명단·선수]
· 명단 — 전체 명단 보기\n· 휴면 김기항 — 휴면 등록 (미응답에서 제외) / 휴면 김기항 해제 / 휴면 — 목록\n· 등급 참석 — 스쿼드용 명단(MEMBERS) 등급 한번에 보기
· 선출 홍길동 — 선출 등록
· 선출 홍길동 해제 — 선출 해제
· 선출 — 선출 목록
· 포지션 김기항 ST/LB — 선호 포지션 바꾸기"""

VOTE_HINT = "👉 참석 또는 불참 을 입력해주세요."
def vote_buttons(poll: dict) -> list[dict]:
    """이 투표 전용 버튼. 누르면 '참석 #id' 가 전송돼 해당 투표에 기록된다."""
    return [
        {"label": "참석", "action": "message", "messageText": f"참석 #{poll['no']}"},
        {"label": "불참", "action": "message", "messageText": f"불참 #{poll['no']}"},
    ]
def dormant_names() -> set:
    """휴면 인원 목록 (미응답 명단에서 빠진다)."""
    return set(_load(DORMANT_FILE).get("names", []))


def roster_names() -> list[str]:
    """weekenders-futsal 명단 (grades.json 에 등록된 선수 전체). 못 읽으면 빈 목록."""
    try:
        return [n for n in _load_squad_module("grades.py").PLAYER_ABILITIES.keys() if n not in dormant_names()]
    except Exception as e:
        print("명단 읽기 실패 (미응답 표시 생략):", repr(e))
        return []


def poll_summary(p: dict) -> str:
    """투표 요약 + 명단 중 아직 응답하지 않은 인원 (참석·불참 어느 쪽도 없는 선수)."""
    base = _poll_summary_base(p)
    responded = set(p["attend"].values()) | set(p["absent"].values())
    waiting = [n for n in roster_names() if n not in responded]
    if not waiting:
        return base
    return base + f"\n\n⏳ 미응답 {len(waiting)}명: {', '.join(waiting)}"


def poll_reply(poll: dict, now: datetime | None, text: str) -> tuple[str, list]:
    """진행중이면 문구 + 투표 카드(참석/불참 버튼), 마감됐으면 문구만(스쿼드 버튼은 유지)."""
    if is_closed(poll, now):
        return text, squad_buttons(poll, now)
    return f"{text}\n{poll_summary(poll)}", vote_buttons(poll)


def squad_buttons(poll: dict, now: datetime | None) -> list[dict]:
    """'스쿼드 만들기' 버튼은 더 이상 붙이지 않는다 (팀짜기는 명령어로만)."""
    return []


def _regen_or_card(poll: dict, now: datetime | None, text: str, regen: bool) -> tuple[str, list]:
    """마감된 투표의 참석자가 바뀌었으면 팀을 백그라운드로 자동 재구성한다 (버튼 없음). 진행 중이면 투표 카드."""
    if regen and is_closed(poll, now):
        start_squad(poll, "auto")
        return f"{text}\n⚽ 팀을 자동으로 다시 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n👉 {SQUAD_URL}", []
    return poll_reply(poll, now, text)


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


def group_keys(user_key: str) -> list[str]:
    """이 키와 같은 사람으로 묶인 키 전부 (묶인 적 없으면 자기 키만)."""
    for keys in _load(LINKS_FILE).values():
        if user_key in keys:
            return list(keys)
    return [user_key]


def register_name(user_key: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise PollError("이름을 입력해주세요.")
    group = set(group_keys(user_key))
    users = _load(USERS_FILE)
    if any(n == name and k not in group for k, n in users.items()):  # 동명이인 등록 불가 (같은 사람의 묶인 키는 제외)
        raise PollError(f"'{name}' 이름은 이미 다른 사람이 쓰고 있어요. 다른 이름을 입력해주세요.", 409)
    for k in group:  # 묶인 키 전부 같은 이름으로 바꾼다
        users[k] = name
    _save(USERS_FILE, users)
    polls = load_polls()  # 이미 투표했다면 명단의 이름도 갱신 (묶인 키 전부)
    for poll in (p for lst in polls.values() for p in lst):
        for side in ("attend", "absent"):
            for k in group:
                if k in poll[side]:
                    poll[side][k] = name
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
        "start_hour": DEFAULT_START_HOUR,  # 새 투표는 기본 8시 (시간 명령으로 이 투표만 바꿀 수 있음)
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
        lines.append(f"{i}. {n} · {pos_text}")
    return f"👥 전체 명단 ({len(names)}명)\n(이름 · 포지션 1순위/2순위)\n" + "\n".join(lines)


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


def set_seonchul(name: str, on: bool) -> str:
    """grades.py 의 SEONCHUL_PLAYERS 목록에 선출을 넣거나 뺀다. 바꾼 뒤 GitHub 에 커밋·푸시한다."""
    import subprocess
    if not _valid_grade_name(name):
        raise PollError("선출 이름 형식이 맞지 않아요. 예) 선출 홍길동")
    path = SQUAD_REPO / "grades.py"
    if not path.exists():
        raise PollError(f"grades.py 를 찾을 수 없어요: {path}", 500)
    with SQUAD_LOCK:
        pull_squad()
        src = path.read_text(encoding="utf-8")
        m = re.search(r"SEONCHUL_PLAYERS = \[(.*?)\]", src, re.S)
        if not m:
            raise PollError("grades.py 에서 SEONCHUL_PLAYERS 목록을 찾을 수 없어요.", 500)
        names = re.findall(r'^\s*"([^"]+)"', m.group(1), re.M)  # 주석(# ...) 줄은 제외
        if on and name in names:
            return f"⭐ {name} 은(는) 이미 선출이에요."
        if not on and name not in names:
            return f"{name} 은(는) 선출 목록에 없어요."
        names = names + [name] if on else [n for n in names if n != name]
        body = "SEONCHUL_PLAYERS = [\n" + "".join(f'    "{n}",\n' for n in names) + "]"
        path.write_text(src[:m.start()] + body + src[m.end():], encoding="utf-8")

        def run(*args):
            return subprocess.run(["git", *args], cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)

        run("add", "grades.py")
        r = run("commit", "-m", f"선출 {'등록' if on else '해제'}: {name}")
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            raise PollError(f"커밋 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
        r = run("push", "origin", "HEAD")
        if r.returncode != 0:
            raise PollError(f"GitHub 푸시 실패: {(r.stderr or r.stdout).strip()[:200]}", 500)
    if on:
        return f"⭐ {name} 선출 등록 완료 (합산점수 고정, GitHub 반영)"
    return f"✅ {name} 선출 해제 완료 (GitHub 반영)"


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


def _to_fullwidth(s: str) -> str:
    """영문·기호를 전각 문자로 바꾼다 (한글과 같은 폭으로 보이게)."""
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)


def _em(s: str) -> int:
    """한글·전각 문자 1글자 = 1em. 칸 맞춤 계산용."""
    import unicodedata
    return sum(1 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


GRADE_ABBR = {"패스": "패", "리턴": "리", "수비": "수", "드리블": "드", "볼소유": "볼", "골결정력": "골"}


def _grade_table_lines(names: list[str], gm, compact: bool = False) -> list[str]:
    """등급 표 줄 목록 (머리글 + 선수별 행). 전각 문자·공백으로 칸을 맞춘다."""
    keys = gm.ABILITY_KEYS
    SP = "\u3000"
    name_em = max([_em("이름")] + [_em(n) for n in names])
    label = (lambda k: GRADE_ABBR.get(k, k)) if compact else (lambda k: k)
    key_em = {k: _em(label(k)) for k in keys}

    def left(text: str, em: int) -> str:
        return text + SP * (em - _em(text))

    def center(text: str, em: int) -> str:
        pad = em - _em(text)
        lft = pad // 2
        return SP * lft + text + SP * (pad - lft)

    lines = ["｜".join([left("이름", name_em)] + [label(k) for k in keys])]
    for n in names:
        ab = gm.PLAYER_ABILITIES[n]
        cells = [left(n, name_em)] + [center(_to_fullwidth(ab.get(k, "-")), key_em[k]) for k in keys]
        lines.append("｜".join(cells))
    return lines


def grades_table_text() -> str:
    """전체 등급표: 합계(점수) 높은 순. grades.json 기준 (읽기만 함)."""
    pull_squad()
    gm = _load_squad_module("grades.py")
    names = sorted(gm.PLAYER_ABILITIES.keys(), key=lambda n: (-gm.player_score(n), n))
    return f"📊 등급표 ({len(names)}명)\n" + "\n".join(_grade_table_lines(names, gm, compact=False))


def grades_view_text(names: list[str]) -> str:
    """입력한 여러 명의 등급을 등급표와 같은 틀로 보여 준다. 미등록 이름은 아래에 따로 표시."""
    pull_squad()
    gm = _load_squad_module("grades.py")
    found = [n for n in names if n in gm.PLAYER_ABILITIES]
    missing = [n for n in names if n not in gm.PLAYER_ABILITIES]
    if not found:
        return f"❓ 등급 미등록: {', '.join(missing)}\n등록: 등급 이름 B B A A B B"
    text = f"📊 등급 조회 ({len(found)}명)\n" + "\n".join(_grade_table_lines(found, gm, compact=True))
    if missing:
        text += f"\n❓ 미등록: {', '.join(missing)}"
    return text


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


# 장소별 주소와 사진 (사진은 /var/www/squad 에 올려 둔 파일)
PLACE_INFO = {
    "달월": {"addr": "경기 시흥시 월곶동 956-15", "img": "dalwol.jpeg"},
    "월곶": {"addr": "경기도 시흥시 월곶동 520-256", "img": "wolgot.png"},
    "조남": {"addr": "경기도 시흥시 조남동 산19-1", "img": "jonam.png"},  # 조남은 7:7 로 짠다
}


def place_text(place: str) -> str:
    info = PLACE_INFO.get(place)
    if not info:
        return f"📍 장소: {place}"
    from urllib.parse import quote
    map_url = f"https://map.kakao.com/?q={quote(info['addr'])}"
    lines = [f"📍 장소: {place}", f"🏠 주소: {info['addr']}", f"🗺 지도: {map_url}"]
    if info.get("img"):
        lines.append(f"🖼 사진: {SQUAD_URL}{info['img']}")
    return "\n\n".join(lines)


def latest_poll_any_room() -> dict | None:
    """방과 상관없이 가장 최근에 만든 투표 (일반채팅방에서 스쿼드를 해도 장소를 가져온다)."""
    all_polls = [p for lst in load_polls().values() for p in (lst or [])]
    if not all_polls:
        return None
    def _created(p: dict) -> datetime:
        c = p.get("created")  # 예전 투표는 created 가 비어 있을 수 있다 → 가장 오래된 것으로 취급
        return datetime.fromisoformat(c) if c else datetime.min.replace(tzinfo=KST)
    return max(all_polls, key=_created)


def get_start_hour(poll: dict | None = None) -> int:
    """이 투표의 경기 시작 시각(시). 저장된 값이 없으면 기본 8시."""
    h = (poll or {}).get("start_hour", DEFAULT_START_HOUR)
    return h if isinstance(h, int) and 0 <= h <= 22 else DEFAULT_START_HOUR


def set_poll_start_hour(poll: dict, h: int) -> None:
    """투표 하나의 경기 시작 시각만 바꾼다 (다른 투표에는 영향 없음)."""
    polls = load_polls()
    for lst in polls.values():
        for p in lst or []:
            if p.get("id") == poll.get("id") and p.get("created") == poll.get("created"):
                p["start_hour"] = h
    _save(POLLS_FILE, polls)


def notice_rules(poll: dict | None = None) -> str:
    """경기 시간(2시간)과 지각 체크 시각을 이 투표의 시작 시각으로 만든다. 예) 7 → 07:00~09:00, 07시 01분부터"""
    h = get_start_hour(poll)
    return (f"⏰ 경기 시간: {h:02d}:00~{h + 2:02d}:00\n"
            f"- 지각 체크: {h:02d}시 01분부터\n"
            "- 노쇼: 경기 시작 후 1시간 경과 시점부터")


def make_squad(room: str) -> str:
    """스쿼드 응답 = 주간 공지 (장소·주소·지도·사진 + 시간·규칙 + 스쿼드 링크). 장소는 가장 최근 투표 기준."""
    try:
        poll = latest_poll_any_room()
    except Exception:
        poll = None
    parts = ["📢 이번 주 풋살 공지", f"👥 스쿼드 구성 페이지: {SQUAD_URL}"]
    if poll:
        parts.append(place_text(poll["place"]))
    parts.append(notice_rules(poll))
    parts.append("다들 늦지 않게 와주세요!")
    return "\n\n".join(parts)


def build_squad_html(members: list[str], game_mode: str = "auto", seven: bool = False) -> None:
    """weekenders-futsal 레포의 export_html.generate_html 로 index.html 생성."""
    import importlib.util

    export_path = SQUAD_REPO / "export_html.py"
    if not export_path.exists():
        raise PollError(f"스쿼드 레포를 찾을 수 없어요: {SQUAD_REPO}", 500)

    # export_html.py 를 동적 로드 (team_maker/grades/positions 등 같은 폴더에서 import 됨)
    spec = importlib.util.spec_from_file_location("squad_export", export_path)
    mod = importlib.util.module_from_spec(spec)
    for _m in ("members", "team_maker", "grades", "positions"):  # 매번 최신 파일을 읽도록 캐시 제거
        sys.modules.pop(_m, None)
    sys.path.insert(0, str(SQUAD_REPO))  # team_maker 등 import 가능하게
    spec.loader.exec_module(mod)
    # save_members=True: 투표 인원(보정된 이름)을 weekenders-futsal 의 members.py 에도 저장
    mod.SEVEN_V_SEVEN = seven  # 조남: 14명 자체를 7:7(벤치 0)로 짠다
    # 팀 배정은 난수로 섞으므로, 인원·모드·7:7 여부로 시드를 고정해 같은 조건이면 팀이 바뀌지 않게 한다
    import random, hashlib
    seed_src = f"{','.join(sorted(members))}|{game_mode}|{seven}"
    random.seed(int(hashlib.md5(seed_src.encode("utf-8")).hexdigest()[:8], 16))
    mod.generate_html(players=members, game_mode=game_mode, open_browser=False, save_members=True)


def push_squad(commit_msg: str) -> bool:
    """weekenders-futsal 레포에 index.html 커밋·push. 성공하면 True."""
    import subprocess

    def run(*args):
        return subprocess.run(["git", *args], cwd=SQUAD_REPO,
                              capture_output=True, text=True, timeout=60)

    run("add", "members.py")  # index.html 은 푸시하지 않는다 (웹 폴더로만 복사). 인원 저장(members.py)만 커밋
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
        subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "HEAD"],
                       cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("squad pull 경고 (무시하고 진행):", repr(e))


SQUAD_LOCK = threading.Lock()  # 팀 구성은 한 번에 하나씩


def restore_squad_html() -> None:
    """생성된 index.html 의 변경을 레포에서 되돌린다 (푸시 대상이 아니므로 작업 흔적을 남기지 않는다)."""
    import subprocess
    try:
        subprocess.run(["git", "checkout", "--", "index.html"], cwd=SQUAD_REPO,
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("index.html 복원 경고:", repr(e))


def publish_squad() -> bool:
    """만든 index.html 을 웹 서버 폴더로 복사한다 (GitHub push 없음). 성공하면 True."""
    import shutil
    try:
        SQUAD_PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SQUAD_PUBLISH_DIR / "index.html.tmp"
        shutil.copyfile(SQUAD_REPO / "index.html", tmp)
        tmp.replace(SQUAD_PUBLISH_DIR / "index.html")  # 복사 도중 페이지가 깨지지 않도록 교체
        # 봇 폴더(~/wekend-bot/index.html)에도 같은 파일을 복사한다
        home_tmp = HERE / "index.html.tmp"
        shutil.copyfile(SQUAD_REPO / "index.html", home_tmp)
        home_tmp.replace(HERE / "index.html")
        restore_squad_html()  # 레포의 index.html 은 만든 변경을 버리고 원래 상태로
        return True
    except OSError as e:
        print("squad 복사 실패:", repr(e))
        return False


def clear_seonchul_locked() -> bool:
    """grades.py 의 SEONCHUL_PLAYERS 를 비우고 GitHub 에 커밋·푸시한다. SQUAD_LOCK 을 잡은 상태에서 호출.
    순서: git pull → 목록 비우기 → 커밋 → push (거절되면 pull 후 한 번 더 push)."""
    import subprocess

    def run(*args):
        return subprocess.run(["git", *args], cwd=SQUAD_REPO, capture_output=True, text=True, timeout=60)

    path = SQUAD_REPO / "grades.py"
    if not path.exists():
        print("선출 초기화 건너뜀: grades.py 없음")
        return False
    r = run("pull", "--rebase", "origin", "HEAD")  # 먼저 최신으로 받는다
    if r.returncode != 0:
        print("선출 초기화: pull 실패 (건너뜀):", r.stderr)
        return False
    src = path.read_text(encoding="utf-8")
    m = re.search(r"SEONCHUL_PLAYERS = \[(.*?)\]", src, re.S)
    if not m or not re.findall(r'^\s*"([^"]+)"', m.group(1), re.M):
        return False  # 이미 비어 있음
    path.write_text(src[:m.start()] + "SEONCHUL_PLAYERS = [\n]" + src[m.end():], encoding="utf-8")
    run("add", "grades.py")
    r = run("commit", "-m", "선출 목록 초기화 (마감)")
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        print("선출 초기화 커밋 실패:", r.stderr)
        return False
    r = run("push", "origin", "HEAD")
    if r.returncode != 0:  # 그 사이 원격이 바뀌었으면 받고 한 번 더 시도
        run("pull", "--rebase", "origin", "HEAD")
        r = run("push", "origin", "HEAD")
    if r.returncode != 0:
        print("선출 초기화 푸시 실패:", r.stderr)
        return False
    return True


def clear_members_locked() -> bool:
    """members.py 의 MEMBERS 를 비운다 (SQUAD_LOCK 을 잡은 상태에서 호출)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("export_html", SQUAD_REPO / "export_html.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.update_members_file([])
        return True
    except Exception as e:
        print("명단 초기화 오류:", repr(e))
        return False


def _squad_job(title: str, members: list[str], game_mode: str, seven: bool = False, reset_members: bool = False) -> None:
    with SQUAD_LOCK:
        try:
            pull_squad()
            if reset_members:  # 기본 마감: 최신 pull → 명단 초기화 → push → 참석자로 팀짜기
                clear_members_locked()
                push_squad(f"명단 초기화: {title}")
            if not members:
                print(f"팀짜기 {title}: 참석자가 없어 건너뜀")
                return
            build_squad_html(members, game_mode, seven)
            pushed = push_squad(f"팀 구성 업데이트: {title}")  # GitHub 에 커밋·푸시
            ok = publish_squad()  # VM 웹 페이지 갱신
            print(f"팀짜기 {title}: {'완료' if ok else '실패'}" + ("" if pushed else " (GitHub push 실패)"))
        except Exception as e:
            print(f"팀짜기 {title} 오류:", repr(e))


def start_squad(poll: dict, game_mode: str = "auto", reset_members: bool = False) -> None:
    """팀 구성을 백그라운드에서 시작한다 (봇 응답이 멈추지 않도록)."""
    members = list(poll["attend"].values())
    seven = poll.get("place") == "조남"  # 조남 장소면 7:7
    threading.Thread(target=_squad_job, args=(poll["title"], members, game_mode, seven, reset_members), daemon=True).start()


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
            start_squad(p, reset_members=True)  # 기본 마감: 명단 초기화 → 참석자 추가 → 팀짜기
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


def update_fixed_pair_locked(kind: str, a: str, b: str) -> bool:
    """members.py 의 FIXED_SAME_TEAM / FIXED_DIFF_TEAM 에서 이 쌍을 빼고, kind 에 맞는 쪽에 넣는다.
    SQUAD_LOCK 을 잡은 상태에서 호출. 같은 쌍이 반대쪽에 있으면 자동으로 옮겨진다."""
    import importlib.util, re
    path = SQUAD_REPO / "members.py"
    spec = importlib.util.spec_from_file_location("members_edit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    same = [tuple(x) for x in getattr(mod, "FIXED_SAME_TEAM", [])]
    diff = [tuple(x) for x in getattr(mod, "FIXED_DIFF_TEAM", [])]
    if kind == "all":  # 전체 초기화
        same, diff = [], []
    same = [p for p in same if set(p) != {a, b}]
    diff = [p for p in diff if set(p) != {a, b}]
    if kind == "same":
        same.append((a, b))
    elif kind == "diff":
        diff.append((a, b))

    def fmt(lst):
        return "[" + ", ".join(f'("{x}", "{y}")' for x, y in lst) + "]"

    src = path.read_text(encoding="utf-8")
    src, n1 = re.subn(r"FIXED_SAME_TEAM\s*=\s*\[.*?\]", "FIXED_SAME_TEAM = " + fmt(same), src, count=1, flags=re.S)
    src, n2 = re.subn(r"FIXED_DIFF_TEAM\s*=\s*\[.*?\]", "FIXED_DIFF_TEAM = " + fmt(diff), src, count=1, flags=re.S)
    if n1 == 0 or n2 == 0:
        print("고정 조건 저장 실패: members.py 에서 FIXED_* 항목을 찾지 못함")
        return False
    path.write_text(src, encoding="utf-8")
    return True


def _fixed_job(title: str, members: list[str], kind: str, a: str, b: str, seven: bool) -> None:
    """백그라운드: git pull → 고정 조건 저장 → git push → 팀짜기 → push → 스쿼드 페이지 갱신."""
    import subprocess
    with SQUAD_LOCK:
        try:
            pull_squad()
            if not update_fixed_pair_locked(kind, a, b):
                return
            push_squad(f"고정 조건 변경: {({'same': '같은팀', 'diff': '다른팀', 'none': '팀삭제', 'all': '팀삭제 전체'})[kind]} {a} {b}")
            build_squad_html(members, "auto", seven)
            pushed = push_squad(f"팀 구성 업데이트: {title}")
            ok = publish_squad()
            print(f"고정 팀짜기 {title}: {'완료' if ok else '실패'}" + ("" if pushed else " (GitHub push 실패)"))
        except Exception as e:
            print(f"고정 팀짜기 {title} 오류:", repr(e))


def start_fixed_pair(kind: str, a: str, b: str, poll: dict) -> None:
    """같은팀/다른팀 고정을 저장하고 팀을 백그라운드에서 다시 짠다."""
    members = list(poll["attend"].values())
    seven = poll.get("place") == "조남"
    threading.Thread(target=_fixed_job, args=(poll["title"], members, kind, a, b, seven), daemon=True).start()


def make_team(room: str, no: int | None = None, game_mode: str = "auto") -> str:
    """수동 팀짜기: 최근(또는 지정) 투표 참석자로 팀을 짠다. 결과는 잠시 뒤 링크에 반영된다."""
    if no is None:  # 방과 상관없이 가장 최근 투표 (스쿼드 명령과 같은 기준)
        poll = latest_poll_any_room()
        if poll is None:
            raise PollError(NO_POLL, 404)
    else:
        poll = get_poll_no(room, no)
    members = list(poll["attend"].values())
    if not members:
        raise PollError("참석자가 없어요. 참석 투표부터 받아주세요.")
    min_n = {"self": 10, "match": 6, "auto": 6}.get(game_mode, 6)
    if len(members) < min_n:
        if game_mode == "self":
            raise PollError(f"자체전은 10명 이상이어야 해요. (현재 {len(members)}명)\n9명 이하면 '팀짜기 매칭'으로 짜 주세요.")
        raise PollError(f"팀을 짜려면 최소 {min_n}명이 필요해요. (현재 {len(members)}명)")
    start_squad(poll, game_mode)
    return (f"⚽ 팀 구성을 시작했어요. ({poll['title']}, {len(members)}명)\n"
            f"👉 {SQUAD_URL}\n(바로 반영됩니다)")


# ============================ 카톡 명령 처리 ============================
def _poll_summary_base(p: dict) -> str:
    att, absn = list(p["attend"].values()), list(p["absent"].values())
    return "\n".join([
        f"🗳 {p.get('no', '')}번 · {p['title']}" + (f"\n📝 {p['desc']}" if p.get("desc") else ""),
        f"마감: {fmt_deadline(p['closed_at'] if p.get('closed_at') else p['deadline'])}"
        + (" (마감됨)" if p.get("closed_at") else ""),
        f"\n참석 {len(att)}명: {', '.join(att) if att else '-'}",
        f"\n불참 {len(absn)}명: {', '.join(absn) if absn else '-'}",
    ])


COMMANDS = {"도움말", "이름", "투표", "참석", "불참", "멤버", "스쿼드", "명단", "포지션", "등급", "신규", "장소", "날짜", "투표종료", "투표완료", "투표조회", "투표삭제", "용병", "팀짜기", "팀다시짜기", "시간", "연결", "선출", "같은팀", "다른팀", "팀삭제", "교체", "방키", "미응답", "휴면"}
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


def _remember_room_member(room: str, user_key: str) -> None:
    """이 방에서 메시지를 보낸 등록자의 키를 기록한다 (멘션에 쓰려면 이 방의 키여야 한다)."""
    if not room or not user_key:
        return
    name = _load(USERS_FILE).get(user_key)
    if not name:
        return
    data = _load(ROOM_MEMBERS_FILE)
    members = data.setdefault(room, {})
    if members.get(user_key) != name:
        members[user_key] = name
        _save(ROOM_MEMBERS_FILE, data)


def room_key_names(room: str) -> dict:
    """이 방에서 유효한 {이름: botUserKey}. 투표 기록 + 이 방에서 보낸 메시지 기준."""
    out = {}
    for lst in (load_polls().get(room) or []):
        for k, n in list(lst.get("attend", {}).items()) + list(lst.get("absent", {}).items()):
            if n and k and not str(k).startswith("manual:"):
                out.setdefault(n, k)
    for k, n in (_load(ROOM_MEMBERS_FILE).get(room) or {}).items():
        out.setdefault(n, k)
    return out


def handle(text: str, user_key: str, room: str, now: datetime | None = None) -> tuple[str, list] | None:
    """카톡 명령어를 처리하고 (답장 텍스트, 버튼 목록) 을 돌려준다. 명령어가 아니면 None."""
    _remember_room_member(room, user_key)
    parsed = parse_command(text)
    if not parsed:
        # 봇을 불렀는데 명령을 모르면 도움말 안내 (일반 대화에는 답하지 않는다)
        if (text or "").startswith(BOT_MENTION) and len((text or "").split()) >= 2:
            return UNKNOWN_HINT, []
        return None
    cmd, args = parsed
    try:
        result = _dispatch(cmd, args, user_key, room, now)
    except PollError as e:
        result = (e.message, [])
    _remember_room_member(room, user_key)  # 연결 등으로 이름이 방금 생긴 경우에도 이 방의 키를 기록한다
    return result


def _parse_no(s: str) -> int:
    s = s.rstrip("번")
    if not s.isdigit():
        raise PollError("번호는 숫자로 입력해주세요. 예) 투표삭제 1")
    return int(s)


ALLOWED_NAMES = {"김민규", "이성학", "김기항"}  # 나머지 명령어를 쓸 수 있는 닉네임
OPEN_COMMANDS = {"이름", "참석", "불참", "연결"}  # 이름 등록과 참석/불참은 누구나 쓸 수 있다


def _dispatch(cmd: str, args: list[str], user_key: str, room: str, now: datetime | None) -> tuple[str, list]:
    if cmd in ("장소", "날짜") or (cmd == "멤버" and args):  # 수정 없이: 장소 달월 / 날짜 261031 / 멤버 김기항 김민규
        args, cmd = [cmd] + args, "수정"
    if cmd == "교체":  # 교체 김기항 김민규 → 김기항 불참, 김민규 참석 (멤버 수정과 같은 동작)
        args, cmd = ["멤버"] + args, "수정"
    if cmd not in OPEN_COMMANDS:  # 이름·참석·불참 외에는 지정된 멤버만 쓸 수 있다 (도움말 포함)
        my_name = _load(USERS_FILE).get(user_key)
        if not my_name:
            return "먼저 '@위켄봇 이름 [닉네임]'으로 이름을 등록해 주세요.", []
        if my_name not in ALLOWED_NAMES:
            return "이 명령은 지정된 멤버만 쓸 수 있어요.", []

    if cmd == "도움말":
        return HELP_TEXT, []

    if cmd == "휴면":  # 휴면 김기항 김민규 → 미응답 명단에서 뺀다 / 휴면 김기항 해제 → 되돌림 / 휴면 → 목록
        names = set(dormant_names())
        if not args:
            return ("휴면 인원: " + ", ".join(sorted(names))) if names else "휴면 인원이 없어요.", []
        if args[0] == "해제" or args[-1] == "해제":  # 휴면 해제 김기항 / 휴면 김기항 해제 둘 다 가능
            targets = [a for a in (args[1:] if args[0] == "해제" else args[:-1]) if a and a != "해제"]
            if not targets:
                return "사용법: 휴면 해제 김기항  (또는 휴면 김기항 해제)", []
            names -= set(targets)
            _save(DORMANT_FILE, {"names": sorted(names)})
            return f"휴면 해제: {', '.join(targets)}", []
        names |= set(args)
        _save(DORMANT_FILE, {"names": sorted(names)})
        return f"휴면 등록: {', '.join(args)} (미응답 명단에서 빠져요)", []

    if cmd == "미응답":  # 미응답 → 최근 투표에서 아직 참석·불참을 누르지 않은 사람 (이름 나열)
        poll = latest_poll_any_room()
        if poll is None:
            raise PollError(NO_POLL, 404)
        responded = set(poll["attend"].values()) | set(poll["absent"].values())
        waiting = [n for n in roster_names() if n not in responded]
        if not waiting:
            return "모두 응답했어요. 👏", []
        keys = room_key_names(room)  # 명령을 입력한 이 방에서 유효한 키
        mentions, shown, plain = {}, [], []
        for i, n in enumerate(waiting[:15]):  # 멘션은 한 답장에 최대 15명
            k = keys.get(n)
            if k:
                tag = f"u{i}"
                mentions[tag] = {"type": "botUserKey", "id": k}
                shown.append("{{#mentions." + tag + "}}")
            else:
                shown.append(n)
            plain.append(n)
        head = f"⏳ 미응답 {len(waiting)}명: "
        tail = f"\n참석 여부 부탁드려요! 마감: {fmt_deadline(poll['deadline'])}"
        if mentions:
            globals()["PENDING_MENTIONS"] = {"text": head + ", ".join(shown) + tail,
                                             "plain": head + ", ".join(plain) + tail,
                                             "mentions": mentions}
        return head + ", ".join(plain) + tail, []

    if cmd == "방키":  # 이 방의 키 (정기 투표 방 설정용)
        return f"이 방의 키예요.\n{room}", []

    if cmd == "시간":  # 시간 7 → 이번 투표 07:00~09:00 / 시간 10 → 10:00~12:00 (새 투표는 기본 8시)
        poll = latest_poll_any_room()
        if not args:
            h = get_start_hour(poll)
            return f"⏰ 현재 경기 시간: {h:02d}:00~{h + 2:02d}:00\n사용법: 시간 7 (7:00~9:00으로 변경)", []
        t = args[0].rstrip("시")
        if not t.isdigit() or not 0 <= int(t) <= 22:
            return "사용법: 시간 [시작 시각 0~22]\n예) 시간 7 → 07:00~09:00", []
        if poll is None:
            raise PollError(NO_POLL, 404)
        set_poll_start_hour(poll, int(t))
        h = int(t)
        return f"⏰ 이번 투표 경기 시간을 {h:02d}:00~{h + 2:02d}:00으로 바꿨어요.", []

    if cmd == "연결":  # 연결 → 이 방용 코드 발급 / 연결 4827 → 다른 방에서 받은 코드로 같은 사람 묶기
        now_ts = now_kst()
        codes = {c: v for c, v in _load(LINK_FILE).items() if datetime.fromisoformat(v["exp"]) > now_ts}
        if not args:
            my_name = _load(USERS_FILE).get(user_key)
            if not my_name:
                return "먼저 '@위켄봇 이름 [닉네임]'으로 이름을 등록해 주세요.", []
            code = f"{secrets.randbelow(9000) + 1000}"
            while code in codes:
                code = f"{secrets.randbelow(9000) + 1000}"
            codes[code] = {"key": user_key, "name": my_name,
                           "exp": (now_ts + timedelta(minutes=10)).isoformat()}
            _save(LINK_FILE, codes)
            return (f"🔗 연결 코드: {code}\n10분 안에 다른 방에서 '@위켄봇 연결 {code}'을 입력해 주세요."), []
        entry = codes.get(args[0])
        if not entry:
            _save(LINK_FILE, codes)
            return "코드가 맞지 않거나 만료됐어요. '@위켄봇 연결'로 새 코드를 받아 주세요.", []
        if entry["key"] == user_key:
            return "같은 방에서 받은 코드예요. 다른 방에서 입력해 주세요.", []
        # 두 키를 한 묶음으로 저장하고, 묶인 키 전부 같은 이름으로 맞춘다
        merged = set(group_keys(entry["key"])) | set(group_keys(user_key))
        links = {g: ks for g, ks in _load(LINKS_FILE).items()
                 if not (set(ks) & merged)}
        links[entry["key"]] = sorted(merged)
        _save(LINKS_FILE, links)
        users = _load(USERS_FILE)
        for k in merged:
            users[k] = entry["name"]
        _save(USERS_FILE, users)
        del codes[args[0]]
        _save(LINK_FILE, codes)
        return f"✅ 연결됐어요. 이 방에서도 '{entry['name']}'(으)로 인식합니다.", []

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
            names = [a.strip('"') for a in args if a.strip('"')]  # 띄어쓰기로 여러 명 가능: 참석 김기항 김민규
            cur = (load_polls().get(room) or [])
            if not cur:
                raise PollError(NO_POLL, 404)
            side = SIDES[cmd]
            already = set(cur[-1][side].values())  # 이미 같은 상태인 사람은 건드리지 않는다
            changed = [nm for nm in names if nm not in already]
            if not changed:  # 바뀐 게 없으면 팀을 다시 짜지 않는다
                return f"{', '.join(names)}님은 이미 {cmd} 상태예요.", []
            poll = None
            for nm in changed:
                poll = set_attendance(room, nm, side)
            return _regen_or_card(poll, now, f"{', '.join(changed)}님을 {cmd}으로 바꿨어요.", True)
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
        att = list(poll["attend"].values())
        absn = list(poll["absent"].values())
        status = "마감" if is_closed(poll, now) else "진행중"
        body = "\n".join(f"{i}. {n}" for i, n in enumerate(att, 1)) or "참석자가 없어요."
        abody = ", ".join(absn) if absn else "-"
        return (f"🗳 {poll['no']}번 · {poll['title']} ({status})\n"
                f"참석 {len(att)}명\n{body}\n"
                f"불참 {len(absn)}명: {abody}"), []

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

    if cmd == "선출":  # 선출 홍길동 → 등록 / 선출 홍길동 해제 → 해제 / 선출 → 목록 (합산점수 40점 고정)
        if not args:
            names = _load_squad_module("grades.py").SEONCHUL_PLAYERS
            return ("⭐ 선출 목록: " + ", ".join(names)) if names else "선출 목록이 비어 있어요.", []
        if len(args) >= 2 and args[-1] in ("해제", "취소"):
            return set_seonchul(" ".join(args[:-1]), False), []
        name = " ".join(args)
        msg = set_seonchul(name, True)  # 선출 명단에 등록 (GitHub 반영)
        try:  # 용병처럼 최근 투표의 참석자에도 넣는다 (이미 참석자면 선출 명단에만 남는다)
            poll, added, _ = add_mercenaries(room, None, [name], now)
        except PollError:
            return msg + "\n(진행중인 투표가 없어 참석자 추가는 건너뛰었어요.)", []
        if added:
            msg += f"\n'{poll['title']}' 참석자에 추가했어요."
        else:
            msg += "\n이미 참석자예요. 선출 명단에만 등록했어요."
        return _regen_or_card(poll, now, msg, bool(added))

    if cmd == "등급":  # 등급 → 전체 등급표 / 등급 김민규 → 조회 / 등급 김민규 수비 A / 등급 김민규 B B A A B B
        args = [a for a in args if a != "등급"]  # 입력에 '등급' 단어가 섞여도 무시
        if args == ["참석"]:  # 등급 참석 → 스쿼드용 명단(weekenders-futsal 의 members.py MEMBERS) 등급을 한번에
            names = list(getattr(_load_squad_module("members.py"), "MEMBERS", []))
            if not names:
                return "스쿼드용 명단(MEMBERS)이 비어 있어요.", []
            return grades_view_text(names), []
        if not args:
            return grades_table_text(), []
        names = [a for a in args if a not in ("·", ",", "/")]
        name, mode, changes = parse_grade_args(args)
        if mode == "view":  # 이름만 여러 개 → 여러 명 조회
            if not all(_valid_grade_name(n) for n in names):
                return GRADE_USAGE, []
            return grades_view_text(names), []
        if mode == "error":
            return GRADE_USAGE, []
        return set_player_grades(name, mode, changes), []

    if cmd == "명단":  # weekenders-futsal 의 전체 명단
        return roster_text(), []

    if cmd == "스쿼드":  # 스쿼드 페이지 주소만 안내
        return make_squad(room), []

    if cmd == "팀삭제" and not args:  # 팀삭제 → 같은팀/다른팀 고정 전체 초기화
        poll = latest_poll_any_room()
        if poll is None:
            raise PollError(NO_POLL, 404)
        if not is_closed(poll, now):
            return "같은팀/다른팀은 투표가 마감된 뒤에만 쓸 수 있어요. 먼저 투표를 마감해 주세요.", []
        att = list(poll["attend"].values())
        if len(att) < 6:
            return f"팀을 짜려면 참석자가 6명 이상이어야 해요. (현재 {len(att)}명)", []
        start_fixed_pair("all", "", "", poll)
        return ("✅ 같은팀/다른팀 고정 조건을 모두 지웠어요.\n"
                "⚽ 저장한 뒤 팀을 다시 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n"
                f"👉 {SQUAD_URL}"), []

    if cmd in ("같은팀", "다른팀", "팀삭제"):  # 팀삭제 김민규 김기항 → 두 사람의 고정 조건 삭제 / 같은팀 김민규 김기항 → 같은 팀 고정 / 다른팀 … → 다른 팀 고정 (저장 후 팀 다시 짜기)
        if len(args) != 2:
            return "사용법: 같은팀 [이름1] [이름2] / 다른팀 [이름1] [이름2] / 팀삭제 [이름1] [이름2] (전체 초기화는 팀삭제만)\n예) 같은팀 김민규 김기항", []
        if args[0] == args[1]:
            return "같은 사람끼리는 묶을 수 없어요.", []
        poll = latest_poll_any_room()
        if poll is None:
            raise PollError(NO_POLL, 404)
        if not is_closed(poll, now):  # 투표가 마감된 뒤에만 고정 조건을 정한다
            return "같은팀/다른팀은 투표가 마감된 뒤에만 쓸 수 있어요. 먼저 투표를 마감해 주세요.", []
        att = list(poll["attend"].values())
        missing = [x for x in args if x not in att]
        if missing:
            return f"참석자 명단에 없어요: {', '.join(missing)}", []
        if len(att) < 6:
            return f"팀을 짜려면 참석자가 6명 이상이어야 해요. (현재 {len(att)}명)", []
        kind = {"같은팀": "same", "다른팀": "diff", "팀삭제": "none"}[cmd]
        start_fixed_pair(kind, args[0], args[1], poll)
        if kind == "none":
            head = f"✅ {args[0]} · {args[1]} 고정 조건을 지웠어요."
        else:
            word = "같은 팀" if kind == "same" else "다른 팀"
            head = f"✅ {args[0]} · {args[1]} → {word}으로 고정했어요."
        return (f"{head}\n"
                f"⚽ 저장한 뒤 팀을 다시 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n"
                f"👉 {SQUAD_URL}"), []

    if cmd in ("팀짜기", "팀다시짜기"):  # 팀다시짜기 = 같은 동작 (현재 참석자로 팀을 다시 짜고 members.py 교체)
        # 팀짜기 → 최근 투표 참석자로 자동 모드(인원 따라 매칭/자체) / 팀짜기 2 → 2번 투표
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
            if is_closed(poll, now):  # 마감된 투표의 장소가 바뀌면 팀을 다시 짠다 (같은 인원·같은 규칙이면 팀은 그대로)
                start_squad(poll, "auto")
                return (f"{poll['no']}번 투표 장소를 {poll['place']}(으)로 바꿨어요.\n"
                        "⚽ 팀을 다시 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n"
                        f"👉 {SQUAD_URL}"), []
        elif sub == "날짜":
            if not sub_args or not parse_date(sub_args[0]):
                get_poll(room)
                return "사용법: 날짜 [YYMMDD]  예) 날짜 261031", []
            poll = update_poll(room, date_str=sub_args[0], no=no)
        elif sub == "멤버":  # 수정 멤버 김기항 김민규 → 김기항 불참, 김민규 참석 (이미 그 상태인 사람은 건드리지 않음)
            if len(sub_args) != 2:
                return "사용법: 멤버 [불참할 이름] [참석할 이름]\n예) 멤버 김기항 김민규", []
            out_name, in_name = sub_args
            polls0 = load_polls()
            p0 = (polls0.get(room) or [None])[-1] if no is None else find_by_no(polls0, room, no)
            if p0 is None:
                raise PollError(NO_POLL, 404)
            if in_name in p0["attend"].values():  # 들어올 사람이 이미 참석이면 교체 자체를 하지 않는다
                return f"{in_name}님은 이미 참석 명단에 있어서 교체하지 않았어요.\n{poll_summary(p0)}", []
            changes, poll = [], p0
            if out_name in p0["attend"].values():
                poll = set_attendance(room, out_name, SIDES["불참"], no=no)
                changes.append(f"{out_name}님을 불참")
            if in_name not in p0["attend"].values():
                poll = set_attendance(room, in_name, SIDES["참석"], no=no)
                changes.append(f"{in_name}님을 참석")
            if not changes:  # 이미 원하는 상태면 팀을 다시 짜지 않는다
                return f"바뀐 내용이 없어요. 참석 명단을 그대로 둘게요.\n{poll_summary(p0)}", []
            text = f"{', '.join(changes)}으로 바꿨어요."
            if is_closed(poll, now):  # 마감된 투표의 참석자가 바뀌면 팀을 자동으로 다시 짠다
                start_squad(poll, "auto")
                return (f"{text}\n"
                        "⚽ 팀을 자동으로 다시 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n"
                        f"👉 {SQUAD_URL}"), []
            return poll_reply(poll, now, text)
        else:
            return "수정할 항목은 장소 / 날짜 / 멤버 중 하나예요.", []
        return f"{poll['no']}번 투표를 수정했어요.\n{poll_summary(poll)}", []

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
        return _regen_or_card(poll, now, "\n".join(lines), bool(added))

    if cmd == "투표종료":
        no = _parse_no(args[0]) if args else None
        poll = close_poll(room, now, no)
        att = list(poll["attend"].values())
        # 카드 설명은 400자까지만 보이므로 짧게 쓴다. 버튼을 누르면 '@위켄봇 팀짜기'가 입력된다.
        text = (f"🔒 '{poll['title']}' 투표를 마감했습니다.\n"
                f"참석 {len(att)}명: {', '.join(att) if att else '-'}")
        if not att:  # 참석자가 없어도 스쿼드 명단은 마감 기준으로 비운다
            start_squad(poll, "auto", reset_members=True)
            return text + "\n\n📭 참석자가 없어서 스쿼드 명단을 비웠어요.", []
        start_squad(poll, "auto", reset_members=True)  # 수동 마감: 명단 덮어쓰기(초기화 후 참석자로 팀 짜기)
        text += f"\n\n⚽ 팀을 자동으로 짜는 중이에요. 잠시 후 스쿼드 페이지에 반영됩니다.\n👉 {SQUAD_URL}"
        return text, []

    if cmd == "투표조회":
        if args and args[0] == "전체":  # 투표조회 전체 → 모든 방의 투표를 방별로
            blocks = []
            for i, (r, lst) in enumerate(load_polls().items(), 1):
                if not lst:
                    continue
                lines = [f"{p.get('no')}번 {'🔒' if is_closed(p, now) else '🟢'} {p['title']} (참석 {len(p['attend'])}명)"
                         for p in lst]
                blocks.append(f"[방 {i} · …{r[-6:]}]\n" + "\n".join(lines))
            if not blocks:
                raise PollError(NO_POLL, 404)
            return "[전체 투표 목록] 🟢진행중 🔒마감\n\n" + "\n\n".join(blocks), []
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


PENDING_MENTIONS = None  # 멘션 답장 준비 (미응답 명령이 설정, reply 가 사용 후 비움)


async def reply(token: str, callback_token: str, text: str, buttons: list) -> None:
    """답장을 보낸다. 버튼이 있으면 카드로 먼저 시도하고, 거부되면 텍스트로 다시 보낸다."""
    global PENDING_MENTIONS
    pending, PENDING_MENTIONS = PENDING_MENTIONS, None
    import httpx

    headers = {"Authorization": f"KakaoAK {token}", "X-Bot-Callback-Token": callback_token}

    def body_of(outputs: list) -> dict:
        return {"skillResponse": {"version": "2.0", "template": {"outputs": outputs}}}

    bodies = [body_of([{"simpleText": {"text": text}}])]
    if pending:  # 멘션 답장 → 실패하면 이름만 적힌 평문 답장
        mention_body = {"skillResponse": {"version": "2.0",
                                          "template": {"outputs": [{"simpleText": {"text": pending["text"]}}]},
                                          "extra": {"mentions": pending["mentions"]}}}
        bodies = [mention_body, body_of([{"simpleText": {"text": pending["plain"]}}])]
    elif buttons:
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
