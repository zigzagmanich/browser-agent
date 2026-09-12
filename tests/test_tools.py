"""Инструменты: завершение, детекторы, правила переходов, под-агент чтения."""

import asyncio

import pytest

from agent import subagents
from agent.context import ConversationContext
from agent.orchestrator import _budget_note
from agent.tools import Finished, Toolbox, _url_shape
from helpers import AllowGate, FakeSession, StubUI, text_response


def box(url="https://example.test/", links=(), task=""):
    tb = Toolbox(FakeSession(url), ConversationContext(), AllowGate(), StubUI())
    tb._ref_info = {str(i): {"tag": "a", "url": u} for i, u in enumerate(links)}
    tb.task = task
    return tb


def test_finish_ends_the_task():
    """Раньше общий except превращал finish в «ОШИБКА Finished», и агент крутился до лимита."""
    with pytest.raises(Finished):
        asyncio.run(box().run("finish", {"report": "готово", "success": True}))


def test_unchanged_page_is_flagged_and_changed_is_not():
    tb = box()
    tb._last_signature = "A"

    async def same(a):
        return "Клик выполнен."

    async def changes(a):
        tb._last_signature = "B"
        return "Клик выполнен."

    tb._t_click = same
    assert "ничего не изменилось" in asyncio.run(tb.run("click", {"ref": "5"}))
    tb._t_click = changes
    assert "ничего не изменилось" not in asyncio.run(tb.run("click", {"ref": "5"}))


def test_dismissed_dialog_is_explained_without_false_flag():
    tb = box()
    tb._last_signature = "A"
    tb.s.pending = [{"type": "confirm", "message": "Удалить?", "accepted": False, "reason": "отклонено"}]

    async def same(a):
        return "Клик выполнен."

    tb._t_click = same
    out = asyncio.run(tb.run("click", {"ref": "5"}))
    assert "системное окно" in out and "не выполнено" in out and "ничего не изменилось" not in out


HH = ["https://hh.ru/vacancy/136417891?query=AI&hhtmFrom=vacancy_search_list",
      "https://hh.ru/search/vacancy?text=AI+инженер&salary=150000&page=1"]


@pytest.mark.parametrize("page, links, task, target, allowed", [
    ("https://hh.ru/search", HH, "", "https://hh.ru", True),
    ("https://hh.ru/search", HH, "", "hh.ru", True),
    ("https://hh.ru/search", HH, "", "https://hh.ru/?text=AI", False),
    ("https://hh.ru/search", HH, "", "https://hh.ru/vacancy/136417891", True),
    ("https://hh.ru/search", HH, "",
     "https://hh.ru/search/vacancy?text=AI-инженер&only_with_salary=true&schedule=remote", False),
    ("https://hh.ru/search", HH, "", "https://hh.ru/search/vacancy?text=AI+инженер&page=1", True),
    ("https://ozon.kz/", ["https://ozon.kz/cart"], "", "/cart", True),
    ("about:blank", [], "", "https://www.ozon.ru/cart", False),
    ("about:blank", [], "открой https://example.com/docs/page", "https://example.com/docs/page", True),
    ("https://www.example.com/", ["https://www.example.com/a"], "", "https://example.com/a", True),
])
def test_navigation_only_where_a_newcomer_could_get(page, links, task, target, allowed):
    tb = box(page, links, task)
    assert tb._navigation_allowed(tb._resolve_url(target)) is allowed


def test_address_from_user_answer_is_allowed():
    tb = box("about:blank")
    tb._answers.append("вот ссылка: shop.test/promo/42")
    assert tb._navigation_allowed("https://shop.test/promo/42")


def test_navigation_refusal_reaches_model_without_navigating():
    tb = box("about:blank")
    out = asyncio.run(tb.run("navigate", {"url": "https://www.ozon.ru/cart"}))
    assert out.startswith("ОШИБКА") and "по памяти" in out and tb.s.page.opened == []


MAIL = [f"https://mail.yandex.kz/?uid=1370919821#/message/19365478397693137{i}" for i in range(4)]


def test_url_shape_groups_records_of_one_list():
    assert len({_url_shape(u) for u in MAIL}) == 1
    assert _url_shape(MAIL[0]) != _url_shape("https://mail.yandex.kz/?uid=1370919821#/tabs/relevant")
    assert _url_shape("https://ozon.kz/") != _url_shape("https://ozon.kz/cart")


@pytest.mark.parametrize("urls, flags", [
    (MAIL, [False, False, True, False]),
    # возврат к списку серию не прерывает — третье разное письмо даёт пометку
    ([MAIL[0], "https://mail.yandex.kz/?uid=1370919821#/tabs/relevant", MAIL[1], MAIL[2]], [False, False, False, True]),
    (["https://ozon.kz/", "https://ozon.kz/cart", "https://ozon.kz/checkout"], [False] * 3),
    ([MAIL[0], MAIL[0], MAIL[0]], [False] * 3),
])
def test_one_by_one_detector(urls, flags):
    tb = box()
    assert [bool(tb._series_note(u)) for u in urls] == flags


def test_read_link_refuses_buttons():
    tb = box()
    tb._ref_info = {"7": {"tag": "button", "name": "Скрыть"}}
    out = asyncio.run(tb.run("read_link", {"ref": "7", "question": "?"}))
    assert out.startswith("ОШИБКА") and "не ссылка" in out


def test_budget_notes_at_half_three_quarters_and_end():
    assert [s for s in range(1, 61) if _budget_note(s, 60)] == [30, 45, 55]
    assert "30 из 60" in _budget_note(30, 60)


def test_reader_keeps_partial_answers(monkeypatch):
    """Раньше любой кусок со словом NO_ANSWER выбрасывался вместе с найденным."""
    seen = []

    async def fake_call(*, messages, **kw):
        content = messages[0]["content"]
        if "ЧАСТИЧНЫЕ ОТВЕТЫ" in content:
            seen.append(content)
            return text_response("СВОДКА")
        if "ЧАНК0" in content:
            return text_response("NO_ANSWER")
        if "ЧАНК1" in content:
            return text_response("Вакансия А — 200 000 ₽; ссылки в тексте нет")
        return text_response("Вакансия Б — зарплата не указана. Про ссылки: NO_ANSWER")

    monkeypatch.setattr(subagents.llm, "call", fake_call)
    page = "\n".join(f"ЧАНК{i} " + "x" * 17000 for i in range(3))
    assert asyncio.run(subagents.read("вакансии и зарплаты", page)) == "СВОДКА"
    assert "Вакансия А" in seen[0] and "Вакансия Б" in seen[0]


# Реальная последовательность переходов из прогона 11 (runs/2026-09-11_22-49-02):
# письмо → письмо → список → письмо → список… Детектор «подряд» её пропустил.
RUN11 = [
    "https://360.yandex.kz/mail/", "https://360.yandex.kz/mail/",
    "https://mail.yandex.kz/?uid=1370919821#/tabs/relevant",
    "https://mail.yandex.kz/?uid=1370919821#/message/193654783976931379",
    "https://mail.yandex.kz/?uid=1370919821#/message/193654783976931378",
    "https://mail.yandex.kz/?uid=1370919821#/tabs/relevant",
    "https://mail.yandex.kz/?uid=1370919821#/message/193654783976931377",
    "https://mail.yandex.kz/?uid=1370919821#/tabs/relevant",
    *["https://mail.yandex.kz/?uid=1370919821#/tabs/relevant"] * 5,
    "https://mail.yandex.kz/?uid=1370919821#spam",
]


def test_detector_catches_real_alternating_run():
    tb = box()
    flags = [bool(tb._series_note(u)) for u in RUN11]
    assert flags.count(True) == 1 and flags.index(True) == 6, "пометка на шаге 7 — третьем письме"


# ---------- переход по ссылке, виденной раньше в сессии ----------

def test_navigation_allows_links_seen_earlier_in_session():
    """Возврат к вакансии из прочитанного раньше списка стоил три шага: адрес был
    не в текущем снапшоте. Сайт сам его показал — это не адрес по памяти."""
    from agent.context import ConversationContext
    from agent.tools import Toolbox
    from tests.helpers import AllowGate, FakeSession, StubUI
    tb = Toolbox(FakeSession("https://example.test/vacancy/9"), ConversationContext(), AllowGate(), StubUI())
    tb.task = "найди вакансии"
    tb._seen_links.add("https://example.test/vacancy/1?query=ai&from=list")
    assert tb._navigation_allowed("https://example.test/vacancy/1")
    assert tb._navigation_allowed("https://example.test/vacancy/1?query=ai")
    assert not tb._navigation_allowed("https://example.test/vacancy/1?salary=300000"), "свой фильтр — нельзя"
    assert not tb._navigation_allowed("https://example.test/vacancy/2"), "не виденный адрес — нельзя"


def test_record_path_seen_on_site_opens_site_url():
    """Модель переписала урезанный параметр неточно — путь записи тот же, открываем
    адрес сайта. Для страницы поиска (без номера в пути) подмены нет."""
    from agent.context import ConversationContext
    from agent.tools import Toolbox
    from tests.helpers import AllowGate, FakeSession, StubUI
    tb = Toolbox(FakeSession("https://example.test/"), ConversationContext(), AllowGate(), StubUI())
    tb.task = "найди вакансии"
    seen = "https://example.test/vacancy/137098621?query=AI-engineer&from=list"
    tb._seen_links.update({seen, "https://example.test/search/vacancy?text=AI&area=4"})
    assert tb._seen_record("https://example.test/vacancy/137098621?query=AI") == seen
    assert tb._seen_record("https://example.test/search/vacancy?text=AI&salary=300") is None
    assert tb._seen_record("https://example.test/vacancy/999") is None


def test_snapshot_header_lists_tabs_with_switch_numbers():
    from browser.page_state import build_snapshot
    el = {"type": "element", "ref": "1", "tag": "button", "attrs": {}, "name": "Ок", "viewport": True}
    text = build_snapshot("https://x", "t", [el], {}, tab_index=1, tab_count=2,
                          tab_titles=["Поиск вакансий AI-инженер", "Вакансия Инженер AI-агентов"]).render()
    assert "switch_tab 0: Поиск вакансий AI-инженер" in text
    assert "switch_tab 1: Вакансия Инженер AI-агентов ← активная" in text


def test_date_text_to_iso():
    from browser.session import _to_iso
    assert _to_iso("20.10.2026", "date") == "2026-10-20"
    assert _to_iso("с 20/10/2026", "date") == "2026-10-20"
    assert _to_iso("2026-10-20", "date") == "2026-10-20"
    assert _to_iso("20.10.2026 14:30", "datetime-local") == "2026-10-20T14:30"
    assert _to_iso("10.2026", "month") == "2026-10"
    assert _to_iso("9:05", "time") == "09:05"
    assert _to_iso("завтра", "date") is None
