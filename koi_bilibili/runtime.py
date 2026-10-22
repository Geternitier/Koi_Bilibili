"""几个服务共用的运行时：时钟、存储、后端、通知。"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from koi.protocol import EventIn

from koi_bilibili.backend import BiliBackend
from koi_bilibili.clock import Clock
from koi_bilibili.config import Settings
from koi_bilibili.errors import RISK_CODES, BiliError, public_message
from koi_bilibili.models import Account
from koi_bilibili.store import Store


class Runtime:
    def __init__(
        self,
        *,
        ctx: Any,
        settings: Settings,
        store: Store,
        backend: BiliBackend,
        clock: Clock,
        work_dir: Path,
        can_interact: bool,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.store = store
        self.backend = backend
        self.clock = clock
        self.work_dir = work_dir
        self.can_interact = can_interact
        self.account: Account | None = None
        self.online = False
        self.cookies: dict[str, str] = {}
        self.risk_until: datetime | None = None
        self._noticed: dict[str, datetime] = {}

    async def notify(
        self,
        text: str,
        *,
        priority: int = 2,
        trust: str = "external",
        type: str = "plugin_notice",
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"text": text, "sender": "B站"}
        if extra:
            payload.update(extra)
        await self.ctx.emit(
            EventIn(
                type=type, priority=priority, trust=trust, payload=payload, time=self.clock.now()
            )
        )

    async def notify_once(self, key: str, text: str, *, seconds: float = 1800) -> None:
        last = self._noticed.get(key)
        now = self.clock.now()
        if last is not None and (now - last).total_seconds() < seconds:
            return
        self._noticed[key] = now
        await self.notify(text)

    def pause_reason(self) -> str | None:
        if self.risk_until is not None and self.clock.now() < self.risk_until:
            return "B站风控还没过去，写操作先停一下"
        if self.account is not None and self.account.blocked:
            return "账号当前不可用，只能看不能写"
        return None

    async def note_error(self, error: BiliError) -> None:
        if error.code == -101:
            self.online = False
            self.store.set_account(
                self.account.mid if self.account else 0,
                self.account.uname if self.account else "",
                False,
                False,
            )
            await self.notify_once("offline", "B站登录过期了，需要主人帮我重新登录")
        if error.code in RISK_CODES:
            self.risk_until = self.clock.now() + timedelta(minutes=30)
            await self.notify_once("risk", public_message(error))

    def grant(self) -> dict[str, Any] | None:
        raw = self.store.grant()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            expires = datetime.fromisoformat(str(data["expires_at"]))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None
        if expires <= self.clock.now():
            return None
        return data

    def comment_mode(self) -> str:
        mode = self.settings.comments.mode
        if mode == "off":
            return "off"
        if self.grant() is None:
            return "approve"
        if mode in ("approve", "quota"):
            return "quota"
        return "approve"
