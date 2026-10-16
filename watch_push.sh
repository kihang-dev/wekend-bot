#!/usr/bin/env bash
# 개발 맥 전용: 봇은 서버에서 돌리고, 맥에서는 수정 내용만 GitHub에 자동 푸시
#   사용법: ./watch_push.sh   (종료: Ctrl+C)
cd "$(dirname "$0")"
echo "폴더 변경을 30초마다 확인해 GitHub에 자동 푸시합니다. (종료: Ctrl+C)"
trap 'echo; exit 0' INT TERM
while true; do
  if [ -n "$(git status --porcelain)" ]; then
    ./auto_push.sh && tail -2 auto_push.log
  fi
  sleep 30
done
