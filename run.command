#!/bin/bash
# macOS: двойной клик в Finder открывает Терминал с агентом.
cd "$(dirname "$0")" || exit 1
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi
"$PY" main.py "$@"
