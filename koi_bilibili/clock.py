"""可替换的时钟。测试里把 sleep 变成把时间往前拨，观看不必真等三分钟。"""

import asyncio
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)
