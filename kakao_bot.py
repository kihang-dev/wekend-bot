# 카카오톡 투표 봇
# uv run 시 아래 의존성을 자동 설치합니다 (pip install 불필요)
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "websockets>=15.0"]
# ///
"""
명령어
  !도움말                  봇 사용법
  !이름 홍길동             내 이름 등록 (카카오가 닉네임을 보내주지 않아 최초 1회 필요)
  !투표 261017 달월        '261017 달월' 참석/불참 투표 생성 (다음주 수요일 23시 마감)
  !참석 / !불참            투표하기  (!참석 홍길동 처럼 이름을 붙이면 이름 등록도 함께)
  !멤버                    참석 인원 확인
  !스쿼드                  참석 인원으로 스쿼드 구성 (현재는 "구성완료" 출력)
  !수정 장소 강남          진행중인 투표 장소 변경
  !수정 날짜 261024        진행중인 투표 날짜 변경 (마감일도 다시 계산)
  !수정 멤버 A B           참석 멤버 A를 B로 변경
  !삭제                    진행중인 투표 삭제
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


# ============================ 저장소 ============================
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================ 날짜 계산 ============================
def parse_date(s: str) -> datetime | None:
    """'261017' → 2026-10-17"""
    try:
        return datetime.strptime(s, "%y%m%d").replace(tzinfo=KST)
    except ValueError:
        return None


def calc_deadline(date: datetime) -> datetime:
    """해당 날짜가 속한 주의 다음주 수요일 23:00 (월요일 시작 기준)"""
    monday = date - timedelta(days=date.weekday())
    return (monday + timedelta(days=9)).replace(hour=23, minute=0, second=0, microsecond=0)


def fmt_deadline(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d:%m/%d}({'월화수목금토일'[d.weekday()]}) {d:%H:%M}"


# ============================ 명령 처리 ============================
# 투표 기록: poll["attend"] / poll["absent"] = {사용자키: 이름}  (입력 순서 유지)
def names(votes: dict) -> list[str]:
    return list(votes.values())


def poll_summary(p: dict) -> str:
    att, absn = names(p["attend"]), names(p["absent"])
    return "\n".join([
        f"🗳 {p['title']}",
        f"마감: {fmt_deadline(p['deadline'])}",
        f"참석 {len(att)}명: {', '.join(att) if att else '-'}",
        f"불참 {len(absn)}명: {', '.join(absn) if absn else '-'}",
    ])


def handle(text: str, user_key: str, room: str, now: datetime | None = None) -> tuple[str, list] | None:
    """명령어를 처리하고 (답장 텍스트, 버튼 목록) 을 돌려준다. 명령어가 아니면 None."""
    text = (text or "").strip()
    if not text.startswith("!"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    cmd, args = parts[0], parts[1:]
    now = now or datetime.now(KST)

    users, polls = _load(USERS_FILE), _load(POLLS_FILE)
    poll = polls.get(room)
    closed = poll is not None and now > datetime.fromisoformat(poll["deadline"])

    if cmd == "도움말":
        return HELP_TEXT, []

    if cmd == "이름":
        if not args:
            return f"사용법: !이름 [닉네임]\n현재 등록된 이름: {users.get(user_key, '없음')}", []
        new_name = " ".join(args)
        users[user_key] = new_name
        _save(USERS_FILE, users)
        # 진행중인 투표에 이미 투표했다면 이름도 함께 갱신
        if poll:
            for side in ("attend", "absent"):
                if user_key in poll[side]:
                    poll[side][user_key] = new_name
            _save(POLLS_FILE, polls)
        return f"이름을 '{new_name}'(으)로 등록했어요.", []

    if cmd == "투표":
        if len(args) < 2:
            return "사용법: !투표 날짜 장소\n예) !투표 261017 달월", []
        date = parse_date(args[0])
        if not date:
            return "날짜는 YYMMDD 형식으로 입력해주세요. 예) 261017", []
        if poll and not closed:
            return f"이미 진행중인 투표가 있어요.\n'{poll['title']}'\n먼저 !삭제 후 다시 만들어주세요.", []
        place = " ".join(args[1:])
        poll = {
            "date": args[0],
            "place": place,
            "title": f"{args[0]} {place}",
            "deadline": calc_deadline(date).isoformat(),
            "attend": {},
            "absent": {},
            "creator": user_key,
        }
        polls[room] = poll
        _save(POLLS_FILE, polls)
        return f"투표가 생성되었습니다!\n{poll_summary(poll)}\n\n{VOTE_HINT}", VOTE_BUTTONS

    # ---- 이하 명령은 진행중인 투표가 필요 ----
    if cmd in ("참석", "불참", "멤버", "스쿼드", "수정", "삭제") and not poll:
        return "진행중인 투표가 없어요. !투표 날짜 장소 로 만들어주세요.", []

    if cmd in ("참석", "불참"):
        if closed:
            return f"'{poll['title']}' 투표는 마감되었습니다.", []
        if args:  # "!참석 홍길동" → 이름 등록도 함께
            users[user_key] = " ".join(args)
            _save(USERS_FILE, users)
        name = users.get(user_key)
        if not name:
            return "먼저 이름을 알려주세요.\n예) !이름 홍길동\n(본인 이름을 최초 등록해주세요.)", []
        target, other = ("attend", "absent") if cmd == "참석" else ("absent", "attend")
        poll[other].pop(user_key, None)
        poll[target][user_key] = name
        _save(POLLS_FILE, polls)
        return f"{name}님 {cmd}으로 기록했어요.\n{poll_summary(poll)}", []

    if cmd == "멤버":
        att = names(poll["attend"])
        status = " (마감)" if closed else ""
        body = "\n".join(f"{i}. {n}" for i, n in enumerate(att, 1)) or "아직 참석자가 없어요."
        return f"🗳 {poll['title']}{status}\n참석 {len(att)}명\n{body}", []

    if cmd == "스쿼드":
        # TODO: names(poll["attend"]) 로 실제 스쿼드 구성 로직 추가
        return "구성완료", []

    if cmd == "수정":
        if not args:
            return "사용법: !수정 장소 [새 장소] / !수정 날짜 [YYMMDD] / !수정 멤버 [A] [B]", []
        sub, sub_args = args[0], args[1:]
        if sub == "장소":
            if not sub_args:
                return "사용법: !수정 장소 [새 장소]", []
            poll["place"] = " ".join(sub_args)
        elif sub == "날짜":
            date = parse_date(sub_args[0]) if sub_args else None
            if not date:
                return "사용법: !수정 날짜 [YYMMDD]  예) !수정 날짜 261024", []
            poll["date"] = sub_args[0]
            poll["deadline"] = calc_deadline(date).isoformat()
        elif sub == "멤버":
            if len(sub_args) != 2:
                return "사용법: !수정 멤버 [기존 이름] [새 이름]", []
            a, b = sub_args
            key_a = next((k for k, n in poll["attend"].items() if n == a), None)
            if key_a is None:
                return f"참석 명단에 '{a}'님이 없어요.", []
            # B가 등록된 사용자면 그 키를, 아니면 수동 항목으로 추가
            key_b = next((k for k, n in users.items() if n == b), f"manual:{b}")
            poll["absent"].pop(key_b, None)
            poll["attend"] = {
                (key_b if k == key_a else k): (b if k == key_a else n)
                for k, n in poll["attend"].items()
                if k != key_b
            }
        else:
            return "수정할 항목은 장소 / 날짜 / 멤버 중 하나예요.", []
        poll["title"] = f"{poll['date']} {poll['place']}"
        _save(POLLS_FILE, polls)
        return f"수정되었습니다.\n{poll_summary(poll)}", []

    if cmd == "삭제":
        title = poll["title"]
        del polls[room]
        _save(POLLS_FILE, polls)
        return f"'{title}' 투표를 삭제했습니다.", []

    return "모르는 명령어예요. !도움말 을 입력해보세요.", []


# ============================ 게이트웨이 ============================
def log(line: str) -> None:
    if DEBUG:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


UPDATE_EXIT_CODE = 3          # run_bot.sh 가 이 코드를 받으면 즉시 재시작
UPDATE_CHECK_SEC = 30         # GitHub 새 커밋 확인 주기(초)


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

    text_body = body_of([{"simpleText": {"text": text}}])
    bodies = [text_body]
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
