"""点赞、投币、收藏、评论。额度、去重和审批都在这里，不在核心里藏工具。"""

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

from koi.protocol import Approval, ApprovalRequest

from koi_bilibili.errors import ALREADY_CODES, BiliError, public_message
from koi_bilibili.models import VideoDetail
from koi_bilibili.runtime import Runtime
from koi_bilibili.text import comment_problem, normalize

Run = Callable[[], Awaitable[dict[str, Any]]]


class Actions:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._lock = asyncio.Lock()
        self._rng = random.Random()

    async def like(self, bvid: str) -> dict[str, Any]:
        flags = await self._flags(bvid)
        if isinstance(flags, dict):
            return flags
        if flags.liked or self._used_today("like", bvid):
            return {"ok": True, "liked": True, "already": True}
        if self._count_today("like") >= self.rt.settings.limits.like_per_day:
            return {"ok": False, "error": "今天的点赞额度用完了"}
        return await self._run(
            "like", bvid, lambda: self.rt.backend.like(bvid, True), {"liked": True}
        )

    async def coin(self, bvid: str, multiply: int) -> dict[str, Any]:
        if multiply not in (1, 2):
            return {"ok": False, "error": "投币只能是 1 枚或 2 枚"}
        if multiply > self.rt.settings.limits.coin_per_video:
            return {"ok": False, "error": "单次投币超过了设置的上限"}
        video = await self._video(bvid)
        if isinstance(video, dict):
            return video
        flags = await self._flags(bvid)
        if isinstance(flags, dict):
            return flags
        if flags.coin:
            return {"ok": True, "already": True, "coins": flags.coin}
        limits = self.rt.settings.limits
        if self._count_today("coin") + multiply > limits.coin_per_day:
            return {"ok": False, "error": "今天的投币额度用完了"}
        if self.rt.store.quota_of(self._day(), "coin", bvid) + multiply > limits.coin_per_video:
            return {"ok": False, "error": "这个视频不能再投了"}
        if self.rt.account and self.rt.account.coins < multiply:
            return {"ok": False, "error": "硬币不够"}
        args = {"bvid": bvid, "multiply": multiply}
        detail = f"给《{video.title}》（{bvid}）投 {multiply} 枚硬币。投出后不能退回。"

        async def go() -> dict[str, Any]:
            await self.rt.backend.coin(bvid, multiply)
            left = limits.coin_per_day - self._count_today("coin") - multiply
            return {"ok": True, "coins": multiply, "remaining_today": max(0, left)}

        if self.rt.grant() is None:
            return await self._approve(
                "coin_video", args, "在B站投币", detail, "coin", bvid, go, multiply
            )
        return await self._run("coin", bvid, go, amount=multiply)

    async def favorite(self, bvid: str) -> dict[str, Any]:
        video = await self._video(bvid)
        if isinstance(video, dict):
            return video
        flags = await self._flags(bvid)
        if isinstance(flags, dict):
            return flags
        if flags.favored or self._used_today("favorite", bvid):
            return {"ok": True, "already": True, "favored": True}
        if self._count_today("favorite") >= self.rt.settings.limits.favorite_per_day:
            return {"ok": False, "error": "今天的收藏额度用完了"}
        mid = self.rt.account.mid if self.rt.account else 0
        if not mid:
            return {"ok": False, "error": "还没登录，收藏不了"}

        async def go() -> dict[str, Any]:
            folders = await self.rt.backend.folders(mid)
            folder = next((item for item in folders if "默认收藏夹" in item.title), None)
            folder = folder or (folders[0] if folders else None)
            if folder is None:
                return {"ok": False, "error": "没有收藏夹，没有自动创建"}
            await self.rt.backend.favorite(video.aid, folder.id, on=True)
            return {"ok": True, "favored": True}

        return await self._run("favorite", bvid, go)

    async def comment(self, bvid: str, text: str) -> dict[str, Any]:
        video = await self._video(bvid)
        if isinstance(video, dict):
            return video
        problem = comment_problem(
            text,
            self.rt.settings,
            title=video.title,
            business=video.business,
            own_video=bool(self.rt.account and video.up_mid == self.rt.account.mid),
        )
        if problem:
            return {"ok": False, "error": problem}
        digest = hashlib.sha256(normalize(text).encode()).hexdigest()
        if digest in self.rt.store.recent_comment_hashes():
            return {"ok": False, "error": "这句话和最近发过的太像了"}
        if self._used_today("comment", bvid):
            return {"ok": False, "error": "这个视频今天已经评论过了"}
        if self._count_today("comment") >= self.rt.settings.limits.comment_per_day:
            return {"ok": False, "error": "今天的评论额度用完了"}
        last = self.rt.store.last_action_at("comment")
        gap = self.rt.settings.limits.comment_min_interval_s
        if last is not None and (self.rt.clock.now() - last).total_seconds() < gap:
            return {"ok": False, "error": "离上一条评论太近了"}
        body = text.strip()
        mode = self.rt.comment_mode()
        if mode == "off":
            return {"ok": False, "error": "主人没有允许自动评论"}

        async def go() -> dict[str, Any]:
            rpid = await self.rt.backend.comment(video.aid, body)
            self.rt.store.record_action(
                bvid=bvid,
                action="comment",
                at=self.rt.clock.now().isoformat(),
                ok=True,
                rpid=str(rpid),
                args_hash=digest,
            )
            self.rt.store.add_quota(self._day(), "comment", bvid, 1)
            return {
                "ok": True,
                "rpid": str(rpid),
                "url": f"https://www.bilibili.com/video/{bvid}#reply{rpid}",
            }

        if mode == "approve":
            detail = f"视频《{video.title}》（{bvid}）：{body}"
            return await self._approve(
                "comment_video",
                {"bvid": bvid, "text": body},
                "在B站发评论",
                detail,
                "comment",
                bvid,
                go,
                1,
            )
        return await self._run("comment", bvid, go)

    async def send_reply(
        self, text: str, conversation: str, message_id: str, *, system: bool
    ) -> dict[str, Any]:
        if system:
            return {"ok": False, "error": "B站没有系统消息"}
        if not conversation:
            return {"ok": False, "error": "没有可回复的B站会话；请在 Web 里跟主人说"}
        if conversation.startswith("dm:"):
            return {"ok": False, "error": "B站私信还没有接入"}
        parts = conversation.split(":")
        if len(parts) < 3 or parts[0] != "comment":
            return {"ok": False, "error": "这个会话不能在B站回复"}
        when = self.rt.store.inbound_at(message_id)
        if when is None or (self.rt.clock.now() - when).total_seconds() > 30 * 60:
            return {"ok": False, "error": "这条消息太久了，不再回复"}
        problem = comment_problem(text, self.rt.settings, max_chars=500, max_lines=8)
        if problem:
            return {"ok": False, "error": problem}
        bvid, root = parts[1], parts[2]
        video = await self._video(bvid)
        if isinstance(video, dict):
            return video
        try:
            parent = int(message_id or root or 0)
            root_id = int(root or 0)
        except ValueError:
            return {"ok": False, "error": "这条评论对不上"}

        async def go() -> dict[str, Any]:
            rpid = await self.rt.backend.comment(
                video.aid, text.strip(), root=root_id, parent=parent
            )
            return {"ok": True, "rpid": str(rpid)}

        return await self._execute(go)

    def on_approval(self, approval: Approval) -> None:
        if approval.status == "approved":
            self.rt.store.permit(approval.id)

    async def _approve(
        self,
        capability: str,
        args: dict[str, Any],
        action: str,
        detail: str,
        quota_action: str,
        bvid: str,
        go: Run,
        amount: int,
    ) -> dict[str, Any]:
        digest = _digest(capability, args)
        if self.rt.store.take_permit(digest):
            return await self._run(quota_action, bvid, go, amount=amount)
        paused = self._gate()
        if paused:
            return paused
        approval_id = await self.rt.ctx.request_approval(
            ApprovalRequest(action=action, detail=detail)
        )
        self.rt.store.save_approval(approval_id, digest)
        return {
            "ok": False,
            "needs_approval": True,
            "approval_id": approval_id,
            "error": f"需要主人批准：{detail}",
        }

    async def _run(
        self,
        action: str,
        bvid: str,
        go: Run,
        extra: dict[str, Any] | None = None,
        *,
        amount: int = 1,
        record: bool = True,
    ) -> dict[str, Any]:
        async def wrapped() -> dict[str, Any]:
            try:
                result = await go()
            except BiliError as error:
                if error.code in ALREADY_CODES:
                    return {"ok": True, "already": True, "message": public_message(error)}
                await self.rt.note_error(error)
                self.rt.store.record_action(
                    bvid=bvid,
                    action=action,
                    at=self.rt.clock.now().isoformat(),
                    ok=False,
                    error_code=error.code,
                )
                return {"ok": False, "error": public_message(error)}
            if not isinstance(result, dict):
                result = {"ok": True}
            if result.get("ok") and record and action != "comment":
                self.rt.store.record_action(
                    bvid=bvid,
                    action=action,
                    at=self.rt.clock.now().isoformat(),
                    ok=True,
                )
                self.rt.store.add_quota(self._day(), action, bvid, amount)
            if extra and result.get("ok"):
                result.update(extra)
            return result

        return await self._execute(wrapped)

    async def _execute(self, go: Run) -> dict[str, Any]:
        paused = self._gate()
        if paused:
            return paused
        async with self._lock:
            await self._pause()
            return await go()

    def _gate(self) -> dict[str, Any] | None:
        if not self.rt.online:
            return {"ok": False, "error": "还没登录"}
        reason = self.rt.pause_reason()
        if reason:
            return {"ok": False, "error": reason}
        return None

    async def _pause(self) -> None:
        limits = self.rt.settings.limits
        if limits.write_max_interval_s <= 0:
            return
        await self.rt.clock.sleep(
            self._rng.uniform(limits.write_min_interval_s, limits.write_max_interval_s)
        )

    async def _video(self, bvid: str) -> VideoDetail | dict[str, Any]:
        try:
            return await self.rt.backend.video(bvid)
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "这个视频没打开"}

    async def _flags(self, bvid: str) -> Any:
        try:
            video = await self.rt.backend.video(bvid)
            return await self.rt.backend.my_flags(bvid, video.aid)
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "没查到现在的互动状态"}

    def _day(self) -> str:
        return self.rt.clock.now().date().isoformat()

    def _count_today(self, action: str) -> int:
        return self.rt.store.quota_total(self._day(), action)

    def _used_today(self, action: str, bvid: str) -> bool:
        return self.rt.store.quota_of(self._day(), action, bvid) > 0


def _digest(capability: str, args: dict[str, Any]) -> str:
    body = json.dumps({"c": capability, "a": args}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()
