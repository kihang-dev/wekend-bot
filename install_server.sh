#!/usr/bin/env bash
# 클라우드 서버(Ubuntu) 설치 스크립트 — 한 번만 실행
#   curl -fsSL https://raw.githubusercontent.com/kihang-dev/wekend-bot/main/install_server.sh | bash
set -e
REPO="https://github.com/kihang-dev/wekend-bot.git"
DIR="$HOME/wekend-bot"

echo "== 1/5 필수 패키지 설치 (새 서버는 자동 업데이트가 끝날 때까지 몇 분 기다릴 수 있음)"
sudo apt-get -o DPkg::Lock::Timeout=600 update -qq
sudo apt-get -o DPkg::Lock::Timeout=600 install -y -qq git curl >/dev/null

echo "== 2/5 메모리 보충 (스왑 1GB)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== 3/5 uv 설치"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
export PATH="$HOME/.local/bin:$PATH"

echo "== 4/5 코드 받기"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else git clone -q "$REPO" "$DIR"; fi
cd "$DIR"
chmod +x run_bot.sh

if [ ! -f .env ]; then
  echo
  read -rsp "봇 인증 토큰을 붙여넣고 Enter (화면에 안 보이는 게 정상): " TOKEN </dev/tty; echo
  cat > .env <<ENV
BOT_TOKEN="$TOKEN"
# 서버는 5분마다 GitHub 새 버전 확인 (트래픽 절약)
UPDATE_CHECK_SEC=300
ENV
  chmod 600 .env
fi
uv run --python 3.12 --quiet python -c "print('파이썬 준비 완료')" 2>/dev/null || true

echo "== 5/5 부팅 시 자동 실행 등록"
sudo tee /etc/systemd/system/wekend-bot.service >/dev/null <<UNIT
[Unit]
Description=wekend kakao bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$DIR/run_bot.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now wekend-bot >/dev/null 2>&1
sudo systemctl restart wekend-bot

sleep 15
echo
sudo journalctl -u wekend-bot -n 15 --no-pager -o cat
echo
echo "설치 완료! 로그 보기: sudo journalctl -u wekend-bot -f"
