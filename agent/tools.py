"""Инструменты оркестратора: схемы для tool-calling и их исполнение.

Инструменты намеренно примитивные и универсальные — это «руки и глаза», а не
сценарии. Ни один из них не знает про почту, магазины или вакансии.
Что именно нажать и куда пойти, решает модель, глядя на снапшот.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from playwright.async_api import Error as PWError

from agent import subagents
from agent.security import Verdict

# Прокрутка ищет, ЧТО крутить: ближайший прокручиваемый предок элемента — панель
# окна поверх страницы, внутренний список почты, — и только иначе само окно.
# window.scrollBy двигал страницу ПОД модалкой: агент крутил фильтры, а
# сдвигалась выдача. Контейнер помечает индексатор (data-agent-scroll).
_SCROLL_CORE = """
  const scrollable = (e) => {
    const oy = getComputedStyle(e).overflowY;
    return (oy === 'auto' || oy === 'scroll') && e.scrollHeight - e.clientHeight > 4;
  };
  let target = null;
  for (let e = el; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
    if (scrollable(e)) { target = e; break; }
  }
  if (!target && !el) target = document.querySelector('[data-agent-scroll]');
  const step = Math.round(vh * 0.85);
  const go = (t, h) => {
    if (dir === 'down') t.scrollBy(0, step);
    else if (dir === 'up') t.scrollBy(0, -step);
    else if (dir === 'top') t.scrollTo(0, 0);
    else t.scrollTo(0, h);
  };
  if (target) { go(target, target.scrollHeight); return 'container'; }
  go(window, document.body.scrollHeight);
  return 'window';
"""
_SCROLL_FROM_ELEMENT = "(el, [dir, vh]) => {" + _SCROLL_CORE + "}"
_SCROLL_PAGE = "([dir, vh]) => { const el = null;" + _SCROLL_CORE + "}"

def _url_shape(url: str) -> str:
    """Вид страницы без конкретных идентификаторов: сегменты с цифрами -> «#».
    …/message/1936547839 и …/message/1933733090 — одна форма, это записи
    одного списка, открываемые по очереди."""
    u = urlsplit(url)
    norm = lambda part: re.sub(r"[^/?#&=]*\d[^/?#&=]*", "#", part)
    return f"{u.hostname}{norm(u.path)}?{norm(u.query)}#{norm(u.fragment)}"


TOOLS: list[dict] = [
    {
        "name": "navigate",
        "description": (
            "Открыть URL в текущей вкладке. Разрешено: главная сайта (без параметров), "
            "адрес от пользователя, адрес ссылки, которую ты видел в снапшотах этой "
            "сессии, — из него можно убрать параметры, но не добавить свои. Так можно "
            "вернуться к записи из списка, прочитанного раньше. Остальное будет отклонено: путь "
            "ищи через навигацию, фильтры и собственный поиск сайта."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "snapshot",
        "description": (
            "Получить свежее состояние страницы: список видимых интерактивных "
            "элементов с номерами [ref] и окружающий текст. Делай это после "
            "любого действия, которое могло изменить страницу."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "click",
        "description": (
            "Кликнуть по элементу с указанным ref из последнего снапшота. Не открывай "
            "записи списка по одной, чтобы их прочитать: содержимое всего списка — "
            "read_page, детали одной записи по ссылке — read_link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "номер элемента из снапшота"},
                "why": {"type": "string", "description": "зачем кликаешь, 3-8 слов"},
            },
            "required": ["ref", "why"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Ввести текст в поле с указанным ref. clear=true заменяет старое значение. "
            "submit=true нажимает Enter после ввода. Поле даты (type='date') принимает "
            "20.10.2026 или 2026-10-20."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "clear": {"type": "boolean", "default": True},
                "submit": {"type": "boolean", "default": False},
            },
            "required": ["ref", "text"],
        },
    },
    {
        "name": "hover",
        "description": (
            "Навести курсор на элемент с ref. Для меню и подсказок, которые раскрываются "
            "при наведении, а не по клику: после наведения их пункты появятся в снапшоте."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "select_option",
        "description": "Выбрать значение в <select> по видимому тексту опции.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "label": {"type": "string"}},
            "required": ["ref", "label"],
        },
    },
    {
        "name": "press_key",
        "description": "Нажать клавишу (Enter, Escape, Tab, ArrowDown, PageDown...).",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "Проскроллить страницу. direction: down|up|top|bottom, либо укажи ref, "
            "чтобы подвести элемент в зону видимости — только для элемента из раздела "
            "«вне видимой области»: элементы основного списка уже на экране."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
                "ref": {"type": "string"},
            },
        },
    },
    {
        "name": "go_back",
        "description": "Вернуться на предыдущую страницу.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_page",
        "description": (
            "Задать вопрос по ПОЛНОМУ тексту текущей страницы. Отдельный под-агент "
            "прочитает страницу целиком и вернёт сжатый ответ. Используй, когда нужен "
            "контент, а не кнопки: списки, описания, детали, длинные тексты. "
            "Это дешевле и надёжнее, чем скроллить и делать снапшоты. "
            "Под-агент видит только видимый текст — ни атрибутов, ни подписей иконок, "
            "ни того, какая кнопка что делает. Вопросы про элементы решай по снапшоту. "
            "Ссылок (href) в тексте нет — адреса бери из снапшота, а детали по ссылке "
            "читай через read_link. Открытое окно или короткая форма уже целиком в "
            "снапшоте — для них read_page не нужен: он читает и страницу под окном и "
            "может выдать её текст за содержимое окна. На ленте с догрузкой читается "
            "только уже загруженное: нужно больше — прокрути вниз и спроси снова."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "read_link",
        "description": (
            "Прочитать страницу по ссылке, не уходя с текущей: под-агент откроет её в "
            "фоне, ответит на вопрос по её тексту и закроет. Один шаг вместо «открыть — "
            "прочитать — вернуться», и снапшот чужой страницы не засоряет контекст. Для "
            "проверки деталей записей из списка. ref — ссылка из текущего снапшота."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "question": {"type": "string"}},
            "required": ["ref", "question"],
        },
    },
    {
        "name": "classify",
        "description": (
            "Пакетно оценить набор объектов по произвольному критерию силами под-агента. "
            "Полезно, когда нужно принять однотипное решение по многим элементам. "
            "items: [{id, content}]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "criterion": {"type": "string", "description": "что считать совпадением"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["id", "content"],
                    },
                },
            },
            "required": ["criterion", "items"],
        },
    },
    {
        "name": "compose_text",
        "description": (
            "Сгенерировать текст по брифу силами под-агента (письмо, сопроводительное, "
            "сообщение, комментарий). Возвращает готовую строку для ввода в поле. "
            "Бриф — тремя частями: «ФАКТЫ О ПОЛЬЗОВАТЕЛЕ» — только то, что ты сам "
            "прочитал на странице, без додумываний; «ПОЛУЧАТЕЛЬ» — кому и о чём "
            "(требования вакансии, суть письма); «ЗАДАЧА» — тон, длина, что сказать. Если "
            "деталей получателя узнать не удалось — так и напиши в брифе и в отчёте."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string"},
                "max_words": {"type": "integer", "default": 200},
                "language": {"type": "string", "default": "русский"},
            },
            "required": ["brief"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Записать факт в рабочие заметки. Заметки переживают вытеснение истории "
            "из контекста. Сюда — то, что понадобится в конце: найденные данные, "
            "выполненные шаги, промежуточные результаты."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string"}},
            "required": ["fact"],
        },
    },
    {
        "name": "switch_tab",
        "description": "Переключиться на вкладку по номеру из шапки снапшота (switch_tab N).",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    },
    {
        "name": "close_tab",
        "description": (
            "Закрыть вкладку (по умолчанию активную) и вернуться на оставшуюся. "
            "Нужна, когда ссылка открылась в новой вкладке: «назад» там не "
            "работает, истории у новой вкладки нет."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer"}},
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Спросить пользователя, когда без него никак: нужен логин, код из СМС, "
            "выбор из неоднозначных вариантов, недостающие данные. Выполнение "
            "приостанавливается до ответа."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Завершить задачу и отчитаться. Вызывай, когда задача выполнена или "
            "выполнить её невозможно. В отчёте — что сделано, что не получилось и почему."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {"type": "string"},
                "success": {"type": "boolean"},
            },
            "required": ["report", "success"],
        },
    },
]


class ToolError(Exception):
    pass


class Toolbox:
    """Исполняет вызовы инструментов. Держит карту ref -> фрейм от снапшота."""

    def __init__(self, session, ctx, gate, ui, snapshot_tokens: int = 4000):
        self.s = session
        self.ctx = ctx
        self.gate = gate
        self.ui = ui
        self.snapshot_tokens = snapshot_tokens
        self._ref_frames: dict = {}
        self._ref_info: dict[str, dict] = {}
        self._last_signature: str | None = None
        # Перекрыта ли страница окном в последнем снапшоте — тогда контролы на
        # экране принадлежат этому окну.
        self._overlay = False
        self._scroll: dict = {}
        self._answers: list[str] = []   # ответы пользователя — адреса из них разрешены
        # Адреса всех ссылок, которые агент видел в снапшотах этой сессии. Сайт
        # сам их показал — это не адрес по памяти. Без них возврат к вакансии из
        # прочитанного раньше списка стоил три шага: назад, скролл, поиск.
        self._seen_links: set[str] = set()
        # Недавние переходы: три разные записи одного вида среди них — перебор
        # записей по одной, даже если между ними агент возвращался к списку.
        self._recent_pages: list[str] = []
        self._warned_shapes: set[str] = set()
        self.task: str = ""
        # Системные окна сайта решает тот же security gate, что и клики.
        session.dialog_policy = self._dialog_policy

    # ---------- снапшот ----------

    async def take_snapshot(self) -> str:
        await self.s.settle()
        snap, nodes, self._ref_frames = await self.s.snapshot(self.snapshot_tokens)
        self._ref_info = {n["ref"]: n for n in nodes if n["type"] == "element"}
        if len(self._seen_links) < 20_000:
            self._seen_links.update(n["url"] for n in self._ref_info.values() if n.get("url"))
        # Сигнатура по содержимому, а не по длине: «Удалить» -> «Отменить» длину
        # не меняет, а страницу меняет.
        self._last_signature = hashlib.md5(f"{self.s.page.url}\n{snap.text}".encode()).hexdigest()
        self._scroll = snap.scroll or {}
        occluded = self._scroll.get("occluded", 0)
        self._overlay = occluded >= 5 and occluded >= snap.visible_count
        return snap.render()

    def _seen_record(self, url: str) -> str | None:
        """Адрес записи, который сайт уже показывал, — по пути, без параметров.
        В снапшоте ссылки урезаны, и модель переписывает параметр неточно
        («query=AI» вместо «query=AI-инженер»): путь /vacancy/137098621 тот же,
        а переход отклонялся. Только для страниц записей (в пути есть номер):
        у страницы поиска параметры — это фильтры, и подменять их нельзя."""
        host, path, _ = self._url_parts(url)
        if not host or not re.search(r"\d", path):
            return None
        for link in self._seen_links:
            h, p, _ = self._url_parts(link)
            if h == host and p == path:
                return link
        return None

    def _locator(self, ref: str):
        frame = self._ref_frames.get(str(ref))
        if frame is None:
            raise ToolError(
                f"ref {ref} не найден — снапшот устарел. Вызови snapshot и возьми актуальные номера."
            )
        return frame.locator(f'[data-agent-ref="{ref}"]')

    # ---------- диспетчер ----------

    async def run(self, name: str, args: dict) -> str:
        """Выполняет вызов инструмента и возвращает текст результата для модели."""
        element = self._ref_info.get(str(args.get("ref"))) if args.get("ref") else None

        # Окно подтверждения сайта сразу после действия, которое человек уже
        # разрешил, — тот же вопрос второй раз. Покрывается разрешением, как и
        # системное окно браузера. Критерий — «следующее действие», а не время:
        # между шагами модель думает дольше, чем живёт окно для системных
        # диалогов. Разрешение одноразовое, любое другое действие его сбрасывает.
        in_overlay = bool(element) and self._overlay and bool(element.get("viewport"))
        covered = in_overlay and self.gate.recently_approved(window=120.0)
        if covered:
            allowed, verdict = True, Verdict("low", "подтверждение сайта для уже разрешённого действия")
        else:
            allowed, verdict = await self.gate.guard(
                name,
                args,
                url=self.s.page.url if self.s.page else "",
                title=(await self.s.page.title()) if self.s.page else "",
                element=element,
                task=self.task,
            )
        if allowed and not covered and verdict.level == "medium" and self.ui:
            self.ui.risk(verdict.level, verdict.reason)
        if not allowed:
            return (
                "ОТКЛОНЕНО ПОЛЬЗОВАТЕЛЕМ. Причина блокировки: "
                f"{verdict.reason}. Не пытайся обойти это действие — предложи альтернативу "
                "или заверши задачу с объяснением."
            )

        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"Неизвестный инструмент: {name}"

        before = self._last_signature
        ok = False
        try:
            output = await handler(args)
            ok = True
        except Finished:
            # Не ошибка, а штатное завершение задачи: его ловит оркестратор.
            # Без этой строки общий except ниже превращал finish в «ОШИБКА
            # Finished», и агент не мог закончить задачу сам — крутился до лимита.
            raise
        except ToolError as e:
            output = f"ОШИБКА: {e}"
        except PWError as e:
            msg = str(e).split("\n")[0][:300]
            output = (
                f"ОШИБКА Playwright: {msg}\n"
                "Возможные причины: элемент исчез, перекрыт модалкой или ещё не отрисован. "
                "Сделай snapshot и посмотри, что на странице сейчас."
            )
        except asyncio.TimeoutError:
            output = "ОШИБКА: таймаут. Сделай snapshot — возможно, страница ещё грузится."
        except Exception as e:
            output = f"ОШИБКА {e.__class__.__name__}: {str(e)[:300]}"
        result = self._annotate(name, args, before, ok, output)
        if covered:
            result = (
                "Окно подтверждения сайта принято без второго вопроса: человек разрешил "
                "это действие шагом раньше.\n\n" + result
            )
        return result

    # Действия, после которых страница обязана измениться. Если снапшот тот же —
    # действие ушло в пустоту, и модели нужно сказать об этом прямо, а не
    # надеяться, что она сама сравнит два снапшота по четыре тысячи токенов.
    _MUTATING = {"click", "select_option", "press_key", "hover"}

    def _annotate(self, name: str, args: dict, before: str | None, ok: bool, output: str) -> str:
        notes = []
        dialogs = self.s.drain_dialogs()
        for d in dialogs:
            verdict = "принято" if d["accepted"] else "отклонено"
            notes.append(
                f"Сайт показал системное окно ({d['type']}): «{d['message'][:200]}» — "
                f"{verdict} ({d['reason']})."
            )
        if any(not d["accepted"] for d in dialogs):
            notes.append("Окно отклонено — действие, скорее всего, не выполнено.")
        else:
            mutating = name in self._MUTATING or (name == "type_text" and args.get("submit"))
            unchanged = ok and before is not None and self._last_signature == before
            if unchanged and mutating:
                notes.append(
                    "⚠ Снапшот после действия совпадает с предыдущим: на странице ничего не "
                    "изменилось. Действие могло уйти мимо — элемент неактивен, перекрыт или "
                    "реагирует не на клик. Не повторяй его: выбери другой элемент или путь."
                )
            elif unchanged and name == "scroll":
                notes.append(
                    "Страница не сдвинулась: элемент уже был на экране или прокручивать "
                    "дальше некуда."
                )
        if ok and name in ("click", "navigate") and self.s.page:
            series = self._series_note(self.s.page.url)
            if series:
                notes.append(series)
        if not notes:
            return output
        return "\n".join(notes) + "\n\n" + output

    def _series_note(self, url: str) -> str:
        """Три разные записи одного вида среди последних переходов — агент
        перебирает список по одной. Промпт и описание click это запрещают, но
        модель следует им не всегда. Возврат к списку между записями серию НЕ
        прерывает: Sonnet ходил «письмо → список → письмо → список», и детектор
        «подряд» его не видел. Пометка — один раз на вид страницы."""
        if self._recent_pages and self._recent_pages[-1] == url:
            return ""
        self._recent_pages = (self._recent_pages + [url])[-8:]
        shape = _url_shape(url)
        same = {u for u in self._recent_pages if _url_shape(u) == shape}
        if len(same) >= 3 and shape not in self._warned_shapes:
            self._warned_shapes.add(shape)
            return (
                f"⚠ Уже третья запись одного вида ({shape}) за последние переходы. Если ты "
                "перебираешь записи списка по одной — остановись: то, что видно в строках "
                "списка, бери из его снапшота или одним read_page, детали по ссылке — "
                "read_link. Так в разы дешевле и быстрее."
            )
        return ""

    async def _dialog_policy(self, dialog) -> tuple[bool, str]:
        """Решение по системному окну сайта (confirm / prompt / beforeunload)."""
        # Человек только что разрешил исходное действие, а сайт переспрашивает о
        # нём же. Второй раз не спрашиваем.
        if self.gate.recently_approved():
            return True, "подтверждено человеком вместе с исходным действием"
        allowed, verdict = await self.gate.guard(
            "confirm_dialog",
            {"text": dialog.message},
            url=self.s.page.url if self.s.page else "",
            # Заголовок не запрашиваем: пока окно открыто, JS страницы стоит,
            # и page.title() повис бы до его закрытия.
            title="",
            element={"tag": "dialog", "attrs": {"type": dialog.type}, "name": dialog.message},
            task=self.task,
        )
        return allowed, verdict.reason or ("разрешено" if allowed else "отклонено")

    # ---------- реализации ----------

    def _resolve_url(self, raw: str) -> str:
        raw = raw.strip()
        if raw.startswith(("http://", "https://")):
            return raw
        current = self.s.page.url if self.s.page else ""
        if raw.startswith("/") and current.startswith(("http://", "https://")):
            return urljoin(current, raw)
        return "https://" + raw

    @staticmethod
    def _url_parts(url: str) -> tuple[str, str, list]:
        u = urlsplit(url)
        host = (u.hostname or "").removeprefix("www.")
        return host, u.path.rstrip("/") or "/", parse_qsl(u.query, keep_blank_values=True)

    def _navigation_allowed(self, url: str) -> bool:
        """Можно ли туда попасть, не зная сайт заранее — как человек, который
        видит его впервые. Адрес, собранный по памяти модели (параметры фильтров,
        внутренние пути), — это знание конкретного сайта, от которого ТЗ требует
        агента отучить."""
        host, path, query = self._url_parts(url)
        if not host:
            return False
        if path == "/" and not query:
            return True  # главная сайта
        # Ссылка, которую сайт показал в этой сессии: тот же путь, параметры —
        # подмножество параметров ссылки. Убрать метки можно, дописать фильтр — нет.
        links = [n["url"] for n in self._ref_info.values() if n.get("url")]
        links += self._seen_links
        if self.s.page:
            links.append(self.s.page.url)  # перезагрузка текущей страницы
        for link in links:
            h, p, q = self._url_parts(link)
            if h == host and p == path and set(query) <= set(q):
                return True
        # Адрес от пользователя: в задаче или в ответах на ask_user.
        bare = url.split("://", 1)[-1].removeprefix("www.").rstrip("/").lower()
        return any(bare in t.lower().replace("www.", "") for t in (self.task, *self._answers))

    async def _t_navigate(self, a: dict):
        url = self._resolve_url(a["url"])
        if not self._navigation_allowed(url):
            url = self._seen_record(url) or url
        if not self._navigation_allowed(url):
            raise ToolError(
                f"переход на {url} не выполнен: такого адреса нет ни в ссылках, которые "
                "сайт показывал в этой сессии, ни в словах пользователя, и это не главная "
                "сайта. Адреса по памяти не собираются — путь ищи через навигацию, фильтры "
                "и поиск сайта."
            )
        try:
            await self.s.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PWError as e:
            return f"Не удалось открыть {url}: {str(e)[:200]}"
        return f"Открыто: {self.s.page.url}\n\n" + await self.take_snapshot()

    async def _t_snapshot(self, a: dict):
        return await self.take_snapshot()

    async def _t_click(self, a: dict):
        loc = self._locator(a["ref"])
        before_url = self.s.page.url
        try:
            await loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        opened = self.s.dialogs_opened
        try:
            await loc.click(timeout=8000)
        except PWError:
            if self.s.dialogs_opened != opened:
                # Клик дошёл: сайт открыл системное окно, и Playwright ждал ответа
                # на него дольше таймаута — человек думал над подтверждением.
                # Запасной клик здесь нажал бы кнопку второй раз, а список за это
                # время мог перерисоваться под соседнюю запись. Проверено
                # экспериментом: без этой ветки обработчик срабатывал дважды.
                pass
            else:
                # Элемент перехвачен оверлеем — пробуем программный клик.
                await loc.evaluate("el => el.click()")
        await self.s.settle()
        changed = self.s.page.url != before_url
        note = f"Клик выполнен. URL {'изменился на ' + self.s.page.url if changed else 'тот же'}."
        return note + "\n\n" + await self.take_snapshot()

    async def _t_type_text(self, a: dict):
        loc = self._locator(a["ref"])
        await loc.scroll_into_view_if_needed(timeout=4000)
        await loc.click(timeout=6000)
        note = await self.s.type_text(loc, a["text"], clear=a.get("clear", True))
        if a.get("submit"):
            await self.s.page.keyboard.press("Enter")
        await self.s.settle()
        return (
            f"Введено {len(a['text'])} символов"
            + (" и отправлено Enter" if a.get("submit") else "")
            + ".\n"
            + note
            + "\n"
            + await self.take_snapshot()
        )

    async def _t_hover(self, a: dict):
        await self._locator(a["ref"]).hover(timeout=6000)
        await asyncio.sleep(0.4)  # меню раскрываются с анимацией
        return "Курсор наведён.\n\n" + await self.take_snapshot()

    async def _t_select_option(self, a: dict):
        loc = self._locator(a["ref"])
        await loc.select_option(label=a["label"], timeout=6000)
        await self.s.settle()
        return f"Выбрано: {a['label']}\n\n" + await self.take_snapshot()

    async def _t_press_key(self, a: dict):
        await self.s.page.keyboard.press(a["key"])
        await self.s.settle()
        return f"Нажато: {a['key']}\n\n" + await self.take_snapshot()

    def _overlay_anchor(self) -> str | None:
        """Контрол из середины открытого окна: от него ищется прокручиваемый
        контейнер. Пока страница перекрыта, всё на экране принадлежит окну;
        середина — чтобы не попасть в шапку окна, которая сама не крутится."""
        if not self._overlay:
            return None
        on_screen = [r for r, n in self._ref_info.items() if n.get("viewport")]
        return on_screen[len(on_screen) // 2] if on_screen else None

    async def _t_scroll(self, a: dict):
        note = ""
        if a.get("ref"):
            await self._locator(a["ref"]).scroll_into_view_if_needed(timeout=5000)
        else:
            d = a.get("direction", "down")
            vh = self._scroll.get("viewport") or 800
            anchor = self._overlay_anchor()
            if anchor is not None:
                where = await self._locator(anchor).evaluate(_SCROLL_FROM_ELEMENT, [d, vh])
            else:
                where = await self.s.page.evaluate(_SCROLL_PAGE, [d, vh])
            if where == "container":
                note = "Прокручен вложенный контейнер (окно поверх страницы или внутренний список).\n\n"
        await asyncio.sleep(0.5)
        return note + await self.take_snapshot()

    async def _t_go_back(self, a: dict):
        await self.s.page.go_back(wait_until="domcontentloaded")
        await self.s.settle()
        return f"Возврат на {self.s.page.url}\n\n" + await self.take_snapshot()

    async def _t_read_page(self, a: dict):
        text = await self.s.page_text()
        self.ui.subagent("reader", a["question"], chars=len(text))
        answer = await subagents.read(a["question"], text)
        return f"Ответ под-агента по содержимому страницы:\n{answer}"

    def _link_of(self, ref: str) -> str | None:
        """Адрес для read_link. Модель часто передаёт реф записи списка (карточка
        вакансии, строка письма), а не ссылки внутри неё — прогоны 20–21. Если в
        записи ровно одна ссылка, берём её: ошибку, которую легко предотвратить,
        лучше не допускать (poka-yoke из руководства Anthropic по инструментам)."""
        info = self._ref_info.get(str(ref)) or {}
        if info.get("url"):
            return info["url"]
        name = info.get("name") or ""
        inner = {n["url"] for n in self._ref_info.values()
                 if n.get("url") and name and (n.get("item") or "") and name.startswith(n["item"].rstrip("…"))}
        return inner.pop() if len(inner) == 1 else None

    async def _t_read_link(self, a: dict):
        url = self._link_of(a["ref"])
        if not url:
            raise ToolError(
                f"[{a['ref']}] — не ссылка, и внутри нет единственной ссылки. read_link читает "
                "страницу по ссылке из текущего снапшота; запись без ссылки открой click, "
                "прочитай read_page и вернись."
            )
        text = await self.s.read_in_background(url)
        self.ui.subagent("reader", a["question"], chars=len(text))
        answer = await subagents.read(a["question"], text)
        return f"Ответ под-агента по странице {url}:\n{answer}"

    async def _t_classify(self, a: dict):
        items = a["items"]
        self.ui.subagent("classifier", a["criterion"], count=len(items))
        verdicts = await subagents.classify(a["criterion"], items)
        return "Вердикты под-агента:\n" + json.dumps(verdicts, ensure_ascii=False, indent=1)

    async def _t_compose_text(self, a: dict):
        self.ui.subagent("writer", a["brief"][:80])
        text = await subagents.write(
            a["brief"], max_words=a.get("max_words", 200), language=a.get("language", "русский")
        )
        return f"Сгенерированный текст ({len(text.split())} слов):\n{text}"

    async def _t_remember(self, a: dict):
        self.ctx.remember(a["fact"])
        return "Записано в рабочие заметки."

    async def _t_switch_tab(self, a: dict):
        await self.s.switch_tab(int(a["index"]))
        return f"Активна вкладка {a['index']}\n\n" + await self.take_snapshot()

    async def _t_close_tab(self, a: dict):
        try:
            await self.s.close_tab(int(a["index"]) if a.get("index") is not None else None)
        except IndexError as e:
            raise ToolError(str(e)) from e
        return "Вкладка закрыта.\n\n" + await self.take_snapshot()

    async def _t_ask_user(self, a: dict):
        answer = await self.ui.ask(a["question"])
        self._answers.append(answer)
        return f"Ответ пользователя: {answer}"

    async def _t_finish(self, a: dict):
        raise Finished(a["report"], bool(a.get("success")))


class Finished(Exception):
    def __init__(self, report: str, success: bool):
        super().__init__(report)
        self.report = report
        self.success = success
