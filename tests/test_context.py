"""Контекст: сжатие пачкой ради кеша, журнал, память между задачами."""

import copy
import json
from types import SimpleNamespace as NS

from agent.context import ELIDED_MARK, ConversationContext
from agent.orchestrator import Orchestrator

SNAP = "URL: x\n" + "строка снапшота " * 400   # ~2k токенов


def test_history_is_rewritten_rarely_in_batches():
    """Каждая правка истории сбрасывает кеш промпта с этой позиции."""
    ctx = ConversationContext()
    ctx.add_user("ЗАДАЧА")
    rewrites = []
    for step in range(1, 21):
        ctx.add_assistant([{"type": "tool_use", "id": f"t{step}", "name": "click", "input": {"ref": "1"}}])
        ctx.add_user([{"type": "tool_result", "tool_use_id": f"t{step}", "content": f"{step}\n{SNAP}"}])
        before = copy.deepcopy(ctx.messages[:-2])
        ctx.compact()
        if json.dumps(ctx.messages[: len(before)], ensure_ascii=False) != json.dumps(before, ensure_ascii=False):
            rewrites.append(step)
    full = sum(1 for m in ctx.messages if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result" and not b["content"].startswith(ELIDED_MARK))
    assert 0 < len(rewrites) <= 4 and full <= ctx.elide_at


def test_journal_stays_out_of_system_prompt_until_truncation():
    ctx = ConversationContext()
    ctx.remember("факт")
    assert ctx.journal_block() == ""
    ctx.add_user("x" * 30000)
    for _ in range(8):
        ctx.add_assistant("a" * 30000)
        ctx.add_user("u" * 30000)
    ctx.max_tokens = 3000
    ctx.compact()
    assert "факт" in ctx.journal_block()


def test_next_task_sees_notes_from_previous_one():
    orch = Orchestrator(NS(dialog_policy=None), gate=None, ui=None)
    assert "Заметки" not in orch._opening("найди письма", "SNAP")
    orch.ctx.remember("Нашёл 3 письма от Сервис А: темы А, Б, В")
    orch.ctx._dropped = True
    orch.ctx.start_task()
    opening = orch._opening("удали второе из них", "SNAP2")
    assert "Сервис А" in opening and orch.ctx._dropped is False and orch.ctx.messages == []


# ---------- обрезанное блоком на экране — первым в разделе ниже экрана ----------

def test_clipped_elements_rendered_before_page_footer():
    """Всплывающий календарь стоит в конце документа, после подвала. Его дни,
    обрезанные краем блока, должны идти раньше подвала и не уходить в «ещё N»."""
    from browser.page_state import build_snapshot
    nodes = [{"type": "element", "ref": "1", "tag": "button", "attrs": {}, "name": "На экране", "viewport": True}]
    nodes += [{"type": "element", "ref": str(10 + i), "tag": "a", "attrs": {}, "name": f"Ссылка подвала номер {i} " + "x" * 40,
               "viewport": False} for i in range(200)]
    nodes += [{"type": "element", "ref": str(500 + d), "tag": "div", "attrs": {}, "name": str(d),
               "viewport": False, "clipped": True} for d in range(19, 32)]
    text = build_snapshot("https://x", "t", nodes, {}, max_tokens=1500).render()
    assert "внутри прокручиваемого блока" in text
    assert "[519]<div> 19" in text and "[531]<div> 31" in text, "все дни блока видны"
    assert text.index("[519]") < text.index("Ссылка подвала"), "дни раньше подвала"
    assert "⚠ Снапшот урезан" not in text


# ---------- текст, повторяющий подпись соседней кнопки ----------

def test_text_repeating_nearby_element_name_is_dropped():
    from browser.page_state import build_snapshot
    el = lambda ref, name: {"type": "element", "ref": ref, "tag": "button", "attrs": {}, "name": name, "viewport": True}
    tx = lambda v: {"type": "text", "value": v, "viewport": True}
    nodes = [el("1", "Кинг Фри большой, 210 ₽, 154 г · 310 ккал"), el("2", "В корзину"),
             tx("210 ₽"), tx("Кинг Фри большой"), tx("154 г · 310 ккал"),
             el("3", "Уменьшить"), tx("1"), el("4", "Увеличить"), tx("Выбор пользователей")]
    text = build_snapshot("https://x", "t", nodes, {}).render()
    body = text.split("\n\n", 1)[1]
    assert "\n    210 ₽" not in body and "154 г · 310 ккал\n" not in body.replace("ккал\n[", "")
    assert body.count("Кинг Фри большой") == 1, body
    assert "\n1\n" in "\n" + body + "\n", "количество между − и + остаётся"
    assert "Выбор пользователей" in body, "новый текст не трогаем"


# ---------- критерии от планировщика в первом сообщении ----------

def test_opening_carries_planner_criteria():
    from types import SimpleNamespace as NS
    from agent.orchestrator import Orchestrator
    fake = NS(ctx=NS(journal=[]))
    with_plan = Orchestrator._opening(fake, "удали спам", "СНАПШОТ", "Спам — рекламные рассылки, фишинг.")
    assert "КРИТЕРИИ И ГРАНИЦЫ" in with_plan and "рекламные рассылки" in with_plan
    assert with_plan.index("КРИТЕРИИ") < with_plan.index("СНАПШОТ")
    assert "КРИТЕРИИ" not in Orchestrator._opening(fake, "удали спам", "СНАПШОТ")


def test_planner_branch_runs_and_puts_plan_in_opening(monkeypatch):
    """Ветку планировщика тест раньше не проходил — и не заметил отсутствующий
    импорт: с PLANNER_MODEL агент упал бы на первой задаче."""
    import asyncio
    from types import SimpleNamespace as NS
    from agent import llm, orchestrator, subagents

    async def fake_plan(task, today=""):
        return "Спам — рекламные рассылки, подозрительные отправители, фишинг."
    monkeypatch.setattr(llm, "PLANNER_MODEL", "planner-test")
    monkeypatch.setattr(subagents, "plan", fake_plan)

    added = []

    class Stop(Exception):
        pass

    class Tools:
        task = ""
        async def take_snapshot(self):
            return "СНАПШОТ"

    class Ctx:
        journal: list = []
        def start_task(self): pass
        def add_user(self, text):
            added.append(text)
            raise Stop  # дальше цикла не идём — нужна только ветка планировщика

    class Log:
        def write(self, *a, **k): pass

    ui = NS(subagent=lambda *a, **k: None, info=lambda *a, **k: None)
    fake = NS(tools=Tools(), ctx=Ctx(), ui=ui, _opening=None)
    fake._opening = lambda task, snap, plan="": orchestrator.Orchestrator._opening(fake, task, snap, plan)
    try:
        asyncio.run(orchestrator.Orchestrator._run(fake, "удали спам", Log()))
    except Stop:
        pass
    assert added and "рекламные рассылки" in added[0]


def test_chunk_text_covers_everything_with_overlap():
    from browser.page_state import CHARS_PER_TOKEN, chunk_text
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(2000))
    chunks = chunk_text(text, chunk_tokens=1000, overlap=200)
    size = 1000 * CHARS_PER_TOKEN
    assert len(chunks) > 1 and all(len(c) <= size for c in chunks)
    assert chunks[0].startswith("строка 0 ") and chunks[-1].endswith(text[-20:])
    for a, b in zip(chunks, chunks[1:]):
        assert a[-200:] == b[:200], "стык перекрыт"
    assert chunk_text("коротко") == ["коротко"]


def test_prompt_forbids_guessing_where_goods_are_sold():
    """Прогон 26: «Шефбургер — блюдо Вкусно и точка» по памяти модели — неверно;
    поиск сайта по блюду сразу показал бы ресторан."""
    from agent.orchestrator import SYSTEM
    assert "ищи сам товар поиском сайта" in SYSTEM


def test_today_is_given_to_agent_and_planner(monkeypatch):
    """Своей даты у модели нет: «завтра» и «на прошлой неделе» считались бы от даты обучения."""
    import asyncio
    from datetime import datetime
    from types import SimpleNamespace as NS
    from agent import llm, subagents
    from agent.orchestrator import Orchestrator, today_line
    line = today_line(datetime(2026, 9, 13, 14, 30))
    assert line.startswith("Сегодня: суббота, 13 сентября 2026, 14:30")
    opening = Orchestrator._opening(NS(ctx=NS(journal=[])), "закажи на завтра", "СНАПШОТ")
    assert "Сегодня:" in opening and opening.index("Сегодня:") < opening.index("СНАПШОТ")
    sent = []

    async def fake_call(**kw):
        sent.append(kw["messages"][0]["content"])
        return NS(content=[NS(type="text", text="план")], usage=None)
    monkeypatch.setattr(llm, "call", fake_call)
    asyncio.run(subagents.plan("закажи на завтра", today=line))
    assert line in sent[0]


def test_prompt_rules_against_guessing_instead_of_looking():
    """Класс ошибок «додумать вместо того, чтобы посмотреть» — закрыт правилами заранее."""
    from agent.orchestrator import SYSTEM
    from agent.subagents import PLANNER_SYSTEM
    for phrase in ("бери со страницы этой задачи, а не из памяти", "Проверь начальное состояние",
                   "Чужие позиции в корзине молча в заказ не включай", "Сверяй найденное с запрошенным",
                   "Рекламные и спонсорские карточки", "ничего платного сверх задачи",
                   "Перед `finish` сверь итог с задачей"):
        assert phrase in SYSTEM, phrase
    assert "Не утверждай фактов о товарах" in PLANNER_SYSTEM
