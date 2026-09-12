"""Заглушки для тестов механики агента — без модели и, где можно, без браузера."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace as NS

from agent.security import Verdict


class AllowGate:
    """Пропускает всё: для тестов механики, а не политики безопасности."""

    async def guard(self, *a, **k):
        return True, Verdict("low", "")

    def recently_approved(self, window: float = 15.0) -> bool:
        return False


class StubUI:
    """Записывает всё, что агент хотел бы напечатать."""

    def __init__(self):
        self.lines: list[tuple] = []

    def __getattr__(self, name):
        def record(*a, **k):
            self.lines.append((name, a))
        return record


class FakePage:
    def __init__(self, url: str = "https://example.test/"):
        self.url = url
        self.opened: list[str] = []

    async def title(self) -> str:
        return "t"

    async def goto(self, url, **kw):
        self.opened.append(url)
        self.url = url


class FakeSession:
    def __init__(self, url: str = "https://example.test/"):
        self.page = FakePage(url)
        self.dialog_policy = None
        self.pending: list[dict] = []
        self.dialogs_opened = 0

    def drain_dialogs(self) -> list[dict]:
        out, self.pending = self.pending, []
        return out

    async def settle(self) -> None:
        pass


@contextlib.asynccontextmanager
async def real_browser(tmp_path):
    """Настоящий Chrome без окна на временном профиле — пользовательский не трогаем."""
    from browser.session import BrowserSession
    session = BrowserSession(str(tmp_path / "profile"), headless=True)
    await session.start()
    try:
        yield session
    finally:
        await session.close()


def text_response(text: str):
    """Ответ модели одним текстовым блоком — для под-агентов и guard-модели."""
    return NS(content=[NS(type="text", text=text)], usage=NS(input_tokens=1, output_tokens=1))
