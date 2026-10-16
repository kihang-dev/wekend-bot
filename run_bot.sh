#!/usr/bin/env bash
# 봇 실행기: .env에서 토큰을 읽고, 봇이 업데이트/오류로 종료되면 자동으로 다시 켠다.
#   사용법: ./run_bot.sh          (DEBUG=1 ./run_bot.sh 로 디버그 로그)
#   종료:   Ctrl+C
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1   # 로그가 바로바로 보이도록


trap 'echo; echo "봇 실행기를 종료합니다."; exit 0' INT TERM
while true; do
  git pull --ff-only -q 2>/dev/null
  # 재시작할 때마다 .env 를 다시 읽어 설정 변경도 반영
  if [ -f .env ]; then set -a; . ./.env; set +a; fi
  if [ -z "$BOT_TOKEN" ]; then
    echo '.env 파일에 BOT_TOKEN="봇 인증 토큰" 을 적어주세요.'; exit 1
  fi
  uv run --python 3.12 kakao_bot.py
  code=$?
  case $code in
    0|130) exit 0 ;;                                   # Ctrl+C 로 정상 종료
    3)     echo "업데이트 적용 → 재시작" ;;
    *)     echo "봇 종료(코드 $code) → 5초 후 재시작"; sleep 5 ;;
  esac
done
