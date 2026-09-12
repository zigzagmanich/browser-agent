"""Запись прогона на диск: каждое событие — строка JSON, сразу с flush.

Зачем:
  • история прогона не теряется в терминале — её можно открыть и перечитать;
  • по записи восстанавливается ровно тот запрос, что ушёл модели на любом
    шаге (replay.py). Одно решение модели перепроверяется одним вызовом, а не
    целым прогоном — главный способ экономить на отладке промптов.

Файл пишется построчно и переживает Ctrl+C.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


class RunLog:
    def __init__(self, directory: Path | None = None):
        # Папка берётся в момент создания, а не при определении класса: так
        # тесты подставляют временную и не пишут в настоящую runs/.
        directory = directory or RUNS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = directory / f"{stamp}.jsonl"
        n = 1
        while self.path.exists():
            self.path = directory / f"{stamp}_{n}.jsonl"
            n += 1
        self._f = self.path.open("a", encoding="utf-8")

    def write(self, kind: str, **data) -> None:
        self._f.write(json.dumps({"t": kind, **data}, ensure_ascii=False, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def load(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
