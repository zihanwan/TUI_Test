import asyncio
from contextlib import asynccontextmanager

from mcinst.downloads.settings import DEFAULT_PER_SOURCE


class SourceLimiter:
    """One logical source budget shared across artifacts and their retries."""

    def __init__(self, limit: int = DEFAULT_PER_SOURCE):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("每来源并发数必须在 1～64 之间")
        self.limit = limit
        self._slots: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(self, source: str):
        semaphore = self._slots.setdefault(source, asyncio.Semaphore(self.limit))
        async with semaphore:
            yield
