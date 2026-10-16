# wekend-bot

카카오톡 참석/불참 투표 봇

## 요구 사항
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`brew install uv`)

## 실행
```bash
cp .env.example .env      # .env 에 봇 인증 토큰 입력 (저장소에 올라가지 않음)
./run_bot.sh              # 디버그 로그: DEBUG=1 ./run_bot.sh
```
- 연결이 끊기면 자동 재접속, 오류로 꺼지면 5초 후 자동 재시작
- 30초마다 GitHub를 확인해 새 커밋이 있으면 받아와서 자동 재시작

## 자동 커밋·푸시 (개발 맥)
`.env` 에 `AUTO_PUSH=1` 을 넣고 `./run_bot.sh` 로 실행하면, 봇이 30초마다 폴더 변경을 확인해
자동으로 커밋·푸시한 뒤 새 버전으로 재시작합니다. 기록은 `auto_push.log`.
수동 푸시: `./auto_push.sh "커밋 메시지"`

## 명령어
앞의 `!` 는 붙여도, 안 붙여도 됩니다. 단체방에서 `@봇이름 참석` 처럼 멘션을 붙여도 인식합니다.
문장 중간의 단어(예: "나 내일 참석")는 명령으로 보지 않습니다.

| 명령어 | 설명 |
|---|---|
| `도움말` | 봇 사용법 |
| `이름 홍길동` | 본인 이름 최초 등록 |
| `투표 261017 달월` | '261017 달월' 참석/불참 투표 생성 (해당 날짜 다음주 수요일 23시 마감) |
| `참석` / `불참` | 투표하기 |
| `멤버` | 참석 인원 확인 |
| `스쿼드` | 참석 인원으로 스쿼드 구성 (현재 "구성완료" 출력) |
| `수정 장소 강남` | 진행중인 투표 장소 변경 |
| `수정 날짜 261024` | 진행중인 투표 날짜 변경 (마감일 재계산) |
| `수정 멤버 A B` | 참석 멤버 A → B 변경 |
| `투표종료` (`투표 종료`) | 지금 바로 투표 마감 + 최종 결과 |
| `삭제` | 진행중인 투표 삭제 |

## 관리용 REST API
`.env` 에 `API_KEY` 를 넣으면 봇과 함께 API가 켜집니다 (기본 포트 8000, `API_PORT` 로 변경).
브라우저에서 `http://localhost:8000/docs` 를 열면 모든 API를 바로 시험해볼 수 있습니다.
모든 요청에 `X-API-Key: <API_KEY>` 헤더가 필요합니다. `room` 은 카톡방 키로, `GET /polls` 에서 확인합니다.

| 메서드 | 경로 | 설명 | 카톡 명령 |
|---|---|---|---|
| GET | `/polls` | 모든 카톡방의 투표 (날짜·장소·멤버) | |
| GET | `/rooms` | 카톡방 키 목록 | |
| GET | `/polls/{room}` | 투표 상세 (참석·불참 명단, 마감 여부) | |
| GET | `/polls/{room}/members` | 참석 인원 | `!멤버` |
| POST | `/polls/{room}` | 투표 생성 `{"date":"261017","place":"달월"}` | `!투표` |
| PATCH | `/polls/{room}` | 날짜·장소 수정 `{"date":"261024"}` / `{"place":"강남"}` | `!수정 날짜/장소` |
| POST | `/polls/{room}/votes` | 이름으로 투표 `{"name":"홍길동","status":"참석"}` | `!참석` / `!불참` |
| DELETE | `/polls/{room}/votes/{name}` | 명단에서 사람 빼기 | |
| PUT | `/polls/{room}/members` | 참석 멤버 변경 `{"old":"A","new":"B"}` | `!수정 멤버` |
| POST | `/polls/{room}/squad` | 스쿼드 구성 | `!스쿼드` |
| DELETE | `/polls/{room}` | 투표 삭제 | `!삭제` |
| POST | `/polls/{room}/close` | 지금 바로 마감 | `투표종료` |
| GET | `/users` | 등록된 이름 목록 | |

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/polls
```

## 파일
- `kakao_bot.py` — 투표 봇
- `tutorial_echo_bot.py` — 카카오 튜토리얼 에코 봇
- `setup_venv6.sh` — uv 설치 확인 및 가상환경 생성

투표 기록(`polls.json`)과 이름 등록(`users.json`)은 실행 폴더에 저장되며 저장소에는 올라가지 않습니다.
