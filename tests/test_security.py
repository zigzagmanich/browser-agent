"""Security gate: подтверждения, одноразовое разрешение, кеш вердиктов, ответ текстом."""

import asyncio
import time

import pytest

from agent import security
from agent.context import ConversationContext
from agent.security import SecurityGate, Verdict
from agent.tools import Toolbox
from agent.ui import TerminalUI
from helpers import FakeSession, StubUI, text_response

ICON = {"29": {"tag": "button", "name": "", "viewport": True, "item": "Товар"}}
MODAL = {"116": {"tag": "button", "name": "Удалить", "viewport": True}}


def make(answer=True, level="high"):
    asked = []

    async def confirm(action, args, element, verdict):
        asked.append(args.get("ref"))
        return answer

    gate = SecurityGate(confirm_fn=confirm)

    async def assess(action, args, *, url, title, element, task):
        if gate._cheap_skip(action, args, element):
            return Verdict("low", "не меняет состояние")
        return Verdict(level, "тест")

    gate.assess = assess
    tb = Toolbox(FakeSession(), ConversationContext(), gate, StubUI())
    executed = []

    async def act(a):
        executed.append(a)
        return "ок"

    tb._t_click = act
    tb._t_scroll = act
    return tb, gate, asked, executed


def approve_icon_then_click_modal(tb, *, between=None, overlay=True, modal=MODAL, age=None):
    async def go():
        tb._ref_info = ICON
        await tb.run("click", {"ref": "29"})
        if between:
            await tb.run(*between)
        if age is not None:
            tb.gate._approved_at = time.monotonic() - age
        tb._overlay, tb._ref_info = overlay, modal
        return await tb.run("click", {"ref": "116"})
    return asyncio.run(go())


def test_site_confirmation_after_approved_click_is_not_asked_again():
    tb, _, asked, _ = make()
    out = approve_icon_then_click_modal(tb)
    assert asked == ["29"] and "без второго вопроса" in out


def test_approval_is_one_shot():
    tb, _, asked, _ = make()
    approve_icon_then_click_modal(tb)
    asyncio.run(tb.run("click", {"ref": "116"}))
    assert asked == ["29", "116"]


def test_action_in_between_resets_approval():
    tb, _, asked, _ = make()
    approve_icon_then_click_modal(tb, between=("scroll", {"direction": "down"}))
    assert asked == ["29", "116"]


def test_without_overlay_it_asks():
    tb, _, asked, _ = make()
    approve_icon_then_click_modal(tb, overlay=False)
    assert asked == ["29", "116"]


def test_element_off_screen_under_overlay_asks():
    tb, _, asked, _ = make()
    approve_icon_then_click_modal(tb, modal={"116": {"tag": "button", "name": "Удалить", "viewport": False}})
    assert asked == ["29", "116"]


def test_long_model_thinking_does_not_burn_approval():
    tb, _, asked, _ = make()
    approve_icon_then_click_modal(tb, age=60)
    assert asked == ["29"]


def test_free_text_answer_is_refusal_with_instruction():
    tb, _, _, executed = make(answer=("comment", "всё, остановись, ты сделал что нужно"))
    tb._ref_info = MODAL
    out = asyncio.run(tb.run("click", {"ref": "116"}))
    assert out.startswith("ОТКЛОНЕНО") and "остановись" in out and executed == []


@pytest.mark.parametrize("typed, expected", [
    ("y", True), ("", False), ("a", "always"),
    ("хватит, отчитайся", ("comment", "хватит, отчитайся")),
])
def test_confirm_prompt_answers(typed, expected):
    ui = TerminalUI(verbose=False)

    async def fake_input(prompt):
        return typed

    ui.read_line = fake_input
    got = asyncio.run(ui.confirm("click", {"ref": "1"}, {"tag": "button", "name": "Удалить"}, Verdict("high", "т")))
    assert got == expected


@pytest.mark.parametrize("level, shown", [("medium", 1), ("low", 0)])
def test_medium_verdict_is_visible_in_terminal(level, shown):
    tb, _, _, _ = make(level=level)
    tb._ref_info = {"1": {"tag": "button", "name": "В корзину", "viewport": True}}
    asyncio.run(tb.run("click", {"ref": "1"}))
    assert len([l for l in tb.ui.lines if l[0] == "risk"]) == shown


def test_native_dialog_after_approved_click_is_accepted_once():
    tb, _, asked, _ = make()
    dialog = type("D", (), {"type": "confirm", "message": "Удалить безвозвратно?"})()

    async def go():
        tb._ref_info = ICON
        await tb.run("click", {"ref": "29"})
        first = await tb._dialog_policy(dialog)
        second = await tb._dialog_policy(dialog)
        return first, second

    first, second = asyncio.run(go())
    assert first[0] and asked == ["29", None] and second[0]


def test_nameless_elements_are_never_cached_or_always_allowed(monkeypatch):
    calls = []

    async def fake_call(**kw):
        calls.append(kw)
        return text_response('{"level":"high","reason":"иконка в строке корзины"}')

    monkeypatch.setattr(security.llm, "call", fake_call)

    async def always(*a):
        return "always"

    gate = SecurityGate(confirm_fn=always)
    nameless = {"tag": "button", "attrs": {}, "name": ""}
    named = {"tag": "button", "attrs": {}, "name": "Удалить"}

    async def go():
        for element in (nameless, nameless, named, named):
            await gate.guard("click", {"ref": "1"}, url="https://shop.test/", title="",
                             element=element, task="")

    asyncio.run(go())
    assert len(calls) == 3, "безымянный — каждый раз заново, именованный — из «всегда»"
    assert all(key[2] for key in gate._always_allow), "в «всегда» нет пустых подписей"
