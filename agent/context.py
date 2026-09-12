"""Управление окном контекста оркестратора.

Проблема: на каждом шаге в историю падает снапшот страницы (1–4k токенов).
За 40 шагов это 100k+ токенов почти бесполезного мусора — старые снапшоты
описывают страницы, которых уже нет.

Решение — трёхуровневая память:

  • RECENT   последние N результатов инструментов хранятся целиком;
  • ELIDED   более старые снапшоты схлопываются в одну строку
             («был снапшот страницы X, 42 элемента»);
  • JOURNAL  факты, которые агент явным образом записал через remember():
             переживают любую компрессию, в системный промпт попадают только
             после жёсткой обрезки (см. ниже).

Плюс жёсткий предохранитель: если оценка истории всё равно превышает лимит,
самые старые пары сообщений выбрасываются.

Всё это согласовано с кешем промпта. Кеш — совпадение префикса: любая правка
истории сбрасывает его с этой позиции. Поэтому:
  • снапшоты схлопываются ПАЧКОЙ, когда полных накопилось elide_at, — а не по
    одному на каждом шаге. Между пачками история только дописывается и
    целиком читается из кеша;
  • журнал не вклеивается в системный промпт на каждом шаге: системный промпт
    стоит в самом начале, и каждая новая заметка сбрасывала бы весь кеш.
    Факты и так лежат в истории — это аргументы вызовов remember, которые
    сжатие не трогает. Журнал нужен только после жёсткой обрезки.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from browser.page_state import est_tokens

ELIDED_MARK = "[снапшот вытеснен из контекста — при необходимости сделай snapshot заново]"


@dataclass
class ConversationContext:
    keep_full: int = 3           # сколько последних результатов оставить после схлопывания
    elide_at: int = 8            # схлопывать пачкой, когда полных накопилось столько
    max_tokens: int = 120_000    # мягкий потолок на историю
    messages: list[dict] = field(default_factory=list)
    journal: list[str] = field(default_factory=list)
    _step: int = 0
    _dropped: bool = False       # была ли жёсткая обрезка — тогда нужен журнал
    # Запись прогона (agent/runlog.py). По событиям контекста replay.py
    # восстанавливает запрос модели на любом шаге тем же кодом сжатия.
    log: object | None = None

    def _log(self, kind: str, **data) -> None:
        if self.log is not None:
            self.log.write(kind, **data)

    # ---------- запись ----------

    def add_user(self, content) -> None:
        self.messages.append({"role": "user", "content": content})
        self._log("user", content=content)

    def add_assistant(self, content) -> None:
        self.messages.append({"role": "assistant", "content": content})
        self._log("assistant", content=content)

    def start_task(self) -> None:
        """Новая задача: история своя, журнал — общий на сессию (чистит /new)."""
        self._log("start_task")
        self.messages.clear()
        self._dropped = False  # обрезка относилась к прошлой задаче

    def remember(self, fact: str) -> None:
        """Факт, который должен пережить любую компрессию."""
        if fact and fact not in self.journal:
            self.journal.append(fact)
            self._log("remember", fact=fact)

    def journal_block(self) -> str:
        # Пока история цела, заметки видны модели в её же вызовах remember —
        # дублировать их в системный промпт значит сбрасывать кеш на каждой.
        if not self.journal or not self._dropped:
            return ""
        items = "\n".join(f"- {f}" for f in self.journal)
        return f"\n\n# Рабочие заметки (собраны тобой по ходу задачи)\n{items}"

    # ---------- компрессия ----------

    def compact(self) -> None:
        self._log("compact")  # точка запроса модели: replay считает шаги по ним
        self._step += 1
        self._elide_old_snapshots()
        if self._estimate() > self.max_tokens:
            self._drop_oldest()

    def _tool_result_positions(self) -> list[int]:
        pos = []
        for i, m in enumerate(self.messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            if any(b.get("type") == "tool_result" for b in m["content"]):
                pos.append(i)
        return pos

    def _elide_old_snapshots(self) -> None:
        full = []
        for i in self._tool_result_positions():
            for block in self.messages[i]["content"]:
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else ""
                if not text or text.startswith(ELIDED_MARK):
                    continue
                if est_tokens(text) < 200:
                    continue  # короткие результаты не трогаем, они дёшевы
                full.append(block)
        # Пачкой, а не по одному: каждое схлопывание — правка истории и сброс
        # кеша с этой позиции. Раз в несколько шагов — дёшево, каждый шаг —
        # кеш бесполезен.
        if len(full) <= self.elide_at:
            return
        for block in full[: len(full) - self.keep_full]:
            first = block["content"].split("\n", 1)[0][:160]
            block["content"] = f"{ELIDED_MARK}\nБыло: {first}"

    def _drop_oldest(self) -> None:
        # Никогда не выкидываем самое первое сообщение — там постановка задачи.
        keep_head = 1
        while self._estimate() > self.max_tokens and len(self.messages) > keep_head + 6:
            del self.messages[keep_head : keep_head + 2]
            self._dropped = True  # ранние вызовы remember могли уйти — нужен журнал

    def _estimate(self) -> int:
        total = 0
        for m in self.messages:
            c = m["content"]
            if isinstance(c, str):
                total += est_tokens(c)
            else:
                for b in c:
                    if isinstance(b, dict):
                        total += est_tokens(str(b.get("text") or b.get("content") or ""))
                        total += est_tokens(str(b.get("input") or ""))
                    else:
                        total += est_tokens(str(getattr(b, "text", "") or ""))
        return total

    @property
    def approx_tokens(self) -> int:
        return self._estimate()
