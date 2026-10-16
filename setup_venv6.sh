#!/usr/bin/env bash
# 카카오톡 에코 봇 설치 스크립트
# 사용법: 이 파일과 tutorial_echo_bot.py를 같은 폴더(예: ~/Downloads)에 두고
#         bash setup_venv6.sh
set -e

TARGET="$HOME/Desktop/python/venv6"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1) uv 설치 확인
if ! command -v uv >/dev/null 2>&1; then
  echo "uv가 없어 설치합니다..."
  if command -v brew >/dev/null 2>&1; then
    brew install uv
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
echo "uv 버전: $(uv --version)"

# 2) 작업 폴더 + 가상환경(Python 3.12) 만들기
mkdir -p "$TARGET"
cd "$TARGET"
[ -d .venv ] || uv venv --python 3.12 .venv

# 3) 봇 코드 복사
if [ "$SRC_DIR" != "$TARGET" ]; then
  cp "$SRC_DIR/tutorial_echo_bot.py" "$TARGET/"
fi

echo ""
echo "준비 완료: $TARGET"
echo "실행 방법:"
echo "  cd $TARGET"
echo '  export BOT_TOKEN="봇 인증 토큰"'
echo "  uv run --python 3.12 tutorial_echo_bot.py"
