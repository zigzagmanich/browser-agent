"""Оркестратор: главный цикл «модель решает — инструмент исполняет».

Помимо самого цикла здесь живут предохранители, без которых автономный агент
рано или поздно встаёт колом:

  • бюджет шагов — чтобы не крутиться вечно;
  • детектор зацикливания — три одинаковых вызова подряд трактуются как
    тупик, агенту принудительно сообщается об этом и предлагается сменить
    подход (а не мягко игнорируется);
  • детектор «ничего не изменилось» — сигнатура страницы до и после действия
    (живёт в Toolbox.run, пометка попадает прямо в результат инструмента);
  • принуждение к отчёту — если шаги кончились, модель просят подвести итог.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from agent import llm, subagents
from agent.context import ConversationContext
from agent.runlog import RunLog
from agent.tools import TOOLS, Finished, Toolbox

SYSTEM = (Path(__file__).parent.parent / "prompts" / "orchestrator.md").read_text(encoding="utf-8")


_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def today_line(now: datetime | None = None) -> str:
    """Сегодняшняя дата для модели. Своей даты у модели нет — она считает от
    даты обучения, и «завтра», «на выходных», «на прошлой неделе» уехали бы на
    месяцы. Время — компьютера пользователя."""
    now = now or datetime.now()
    return (f"Сегодня: {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]} {now.year}, "
            f"{now:%H:%M} (время компьютера пользователя). Относительные даты задачи считай от неё.")


def _budget_note(step: int, max_steps: int) -> str:
    """Модель не видит счётчик шагов, который видит пользователь. Без него она
    перебирала варианты до лимита и получала принудительное завершение (60 из 60).
    Дописывается в конец результата инструмента — кеш истории не сбивает."""
    marks = {max_steps // 2: "половина", int(max_steps * 0.75): "три четверти", max_steps - 5: "почти все"}
    what = marks.get(step)
    if not what:
        return ""
    return (
        f"\n\n⏳ Использовано {step} из {max_steps} шагов ({what}). Если к цели не "
        "приблизился — смени подход или подведи итог с тем, что есть."
    )


def _sig(name: str, args: dict) -> str:
    return hashlib.md5(f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:10]


class Orchestrator:
    def __init__(self, session, gate, ui, max_steps: int = 60, snapshot_tokens: int = 4000):
        self.s = session
        self.ui = ui
        self.ctx = ConversationContext()
        self.tools = Toolbox(session, self.ctx, gate, ui, snapshot_tokens=snapshot_tokens)
        self.max_steps = max_steps

    async def _ask_model(self, max_tokens: int):
        return await llm.call(
            model=llm.ORCHESTRATOR_MODEL,
            system=SYSTEM + self.ctx.journal_block(),
            messages=self.ctx.messages,
            tools=TOOLS,
            max_tokens=max_tokens,
            cache_tail=True,
            # Одно действие за ход. Рефы живут один снапшот: второй клик из
            # того же хода бьёт по рефу, который после первого мог указать
            # на другой элемент. На hh.ru модель открыла два выпадающих
            # списка разом и потратила шаг, чтобы разобраться.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )

    def _opening(self, task: str, snapshot: str, plan: str = "") -> str:
        text = f"ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:\n{task}\n\n{today_line()}\n\n"
        # Критерии от планировщика — в самой задаче, а не в общих правилах
        # промпта: общее правило про оценочные слова Sonnet пропускал пять раз.
        if plan:
            text += (
                "КРИТЕРИИ И ГРАНИЦЫ ЗАДАЧИ (от планировщика; он не видел сайт — путь ищи "
                f"сам, а критерии применяй как есть):\n{plan}\n\n"
            )
        # Память между задачами одной сессии — заметки remember из прошлых задач.
        # Системный промпт заморожен ради кеша, поэтому их место — первое
        # сообщение новой задачи: здесь они видны, а кеш посреди работы не сбит.
        if self.ctx.journal:
            notes = "\n".join(f"- {f}" for f in self.ctx.journal)
            text += f"Заметки из предыдущих задач этой сессии (записаны тобой):\n{notes}\n\n"
        return text + f"Текущее состояние браузера:\n{snapshot}"

    async def run(self, task: str) -> str:
        # Каждая задача пишется на диск: историю можно перечитать, а любое
        # решение модели — перепроверить одним вызовом через replay.py.
        log = RunLog()
        self.ctx.log = log
        log.write(
            "start", task=task, model=llm.ORCHESTRATOR_MODEL, worker=llm.WORKER_MODEL,
            max_steps=self.max_steps, journal=list(self.ctx.journal),
        )
        report, status = None, "interrupted"
        spent_before = llm.USAGE.total()
        try:
            report, status = await self._run(task, log)
            return report
        finally:
            # В finally — чтобы итог попал в лог и после Ctrl+C. Стоимость —
            # этой задачи: в диалоге задач несколько, а счётчик общий на сессию.
            log.write(
                "end", status=status, report=report,
                cost=f"≈ ${llm.USAGE.total() - spent_before:.3f}", session_cost=llm.USAGE.summary(),
            )
            log.close()
            self.ctx.log = None
            self.ui.info(f"Лог прогона: {log.path}")

    async def _run(self, task: str, log: RunLog) -> tuple[str, str]:
        self.tools.task = task
        self.ctx.start_task()

        plan = ""
        if llm.PLANNER_MODEL:
            self.ui.subagent("planner", task[:80])
            try:
                plan = await subagents.plan(task, today=today_line())
            except Exception as e:
                # Без плана агент работает как раньше — это не повод падать.
                self.ui.info(f"планировщик недоступен: {e.__class__.__name__}")
            log.write("plan", model=llm.PLANNER_MODEL, text=plan)
            if plan:
                self.ui.info(f"План:\n{plan}")

        opening = await self.tools.take_snapshot()
        self.ctx.add_user(self._opening(task, opening, plan))

        recent: list[str] = []
        step = 0

        while step < self.max_steps:
            step += 1
            self.ctx.compact()
            self.ui.step(step, self.max_steps, self.ctx.approx_tokens)

            resp = await self._ask_model(max_tokens=8000)
            log.write("usage", step=step, **llm.usage_of(resp))

            thought = llm.text_of(resp)
            if thought:
                self.ui.thought(thought)

            calls = llm.tool_uses(resp)
            self.ctx.add_assistant(
                [b.model_dump(exclude_none=True) for b in resp.content]
            )

            if not calls:
                # Модель заговорила вместо действия — возвращаем её в цикл.
                self.ctx.add_user(
                    "Ты не вызвал ни одного инструмента. Продолжай работу: сделай "
                    "следующее действие или вызови finish с отчётом."
                )
                continue

            results = []
            stop_report = None

            for call in calls:
                args = call.input or {}
                self.ui.tool_call(call.name, args)

                sig = _sig(call.name, args)
                recent.append(sig)
                looping = len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]

                try:
                    output = await self.tools.run(call.name, args)
                except Finished as fin:
                    stop_report = fin
                    break

                if looping:
                    output += (
                        "\n\n⚠ Ты вызываешь один и тот же инструмент с теми же аргументами "
                        "третий раз подряд — текущий подход не работает. Смени стратегию: "
                        "другой элемент, другой раздел сайта, read_page вместо снапшота, "
                        "или ask_user, если нужна информация от человека."
                    )
                    recent.clear()

                output += _budget_note(step, self.max_steps)
                self.ui.tool_result(call.name, output)
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": output}
                )

            if stop_report is not None:
                self.ui.done(stop_report.report, stop_report.success)
                return stop_report.report, ("done" if stop_report.success else "done_with_caveats")

            self.ctx.add_user(results)

        # Шаги кончились — требуем отчёт о том, что успели.
        self.ctx.add_user(
            f"Лимит в {self.max_steps} шагов исчерпан. Немедленно вызови finish и честно "
            "опиши: что удалось сделать, на чём остановился, что осталось."
        )
        resp = await self._ask_model(max_tokens=3000)
        log.write("usage", step=step + 1, **llm.usage_of(resp))
        for b in llm.tool_uses(resp):
            if b.name == "finish":
                report = b.input.get("report", "")
                self.ui.done(report, False)
                return report, "step_limit"
        report = llm.text_of(resp) or "Лимит шагов исчерпан, отчёт не сформирован."
        self.ui.done(report, False)
        return report, "step_limit"
