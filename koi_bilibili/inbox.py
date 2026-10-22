"""回复我的和 @我的。只认主人的 mid，昵称相同不算。"""

import asyncio
import contextlib
import logging

from koi_bilibili.errors import BiliError
from koi_bilibili.models import MsgItem
from koi_bilibili.runtime import Runtime
from koi_bilibili.text import clean

log = logging.getLogger("koi_bilibili")


class Inbox:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._task: asyncio.Task[None] | None = None
        self._sent_in_minute = 0
        self._minute = ""

    def start(self) -> None:
        if self.rt.settings.inbox.enabled:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def poll_once(self) -> None:
        if not self.rt.online:
            return
        try:
            reply, at = await self.rt.backend.unread()
        except BiliError as error:
            await self.rt.note_error(error)
            return
        if reply == 0 and at == 0:
            return
        if reply:
            await self._pull("reply")
        if at:
            await self._pull("at")

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("msgfeed 轮询失败")
            await self.rt.clock.sleep(self.rt.settings.inbox.poll_s)

    async def _pull(self, kind: str) -> None:
        try:
            items = await self.rt.backend.msgfeed(kind, self.rt.store.cursor(kind))
        except BiliError as error:
            await self.rt.note_error(error)
            return
        account = self.rt.account.mid if self.rt.account else 0
        emitted = 0
        for item in items[: self.rt.settings.inbox.max_items_per_poll]:
            if not item.id or self.rt.store.seen(item.id):
                continue
            if item.business_id not in (1, 11):
                self.rt.store.mark_seen(item.id, self.rt.clock.now().isoformat())
                continue
            if account and item.sender_mid == account:
                self.rt.store.mark_seen(item.id, self.rt.clock.now().isoformat())
                continue
            if emitted >= self._room():
                break
            await self._emit(item)
            self._sent_in_minute += 1
            self.rt.store.mark_seen(item.id, self.rt.clock.now().isoformat())
            self.rt.store.set_cursor(kind, item.id)
            emitted += 1

    async def _emit(self, item: MsgItem) -> None:
        owner = self.rt.store.owner_mid()
        is_owner = owner is not None and item.sender_mid == owner
        kind = "mention" if item.kind == "at" else "comment_reply"
        if is_owner:
            priority, trust = 0, "owner"
            lead = "主人回复了你的评论" if kind == "comment_reply" else "主人在评论里@了你"
        elif kind == "mention":
            priority, trust = 1, "external"
            lead = "有人在评论里@了你"
        else:
            priority, trust = 2, "external"
            lead = "有人回复了你的评论"
        title = item.title or item.bvid
        text = clean(f"{lead}（视频《{title}》）：{item.text}", limit=2000)
        conversation = f"comment:{item.bvid}:{item.root_id or item.source_id}"
        now = self.rt.clock.now()
        self.rt.store.remember_inbound(item.source_id, conversation, now)
        await self.rt.notify(
            text,
            priority=priority,
            trust=trust,
            type=kind,
            extra={
                "sender": item.sender_name,
                "sender_id": str(item.sender_mid),
                "conversation": conversation,
                "message_id": item.source_id,
                "conversation_type": "comment",
            },
        )

    def _room(self) -> int:
        minute = self.rt.clock.now().strftime("%Y-%m-%dT%H:%M")
        if minute != self._minute:
            self._minute = minute
            self._sent_in_minute = 0
        return max(0, self.rt.settings.inbox.max_events_per_min - self._sent_in_minute)
