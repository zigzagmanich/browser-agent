"""Превращает сырой DOM в компактное текстовое представление под бюджет токенов.

Ключевая идея управления контекстом: в LLM никогда не уходит HTML. Уходит
линеаризованный список видимых интерактивных элементов с числовыми рефами
плюс окружающий текст, урезанный по приоритету:

  1. элементы во вьюпорте   — всегда
  2. текст во вьюпорте      — всегда, с усечением длинных блоков
  3. элементы вне вьюпорта  — только имя, без атрибутов
  4. остальное              — выбрасывается, агенту сообщается как это дочитать

Текст вне вьюпорта выбрасывается целиком: кликнуть по нему нельзя, а прочитать
можно после скролла или через read_page. Иначе футер и нижняя часть длинного
списка съедают бюджет раньше, чем до него доходят элементы на экране.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Грубая, но стабильная оценка: для смеси русского/английского ~3 символа на токен.
CHARS_PER_TOKEN = 3

# Доля бюджета на элементы ниже экрана. В длинном списке их сотни, кликнуть по
# ним всё равно нельзя без скролла — но знать, что там ещё есть записи, нужно.
# Каждый снапшот один раз пишется в кеш промпта и потом читается на каждом
# шаге, поэтому этот раздел — прямые деньги: на странице с длинной лентой
# ниже экрана он занимал ~60% снапшота. Ниже экрана кликнуть всё равно нельзя,
# разделу нужно лишь ответить на вопрос «стоит ли туда скроллить».
DEFERRED_SHARE = 0.25
# Подпись элемента ниже экрана: чтобы понять, что там есть, хватает начала,
# полное имя записи (до 160 символов) агент увидит, когда доскроллит.
COMPACT_NAME = 70


def est_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class Snapshot:
    url: str
    title: str
    text: str
    ref_count: int
    visible_count: int = 0
    truncated: bool = False
    scroll: dict = field(default_factory=dict)
    tab_index: int = 0
    tab_count: int = 1
    can_go_back: bool = True
    tab_titles: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"URL: {self.url}\nЗаголовок: {self.title}"
        s = self.scroll or {}
        if s:
            pos = s.get("y", 0)
            total = max(s.get("height", 1) - s.get("viewport", 0), 1)
            pct = min(100, round(100 * pos / total)) if total else 100
            where = "внутри списка" if s.get("inner") else "страницы"
            tail = "конец" if s.get("atBottom") else "есть контент ниже"
            head += f"\nСкролл {where}: {pct}% ({tail})"
        head += (
            f"\nИнтерактивных элементов: {self.ref_count} "
            f"(на экране {self.visible_count})"
        )
        # Ссылка часто открывается в новой вкладке, и тогда «назад» ведёт в
        # пустоту: истории у свежей вкладки нет. Без этой строки агент будет
        # раз за разом жать go_back и не понимать, почему ничего не меняется.
        if self.tab_count > 1:
            head += f"\nВкладок открыто: {self.tab_count}, активна {self.tab_index + 1}-я"
            # Заголовки вкладок с номерами для switch_tab: без них агент тратил
            # шаг на list_tabs перед каждым возвратом к списку.
            for i, t in enumerate(self.tab_titles):
                mark = " ← активная" if i == self.tab_index else ""
                head += f"\n  switch_tab {i}: {t[:60]}{mark}"
        if not self.can_go_back:
            head += "\n«Назад» здесь не работает: у вкладки нет истории."
        # Перекрытые контролы по отдельности не показываются — но если их много,
        # а доступных на экране почти нет, модель должна знать почему.
        occluded = s.get("occluded", 0)
        if occluded >= 5 and occluded >= self.visible_count:
            head += (
                f"\n⚠ Перекрыто другим слоем: {occluded} элементов, доступно на экране "
                f"{self.visible_count}. Похоже, поверх страницы открыто окно — сначала "
                "разберись с ним."
            )
        if self.truncated:
            head += (
                "\n⚠ Снапшот урезан по лимиту токенов. Если нужного элемента нет — "
                "проскролль или вызови read_page с конкретным вопросом."
            )
        return f"{head}\n\n{self.text}"


def _fmt_element(n: dict, compact: bool = False) -> str:
    attrs = n.get("attrs", {})
    tag = n["tag"]
    name = n.get("name", "")
    if compact:
        # Роль и адрес — единственное, чем безымянные элементы отличаются друг
        # от друга: строка «[91]<div>» не говорит модели вообще ничего.
        hint = ""
        if attrs.get("role"):
            hint += f" role={attrs['role']!r}"
        if not name and attrs.get("href"):
            hint += f" href={attrs['href'][:40]!r}"
        if len(name) > COMPACT_NAME:
            name = name[:COMPACT_NAME] + "…"
        return f"[{n['ref']}]<{tag}{hint}> {name}"
    parts = [f"{k}={v!r}" for k, v in attrs.items() if v]
    attr_s = (" " + " ".join(parts)) if parts else ""
    return f"[{n['ref']}]<{tag}{attr_s}> {name}"


def build_snapshot(
    url: str,
    title: str,
    nodes: list[dict],
    scroll: dict,
    max_tokens: int = 4000,
    tab_index: int = 0,
    tab_count: int = 1,
    can_go_back: bool = True,
    tab_titles: list[str] | None = None,
) -> Snapshot:
    budget = max_tokens * CHARS_PER_TOKEN
    lines: list[str] = []
    used = 0
    truncated = False
    ref_count = 0

    # Первый проход — всё, что во вьюпорте, плюс текст.
    deferred: list[dict] = []
    prev_text: str | None = None
    # Имена последних элементов на экране. Карточка блюда или товара часто —
    # кнопка с полной подписью («Кинг Фри большой, 210 ₽, 154 г») и тут же тот
    # же текст видимыми строками: цена, название, вес. Для модели это повтор,
    # на экране меню ~30 строк. Короткое («1» между «−» и «+») не трогаем.
    recent_names: list[str] = []
    for n in nodes:
        if n["type"] == "element":
            ref_count += 1
            prev_text = None
            if not n.get("viewport", True):
                deferred.append(n)
                continue
            if n.get("name"):
                recent_names = (recent_names + [n["name"]])[-3:]
            line = " " * min(n.get("depth", 0), 4) + _fmt_element(n)
        else:
            if not n.get("viewport", True):
                continue
            value = n["value"]
            if len(value) > 300:
                value = value[:300] + "…"
            # Подряд идущие одинаковые куски текста (заглушки фреймов,
            # повторы разметки) агенту ничего не добавляют.
            if value == prev_text:
                continue
            if len(value) >= 4 and any(value in nm for nm in recent_names):
                continue
            prev_text = value
            line = " " * min(n.get("depth", 0), 4) + value

        if used + len(line) > budget:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1

    # Второй проход — элементы за пределами вьюпорта, в сжатом виде и под
    # отдельным потолком: иначе низ длинного списка вытесняет всё остальное.
    if deferred and not truncated:
        stop = min(budget, used + int(budget * DEFERRED_SHARE))
        # Обрезанное краем прокручиваемого блока на экране (календарь, длинный
        # выпадающий список) — первым и отдельно. В порядке документа такой блок
        # часто стоит после подвала сайта: всплывашки рендерят в конец страницы,
        # и дни календаря уходили в «…ещё N ниже» за ссылками подвала. Плюс
        # scroll без ref крутит страницу, а не блок, — это надо сказать явно.
        clipped = [n for n in deferred if n.get("clipped")]
        below = [n for n in deferred if not n.get("clipped")]
        groups = [
            (clipped, "\n— внутри прокручиваемого блока на экране, за его краем "
                      "(scroll с ref элемента докрутит блок) —"),
            (below, "\n— элементы вне видимой области (нужен скролл перед кликом) —"),
        ]
        cut = False
        for group, header in groups:
            if not group or cut:
                continue
            if used + len(header) >= stop:
                if group is clipped or not clipped:
                    truncated = True
                break
            lines.append(header)
            used += len(header)
            for i, n in enumerate(group):
                line = _fmt_element(n, compact=True)
                if used + len(line) > stop:
                    # Это не исчерпание бюджета, а сознательный обрез хвоста:
                    # флаг truncated поднимать нельзя, иначе агент будет искать
                    # несуществующую причину и звать read_page вместо скролла.
                    lines.append(f"…ещё {len(group) - i} элементов ниже — нужен скролл")
                    cut = True
                    break
                lines.append(line)
                used += len(line) + 1
    elif deferred:
        truncated = True

    return Snapshot(
        url=url,
        title=title,
        text="\n".join(lines),
        ref_count=ref_count,
        visible_count=ref_count - len(deferred),
        truncated=truncated,
        scroll=scroll,
        tab_index=tab_index,
        tab_count=tab_count,
        can_go_back=can_go_back,
        tab_titles=tab_titles or [],
    )


def chunk_text(text: str, chunk_tokens: int = 6000, overlap: int = 200) -> list[str]:
    """Режет длинный текст страницы на куски для под-агента-читателя: по
    границе строки, с перекрытием — чтобы факт на стыке не потерялся."""
    size = chunk_tokens * CHARS_PER_TOKEN
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while True:
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end])
        if end >= len(text):
            return chunks
        start = end - overlap
