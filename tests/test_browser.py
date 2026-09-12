"""Настоящий Chrome без окна на синтетических страницах: окна, клики, скролл,
чтение по ссылке, прозрачные чекбоксы — и сквозной прогон всего цикла агента,
где вместо модели работает заготовленный сценарий ответов."""

import asyncio
import copy
import re
import time
from types import SimpleNamespace as NS


import agent.runlog as runlog
import agent.subagents as subagents
import replay
from agent import llm
from agent.context import ConversationContext
from agent.orchestrator import Orchestrator
from agent.tools import Toolbox
from browser.page_state import build_snapshot
from helpers import AllowGate, StubUI, real_browser

CONFIRM = """<button id=c onclick="window.n=(window.n||0)+1; document.body.dataset.r =
  confirm('Удалить безвозвратно?') ? 'yes' : 'no'">del</button>
<button id=a onclick="alert('Сохранено'); document.body.dataset.a='done'">alert</button>"""


# ---------- системные окна ----------

def test_native_dialogs(tmp_path):
    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(CONFIRM)
            await s.page.click("#c")
            await asyncio.sleep(0.3)
            assert await s.page.evaluate("document.body.dataset.r") == "no", "без политики — отклонено"
            assert s.drain_dialogs()[0]["message"] == "Удалить безвозвратно?"

            async def slow_yes(dialog):
                await asyncio.sleep(1.0)
                return True, "тест"
            s.dialog_policy = slow_yes
            t0 = time.monotonic()
            await s.page.click("#c")
            assert time.monotonic() - t0 >= 0.9, "клик ждёт решения по окну"
            assert await s.page.evaluate("document.body.dataset.r") == "yes"
            s.drain_dialogs()

            s.dialog_policy = None
            await s.page.click("#a")
            await asyncio.sleep(0.3)
            assert await s.page.evaluate("document.body.dataset.a") == "done", "alert принят сам"
            s.drain_dialogs()

            async def broken(dialog):
                raise RuntimeError("boom")
            s.dialog_policy = broken
            await s.page.click("#c")
            assert await s.page.evaluate("document.body.dataset.r") == "no"
            assert "ошибка" in s.drain_dialogs()[0]["reason"], "сломанная политика не вешает страницу"
    asyncio.run(go())


# ---------- клик ----------

def test_slow_human_does_not_cause_second_click_and_overlay_fallback_works(tmp_path):
    page = CONFIRM + """<div style="position:fixed;left:0;top:200px;width:100%;height:100px;z-index:9;background:#0001"></div>
<button id=u style="position:absolute;top:230px" onclick="window.m=(window.m||0)+1">under</button>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            calls = []

            async def slow_human(dialog):
                calls.append(1)
                await asyncio.sleep(9.0)  # дольше таймаута клика в 8 с
                return False, "человек отказал"
            s.dialog_policy = slow_human
            await tb.take_snapshot()
            ref = next(r for r, n in tb._ref_info.items() if n.get("name") == "del")
            out = await tb.run("click", {"ref": ref})
            assert await s.page.evaluate("window.n") == 1 and len(calls) == 1
            assert "системное окно" in out and "не выполнено" in out

            await s.page.evaluate("document.getElementById('u').setAttribute('data-agent-ref', '999')")
            tb._ref_frames["999"] = s.page.main_frame
            await tb._t_click({"ref": "999"})
            assert await s.page.evaluate("window.m || 0") == 1, "перекрытая кнопка — запасным кликом"
    asyncio.run(go())


# ---------- скролл ----------

rows = lambda word, n: "".join(f'<div style="height:60px"><button>{word} {i}</button></div>' for i in range(n))


def test_scroll_moves_what_is_actually_under_the_user(tmp_path):
    modal = f"""<body style="margin:0">{rows('Кнопка', 40)}
<div style="position:fixed;inset:0;background:#0006;z-index:10"></div>
<div style="position:fixed;top:50px;left:25%;width:50%;height:500px;z-index:11;background:#fff;display:flex;flex-direction:column">
  <div style="height:40px"><button>Закрыть</button></div>
  <div id=panel style="overflow-y:auto;flex:1">{rows('Фильтр', 30)}</div></div></body>"""
    inner = f"""<body style="margin:0;height:100vh;overflow:hidden">
<div id=list style="height:100vh;overflow-y:auto">{rows('Письмо', 50)}</div></body>"""
    plain = f"""<body style="margin:0">{rows('Строка', 60)}</body>"""

    async def go():
        async with real_browser(tmp_path) as s:
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await s.page.set_content(modal)
            await tb.take_snapshot()
            assert tb._overlay
            out = await tb.run("scroll", {"direction": "down"})
            assert await s.page.evaluate("document.getElementById('panel').scrollTop") > 0
            assert await s.page.evaluate("window.scrollY") == 0, "страница под окном стоит"
            assert "вложенный контейнер" in out

            await s.page.set_content(inner)
            await tb.take_snapshot()
            await tb.run("scroll", {"direction": "down"})
            assert await s.page.evaluate("document.getElementById('list').scrollTop") > 0

            await s.page.set_content(plain)
            await tb.take_snapshot()
            await tb.run("scroll", {"direction": "down"})
            assert await s.page.evaluate("window.scrollY") > 0
            await tb.run("scroll", {"direction": "bottom"})
            out = await tb.run("scroll", {"direction": "bottom"})
            assert "не сдвинулась" in out
    asyncio.run(go())


# ---------- чтение по ссылке ----------

def test_read_link_reads_in_background(tmp_path, monkeypatch):
    pages = {
        "/list": '<h1>Выдача</h1><a href="/vacancy/1">Вакансия один</a> <button>Скрыть</button>',
        "/vacancy/1": "<h1>Вакансия один</h1><p>Зарплата 200 000 ₽, удалённо</p>",
    }

    async def route(r):
        path = r.request.url.split("example.test", 1)[1]
        await r.fulfill(status=200, content_type="text/html; charset=utf-8", body=pages.get(path, "404"))

    async def fake_read(question, text):
        return f"прочитано: {'200 000' in text}"
    monkeypatch.setattr(subagents, "read", fake_read)

    async def go():
        async with real_browser(tmp_path) as s:
            await s.context.route("https://example.test/**", route)
            await s.page.goto("https://example.test/list")
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            link = next(r for r, n in tb._ref_info.items() if n.get("name") == "Вакансия один")
            out = await tb.run("read_link", {"ref": link, "question": "зарплата?"})
            assert "прочитано: True" in out
            assert s.page.url.endswith("/list") and len(s.tabs()) == 1
    asyncio.run(go())


# ---------- прозрачный чекбокс ----------

MAIL_ROW = lambda i: f"""<div role="listitem" style="position:relative;cursor:pointer;padding:8px">
  <span style="position:relative;display:inline-block;width:16px;height:16px;border:1px solid #888">
    <input type="checkbox" class="box{i}" style="position:absolute;inset:-2px;width:20px;height:20px;opacity:0;margin:0">
  </span>
  <a href="#from{i}">Отправитель {i}</a> Тема письма номер {i}, короткий текст
  <button style="opacity:0">Архивировать</button>
</div>"""
MAILBOX = ('<div role="toolbar"><button>Удалить</button></div>' + "".join(MAIL_ROW(i) for i in range(3))
           + '<input type="checkbox" class="dead" style="opacity:0;pointer-events:none">')


def test_transparent_native_checkbox_is_indexed_and_clickable(tmp_path):
    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(MAILBOX)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            snap = await tb.take_snapshot()
            boxes = [r for r, n in tb._ref_info.items() if n["tag"] == "input" and n["attrs"].get("type") == "checkbox"]
            assert len(boxes) == 3, "три письма — три чекбокса; без кликов (pointer-events:none) — не в индексе"
            assert not [r for r, n in tb._ref_info.items() if n.get("name") == "Архивировать"], \
                "кнопка «при наведении» своего рефа не получает"
            assert "value=" not in next(l for l in snap.splitlines() if "checkbox" in l)
            await tb.run("click", {"ref": boxes[1]})
            assert await s.page.evaluate("document.querySelector('.box1').checked") is True
            assert "checked='true'" in await tb.take_snapshot()
    asyncio.run(go())


def test_full_link_address_is_indexed_but_not_shown_to_model(tmp_path):
    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content('<base href="https://example.test/shop/"><a href="item?id=7&utm=x">Товар</a><button>Купить</button>')
            nodes, scroll, _ = await s.index_elements()
            by = {n["name"]: n for n in nodes if n["type"] == "element"}
            assert by["Товар"]["url"] == "https://example.test/shop/item?id=7&utm=x"
            assert by["Купить"].get("url") is None
            assert "example.test/shop/item" not in build_snapshot("u", "t", nodes, scroll).render()
    asyncio.run(go())


# ---------- сквозной прогон и восстановление ----------

class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


def scripted_model(sent):
    """Вместо модели — сценарий: отметить первый чекбокс, записать заметку,
    завершить. Каждый запрос сохраняется, чтобы сравнить его с восстановленным."""
    async def call(*, messages, **kw):
        sent.append(copy.deepcopy(messages))
        n = len(sent)
        if n == 1:
            last = messages[-1]["content"]
            ref = re.search(r"\[(\d+)\]<input type='checkbox'", last).group(1)
            block = Block(type="tool_use", id="t1", name="click", input={"ref": ref, "why": "отметить письмо"})
        elif n == 2:
            block = Block(type="tool_use", id="t2", name="remember", input={"fact": "отмечено письмо 0"})
        else:
            block = Block(type="tool_use", id="t3", name="finish", input={"report": "готово", "success": True})
        return NS(content=[Block(type="text", text=f"шаг {n}"), block],
                  usage=NS(input_tokens=10, output_tokens=5))
    return call


def test_agent_loop_end_to_end_and_exact_replay(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "call", scripted_model(sent))
    # Здесь проверяется цикл; планировщик (включён по умолчанию) забрал бы первый
    # заготовленный ответ. Его ветка — отдельный тест в test_context.py.
    monkeypatch.setattr(llm, "PLANNER_MODEL", "")
    monkeypatch.setattr(runlog, "RUNS_DIR", tmp_path / "runs")

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(MAILBOX)
            ui = StubUI()
            orch = Orchestrator(s, AllowGate(), ui, max_steps=10)
            report = await orch.run("отметь первое письмо")
            checked = await s.page.evaluate("document.querySelector('.box0').checked")
            return report, checked, ui

    report, checked, ui = asyncio.run(go())
    assert report == "готово" and checked is True, "цикл дошёл до finish и действие выполнено"

    logs = list((tmp_path / "runs").glob("*.jsonl"))
    assert len(logs) == 1
    events = runlog.load(logs[0])
    assert events[-1]["t"] == "end" and events[-1]["status"] == "done"
    assert [c for _, c, _ in replay.steps(events)] and len(replay.steps(events)) == 3
    for step in (1, 2, 3):
        ctx, _ = replay.rebuild(events, step)
        assert ctx.messages == sent[step - 1], f"шаг {step}: восстановлен не тот запрос, что ушёл модели"
    assert any(l[0] == "info" and "Лог прогона" in l[1][0] for l in ui.lines)


# ---------- повторы в имени карточки ----------

def test_repeated_phrases_are_dropped_from_card_name(tmp_path):
    """Заголовок + скрытая подпись для скринридера + дубль — одна фраза трижды.
    Повтор съедал лимит имени и вытеснял цену из строки ниже экрана."""
    card = """<div role="list"><div role="listitem" style="cursor:pointer">
      <span style="position:absolute;width:1px;height:1px;overflow:hidden">Хот-дог Датский Грабли Box 135 г</span>
      <h3>Хот-дог Датский Грабли Box 135 г</h3>
      <span style="position:absolute;width:1px;height:1px;overflow:hidden">Хот-дог Датский Грабли Box 233 ₽ вместо обычной цены 275 ₽</span>
      <div>233 ₽ 275 ₽ −15%</div><button aria-label="Увеличить">+</button></div></div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(card)
            nodes, _, _ = await s.index_elements()
            item = next(n for n in nodes if n["type"] == "element" and n["attrs"].get("role") == "listitem")
            name = item["name"]
            assert name.count("Хот-дог Датский Грабли Box") == 1, name
            assert "233 ₽" in name and "−15%" in name, "цена и скидка остались"
            assert any(n.get("name") == "Увеличить" for n in nodes if n["type"] == "element")
    asyncio.run(go())


# ---------- состояние нарисованных отметок, подпись галереи ----------

def test_drawn_checkbox_and_switch_report_state(tmp_path):
    """Фильтр-отметка нарисована: <label role=checkbox> над спрятанным input
    или div с aria-checked. Без состояния модель не видит, включён ли фильтр."""
    page = """<input id="sale" type="checkbox" checked style="display:none">
      <label for="sale" role="checkbox" style="cursor:pointer">Распродажа</label>
      <div role="switch" aria-checked="false" tabindex="0">Сначала рядом</div>
      <button aria-pressed="true">Жирный</button>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            nodes, _, _ = await s.index_elements()
            by = {n["name"]: n["attrs"] for n in nodes if n["type"] == "element"}
            assert by["Распродажа"].get("checked") == "true", by
            assert by["Сначала рядом"].get("checked") == "false", by
            assert by["Жирный"].get("pressed") == "true", by
    asyncio.run(go())


def test_gallery_label_dropped_when_title_link_duplicates_it(tmp_path):
    """Первой в карточке идёт картинка-галерея «Ещё 13 фото», заголовок — вторым
    с тем же адресом. Основной переход не должен выглядеть как «открыть фото»."""
    card = """<div style="cursor:pointer">
      <a href="/item/1">Ещё 13 фото</a>
      <a href="/item/1">1-к. квартира, 45 м², 2/6 эт.</a>
      <div>15 900 ₽ в месяц</div><button>В избранное</button></div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(card)
            nodes, _, _ = await s.index_elements()
            links = [n for n in nodes if n["type"] == "element" and n["tag"] == "a"]
            assert len(links) == 1, links
            assert links[0]["name"] == "", links[0]
            assert any(n.get("name") == "В избранное" for n in nodes if n["type"] == "element")
    asyncio.run(go())


# ---------- сетка ячеек внутри «кнопки» ----------

def test_calendar_grid_inside_button_gets_day_refs(tmp_path):
    """Весь календарь — один role=button, дни — голые div с унаследованным
    курсором. Раньше месяц склеивался в одну кнопку «ПН ВТ … 1 2 3 … 30»."""
    days = "".join(f'<div style="width:32px;height:36px">{d}</div>' for d in range(1, 31))
    heads = "".join(f'<span style="cursor:default">{h}</span>' for h in ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"])
    page = f"""<div role="button" style="cursor:pointer">
      <div style="cursor:default">{heads}</div><div style="cursor:default">Сентябрь</div>
      <div style="display:grid;grid-template-columns:repeat(7,32px)">{days}</div></div>
      <div role="button" style="cursor:pointer"><h3>Карточка отеля с длинным названием</h3>
      <p>Описание номера в несколько слов</p>{''.join(f'<i>подпись номер {i} разной длины{"!" * i}</i>' for i in range(20))}</div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            nodes, _, _ = await s.index_elements()
            els = [n for n in nodes if n["type"] == "element"]
            names = [n["name"] for n in els]
            assert all(str(d) in names for d in range(1, 31)), names
            assert not any("1 2 3" in n for n in names), "месяц не склеен в одну кнопку"
            assert "ПН" not in names, "заголовки дней недели не кликабельны"
            card = [n for n in els if n["name"].startswith("Карточка отеля")]
            assert len(card) == 1, "обычная кнопка с разнородным текстом не разбирается на части"
    asyncio.run(go())


# ---------- спрятанный контрол внутри кастомного выпадающего списка ----------

def test_dropdown_wrapper_with_hidden_controls_is_indexed(tmp_path):
    """Кастомный выпадающий список: обёртка с курсором-рукой, подпись и спрятанные
    <input type=hidden> и <select style=display:none>. Раньше спрятанный контрол
    делал обёртку некликабельной — кнопка «Сортировка» выпадала из индекса."""
    page = """<div style="cursor:pointer"><span>Сортировка</span>
      <input type="hidden" name="s" value="1">
      <select style="display:none"><option>По умолчанию</option></select></div>
      <div style="cursor:pointer"><span>Фильтр</span><button>Сбросить</button></div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            nodes, _, _ = await s.index_elements()
            names = [n["name"] for n in nodes if n["type"] == "element"]
            assert any(n.startswith("Сортировка") for n in names), names
            assert "Сбросить" in names, "видимый контрол внутри обёртки по-прежнему свой реф"
    asyncio.run(go())


# ---------- обрезано контейнером прокрутки ≠ перекрыто ----------

def test_clipped_by_inner_scroller_is_offscreen_not_occluded(tmp_path):
    """Календарь прокручивается внутри себя: дни ниже его края в пределах окна,
    но обрезаны. Раньше они шли как перекрытые и выпадали из снапшота целиком."""
    buttons = "".join(f'<button style="display:block;height:40px">День {i}</button>' for i in range(1, 11))
    page = f'<div style="height:100px;overflow-y:auto">{buttons}</div>'

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            nodes, scroll, _ = await s.index_elements()
            els = {n["name"]: n for n in nodes if n["type"] == "element"}
            assert all(f"День {i}" in els for i in range(1, 11)), sorted(els)
            assert els["День 1"]["viewport"] is True
            assert els["День 10"]["viewport"] is False, "обрезанный — «нужен скролл»"
            assert els["День 10"].get("clipped") is True, "помечен как обрезанный блоком"
            assert scroll.get("occluded", 0) == 0, "обрезанное не считается перекрытым"
            # scroll по рефу докручивает вложенный контейнер
            ref = els["День 10"]["ref"]
            await s.page.locator(f'[data-agent-ref="{ref}"]').scroll_into_view_if_needed()
            nodes, _, _ = await s.index_elements()
            els = {n["name"]: n for n in nodes if n["type"] == "element"}
            assert els["День 10"]["viewport"] is True
    asyncio.run(go())


# ---------- ссылка без href с обработчиком в скрипте ----------

def test_anchor_without_href_but_own_pointer_is_indexed(tmp_path):
    """Кнопка «Сортировка» — <a> без href, клик ловит скрипт. Раньше любая
    ссылка без href отбрасывалась до проверки курсора."""
    page = """<a style="cursor:pointer"><span>Сортировка</span></a>
      <a name="top">Якорь без курсора</a>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            await s.page.evaluate(
                "document.querySelector('a').addEventListener('click', () => document.title = 'открыто')")
            nodes, _, frames = await s.index_elements()
            els = {n["name"]: n for n in nodes if n["type"] == "element"}
            assert "Сортировка" in els, sorted(els)
            assert "Якорь без курсора" not in els, "якорь без курсора — не кнопка"
            await s.page.locator(f'[data-agent-ref="{els["Сортировка"]["ref"]}"]').click()
            assert await s.page.title() == "открыто"
    asyncio.run(go())


# ---------- обрезанное краем блока под открытым окном — перекрыто ----------

def test_clipped_elements_under_modal_are_occluded(tmp_path):
    """Лента категорий обрезана краем и лежит под окном блюда. «За краем блока»
    не значит «доступно»: если перекрыт сам блок, перекрыто и обрезанное."""
    cats = "".join(f'<button style="display:inline-block;width:100px">Категория {i}</button>' for i in range(10))
    page = f"""<div style="width:220px;overflow-x:auto;white-space:nowrap">{cats}</div>
      <div style="position:fixed;inset:0;background:#fff"><button>Закрыть окно</button></div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            nodes, _, _ = await s.index_elements()
            names = [n["name"] for n in nodes if n["type"] == "element"]
            assert "Закрыть окно" in names
            assert not any(n.startswith("Категория") for n in names), names
    asyncio.run(go())


# ---------- короткая подпись действия внутри карточки ----------

def test_short_action_label_inside_item_is_kept(tmp_path):
    """Текст карточки включает надписи её кнопок, и «Откликнуться» (12 символов)
    считалось повтором имени записи — кнопка отклика шла безымянной."""
    card = """<div role="button" style="cursor:pointer">
      <a href="/vacancy/1">Инженер AI-агентов, удалённо</a> <span>Опыт 1-3 года Можно удалённо</span>
      <a role="button" href="/response?id=1">Откликнуться</a></div>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(card)
            nodes, _, _ = await s.index_elements()
            els = [n for n in nodes if n["type"] == "element"]
            resp = [n for n in els if n["attrs"].get("href") == "/response?id=1"]
            assert resp and resp[0]["name"] == "Откликнуться", els
            title = [n for n in els if n["attrs"].get("href") == "/vacancy/1"]
            assert title and title[0]["name"] == "", "заголовок в начале карточки — по-прежнему повтор"
    asyncio.run(go())


# ---------- ввод длинного текста ----------

def test_long_text_is_inserted_fast_and_short_is_typed(tmp_path):
    """Письмо в 700 знаков печаталось посимвольно дольше таймаута действия 15 с:
    инструмент падал, хотя текст доходил. Длинное — вставкой, короткое — по
    клавишам (поиск и автокомплит слушают keydown)."""
    import time
    page = """<textarea aria-label="Сопроводительное письмо"></textarea>
      <input aria-label="Поиск">
      <script>
        window.inputs = 0; window.keys = 0;
        document.querySelector('textarea').addEventListener('input', () => window.inputs++);
        document.querySelector('input').addEventListener('keydown', () => window.keys++);
      </script>"""
    letter = "Здравствуйте! " + "Опыт разработки LLM-ассистентов и RAG-систем. " * 15

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            ref = lambda name: next(r for r, n in tb._ref_info.items() if n.get("name") == name)
            t0 = time.monotonic()
            out = await tb.run("type_text", {"ref": ref("Сопроводительное письмо"), "text": letter})
            assert time.monotonic() - t0 < 8, "без посимвольной печати"
            assert "ОШИБКА" not in out, out[:200]
            assert await s.page.evaluate("document.querySelector('textarea').value") == letter
            assert await s.page.evaluate("window.inputs") >= 1, "форма увидела событие input"
            await tb.take_snapshot()
            # Латиницей: кириллицу Playwright вставляет без keydown — так было и до правки.
            await tb.run("type_text", {"ref": ref("Поиск"), "text": "python dev"})
            assert await s.page.evaluate("document.querySelector('input').value") == "python dev"
            assert await s.page.evaluate("window.keys") >= len("python dev"), "короткое — по клавишам"
    asyncio.run(go())


# ---------- скрытые служебные фреймы ----------

def test_hidden_frames_are_not_read_or_indexed(tmp_path):
    """Фрейм сборщика ошибок нулевого размера отдавал читателю 50k символов
    JavaScript — резюме не читалось. Видимый фрейм по-прежнему читается."""
    page = """<p>Резюме: Python AI Developer</p>
      <iframe width="0" height="0" style="border:0" srcdoc="<p>SENTRY_CODE function(){}</p><button>hidden</button>"></iframe>
      <iframe width="300" height="120" srcdoc="<p>Видимый фрейм</p><button>Ок</button>"></iframe>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            await s.page.wait_for_timeout(300)
            text = await s.page_text()
            assert "Резюме" in text and "Видимый фрейм" in text
            assert "SENTRY_CODE" not in text, "скрытый фрейм не читается"
            nodes, _, _ = await s.index_elements()
            names = [n.get("name") for n in nodes if n["type"] == "element"]
            assert "Ок" in names and "hidden" not in names
    asyncio.run(go())


# ---------- поле, которое само возвращает значение, когда его опустошают ----------

def test_replacing_value_in_self_restoring_field(tmp_path):
    """Фильтр «цена до» на сайте отелей: пустое поле тут же становится 25 000.
    Очистка отдельным шагом давала «25 0003500»; выделить и печатать поверх — нет."""
    page = """<input aria-label="Цена до" value="25000">
      <script>
        const f = document.querySelector('input');
        f.addEventListener('input', () => { if (f.value === '') f.value = '25000'; });
      </script>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            ref = next(r for r, n in tb._ref_info.items() if n.get("name") == "Цена до")
            out = await tb.run("type_text", {"ref": ref, "text": "3500"})
            assert "ОШИБКА" not in out, out[:200]
            assert await s.page.evaluate("document.querySelector('input').value") == "3500"
    asyncio.run(go())


def test_field_validating_each_keystroke_gets_value_inserted_whole(tmp_path):
    """«Цена до» проверяет каждую клавишу: «3», «35», «350» меньше нижней границы
    1500 — сайт возвращает максимум. Посимвольно «3500» не встаёт никогда."""
    page = """<input aria-label="Цена до" value="100000">
      <script>
        const f = document.querySelector('input');
        f.addEventListener('input', () => { if (Number(f.value) < 1500) f.value = '100000'; });
      </script>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            ref = next(r for r, n in tb._ref_info.items() if n.get("name") == "Цена до")
            out = await tb.run("type_text", {"ref": ref, "text": "3500"})
            assert await s.page.evaluate("document.querySelector('input').value") == "3500"
            assert "вставлено целиком" in out
    asyncio.run(go())


def test_field_that_expands_input_is_not_overwritten(tmp_path):
    """Автодополнение превращает «Казань» в «Казань, Россия». Это успех ввода —
    запасная вставка поверх стёрла бы выбранную подсказку."""
    page = """<input aria-label="Город" value="Москва">
      <script>
        const f = document.querySelector('input');
        f.addEventListener('input', () => { if (f.value === 'Казань') f.value = 'Казань, Россия'; });
      </script>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            ref = next(r for r, n in tb._ref_info.items() if n.get("name") == "Город")
            out = await tb.run("type_text", {"ref": ref, "text": "Казань"})
            assert await s.page.evaluate("document.querySelector('input').value") == "Казань, Россия"
            assert "вставлено целиком" not in out and "⚠ Поле" not in out
    asyncio.run(go())


# ---------- известные провалы веб-агентов, закрытые заранее ----------

def test_hover_reveals_menu(tmp_path):
    """Меню, раскрывающееся только при наведении: без hover агенту его не открыть."""
    page = """<div id="cat" style="display:inline-block;padding:8px" tabindex="0">Каталог</div>
      <nav id="menu" style="display:none"><a href="/laptops">Ноутбуки</a></nav>
      <script>
        const c = document.getElementById('cat'), m = document.getElementById('menu');
        c.addEventListener('mouseenter', () => m.style.display = 'block');
      </script>"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            snap = await tb.take_snapshot()
            assert "Ноутбуки" not in snap
            ref = next(r for r, n in tb._ref_info.items() if n.get("name") == "Каталог")
            out = await tb.run("hover", {"ref": ref})
            assert "Ноутбуки" in out
    asyncio.run(go())


def test_native_date_input_accepts_local_and_iso_formats(tmp_path):
    """«2026-10-20» посимвольно в поле ДД.ММ.ГГГГ — мусор; вставляем ISO."""
    page = """<input type="date" aria-label="Заезд"><input type="time" aria-label="Время">"""

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(page)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            ref = lambda name: next(r for r, n in tb._ref_info.items() if n.get("name") == name)
            val = lambda i: s.page.evaluate(f"document.querySelectorAll('input')[{i}].value")
            out = await tb.run("type_text", {"ref": ref("Заезд"), "text": "20.10.2026"})
            assert await val(0) == "2026-10-20" and "вставлено 2026-10-20" in out
            await tb.run("type_text", {"ref": ref("Заезд"), "text": "2026-10-24"})
            assert await val(0) == "2026-10-24"
            await tb.run("type_text", {"ref": ref("Время"), "text": "14:30"})
            assert await val(1) == "14:30"
    asyncio.run(go())


def test_read_link_accepts_record_ref_with_single_link(tmp_path, monkeypatch):
    """Модель передаёт реф карточки, а не ссылки внутри неё. Одна ссылка внутри —
    берём её, а не возвращаем ошибку (прогоны 20–21)."""
    card = """<article style="cursor:pointer"><a href="https://example.test/vacancy/7">ML-инженер, удалённо</a>
      <span>Опыт 1–3 года · 250 000 ₽</span><button>Откликнуться</button></article>"""
    read = []

    async def fake_read_in_background(url):
        read.append(url)
        return "Требования: Python"

    async def fake_read(question, text):
        return "Python"

    async def go():
        async with real_browser(tmp_path) as s:
            await s.page.set_content(card)
            monkeypatch.setattr(s, "read_in_background", fake_read_in_background)
            from agent import subagents
            monkeypatch.setattr(subagents, "read", fake_read)
            tb = Toolbox(s, ConversationContext(), AllowGate(), StubUI())
            await tb.take_snapshot()
            record = next(r for r, n in tb._ref_info.items() if n["tag"] == "article")
            out = await tb.run("read_link", {"ref": record, "question": "требования?"})
            assert read == ["https://example.test/vacancy/7"], out
            assert "ОШИБКА" not in out
    asyncio.run(go())
