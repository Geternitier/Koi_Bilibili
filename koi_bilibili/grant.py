"""主人在表单里授予一段时间内的互动额度。她不能自己把表单填了。"""

import json
from datetime import timedelta

from koi.protocol import InteractionField, InteractionRequest

from koi_bilibili.runtime import Runtime

_TITLE = "B站自主互动授权"
_YES = "同意授权B站互动"
_NO = "拒绝授权"


class Grant:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._pending = ""
        self._days = 30

    async def request(self, days: int) -> dict[str, object]:
        if not self.rt.can_interact:
            return {"ok": False, "error": "没有 interact 权限，不能请主人授权"}
        if self._pending:
            return {"ok": True, "message": "授权确认还在等主人"}
        self._days = min(30, max(1, days))
        limits = self.rt.settings.limits
        self._pending = await self.rt.ctx.request_interaction(
            InteractionRequest(
                kind="form",
                title=_TITLE,
                message=(
                    "允许她在下面的额度内自己刷B站并互动吗？\n"
                    f"点赞：每天最多 {limits.like_per_day} 个\n"
                    f"收藏：每天最多 {limits.favorite_per_day} 个\n"
                    f"投币：每天最多 {limits.coin_per_day} 枚，"
                    f"单个视频最多 {limits.coin_per_video} 枚（投出不能退回）\n"
                    f"评论：每天最多 {limits.comment_per_day} 条，"
                    f"间隔至少 {limits.comment_min_interval_s // 60} 分钟（公开内容）\n"
                    f"有效期：{self._days} 天\n"
                    f"输入「{_YES}」授权；输入「{_NO}」保持逐条审批。"
                ),
                fields=[
                    InteractionField(
                        name="consent",
                        label="是否授权",
                        pattern=rf"{_YES}|{_NO}",
                        placeholder=_YES,
                    )
                ],
                expires_in_s=300,
            )
        )
        return {"ok": True, "message": "授权确认已经发给主人了"}

    async def submit(self, values: dict[str, str] | None) -> None:
        interaction = self._pending
        self._pending = ""
        if not interaction:
            return
        answer = (values or {}).get("consent", "").strip()
        if answer != _YES:
            await self.rt.ctx.close_interaction(
                interaction, False, "没有授权，评论和投币仍逐条问主人"
            )
            return
        now = self.rt.clock.now()
        limits = self.rt.settings.limits
        payload = {
            "granted_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self._days)).isoformat(),
            "caps": {
                "like": limits.like_per_day,
                "favorite": limits.favorite_per_day,
                "coin": limits.coin_per_day,
                "comment": limits.comment_per_day,
            },
        }
        self.rt.store.set_grant(json.dumps(payload, ensure_ascii=False))
        await self.rt.ctx.close_interaction(interaction, True, f"已授权 {self._days} 天的B站互动")
