# 카카오톡 투표 봇 + 관리용 REST API
# uv run 시 아래 의존성을 자동 설치합니다 (pip install 불필요)
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "websockets>=15.0", "fastapi>=0.110", "uvicorn>=0.29"]
# ///
"""
카톡 명령어
  !도움말                  봇 사용법
  !이름 홍길동             내 이름 등록 (카카오가 닉네임을 보내주지 않아 최초 1회 필요)
  !투표 261017 달월        '261017 달월' 참석/불참 투표 생성 (다음주 수요일 23시 마감)
  !참석 / !불참            투표하기
  !멤버                    참석 인원 확인
  !스쿼드                  참석 인원으로 스쿼드 구성 (현재는 "구성완료" 출력)
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
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
POLLS_FILE = HERE / "polls.json"
USERS_FILE = HERE / "users.json"
LOG_FILE = HERE / "events.log"
DEBUG = os.environ.get("DEBUG") == "1"

GW_URL = "wss://bot-gateway.kakao.com/gateway"
CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"

HELP_TEXT = """[봇 사용법]
!이름 [닉네임] : 내 이름 등록 (처음 1회)
!투표 날짜 장소 : 참석/불참 투표 생성
   예) !투표 261017 달월
   → 해당 날짜 다음주 수요일 23시 마감
!참석 / !불참 : 투표하기
!멤버 : 참석 인원 확인
!스쿼드 : 참석 인원으로 스쿼드 구성
!수정 장소 [새 장소] : 장소 변경
!수정 날짜 [YYMMDD] : 날짜 변경
!수정 멤버 [A] [B] : 참석자 A → B 변경
!삭제 : 진행중인 투표 삭제"""

VOTE_HINT = "👉 !참석 또는 !불참 을 입력해주세요."
VOTE_BUTTONS = [
    {"label": "참석", "action": "message", "messageText": "!참석"},
    {"label": "불참", "action": "message", "messageText": "!불참"},
]
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


def calc_deadline(date: datetime) -> datetime:
    """해당 날짜가 속한 주의 다음주 수요일 23:00 (월요일 시작 기준)"""
    monday = date - timedelta(days=date.weekday())
    return (monday + timedelta(days=9)).replace(hour=23, minute=0, second=0, microsecond=0)


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


NO_POLL = "진행중인 투표가 없어요. !투표 날짜 장소 로 만들어주세요."


def now_kst() -> datetime:
    return datetime.now(KST)


def is_closed(poll: dict, now: datetime | None = None) -> bool:
    return (now or now_kst()) > datetime.fromisoformat(poll["deadline"])


def _require(polls: dict, room: str) -> dict:
    poll = polls.get(room)
    if not poll:
        raise PollError(NO_POLL, 404)
    return poll


def _retitle(poll: dict) -> None:
    poll["title"] = f"{poll['date']} {poll['place']}"


def get_poll(room: str) -> dict:
    return _require(_load(POLLS_FILE), room)


def list_polls() -> dict:
    return _load(POLLS_FILE)


def register_name(user_key: str, name: str) -> str:
    name = name.strip()
    if not name:
        raise PollError("이름을 입력해주세요.")
    users = _load(USERS_FILE)
    users[user_key] = name
    _save(USERS_FILE, users)
    polls = _load(POLLS_FILE)  # 이미 투표했다면 명단의 이름도 갱신
    for poll in polls.values():
        for side in ("attend", "absent"):
            if user_key in poll[side]:
                poll[side][user_key] = name
    _save(POLLS_FILE, polls)
    return name


def create_poll(room: str, date_str: str, place: str, creator: str, now: datetime | None = None) -> dict:
    date = parse_date(date_str)
    if not date:
        raise PollError("날짜는 YYMMDD 형식으로 입력해주세요. 예) 261017")
    place = (place or "").strip()
    if not place:
        raise PollError("장소를 입력해주세요.")
    polls = _load(POLLS_FILE)
    old = polls.get(room)
    if old and not is_closed(old, now):
        raise PollError(f"이미 진행중인 투표가 있어요.\n'{old['title']}'\n먼저 !삭제 후 다시 만들어주세요.", 409)
    poll = {
        "date": date_str,
        "place": place,
        "title": "",
        "deadline": calc_deadline(date).isoformat(),
        "attend": {},
        "absent": {},
        "creator": creator,
    }
    _retitle(poll)
    polls[room] = poll
    _save(POLLS_FILE, polls)
    return poll


def cast_vote(room: str, user_key: str, name: str, side: str, now: datetime | None = None) -> dict:
    """side: 'attend' | 'absent'"""
    polls = _load(POLLS_FILE)
    poll = _require(polls, room)
    if is_closed(poll, now):
        raise PollError(f"'{poll['title']}' 투표는 마감되었습니다.", 409)
    other = "absent" if side == "attend" else "attend"
    poll[other].pop(user_key, None)
    poll[side][user_key] = name
    _save(POLLS_FILE, polls)
    return poll


def remove_vote(room: str, name: str) -> dict:
    polls = _load(POLLS_FILE)
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


def update_poll(room: str, date_str: str | None = None, place: str | None = None) -> dict:
    polls = _load(POLLS_FILE)
    poll = _require(polls, room)
    if date_str is not None:
        date = parse_date(date_str)
        if not date:
            raise PollError("날짜는 YYMMDD 형식으로 입력해주세요. 예) 261024")
        poll["date"] = date_str
        poll["deadline"] = calc_deadline(date).isoformat()
    if place is not None:
        if not place.strip():
            raise PollError("장소를 입력해주세요.")
        poll["place"] = place.strip()
    _retitle(poll)
    _save(POLLS_FILE, polls)
    return poll


def replace_member(room: str, a: str, b: str) -> dict:
    polls = _load(POLLS_FILE)
    poll = _require(polls, room)
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


def delete_poll(room: str) -> str:
    polls = _load(POLLS_FILE)
    title = _require(polls, room)["title"]
    del polls[room]
    _save(POLLS_FILE, polls)
    return title


def make_squad(room: str) -> str:
    _require(_load(POLLS_FILE), room)
    # TODO: 참석 인원으로 실제 스쿼드 구성 로직 추가
    return "구성완료"


# ============================ 카톡 명령 처리 ============================
def poll_summary(p: dict) -> str:
    att, absn = list(p["attend"].values()), list(p["absent"].values())
    return "\n".join([
        f"🗳 {p['title']}",
        f"마감: {fmt_deadline(p['deadline'])}",
        f"참석 {len(att)}명: {', '.join(att) if att else '-'}",
        f"불참 {len(absn)}명: {', '.join(absn) if absn else '-'}",
    ])


def handle(text: str, user_key: str, room: str, now: datetime | None = None) -> tuple[str, list] | None:
    """카톡 명령어를 처리하고 (답장 텍스트, 버튼 목록) 을 돌려준다. 명령어가 아니면 None."""
    text = (text or "").strip()
    if not text.startswith("!"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    cmd, args = parts[0], parts[1:]
    try:
        return _dispatch(cmd, args, user_key, room, now)
    except PollError as e:
        return e.message, []


def _dispatch(cmd: str, args: list[str], user_key: str, room: str, now: datetime | None) -> tuple[str, list]:
    if cmd == "도움말":
        return HELP_TEXT, []

    if cmd == "이름":
        if not args:
            current = _load(USERS_FILE).get(user_key, "없음")
            return f"사용법: !이름 [닉네임]\n현재 등록된 이름: {current}", []
        name = register_name(user_key, " ".join(args))
        return f"이름을 '{name}'(으)로 등록했어요.", []

    if cmd == "투표":
        if len(args) < 2:
            return "사용법: !투표 날짜 장소\n예) !투표 261017 달월", []
        poll = create_poll(room, args[0], " ".join(args[1:]), user_key, now)
        return f"투표가 생성되었습니다!\n{poll_summary(poll)}\n\n{VOTE_HINT}", VOTE_BUTTONS

    if cmd in SIDES:
        _require(_load(POLLS_FILE), room)
        if args:  # "!참석 홍길동" → 이름 등록도 함께
            register_name(user_key, " ".join(args))
        name = _load(USERS_FILE).get(user_key)
        if not name:
            return "먼저 이름을 알려주세요.\n예) !이름 홍길동\n(본인 이름을 최초 등록해주세요.)", []
        poll = cast_vote(room, user_key, name, SIDES[cmd], now)
        return f"{name}님 {cmd}으로 기록했어요.\n{poll_summary(poll)}", []

    if cmd == "멤버":
        poll = get_poll(room)
        att = list(poll["attend"].values())
        status = " (마감)" if is_closed(poll, now) else ""
        body = "\n".join(f"{i}. {n}" for i, n in enumerate(att, 1)) or "아직 참석자가 없어요."
        return f"🗳 {poll['title']}{status}\n참석 {len(att)}명\n{body}", []

    if cmd == "스쿼드":
        return make_squad(room), []

    if cmd == "수정":
        usage = "사용법: !수정 장소 [새 장소] / !수정 날짜 [YYMMDD] / !수정 멤버 [A] [B]"
        if not args:
            get_poll(room)
            return usage, []
        sub, sub_args = args[0], args[1:]
        if sub == "장소":
            if not sub_args:
                return "사용법: !수정 장소 [새 장소]", []
            poll = update_poll(room, place=" ".join(sub_args))
        elif sub == "날짜":
            if not sub_args or not parse_date(sub_args[0]):
                get_poll(room)
                return "사용법: !수정 날짜 [YYMMDD]  예) !수정 날짜 261024", []
            poll = update_poll(room, date_str=sub_args[0])
        elif sub == "멤버":
            if len(sub_args) != 2:
                return "사용법: !수정 멤버 [기존 이름] [새 이름]", []
            poll = replace_member(room, *sub_args)
        else:
            return "수정할 항목은 장소 / 날짜 / 멤버 중 하나예요.", []
        return f"수정되었습니다.\n{poll_summary(poll)}", []

    if cmd == "삭제":
        return f"'{delete_poll(room)}' 투표를 삭제했습니다.", []

    return "모르는 명령어예요. !도움말 을 입력해보세요.", []


# ============================ REST API ============================
def poll_view(room: str, p: dict) -> dict:
    return {
        "room": room,
        "title": p["title"],
        "date": p["date"],
        "place": p["place"],
        "deadline": p["deadline"],
        "closed": is_closed(p),
        "attend": list(p["attend"].values()),
        "absent": list(p["absent"].values()),
        "attend_count": len(p["attend"]),
        "absent_count": len(p["absent"]),
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

    class PollUpdate(BaseModel):
        date: str | None = Field(None, examples=["261024"], description="YYMMDD")
        place: str | None = Field(None, examples=["강남"])

    class Vote(BaseModel):
        name: str = Field(..., examples=["홍길동"])
        status: Literal["참석", "불참"] = "참석"

    class MemberChange(BaseModel):
        old: str = Field(..., examples=["홍길동"], description="바꿀 참석자 이름")
        new: str = Field(..., examples=["김철수"], description="새 이름")

    @app.get("/polls", summary="모든 카톡방의 투표 목록")
    def all_polls():
        return [poll_view(r, p) for r, p in list_polls().items()]

    @app.get("/polls/{room}", summary="투표 상세")
    def one_poll(room: str):
        return poll_view(room, get_poll(room))

    @app.get("/polls/{room}/members", summary="참석 인원 (!멤버)")
    def members(room: str):
        p = get_poll(room)
        return {"title": p["title"], "count": len(p["attend"]), "members": list(p["attend"].values())}

    @app.post("/polls/{room}", status_code=201, summary="투표 생성 (!투표)")
    def create(room: str, body: PollCreate):
        return poll_view(room, create_poll(room, body.date, body.place, creator="api"))

    @app.patch("/polls/{room}", summary="날짜·장소 수정 (!수정 날짜 / !수정 장소)")
    def update(room: str, body: PollUpdate):
        if body.date is None and body.place is None:
            raise PollError("date 또는 place 중 하나는 있어야 해요.")
        return poll_view(room, update_poll(room, body.date, body.place))

    @app.post("/polls/{room}/votes", summary="이름으로 참석/불참 기록")
    def vote(room: str, body: Vote):
        users = _load(USERS_FILE)
        key = next((k for k, n in users.items() if n == body.name), f"manual:{body.name}")
        return poll_view(room, cast_vote(room, key, body.name, SIDES[body.status]))

    @app.delete("/polls/{room}/votes/{name}", summary="명단에서 사람 빼기")
    def unvote(room: str, name: str):
        return poll_view(room, remove_vote(room, name))

    @app.put("/polls/{room}/members", summary="참석 멤버 변경 (!수정 멤버 A B)")
    def change_member(room: str, body: MemberChange):
        return poll_view(room, replace_member(room, body.old, body.new))

    @app.post("/polls/{room}/squad", summary="스쿼드 구성 (!스쿼드)")
    def squad(room: str):
        return {"result": make_squad(room)}

    @app.delete("/polls/{room}", summary="투표 삭제 (!삭제)")
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


async def main() -> int:
    import websockets

    token = os.environ.get("BOT_TOKEN")
    if not token:
        sys.exit('BOT_TOKEN 환경변수가 없습니다. .env 파일이나 export BOT_TOKEN="봇 인증 토큰" 으로 설정하세요.')

    start_head = git("rev-parse", "HEAD")
    print(f"봇 시작 (버전 {start_head[:7] or '알수없음'})")
    stop = asyncio.Event()
    if start_head:
        asyncio.create_task(watch_updates(start_head, stop))
    api_task = asyncio.create_task(serve_api(stop))

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
                try:
                    result = handle(content, user_key, room)
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
