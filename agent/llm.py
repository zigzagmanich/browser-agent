"""Тонкая обёртка над Anthropic Messages API.

Единственное место, где мы ходим в LLM. Здесь же — ретраи с backoff и
prompt caching (системный промпт и схемы инструментов не меняются между
шагами, поэтому кешируются и не оплачиваются заново на каждом шаге цикла).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic
from dotenv import load_dotenv

# Модели читаются из окружения ниже, при импорте, а main.py импортирует этот
# модуль раньше, чем успевает вызвать load_dotenv(). Без этой строки значения
# из .env молча игнорировались, и агент работал на модели по умолчанию.
# Переменные, заданные в самом шелле, по-прежнему важнее .env.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# По умолчанию — Sonnet исполняет, Opus один раз задаёт критерии (планировщик).
# Одна Sonnet без плана проваливала задачи на суждение (спам 0 из 5), с планом —
# справилась; навигацию ведёт не хуже Opus в 2–3 раза дешевле. См. CLAUDE.md,
# прогоны 15, 18–21.
ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "claude-sonnet-5")
# Sub-агенты — быстрая и дешёвая: чтение простыней текста и классификация.
WORKER_MODEL = os.getenv("WORKER_MODEL", "claude-haiku-4-5-20251001")
# Планировщик: один вызов сильной модели по тексту задачи до начала работы —
# явные критерии для оценочных слов. Пусто — выключен.
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "claude-opus-5")

# Модели, которые принимают temperature. У новых поколений (Opus 4.7+,
# Sonnet 5, Fable) параметры сэмплинга удалены и дают 400 — для них этот
# рычаг детерминированности недоступен, и параметр просто не отправляется.
# Иначе смена WORKER_MODEL на новую модель сломала бы security gate: он
# закрывается при ошибке и начал бы спрашивать про каждый клик.
_SAMPLING_MODELS = (
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-sonnet-4-5", "claude-opus-4-5",
)


def accepts_sampling(model: str) -> bool:
    return model.startswith(_SAMPLING_MODELS)


_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Не задан ANTHROPIC_API_KEY (положи его в .env)")
        _client = AsyncAnthropic(api_key=key, max_retries=0)
    return _client


# Цены за миллион токенов (вход, выход), USD. Запись в кеш — 1.25× входа
# (TTL 5 минут), чтение — 0.1×. Нужны только для оценки стоимости прогона;
# при смене прайса поправить здесь.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    # Раньше не считалось вовсе — и стоимость прогона по логу не сходилась.
    cache_write: int = 0
    # модель -> [вход, выход, чтение кеша, запись в кеш]: оркестратор и
    # под-агенты работают на моделях с разной ценой, общая сумма токенов врёт.
    by_model: dict[str, list[int]] = field(default_factory=dict)

    def add(self, model: str, u: Any) -> None:
        inp, out, read, write = _counts(u)
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += read
        self.cache_write += write
        m = self.by_model.setdefault(model, [0, 0, 0, 0])
        m[0] += inp
        m[1] += out
        m[2] += read
        m[3] += write

    def cost_usd(self) -> dict[str, float | None]:
        costs: dict[str, float | None] = {}
        for model, (inp, out, read, write) in self.by_model.items():
            price = PRICES.get(model)
            if price is None:
                costs[model] = None
                continue
            p_in, p_out = price
            costs[model] = (inp * p_in + write * p_in * 1.25 + read * p_in * 0.1 + out * p_out) / 1e6
        return costs

    def total(self) -> float:
        return sum(c for c in self.cost_usd().values() if c)

    def summary(self) -> str:
        costs = self.cost_usd()
        total = self.total()
        parts = "; ".join(
            f"{m} ${c:.3f}" if c is not None else f"{m} — цена неизвестна" for m, c in costs.items()
        )
        return f"≈ ${total:.3f} ({parts})" if parts else "≈ $0"

    def report(self) -> str:
        return (
            f"вход {self.input_tokens:,}, выход {self.output_tokens:,}, "
            f"из кеша {self.cache_read:,}, в кеш {self.cache_write:,}\n"
            f"стоимость {self.summary()}"
        )


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(u: Any) -> tuple[int, int, int, int]:
    return tuple(getattr(u, k, 0) or 0 for k in _USAGE_KEYS)  # type: ignore[return-value]


def usage_of(resp) -> dict:
    """Токены одного ответа — для записи прогона по шагам."""
    return dict(zip(_USAGE_KEYS, _counts(getattr(resp, "usage", None))))


USAGE = Usage()


async def call(
    *,
    model: str,
    system: str | list[dict],
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 8000,
    cache_system: bool = True,
    cache_tail: bool = False,
    temperature: float | None = None,
    tool_choice: dict | None = None,
    effort: str | None = None,
    attempts: int = 4,
):
    """Один вызов Messages API с ретраями на 429/5xx/сетевых ошибках."""
    sys_block: Any = system
    if cache_system and isinstance(system, str):
        sys_block = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": sys_block,
        "messages": messages,
    }
    if temperature is not None and accepts_sampling(model):
        # SDK 1.x убрал temperature из messages.create (передача — TypeError),
        # хотя API для этих моделей его по-прежнему принимает. Путь, который
        # предписывает руководство по переходу на 1.x, — extra_body.
        kwargs["extra_body"] = {"temperature": temperature}
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if effort:
        # Только по явной просьбе: Haiku 4.5 (под-агенты) effort не принимает.
        kwargs["output_config"] = {"effort": effort}
    if cache_tail:
        # Автоматическая метка на последнем блоке истории: следующий шаг цикла
        # прочитает всю предыдущую историю из кеша (0.1× цены) и допишет только
        # новый ход. Без неё снапшоты оплачивались заново на каждом шаге — три
        # четверти стоимости первого прогона. Одноразовым вызовам под-агентов
        # это только вредит: запись дороже входа, а прочитать её будет некому.
        kwargs["cache_control"] = {"type": "ephemeral"}
    if tools:
        tools = [dict(t) for t in tools]
        if cache_system:
            # Кешируем хвост описания инструментов — он статичен. Для разового
            # вызова (replay.py) метка — чистая наценка за запись, которую
            # никто не прочтёт, поэтому она ставится только вместе с кешем.
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        kwargs["tools"] = tools

    delay = 1.5
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = await client().messages.create(**kwargs)
            USAGE.add(model, resp.usage)
            return resp
        except APIStatusError as e:
            # Повтор помогает только перегрузке (429) и сбоям сервера (5xx).
            # Остальные 4xx — ошибка самого запроса, повтор вернёт то же.
            if e.status_code != 429 and e.status_code < 500:
                raise
            last = e
        except APIConnectionError as e:  # обрыв сети; таймаут — его подкласс
            last = e
        # Всё прочее (TypeError и т. п.) — ошибка в нашем коде: поднимается сразу.
        # Раньше её ловил общий except и честно ретраил четыре раза с паузами —
        # gate падал через ~10 секунд на каждом клике.
        if i < attempts - 1:
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError(f"LLM недоступна после {attempts} попыток: {last}")


def text_of(resp) -> str:
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def json_text(resp) -> str:
    """Текст ответа без markdown-обёртки ```json — модели иногда её добавляют."""
    return text_of(resp).removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def tool_uses(resp) -> list:
    return [b for b in resp.content if b.type == "tool_use"]
