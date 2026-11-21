#!/bin/zsh
set -e
APP_PATH="${0:A:h}"
cd "$APP_PATH"
TASK_PYTHON=""
if [[ -x "$APP_PATH/.venv/bin/python3" ]] && "$APP_PATH/.venv/bin/python3" -c 'import openpyxl' 2>/dev/null; then
  TASK_PYTHON="$APP_PATH/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import openpyxl' 2>/dev/null; then
  TASK_PYTHON="$(command -v python3)"
elif [[ -x "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" ]] && "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" -c 'import openpyxl' 2>/dev/null; then
  TASK_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
else
  if ! command -v python3 >/dev/null 2>&1; then
    print '请先安装 Python 3.10 或更新版本，然后重新双击启动。'
    read '?按回车关闭。'
    exit 1
  fi
  print '首次启动，正在准备运行环境…'
  python3 -m venv "$APP_PATH/.venv"
  "$APP_PATH/.venv/bin/python3" -m pip install -r "$APP_PATH/requirements.txt"
  TASK_PYTHON="$APP_PATH/.venv/bin/python3"
fi
exec "$TASK_PYTHON" "$APP_PATH/server.py" --open
