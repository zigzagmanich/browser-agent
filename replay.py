#!/usr/bin/env python3
"""Одно решение модели по сохранённому прогону — вместо целого прогона.

Прогон пишется в runs/*.jsonl. По записи восстанавливается ровно тот запрос,
что ушёл модели на шаге N, и модели задаётся только он — с ТЕКУЩИМ промптом и
инструментами. Так проверяется правка промпта: «что модель сделает на шаге 7
теперь?» — за один вызов (~$0.01–0.05 на Sonnet), а не за прогон ($0.25+).

    python3 replay.py runs/<файл>.jsonl --list                  # шаги и что на них сделано
    python3 replay.py runs/<файл>.jsonl --step 7                # решение на шаге 7 сейчас
    python3 replay.py runs/<файл>.jsonl --step 7 --model claude-opus-5 --n 3
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent import llm  # noqa: E402
from agent.context import ConversationContext  # noqa: E402
from agent.orchestrator import SYSTEM  # noqa: E402
from agent.runlog import load  # noqa: E402
from agent.tools import TOOLS  # noqa: E402


def _short(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)[:100]


def steps(events: list[dict]) -> list[tuple[int, list[str], str]]:
    """(номер шага, вызовы инструментов, текст модели) — для --list."""
    out, n = [], 0
    for ev in events:
        if ev["t"] == "compact":
            n += 1
        elif ev["t"] == "assistant" and n:
            blocks = ev["content"]
            calls = [f"{b['name']}({_short(b.get('input', {}))})" for b in blocks if b.get("type") == "tool_use"]
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            out.append((n, calls, text))
    return out


def rebuild(events: list[dict], step: int) -> tuple[ConversationContext, dict]:
    """Контекст ровно на момент запроса модели на шаге step — тем же кодом
    сжатия, что работал в прогоне."""
    start = next((e for e in events if e["t"] == "start"), {})
    ctx = ConversationContext()
    ctx.journal = list(start.get("journal", []))
    n = 0
    for ev in events:
        t = ev["t"]
        if t == "start_task":
            ctx.start_task()
        elif t in ("user", "assistant"):
            ctx.messages.append({"role": t, "content": copy.deepcopy(ev["content"])})
        elif t == "remember":
            ctx.remember(ev["fact"])
        elif t == "compact":
            ctx.compact()
            n += 1
            if n == step:
                return ctx, start
    raise SystemExit(f"в прогоне нет шага {step} (всего шагов: {n})")


def strip_thinking(messages: list[dict]) -> None:
    """Блоки размышлений привязаны к модели, которая их написала. Другой
    модели отдаём историю без них."""
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            kept = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
            m["content"] = kept or [{"type": "text", "text": "…"}]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--step", type=int)
    ap.add_argument("--model")
    ap.add_argument("--n", type=int, default=1, help="сколько раз спросить (разброс решений)")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                    help="глубина размышлений модели — проверить, меняет ли она решение")
    args = ap.parse_args()

    events = load(args.path)
    recorded = steps(events)
    if args.list or not args.step:
        for n, calls, text in recorded:
            print(f"шаг {n:>2}: {', '.join(calls) or '— без инструмента'}" + (f"\n        │ {text[:110]}" if text else ""))
        return

    ctx, start = rebuild(events, args.step)
    model = args.model or start.get("model") or llm.ORCHESTRATOR_MODEL
    if model != start.get("model"):
        strip_thinking(ctx.messages)
    was = next((c for n, c, _ in recorded if n == args.step), None)
    print(f"задача: {start.get('task')}")
    print(f"шаг {args.step}: в записи {start.get('model')} сделала {', '.join(was) if was else '—'}")

    for i in range(args.n):
        resp = await llm.call(
            model=model,
            system=SYSTEM + ctx.journal_block(),
            messages=ctx.messages,
            tools=TOOLS,
            max_tokens=8000,
            cache_system=args.n > 1,  # кеш окупается только при повторах
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            effort=args.effort,
        )
        calls = [f"{b.name}({_short(b.input)})" for b in llm.tool_uses(resp)]
        label = model + (f" · effort={args.effort}" if args.effort else "")
        print(f"\n[{i + 1}] {label}: {', '.join(calls) or '— без инструмента'}")
        if llm.text_of(resp):
            print("    │ " + llm.text_of(resp)[:300])
    print(f"\nстоимость: {llm.USAGE.summary()}")


if __name__ == "__main__":
    asyncio.run(main())
