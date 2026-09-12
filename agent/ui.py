"""Терминальный интерфейс. Всё, что видит человек во время работы агента."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

try:  # строка ввода с историей и правкой; без неё — обычный input()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
except ImportError:  # pragma: no cover
    PromptSession = None

C = {
    "dim": "\033[2m",
    "b": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "off": "\033[0m",
}


def _w() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 110)


HOTKEYS = "Ctrl+C — прервать задачу (браузер останется) · Ctrl+D или /exit — выход · ↑/↓ — прошлые задачи · /help — команды"


class TerminalUI:
    def __init__(self, verbose: bool = True, result_preview: int = 700, interactive: bool | None = None):
        self.verbose = verbose
        self.result_preview = result_preview
        if interactive is None:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self._pt_task = self._pt_answer = None
        if interactive and PromptSession is not None:
            history = Path(os.getenv("AGENT_HISTORY", "~/.browser-agent/history")).expanduser()
            history.parent.mkdir(parents=True, exist_ok=True)
            self._pt_task = PromptSession(history=FileHistory(str(history)))
            self._pt_answer = PromptSession()  # ответы агенту в историю задач не пишем

    # ---------- вывод ----------

    def banner(self, profile: str, models: str) -> None:
        print(f"{C['cyan']}{'━' * _w()}{C['off']}")
        print(f"{C['b']}  Автономный браузерный агент{C['off']}")
        print(f"{C['dim']}  {models}   профиль: {profile}{C['off']}")
        print(f"  Пиши задачу обычным текстом. После неё можно дать следующую — браузер остаётся открытым.")
        print(f"{C['dim']}  {HOTKEYS}{C['off']}")
        print(f"{C['cyan']}{'━' * _w()}{C['off']}")

    def task_cost(self, task: float, total: float) -> None:
        print(f"{C['dim']}Задача ≈ ${task:.3f} · за сессию ≈ ${total:.3f}. Браузер открыт — жду следующую задачу.{C['off']}")

    def interrupted(self) -> None:
        print(f"\n{C['yellow']}⏹ Задача прервана. Браузер открыт — можно дать следующую.{C['off']}")

    def step(self, n: int, total: int, tokens: int) -> None:
        print(f"\n{C['dim']}{'─' * _w()}{C['off']}")
        print(f"{C['dim']}шаг {n}/{total}  ·  контекст ≈{tokens // 1000}k токенов{C['off']}")

    def thought(self, text: str) -> None:
        for line in text.strip().splitlines():
            print(f"{C['magenta']}│{C['off']} {line}")

    def tool_call(self, name: str, args: dict) -> None:
        pretty = json.dumps(args, ensure_ascii=False)
        if len(pretty) > 400:
            pretty = pretty[:400] + "…"
        print(f"{C['blue']}▸ {C['b']}{name}{C['off']}{C['blue']}({pretty}){C['off']}")

    def tool_result(self, name: str, output: str) -> None:
        if not self.verbose:
            return
        head = output.strip()
        if len(head) > self.result_preview:
            head = head[: self.result_preview] + f"\n{C['dim']}… (+{len(output) - self.result_preview} симв.){C['off']}"
        color = C["red"] if output.startswith("ОШИБКА") or "ОТКЛОНЕНО" in output[:40] else C["dim"]
        for line in head.splitlines():
            print(f"{color}  {line}{C['off']}")

    def subagent(self, kind: str, detail: str, **kw) -> None:
        extra = " ".join(f"{k}={v}" for k, v in kw.items())
        print(f"{C['yellow']}  ↳ sub-agent[{kind}] {detail[:70]} {extra}{C['off']}")

    def risk(self, level: str, reason: str) -> None:
        # Security layer виден и тогда, когда не останавливает: на демо видно,
        # что каждое меняющее действие проходит оценку, и по какой причине
        # оно не потребовало подтверждения.
        print(f"  {C['dim']}🛡 риск {level}: {reason}{C['off']}")

    def info(self, text: str) -> None:
        print(f"{C['dim']}{text}{C['off']}")

    def done(self, report: str, success: bool) -> None:
        mark = f"{C['green']}✓ ЗАДАЧА ВЫПОЛНЕНА" if success else f"{C['yellow']}▲ ЗАВЕРШЕНО С ОГОВОРКАМИ"
        print(f"\n{C['cyan']}{'━' * _w()}{C['off']}")
        print(f"{mark}{C['off']}\n")
        print(report.strip())
        print(f"{C['cyan']}{'━' * _w()}{C['off']}")

    def error(self, msg: str) -> None:
        print(f"{C['red']}✖ {msg}{C['off']}")

    # ---------- ввод ----------

    async def prompt_task(self) -> str:
        """Строка ввода задачи. Ctrl+C (KeyboardInterrupt) и Ctrl+D (EOFError)
        уходят вызывающему: там решается, выходить ли."""
        text = f"\n{C['green']}задача>{C['off']} "
        if self._pt_task is not None:
            return (await self._pt_task.prompt_async(ANSI(text))).strip()
        return (await asyncio.to_thread(_read, text)).strip()

    async def read_line(self, prompt: str) -> str:
        """Ответ на вопрос агента — изнутри его задачи. Ctrl+C здесь прерывает
        задачу, а не программу: KeyboardInterrupt внутри asyncio-задачи прошёл
        бы мимо неё и уронил весь цикл событий."""
        try:
            if self._pt_answer is not None:
                return (await self._pt_answer.prompt_async(ANSI(prompt))).strip()
            return (await asyncio.to_thread(_read, prompt)).strip()
        except (KeyboardInterrupt, EOFError):
            raise asyncio.CancelledError from None

    async def ask(self, question: str) -> str:
        print(f"\n{C['cyan']}? {C['b']}Агент спрашивает:{C['off']} {question}")
        while True:
            ans = await self.read_line(f"{C['cyan']}> {C['off']}")
            if ans:
                return ans

    async def confirm(self, action: str, args: dict, element: dict | None, verdict) -> object:
        label = (element or {}).get("name") or args.get("text") or ""
        print(f"\n{C['red']}{'!' * _w()}{C['off']}")
        print(f"{C['red']}{C['b']}  ПОДТВЕРЖДЕНИЕ ДЕСТРУКТИВНОГО ДЕЙСТВИЯ{C['off']}")
        print(f"  действие : {C['b']}{action}{C['off']} {json.dumps(args, ensure_ascii=False)[:200]}")
        if label:
            print(f"  элемент  : «{label}»")
        item = (element or {}).get("item")
        if item:
            # Для иконки без подписи это единственная подсказка человеку, что
            # именно он разрешает: «кнопка в строке такого-то товара».
            print(f"  в записи : «{item}»")
        elif not label and element:
            print(f"  элемент  : <{element.get('tag', '?')}> без подписи")
        print(f"  причина  : {verdict.reason}")
        print(f"{C['red']}{'!' * _w()}{C['off']}")
        while True:
            raw = (await self.read_line(
                "  разрешить? [y = да / n = нет / a = всегда для таких / или напиши агенту]: "
            )).strip()
            ans = raw.lower()
            if ans in ("y", "yes", "д", "да"):
                return True
            if ans in ("n", "no", "н", "нет", ""):
                return False
            if ans in ("a", "always", "в", "всегда"):
                return "always"
            # Свободный текст — отказ с указанием для агента («хватит, ты всё
            # сделал»). Раньше такой ответ молча игнорировался и вопрос повторялся.
            return ("comment", raw)


def _read(prompt: str) -> str:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line
