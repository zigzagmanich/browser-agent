"""Запуск: Ctrl+C прерывает задачу, а не программу, и не закрывает браузер."""

import asyncio
import os
import signal
import sys
from types import SimpleNamespace as NS

import pytest

import main
from tests.helpers import real_browser


def test_ctrl_c_cancels_task_not_program():
    events = []

    class Orch:
        async def run(self, text):
            await asyncio.sleep(30)  # «долгая задача»

    ui = NS(
        interrupted=lambda: events.append("прервано"),
        error=lambda m: events.append(m),
        task_cost=lambda task, total: events.append("стоимость"),
    )

    async def go():
        before = signal.getsignal(signal.SIGINT)
        loop = asyncio.get_running_loop()
        # то, что делает Ctrl+C во время задачи: вызывает обработчик сигнала
        loop.call_later(0.2, lambda: signal.getsignal(signal.SIGINT)(signal.SIGINT, None))
        await main.run_task(Orch(), ui, "задача")
        assert signal.getsignal(signal.SIGINT) is before, "обработчик на время задачи снят"
        return "программа жива"

    assert asyncio.run(go()) == "программа жива"
    assert events == ["прервано", "стоимость"]


@pytest.mark.skipif(sys.platform == "win32", reason="группы процессов — POSIX")
def test_playwright_driver_is_outside_terminal_process_group(tmp_path):
    """Ctrl+C терминал шлёт своей группе; драйвер там раньше и был — и умирал
    вместе с браузером (эксперимент: handle_sigint × группа, см. CLAUDE.md)."""
    async def go():
        async with real_browser(tmp_path) as s:
            driver = s._pw._impl_obj._connection._transport._proc
            assert os.getpgid(driver.pid) != os.getpgid(0)
    asyncio.run(go())
