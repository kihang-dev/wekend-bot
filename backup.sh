#!/bin/bash
# 봇 데이터 백업: 투표·휴면·이름·연결·방 기록을 날짜별 폴더에 복사하고, 30일 지난 백업은 지운다.
# VM에서 매일 04:00(KST)에 cron으로 실행한다.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/backups/$(date +%Y%m%d)"
mkdir -p "$DEST"
copied=0
for f in polls.json dormant.json users.json links.json room_members.json schedule.json; do
  if [ -f "$HERE/$f" ]; then
    cp -p "$HERE/$f" "$DEST/" && copied=$((copied + 1))
  fi
done
find "$HERE/backups" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null
echo "$(date '+%F %T') 백업 완료: ${copied}개 파일 → $DEST"
