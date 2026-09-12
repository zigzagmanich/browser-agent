#!/usr/bin/env python3
"""Автономный браузерный агент — точка входа.

    python main.py                       # диалог: задача за задачей, браузер остаётся открытым
    python main.py --task "..."          # сразу выполнить задачу и продолжить диалог
    python main.py --task "..." --once   # одна задача и выход (для скриптов)
    python main.py --profile work        # отдельный профиль браузера

Отдельное окно по двойному клику: run.command (macOS), run.bat (Windows),
run.sh (Linux).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent import llm  # noqa: E402  (.env загружается здесь, до чтения переменных)
from agent.orchestrator import Orchestrator  # noqa: E402
from agent.security import SecurityGate  # noqa: E402
from agent.ui import C, HOTKEYS, TerminalUI  # noqa: E402
from browser.session import BrowserSession, profiles_root  # noqa: E402

HELP = f"""
Команды:
  /help            эта справка
  /new             забыть заметки прошлых задач и начать с чистого листа
  /url <адрес>     открыть страницу вручную (например, чтобы залогиниться)
  /snap            показать, как агент видит текущую страницу
  /tabs            список вкладок
  /usage           потраченные токены и стоимость
  /exit            выход
Всё остальное — задача для агента.

{HOTKEYS}
"""


async def run_task(orch: Orchestrator, ui: TerminalUI, text: str) -> None:
    """Задача — отдельная asyncio-задача: Ctrl+C отменяет её, а не программу.
    Обработчик сигнала ставится только на время задачи; в строке ввода Ctrl+C
    ловит сама строка ввода."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(orch.run(text))
    previous = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(task.cancel))
    before = llm.USAGE.total()
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled():
            raise  # отменили не задачу, а саму программу
        ui.interrupted()
    except Exception as e:
        ui.error(f"{e.__class__.__name__}: {e}")
    finally:
        signal.signal(signal.SIGINT, previous)
    ui.task_cost(llm.USAGE.total() - before, llm.USAGE.total())


async def command(line: str, orch: Orchestrator, session: BrowserSession) -> bool:
    """Служебная команда. False — выход."""
    cmd, _, rest = line.partition(" ")
    if cmd in ("/exit", "/quit"):
        return False
    if cmd == "/help":
        print(HELP)
    elif cmd == "/new":
        orch.ctx.messages.clear()
        orch.ctx.journal.clear()
        print("Контекст и заметки очищены.")
    elif cmd == "/url":
        await session.page.goto(rest if rest.startswith("http") else "https://" + rest, wait_until="domcontentloaded")
        print(f"Открыто: {session.page.url}")
    elif cmd == "/snap":
        print(await orch.tools.take_snapshot())
    elif cmd == "/tabs":
        for i, tab in enumerate(session.tabs()):
            mark = " ← активная" if tab is session.page else ""
            print(f"{i}: {await tab.title()} — {tab.url}{mark}")
    elif cmd == "/usage":
        print(llm.USAGE.report())
    else:
        print("Неизвестная команда. /help")
    return True


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", default="default", help="имя persistent-профиля браузера")
    p.add_argument("--task", help="сразу выполнить задачу")
    p.add_argument("--once", action="store_true", help="после --task выйти, не открывая диалог")
    p.add_argument("--max-steps", type=int, default=60)
    p.add_argument("--snapshot-tokens", type=int, default=4000)
    p.add_argument("--headless", action="store_true", help="без окна (не рекомендуется)")
    p.add_argument("--yolo", action="store_true", help="не спрашивать подтверждений (опасно)")
    p.add_argument("--quiet", action="store_true", help="не печатать результаты инструментов")
    args = p.parse_args()

    if os.name == "nt":
        os.system("")  # включить цвета ANSI в консоли Windows

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Не задан ANTHROPIC_API_KEY. Скопируй .env.example в .env и впиши ключ.")
        sys.exit(1)

    profile_dir = profiles_root() / args.profile
    ui = TerminalUI(verbose=not args.quiet)
    session = BrowserSession(str(profile_dir), headless=args.headless)

    models = f"модель: {llm.ORCHESTRATOR_MODEL}" + (f" · планировщик: {llm.PLANNER_MODEL}" if llm.PLANNER_MODEL else "")
    ui.banner(str(profile_dir), models)
    print(f"{C['dim']}Запускаю браузер…{C['off']}")
    await session.start()

    gate = SecurityGate(confirm_fn=ui.confirm, auto_approve=args.yolo)
    orch = Orchestrator(session, gate, ui, max_steps=args.max_steps, snapshot_tokens=args.snapshot_tokens)

    try:
        if args.task:
            await run_task(orch, ui, args.task)
            if args.once:
                return

        presses = 0  # Ctrl+C подряд на пустой строке
        while True:
            try:
                line = await ui.prompt_task()
            except KeyboardInterrupt:
                presses += 1
                if presses >= 2:
                    break
                ui.info("Ещё раз Ctrl+C, Ctrl+D или /exit — выход.")
                continue
            except EOFError:
                break
            presses = 0
            if not line:
                continue
            if line.startswith("/"):
                if not await command(line, orch, session):
                    break
                continue
            if not session.alive():
                ui.info("Окно браузера закрыто — открываю снова (логины в профиле на месте)…")
                await session.restart()
            await run_task(orch, ui, line)
    finally:
        print(f"\n{C['dim']}Итого токенов — {llm.USAGE.report()}{C['off']}")
        try:
            await session.close()
        except Exception:
            pass  # браузер уже закрыт пользователем


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
