#!/usr/bin/env bash
# 폴더 변경을 GitHub에 자동 커밋·푸시한다. (launchd가 파일 변경 시 실행)
#   수동 실행: ./auto_push.sh "커밋 메시지"
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG=auto_push.log

sleep 5   # 연속 저장을 한 번에 묶기
[ -z "$(git status --porcelain)" ] && exit 0

git add -A
changed=$(git diff --cached --name-only | tr '\n' ' ')
msg=${1:-"자동 업데이트: $changed"}
{
  echo "[$(date '+%F %T')] $msg"
  git commit -q -m "$msg" && git push -q origin HEAD 2>&1 && echo "  → 푸시 완료" || echo "  → 푸시 실패"
} >> "$LOG" 2>&1
