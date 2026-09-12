"""Стенд для проверки слоя восприятия — без LLM и без агента.

Отвечает на три вопроса, которые нужно закрыть до всего остального:
  1. Читается ли снапшот глазами — то есть поймёт ли его модель.
  2. Сколько он стоит в токенах на тяжёлой странице.
  3. Указывает ли ref на тот элемент, на который мы думаем.

Запуск:
    python probe.py                        # откроет about:blank
    python probe.py https://mail.google.com
    python probe.py --profile work https://hh.ru

Команды:
    snap [N]        снять снапшот с бюджетом N токенов (по умолчанию 4000)
    raw [подстрока] сырые узлы индексатора; с фильтром — все совпадения
    struct REF      из чего состоит элемент: два уровня детей с их формой
    find CSS        почему элемент не в индексе: размер, прозрачность, реф предка
    find text=15    то же по видимому тексту — когда селектора не знаешь (день календаря)
    hl | hl off     подсветить проиндексированные элементы в браузере
    click REF       кликнуть
    type REF текст  очистить поле и ввести текст
    enter REF текст то же + Enter
    scroll [px]     прокрутить (по умолчанию на экран вниз)
    goto URL        перейти
    back            назад
    tabs | tab N    список вкладок / переключиться
    close [N]       закрыть вкладку (по умолчанию активную)
    text            длина полного видимого текста страницы (для сравнения с DOM)
    q               выход
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

from browser.page_state import est_tokens  # noqa: E402
from browser.session import BrowserSession, profiles_root  # noqa: E402


# Диагностика «почему этого нет в снапшоте». CSS пишет человек в стенде — в
# агенте селекторов по-прежнему нет.
FIND_JS = r"""(sel) => {
  // find text=15 — элементы по видимому тексту: у дня календаря или пункта
  // сортировки CSS-селектора заранее не знаешь. Берём самый глубокий элемент
  // с таким текстом, а не всю цепочку его предков.
  let all;
  if (sel.startsWith('text=')) {
    const want = sel.slice(5).trim().toLowerCase();
    all = [...document.querySelectorAll('body *')].filter((el) => {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (t !== want) return false;
      return ![...el.children].some((c) => (c.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase() === want);
    });
  } else {
    all = document.querySelectorAll(sel);
  }
  const rows = [...all].slice(0, 12).map((el) => {
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    const anc = el.parentElement && el.parentElement.closest('[data-agent-ref]');
    return {
      tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      ref: el.getAttribute('data-agent-ref'), anc: anc ? anc.getAttribute('data-agent-ref') : null,
      size: Math.round(r.width) + 'x' + Math.round(r.height),
      opacity: cs.opacity, visibility: cs.visibility, display: cs.display, pe: cs.pointerEvents,
      inLabel: !!el.closest('label'),
      cursor: cs.cursor, tabindex: el.getAttribute('tabindex'),
      aria: [...el.attributes].filter((a) => a.name.startsWith('aria-')).map((a) => a.name + '=' + a.value).join(' ').slice(0, 60),
      // Кто на самом деле носитель курсора-руки и что у него внутри: частая
      // причина «кликабельно, но не в индексе» — контрол внутри обёртки.
      carrier: (() => {
        if (cs.cursor !== 'pointer') return null;
        let c = el;
        while (c.parentElement && getComputedStyle(c.parentElement).cursor === 'pointer') c = c.parentElement;
        const inner = [...c.querySelectorAll('a[href],button,input,select,textarea,[role="button"],[role="link"],[onclick]')]
          .slice(0, 5).map((x) => x.tagName.toLowerCase() + (x.type ? '[' + x.type + ']' : '') + (x.getClientRects().length ? '' : '(скрыт)'));
        return (c === el ? 'сам' : '<' + c.tagName.toLowerCase() + '>' + (c.getAttribute('data-agent-ref') ? ' реф ' + c.getAttribute('data-agent-ref') : ' НЕ в индексе'))
          + ', контролы внутри: ' + (inner.join(' ') || 'нет');
      })(),
      text: ((el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '') + '').trim().slice(0, 40),
    };
  });
  return {total: all.length, rows};
}"""


def report_dialogs(session: BrowserSession) -> None:
    # В стенде политики нет: системное окно отклоняется, но его видно.
    for d in session.drain_dialogs():
        verdict = "принято" if d["accepted"] else "отклонено"
        print(f"!! системное окно ({d['type']}): {d['message'][:120]!r} — {verdict}")


def locator(session: BrowserSession, ref_frames: dict, ref: str):
    frame = ref_frames.get(ref)
    if frame is None:
        raise KeyError(f"ref [{ref}] не найден в последнем снапшоте — сделайте snap заново")
    return frame.locator(f'[data-agent-ref="{ref}"]')


async def repl(session: BrowserSession) -> None:
    ref_frames: dict = {}
    loop = asyncio.get_event_loop()

    while True:
        try:
            raw = (await loop.run_in_executor(None, input, "probe> ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue

        cmd, _, rest = raw.partition(" ")
        cmd, rest = cmd.lower(), rest.strip()

        try:
            if cmd in ("q", "quit", "exit"):
                break

            elif cmd == "help":
                print(__doc__)

            elif cmd == "snap":
                budget = int(rest) if rest else 4000
                t0 = time.time()
                snap, nodes, ref_frames = await session.snapshot(budget)
                rendered = snap.render()
                print(rendered)
                els = sum(1 for n in nodes if n["type"] == "element")
                print(
                    f"\n-- {els} элементов в индексе, {len(nodes) - els} текстовых узлов; "
                    f"снапшот {len(rendered)} симв. ≈ {est_tokens(rendered)} токенов; "
                    f"{'УРЕЗАН; ' if snap.truncated else ''}{time.time() - t0:.2f} с"
                )

            elif cmd == "raw":
                nodes, scroll, ref_frames = await session.index_elements()
                els = [n for n in nodes if n["type"] == "element"]
                in_vp = sum(1 for n in els if n.get("viewport"))
                print(
                    f"элементов: {len(els)} (во вьюпорте {in_vp}), "
                    f"текстовых узлов: {len(nodes) - len(els)}, скролл: {scroll}"
                )
                if rest:
                    # Фильтр по имени/тегу/атрибутам: искать элемент в списке на
                    # четыре сотни строк вручную невозможно.
                    q = rest.lower().replace("'", "").replace('"', "")

                    def hay(n: dict) -> str:
                        attrs = " ".join(f"{k}={v}" for k, v in n.get("attrs", {}).items())
                        return f"{n['tag']} {attrs} {n.get('name', '')}".lower()

                    hits = [n for n in els if q in hay(n)]
                    print(f"совпадений с {rest!r}: {len(hits)}")
                    shown = hits[:60]
                else:
                    shown = els[:25]
                for n in shown:
                    vp = " " if n.get("viewport") else "↓"
                    attrs = n.get("attrs", {})
                    extra = f" role={attrs['role']}" if attrs.get("role") else ""
                    print(f" {vp}[{n['ref']}]<{n['tag']}{extra}> {n.get('name', '')[:70]}")
                if not rest and len(els) > 25:
                    print(f"  … ещё {len(els) - 25} (raw ПОДСТРОКА — найти нужное)")

            elif cmd == "struct":
                loc = locator(session, ref_frames, rest)
                data = await loc.evaluate(
                    r"""(el) => {
                      const SEL = 'a[href],button,input,select,textarea,'
                        + '[role=\"button\"],[role=\"link\"],[onclick]';
                      const info = (e) => ({
                        tag: e.tagName.toLowerCase(),
                        cls: (e.getAttribute('class') || '').slice(0, 50),
                        role: e.getAttribute('role') || '',
                        controls: e.querySelectorAll(SEL).length,
                        text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 45),
                      });
                      const node = (e) => ({
                        ...info(e),
                        kids: [...e.children].map(info),
                      });
                      return { self: info(el), kids: [...el.children].map(node) };
                    }"""
                )

                def show(n: dict, pad: str) -> None:
                    print(
                        f"{pad}<{n['tag']}> role={n['role']!r} контролов: {n['controls']} "
                        f"class={n['cls']!r} | {n['text']}"
                    )

                show(data["self"], "сам: ")
                print(f"детей: {len(data['kids'])}")
                for k in data["kids"]:
                    show(k, "  ")
                    for g in k["kids"]:
                        show(g, "      ")

            elif cmd == "find":
                if not rest:
                    print("find CSS или find text=ТЕКСТ — например: find input[type=checkbox], find text=15")
                    continue
                if not ref_frames:
                    _, _, ref_frames = await session.snapshot()  # чтобы рефы были проставлены
                found = False
                for fi, frame in enumerate(session.frames()):
                    try:
                        res = await frame.evaluate(FIND_JS, rest)
                    except Exception:
                        continue
                    if not res["total"]:
                        continue
                    found = True
                    print(f"фрейм #{fi}: найдено {res['total']}, показаны {len(res['rows'])}")
                    for r in res["rows"]:
                        where = f"реф [{r['ref']}]" if r["ref"] else (
                            f"НЕ в индексе, внутри [{r['anc']}]" if r["anc"] else "НЕ в индексе")
                        print(
                            f"  <{r['tag']}> role={r['role']!r} {where} | {r['size']} "
                            f"opacity={r['opacity']} visibility={r['visibility']} display={r['display']} "
                            f"pointer-events={r['pe']} в_label={r['inLabel']} cursor={r['cursor']} "
                            f"tabindex={r['tabindex']} {r['aria']} | {r['text']}"
                        )
                        if r.get("carrier"):
                            print(f"      носитель курсора: {r['carrier']}")
                if not found:
                    print(f"ничего не найдено по {rest!r} ни в одном фрейме")

            elif cmd == "hl":
                if rest == "off":
                    await session.highlight(False)
                    print("подсветка выключена")
                else:
                    if not ref_frames:
                        _, _, ref_frames = await session.snapshot()
                    n = await session.highlight(True)
                    print(f"подсвечено {n} элементов")

            elif cmd == "click":
                loc = locator(session, ref_frames, rest)
                await loc.scroll_into_view_if_needed(timeout=5000)
                await loc.click(timeout=8000)
                await session.settle()
                print(f"клик по [{rest}] выполнен")
                report_dialogs(session)

            elif cmd in ("type", "enter"):
                ref, _, value = rest.partition(" ")
                loc = locator(session, ref_frames, ref)
                await loc.scroll_into_view_if_needed(timeout=5000)
                await loc.click(timeout=8000)
                note = await session.type_text(loc, value)  # тем же кодом, что у агента
                try:
                    # До Enter: поиск уводит на новую страницу, поля там уже нет,
                    # и чтение значения после Enter висело 15 с и падало.
                    shown = await loc.input_value(timeout=2000)
                except Exception:
                    shown = "?"
                if cmd == "enter":
                    await loc.press("Enter")
                await session.settle()
                print(f"ввод в [{ref}]: {value!r}; в поле: {shown!r}")
                if note:
                    print(note.strip())
                report_dialogs(session)

            elif cmd == "scroll":
                _, scroll, _ = await session.index_elements()
                dy = int(rest) if rest else int(scroll.get("viewport", 800) * 0.85)
                if scroll.get("inner"):
                    # Колесо крутит то, под чем курсор — наводим на сам контейнер.
                    await session.page.mouse.move(scroll["cx"], scroll["cy"])
                await session.page.mouse.wheel(0, dy)
                await asyncio.sleep(0.4)
                print(f"прокрутка на {dy}px {'внутри контейнера' if scroll.get('inner') else 'окна'}")

            elif cmd == "goto":
                url = rest if rest.startswith("http") else "https://" + rest
                await session.page.goto(url, wait_until="domcontentloaded")
                await session.settle()

            elif cmd == "back":
                await session.page.go_back()
                await session.settle()

            elif cmd == "tabs":
                for i, p in enumerate(session.tabs()):
                    mark = "*" if p is session.page else " "
                    print(f" {mark}[{i}] {p.url[:90]}")

            elif cmd == "close":
                await session.close_tab(int(rest) if rest else None)
                print(f"вкладка закрыта, активна: {session.page.url[:90]}")

            elif cmd == "tab":
                await session.switch_tab(int(rest))
                print(f"активна вкладка {rest}: {session.page.url[:90]}")

            elif cmd == "text":
                txt = await session.page_text()
                print(
                    f"полный видимый текст: {len(txt)} симв. ≈ {est_tokens(txt)} токенов "
                    f"(и по нему нельзя кликать — только читать)"
                )

            elif cmd == "html":
                html = await session.page.content()
                snap, nodes, ref_frames = await session.snapshot()
                rendered = snap.render()
                print(
                    f"сырой HTML:  {len(html):>9} симв. ≈ {est_tokens(html):>7} токенов\n"
                    f"снапшот:     {len(rendered):>9} симв. ≈ {est_tokens(rendered):>7} токенов\n"
                    f"сжатие:      {len(html) / max(len(rendered), 1):.1f}x"
                )

            else:
                print("неизвестная команда, наберите help")

        except Exception as exc:
            print(f"!! {type(exc).__name__}: {exc}")


async def main() -> None:
    load_dotenv()  # PROFILES_DIR из .env должен действовать и здесь, как в main.py
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="about:blank")
    ap.add_argument("--profile", default="default")
    args = ap.parse_args()

    session = BrowserSession(user_data_dir=str(profiles_root() / args.profile))
    await session.start()

    if args.url != "about:blank":
        url = args.url if args.url.startswith("http") else "https://" + args.url
        await session.page.goto(url, wait_until="domcontentloaded")
        await session.settle()

    print("\nБраузер открыт. Залогиньтесь руками, если нужно — профиль сохранится.")
    print("help — список команд.\n")

    try:
        await repl(session)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
