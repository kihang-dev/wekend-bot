# uv run 시 아래 의존성을 자동 설치합니다 (pip install 불필요)
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "websockets>=15.0"]
# ///
import asyncio
import json
import os
import sys

import httpx
import websockets

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit('BOT_TOKEN 환경변수가 없습니다. 먼저 export BOT_TOKEN="봇 인증 토큰" 을 실행하세요.')

GW_URL = "wss://bot-gateway.kakao.com/gateway"
CALLBACK_URL = "https://kapi.kakao.com/v1/bot/callback"


async def main() -> None:
    async with websockets.connect(GW_URL) as ws:
        # 1. HELLO 받기 → 봇 인증 토큰을 담은 IDENTIFY 보내기 → READY 받기
        hello = await recv(ws)
        await send(ws, {"op": 2, "d": {"token": BOT_TOKEN}})
        await recv(ws)
        print("게이트웨이 연결 성공")

        # 2. HEARTBEAT를 주기적으로 보내 웹소켓 연결을 유지한다
        interval = hello["d"]["heartbeat_interval"] / 1000
        asyncio.create_task(heartbeat(ws, interval))

        # 3. 메시지를 받으면 reply()로 그대로 되돌려준다
        async for raw in ws:
            event = json.loads(raw)
            if event.get("t") == "MESSAGE_CREATE":
                d = event["d"]
                print("RECV:", d.get("content"))
                if d.get("callbackToken"):
                    await reply(d["callbackToken"], d.get("content", ""))


# ----------------------------- 헬퍼 함수 -----------------------------
async def recv(ws) -> dict:
    return json.loads(await ws.recv())


async def send(ws, frame: dict) -> None:
    await ws.send(json.dumps(frame))


async def heartbeat(ws, interval: float) -> None:
    """interval초마다 HEARTBEAT(op=1)를 보내 웹소켓 연결을 유지한다."""
    while True:
        await asyncio.sleep(interval)
        await send(ws, {"op": 1})


async def reply(callback_token: str, text: str) -> None:
    """메시지 콜백 API로 'echo: {받은 메시지}' 답장을 보낸다."""
    body = {
        "skillResponse": {
            "version": "2.0",
            "template": {
                "outputs": [
                    {"simpleText": {"text": f"echo: {text}"}},
                ],
            },
        },
    }
    headers = {
        "Authorization": f"KakaoAK {BOT_TOKEN}",
        "X-Bot-Callback-Token": callback_token,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(CALLBACK_URL, json=body, headers=headers)
        print("SENT:", resp.status_code, resp.text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n봇을 종료합니다.")
