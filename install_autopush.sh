#!/usr/bin/env bash
# 자동 커밋·푸시 등록 (macOS launchd). 한 번만 실행하면 된다.
#   해제: ./install_autopush.sh uninstall
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.wekend-bot.autopush"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$1" = "uninstall" ]; then rm -f "$PLIST"; echo "자동 푸시 해제됨"; exit 0; fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/auto_push.sh</string></array>
  <key>WatchPaths</key><array>
    <string>$DIR/kakao_bot.py</string>
    <string>$DIR/README.md</string>
    <string>$DIR/run_bot.sh</string>
  </array>
  <key>ThrottleInterval</key><integer>10</integer>
</dict></plist>
PLISTEOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "자동 푸시 등록 완료: kakao_bot.py / README.md / run_bot.sh 가 바뀌면 GitHub에 자동 반영됩니다."
echo "기록: $DIR/auto_push.log"
