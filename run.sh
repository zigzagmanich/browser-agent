#!/usr/bin/env bash
# Linux: запуск агента. Открыт не из терминала (двойной клик) — открывает окно терминала.
cd "$(dirname "$0")" || exit 1
if [ ! -t 0 ] && command -v x-terminal-emulator >/dev/null 2>&1; then
  exec x-terminal-emulator -e "$0" "$@"
fi
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
exec "$PY" main.py "$@"
