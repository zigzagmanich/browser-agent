"""Проверка под-агента-писателя: не добавляет ли он в письмо того, чего нет в брифе.

Брифы — дословно из прогона 16 (runs/2026-09-12_13-25-24.jsonl). Решение
пользователя: требования вакансии в письме допустимы, даже если их нет в
резюме. Выдумкой считается только то, чего нет в брифе вовсе, — ни в фактах о
соискателе, ни в требованиях: технология или название, взятые с потолка, и
непроверяемые оценки. Каждый бриф дважды: писатель работает с температурой по
умолчанию. Четыре вызова быстрой модели, меньше цента.

    python3 writer_eval.py
"""

import asyncio
import re

from agent import llm, subagents

CASES = [
    (
        "требования вакансии вперемешку с фактами",
        "Напиши короткое деловое сопроводительное письмо (150-180 слов) от соискателя на вакансию "
        "\"Инженер по AI-автоматизации\". Требования вакансии: опыт с LLM API (OpenAI, Claude), Python, "
        "RAG-системы, векторные БД, LangChain/LlamaIndex или n8n/Dify, Docker, Linux, промпт-инжиниринг, "
        "разработка AI-агентов (ReAct, Function Calling). Опыт соискателя: 2+ года в AI/ML, разработка "
        "локальных LLM-ассистентов, RAG-систем, AI-пайплайнов для видео, NLP-анализ. Работал ML-инженером "
        "в компании А и компании Б. Сейчас магистрант университета по теме AI agents/LLM. Стек: "
        "Python, PyTorch, Scikit-learn, FastAPI, Flask, PostgreSQL, MCP, Docker, OpenAI API, SQL. Английский "
        "C1. Подчеркнуть опыт именно с RAG, LLM API, Docker и построением AI-агентов. Выразить готовность "
        "работать удалённо/гибридно. Вежливо, по делу, без воды.",
        r"полностью соответств|эксперт|высоконагруж",
    ),
    (
        "просьба подчеркнуть то, чего нет в фактах",
        "Напиши короткое деловое сопроводительное письмо (150-180 слов) от соискателя на вакансию "
        "\"ML-инженер (LLM, продакшн)\". Это позиция про внедрение LLM-решений в production. Опыт "
        "соискателя: 2+ года в AI/ML, разработка локальных LLM-ассистентов, RAG-систем, AI-пайплайнов для "
        "видео, NLP-анализ (sentiment, topic modeling), детекция аномалий (Isolation Forest), кластеризация "
        "(K-means). Работал ML-инженером в компании А и компании Б. Сейчас магистрант университета "
        "по теме AI agents/LLM. Стек: Python, PyTorch, Scikit-learn, FastAPI, Flask, PostgreSQL, MCP, "
        "Docker, OpenAI API, SQL, MySQL. Английский C1. Подчеркнуть опыт доведения ML/LLM решений до "
        "продакшена, работу с реальными пайплайнами данных и готовность к техническим вызовам. Вежливо, "
        "по делу, без воды.",
        r"полностью соответств|эксперт|высоконагруж",
    ),
]


# Технический термин — название технологии, библиотеки, компании: слово латиницей
# с заглавной буквой, цифрой или знаком (Kafka, PyTorch, RAG, C++). Обычные
# английские слова — «analysis», «production» — это перевод текста брифа, а не
# выдуманный факт: «sentiment analysis» при «NLP-анализ» в брифе шло в провал.
TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}")


def is_term(word: str) -> bool:
    return any(c.isupper() or c.isdigit() or c in "+#" for c in word)


def invented(text: str, brief: str, claims: str) -> list[str]:
    """Что в письме взято не из брифа: термины латиницей и непроверяемые оценки."""
    low = brief.lower()
    terms = {t.rstrip(".-") for t in TERM.findall(text) if is_term(t)}
    out = sorted(t for t in terms if t and t.lower() not in low)
    out += sorted({m.group(0) for m in re.finditer(claims, text, re.IGNORECASE)
                   if m.group(0).lower() not in low})
    return out


async def main() -> None:
    failed = 0
    for name, brief, claims in CASES:
        for attempt in (1, 2):
            text = await subagents.write(brief, max_words=200)
            hits = invented(text, brief, claims)
            ok = not hits
            failed += not ok
            mark = "✓" if ok else "✗"
            print(f"{mark} {name} #{attempt}" + ("" if ok else f" — нет в брифе: {', '.join(hits)}"))
            if not ok:
                print("   " + text.replace("\n", " ")[:400])
    print(f"\nпровалов: {failed} из {len(CASES) * 2};  стоимость {llm.USAGE.summary()}")


if __name__ == "__main__":
    asyncio.run(main())
