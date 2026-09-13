"""Обёртка над Playwright: persistent-профиль, видимый браузер, работа с вкладками."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Error as PWError
from playwright.async_api import (
    BrowserContext,
    Dialog,
    Frame,
    Page,
    Playwright,
    async_playwright,
)

from browser.page_state import build_snapshot

_DOM_INDEX_JS = (Path(__file__).parent / "dom_index.js").read_text(encoding="utf-8")
_OVERLAY_JS = (Path(__file__).parent / "overlay.js").read_text(encoding="utf-8")
_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


@contextlib.contextmanager
def _driver_ignores_ctrl_c():
    """Драйвер Playwright — в своей группе процессов, а не в группе терминала.

    Ctrl+C терминал шлёт всей группе переднего плана. Драйвер Playwright
    запускается в той же группе, получал сигнал вместе с нами и умирал, а с
    ним закрывался браузер («Connection closed while reading from the
    driver»). Прервать задачу, не потеряв окно и логины, было нельзя. Своя
    группа — и Ctrl+C достаётся только нашему обработчику, который отменяет
    текущую задачу. Подмена действует только на время запуска драйвера.
    """
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        if sys.platform == "win32":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs.setdefault("start_new_session", True)
        return await original(*args, **kwargs)

    asyncio.create_subprocess_exec = spawn
    try:
        yield
    finally:
        asyncio.create_subprocess_exec = original


def profiles_root() -> Path:
    """Где живут persistent-профили — одно место на агента и стенд.

    Логин, сделанный руками в probe.py, обязан быть виден агенту в main.py.
    Раньше у них были разные умолчания, и агент стартовал разлогиненным.
    """
    return Path(os.getenv("PROFILES_DIR", "~/.browser-agent/profiles")).expanduser()


_DATE_TYPES = {"date", "time", "datetime-local", "month", "week"}


def _to_iso(text: str, kind: str) -> str | None:
    """«20.10.2026», «20/10/2026», «2026-10-20», «14:30» -> формат нативного поля."""
    t = text.strip()
    if kind == "week":
        return t if re.fullmatch(r"\d{4}-W\d{2}", t) else None
    y = mo = d = None
    if m := re.search(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", t):
        y, mo, d = m.groups()
    elif m := re.search(r"(?:(\d{1,2})[./])?(\d{1,2})[./](\d{4})", t):
        d, mo, y = m.groups()
    hm = re.search(r"(\d{1,2}):(\d{2})", t)
    clock = f"{int(hm.group(1)):02d}:{hm.group(2)}" if hm else None
    if kind == "time":
        return clock
    if not (y and mo):
        return None
    if kind == "month":
        return f"{y}-{int(mo):02d}"
    if not d:
        return None
    day = f"{y}-{int(mo):02d}-{int(d):02d}"
    return day if kind == "date" else f"{day}T{clock or '00:00'}"


class BrowserSession:
    """Один persistent-контекст с несколькими вкладками.

    Профиль лежит на диске, поэтому куки и авторизация переживают перезапуск:
    пользователь один раз логинится руками, агент дальше работает в той же сессии.
    """

    def __init__(self, user_data_dir: str, headless: bool = False, channel: str = "chrome"):
        self.user_data_dir = Path(user_data_dir).expanduser()
        self.headless = headless
        self.channel = channel
        self._pw: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # Решение по системному окну (confirm/prompt/beforeunload):
        # корутина dialog -> (принять?, причина). Без неё окно отклоняется —
        # безопасное умолчание.
        self.dialog_policy: Optional[Callable[[Dialog], Awaitable[tuple[bool, str]]]] = None
        self._dialogs: list[dict] = []
        # Растёт в момент открытия окна, ещё до решения по нему. По нему видно,
        # что клик дошёл до цели, даже если сам click() упал по таймауту.
        self.dialogs_opened = 0

    async def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        with _driver_ignores_ctrl_c():
            self._pw = await async_playwright().start()
        self._closed = False
        launch_kwargs = dict(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            ignore_default_args=["--enable-automation"],
            locale="ru-RU",
            # Браузер закрывает только close(): Ctrl+C прерывает задачу, не окно.
            handle_sigint=False,
        )
        try:
            self.context = await self._pw.chromium.launch_persistent_context(
                channel=self.channel, **launch_kwargs
            )
        except Exception:
            # Системного Chrome нет — падаем на chromium из Playwright.
            self.context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)

        self.context.set_default_timeout(15_000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        for page in self.context.pages:
            self._watch(page)
        self.context.on("page", self._on_new_page)
        self.context.on("close", lambda _: setattr(self, "_closed", True))

    def alive(self) -> bool:
        """Окно браузера на месте — человек мог закрыть его между задачами."""
        return self.context is not None and not getattr(self, "_closed", True) and bool(self.tabs())

    async def restart(self) -> None:
        """Открыть браузер заново с тем же профилем — логины на месте."""
        if self.context is not None and not getattr(self, "_closed", True):
            self.page = await self.context.new_page()  # окно живо, закрыты только вкладки
            return
        with contextlib.suppress(Exception):
            await self.close()
        await self.start()

    def _on_new_page(self, page: Page) -> None:
        # Новая вкладка (target=_blank) автоматически становится активной.
        self._watch(page)
        self.page = page

    # ---------- системные окна ----------

    def _watch(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog: Dialog) -> None:
        """Системное окно блокирует страницу, пока на него не ответят.

        Без обработчика Playwright закрывает его отменой — молча. Агент нажал бы
        «удалить», действие отменилось бы, страница не изменилась, и он повторял
        бы клик до упора. Поэтому каждое окно фиксируется для результата
        инструмента, а решение по нему принимает политика — security gate.
        """
        self.dialogs_opened += 1
        kind, message = dialog.type, dialog.message
        accepted, reason = False, "отклонено по умолчанию"
        try:
            if kind == "alert":
                accepted, reason = True, "информационное окно"
            elif self.dialog_policy is not None:
                accepted, reason = await self.dialog_policy(dialog)
        except Exception as e:
            accepted, reason = False, f"ошибка при решении: {e.__class__.__name__}"
        try:
            if accepted:
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:
            pass  # страница уже закрыла окно сама
        self._dialogs.append(
            {"type": kind, "message": message, "accepted": accepted, "reason": reason}
        )

    def drain_dialogs(self) -> list[dict]:
        """Окна, случившиеся с прошлого вызова. Каждое сообщается один раз."""
        out, self._dialogs = self._dialogs, []
        return out

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self._pw:
            await self._pw.stop()

    # ---------- вкладки ----------

    def tabs(self) -> list[Page]:
        return [p for p in (self.context.pages if self.context else []) if not p.is_closed()]

    async def close_tab(self, index: int | None = None) -> Page:
        """Закрыть вкладку и вернуться на оставшуюся.

        Нужно, потому что ссылка с target=_blank открывает новую вкладку, и
        «назад» там не работает — истории у неё нет.
        """
        pages = self.tabs()
        if len(pages) <= 1:
            raise IndexError("Открыта одна вкладка — закрывать нечего.")
        target = self.page if index is None else pages[index]
        await target.close()
        remaining = self.tabs()
        self.page = remaining[-1]
        await self._show(self.page)
        return self.page

    async def _show(self, page: Page) -> None:
        """Сделать вкладку видимой, не поднимая окно браузера без нужды.

        bring_to_front поднимает окно Chrome поверх всех остальных: человек
        терял терминал при каждом переключении вкладок и после каждого
        read_link. Вкладка уже выбрана в окне — не трогаем окно вовсе.
        (С флагами Playwright выбранная вкладка остаётся «visible», даже когда
        окно закрыто терминалом.)"""
        try:
            if await page.evaluate("document.visibilityState") == "visible":
                return
        except Exception:
            pass
        await page.bring_to_front()

    async def _open_background_tab(self) -> Page:
        """Вкладка для фонового чтения — действительно в фоне. new_page()
        создаёт вкладку на переднем плане, и Chrome поднимал своё окно; CDP
        Target.createTarget с background=true — нет. Не вышло — как раньше."""
        try:
            cdp = await self.context.new_cdp_session(self.page)
            try:
                async with self.context.expect_page(timeout=5000) as info:
                    await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
                return await info.value
            finally:
                with contextlib.suppress(Exception):
                    await cdp.detach()
        except Exception:
            return await self.context.new_page()

    async def can_go_back(self) -> bool:
        """Есть ли куда возвращаться. У свежей вкладки истории нет."""
        try:
            return bool(await self.page.evaluate("() => history.length > 1"))
        except Exception:
            return True

    def active_tab_index(self) -> int:
        pages = self.tabs()
        return pages.index(self.page) if self.page in pages else 0

    async def switch_tab(self, index: int) -> Page:
        pages = self.tabs()
        if not 0 <= index < len(pages):
            raise IndexError(f"Вкладки {index} нет. Открыто вкладок: {len(pages)}")
        self.page = pages[index]
        await self._show(self.page)
        return self.page

    async def type_text(self, loc, text: str, clear: bool = True) -> str:
        """Ввести текст в поле -> пометка для агента (пусто, если всё штатно).
        Один код для агента и стенда probe: стенд должен вводить так же, как агент."""
        note = ""
        kind = await self._input_type(loc)
        if kind in _DATE_TYPES:
            return await self._fill_date(loc, text, kind)
        if len(text) > 80:
            # Длинный текст (письмо, комментарий) — вставкой. Посимвольно 700
            # знаков печатались дольше таймаута действия (15 с): инструмент
            # падал с ошибкой, хотя текст доходил, и агент тратил шаг на
            # проверку. fill шлёт событие input — формы на React его видят.
            try:
                if clear:
                    await loc.fill(text)
                else:
                    await self.page.keyboard.insert_text(text)
            except PWError:
                await self.page.keyboard.insert_text(text)
        else:
            if clear:
                # Выделить старое значение и печатать поверх — как человек. Не
                # очищать отдельным шагом: поле фильтра цены на сайте отелей
                # само возвращало максимум, как только становилось пустым, и
                # «3500» дописывалось к «25 000». Первая клавиша заменяет
                # выделение, пустого состояния между шагами нет.
                try:
                    await loc.select_text(timeout=3000)
                except PWError:
                    await self.page.keyboard.press("ControlOrMeta+A")
            # Короткое — посимвольно: поиск и автокомплит слушают keydown.
            await loc.type(text, delay=18, timeout=max(15_000, len(text) * 60))
            note = await self._ensure_value(loc, text)
        return note

    @staticmethod
    async def _input_type(loc) -> str:
        try:
            return await loc.evaluate("el => el.tagName === 'INPUT' ? el.type : ''")
        except PWError:
            return ""

    async def _fill_date(self, loc, text: str, kind: str) -> str:
        """Нативное поле даты или времени. Браузер ждёт ISO (2026-10-20), а
        посимвольный ввод раскладывается по сегментам в порядке локали:
        «2026-10-20» в поле ДД.ММ.ГГГГ превращается в мусор. Формат даты не той
        локали — известная ошибка веб-агентов. Принимаем привычные записи и
        вставляем ISO одним действием."""
        iso = _to_iso(text, kind)
        if iso is None:
            return f"⚠ Не понял «{text}» для поля {kind}: нужна запись вида 20.10.2026, 2026-10-20 или 14:30.\n"
        try:
            await loc.fill(iso)
        except PWError as e:
            return f"⚠ Поле {kind} не приняло «{iso}»: {str(e).splitlines()[0][:120]}\n"
        return f"Поле {kind}: вставлено {iso}.\n"

    async def _ensure_value(self, loc, text: str) -> str:
        """Приняло ли поле посимвольный ввод; если нет — вставить целиком.

        Поле фильтра «цена до» проверяет значение на каждой клавише: «3», «35»,
        «350» меньше нижней границы 1500, и сайт возвращал максимум — «3500» не
        вставало ни с какой попытки, 7 шагов прогона. Вставка целиком даёт одно
        событие без промежуточных значений. Посимвольный ввод остаётся первым:
        его ждут подсказки поиска."""
        norm = lambda v: "".join(ch for ch in v.lower() if ch.isalnum())
        try:
            value = await loc.input_value(timeout=2000)
        except PWError:
            return ""  # не поле формы (contenteditable) — проверять нечего
        want = norm(text)
        # Неудача — введённого в поле нет вовсе («100000» вместо «3500»). Поле,
        # дополнившее ввод («Красноярск» -> «Красноярск, Россия»), — успех:
        # вставка поверх стёрла бы выбранную подсказку.
        if not want or want in norm(value):
            return ""
        try:
            await loc.fill(text)
            await loc.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
            value = await loc.input_value(timeout=2000)
        except PWError:
            pass
        if want in norm(value):
            return "Поле не приняло ввод по клавишам (сбрасывало промежуточное значение) — значение вставлено целиком.\n"
        return f"⚠ Поле показывает «{value}» вместо введённого: сайт отклонил или переформатировал значение.\n"

    # ---------- сырые данные страницы ----------

    async def settle(self, timeout: float = 6.0) -> None:
        """Ждём, пока страница «успокоится»: сеть + отсутствие мутаций DOM."""
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        await asyncio.sleep(0.35)

    def frames(self) -> list[Frame]:
        frames = []
        for f in self.page.frames:
            try:
                if f.is_detached():
                    continue
            except Exception:
                continue
            frames.append(f)
        return frames

    async def _frame_visible(self, frame: Frame) -> bool:
        """Виден ли фрейм на странице. Служебные фреймы — сбор ошибок, счётчики —
        нулевого размера или спрятаны, но их документ внутри себя «видим»: текст
        и элементы проходят проверки индексатора. Так в текст страницы для
        читателя попадало 50 тысяч символов JavaScript из фрейма сборщика ошибок,
        и резюме не читалось."""
        if frame == self.page.main_frame:
            return True
        try:
            el = await frame.frame_element()
            if not await el.is_visible():
                return False
            box = await el.bounding_box()
        except Exception:
            return False
        return bool(box) and box["width"] >= 50 and box["height"] >= 50

    async def visible_frames(self) -> list[Frame]:
        return [f for f in self.frames() if await self._frame_visible(f)]

    async def index_elements(self) -> tuple[list[dict], dict, dict[str, Frame]]:
        """Возвращает (узлы, состояние скролла, карта ref -> фрейм)."""
        nodes: list[dict] = []
        ref_frames: dict[str, Frame] = {}
        scroll: dict = {}
        next_index = 1

        for i, frame in enumerate(await self.visible_frames()):
            try:
                result = await frame.evaluate(_DOM_INDEX_JS, next_index)
            except Exception:
                continue  # cross-origin или фрейм умер по дороге
            if i == 0:
                scroll = result.get("scroll", {})
            frame_nodes = result.get("nodes", [])
            if i > 0 and frame_nodes:
                nodes.append({"type": "text", "value": f"--- фрейм #{i} ---", "depth": 0})
            for n in frame_nodes:
                if n["type"] == "element":
                    ref_frames[n["ref"]] = frame
                nodes.append(n)
            next_index = result.get("nextIndex", next_index)

        return nodes, scroll, ref_frames

    async def tab_titles(self) -> list[str]:
        """Заголовки вкладок — для шапки снапшота, когда их больше одной."""
        tabs = self.tabs()
        if len(tabs) < 2:
            return []
        out = []
        for p in tabs:
            try:
                out.append(await p.title() or p.url)
            except Exception:
                out.append(p.url)
        return out

    async def snapshot(self, max_tokens: int = 4000):
        """Снапшот глазами модели -> (Snapshot, узлы, карта ref -> фрейм).
        Один код для агента и стенда probe: стенд обязан показывать ровно то,
        что увидит модель, — раньше у него была своя копия, и она отстала."""
        nodes, scroll, ref_frames = await self.index_elements()
        snap = build_snapshot(
            url=self.page.url,
            title=await self.page.title(),
            nodes=nodes,
            scroll=scroll,
            max_tokens=max_tokens,
            tab_index=self.active_tab_index(),
            tab_count=len(self.tabs()),
            can_go_back=await self.can_go_back(),
            tab_titles=await self.tab_titles(),
        )
        return snap, nodes, ref_frames

    async def highlight(self, on: bool = True) -> int:
        """Подсветить проиндексированные элементы прямо в браузере.

        Только отладка и демо — в цикле агента не используется.
        """
        total = 0
        for frame in await self.visible_frames():
            try:
                total += await frame.evaluate(_OVERLAY_JS, on) or 0
            except Exception:
                continue
        return total

    async def page_text(self) -> str:
        """Полный видимый текст страницы — для sub-агента-экстрактора."""
        parts = []
        for frame in await self.visible_frames():
            try:
                txt = await frame.evaluate(_TEXT_JS)
            except Exception:
                continue
            if txt and txt.strip():
                parts.append(txt.strip())
        return "\n\n".join(parts)

    async def read_in_background(self, url: str, timeout: float = 20.0) -> str:
        """Текст страницы по адресу — в фоновой вкладке, не уводя активную.

        Для проверки деталей записи из списка: иначе агент тратит три шага на
        «открыть вкладку — прочитать — закрыть и вернуться», а снапшот чужой
        страницы оседает в его контексте.
        """
        active = self.page
        page = await self._open_background_tab()  # _on_new_page сделает её активной
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            return await page.evaluate(_TEXT_JS)
        finally:
            await page.close()
            self.page = active  # вернуть активную вкладку, которую агент видит
            try:
                await self._show(active)
            except Exception:
                pass

