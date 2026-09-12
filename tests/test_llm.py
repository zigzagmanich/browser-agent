"""Вызовы модели: сверка с настоящей сигнатурой SDK, повторы, кеш, стоимость."""

import asyncio
import inspect
import time
from types import SimpleNamespace as NS

import pytest
from anthropic.resources.messages import AsyncMessages

from agent import llm
from agent.tools import TOOLS

SIG = inspect.signature(AsyncMessages.create)
MSG = [{"role": "user", "content": "x"}]


class FakeClient:
    """Как настоящий SDK: неизвестный аргумент — TypeError. Поддельный клиент,
    принимавший что угодно, пропустил удалённый temperature — и gate молча
    закрывался на каждом клике."""

    def __init__(self, raise_exc=None):
        self.calls: list[dict] = []
        self.raise_exc = raise_exc
        self.messages = self

    async def create(self, **kw):
        SIG.bind(None, **kw)
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return NS(usage=NS(input_tokens=1, output_tokens=1), content=[])


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(llm, "_client", c)
    return c


def test_stand_catches_removed_sampling_param():
    with pytest.raises(TypeError):
        SIG.bind(None, model="m", max_tokens=1, messages=MSG, temperature=0)


def test_guard_call_passes_real_signature(client):
    asyncio.run(llm.call(model="claude-haiku-4-5-20251001", system="s", messages=MSG,
                         max_tokens=300, temperature=0))
    assert client.calls[-1]["extra_body"] == {"temperature": 0}


def test_new_models_get_no_temperature(client):
    asyncio.run(llm.call(model="claude-sonnet-5", system="s", messages=MSG, temperature=0))
    assert "extra_body" not in client.calls[-1]


def test_orchestrator_call_passes_real_signature(client):
    asyncio.run(llm.call(model="claude-sonnet-5", system="s", messages=MSG, tools=TOOLS,
                         max_tokens=8000, cache_tail=True,
                         tool_choice={"type": "auto", "disable_parallel_tool_use": True}))
    kw = client.calls[-1]
    assert kw["tool_choice"]["disable_parallel_tool_use"] is True
    assert "cache_control" in kw and "cache_control" in kw["tools"][-1]


def test_one_off_call_pays_no_cache_write(client):
    asyncio.run(llm.call(model="claude-sonnet-5", system="s", messages=MSG, tools=TOOLS,
                         cache_system=False))
    assert "cache_control" not in client.calls[-1]["tools"][-1]
    assert "cache_control" not in TOOLS[-1], "исходный список инструментов не должен меняться"


def test_programming_error_is_raised_at_once(monkeypatch):
    c = FakeClient(raise_exc=TypeError("boom"))
    monkeypatch.setattr(llm, "_client", c)
    t0 = time.monotonic()
    with pytest.raises(TypeError):
        asyncio.run(llm.call(model="claude-haiku-4-5-20251001", system="s", messages=MSG))
    assert len(c.calls) == 1 and time.monotonic() - t0 < 0.5


def test_cost_formula_matches_the_first_real_run():
    u = llm.Usage()
    u.add("claude-opus-5", NS(input_tokens=52785, output_tokens=2290,
                              cache_read_input_tokens=31922, cache_creation_input_tokens=3500))
    assert u.cost_usd()["claude-opus-5"] == pytest.approx(0.359, abs=0.01)


def test_effort_is_sent_only_when_asked(client):
    asyncio.run(llm.call(model="claude-sonnet-5", system="s", messages=MSG))
    assert "output_config" not in client.calls[-1], "Haiku 4.5 effort не принимает — по умолчанию не шлём"
    asyncio.run(llm.call(model="claude-sonnet-5", system="s", messages=MSG, effort="xhigh"))
    assert client.calls[-1]["output_config"] == {"effort": "xhigh"}
