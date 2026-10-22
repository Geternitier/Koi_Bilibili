"""扫码登录。二维码发给主人，轮询在后台走，不占住她这一轮思考。"""

import asyncio
import json
import logging
from datetime import datetime

from koi.protocol import InteractionRequest

from koi_bilibili.backend import COOKIE_NAMES
from koi_bilibili.errors import BiliError
from koi_bilibili.runtime import Runtime

log = logging.getLogger("koi_bilibili")

_TITLE = "B站扫码登录"


class AuthManager:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._task: asyncio.Task[None] | None = None
        self._interaction = ""
        self._buvid: dict[str, str] = {}

    async def login(self, method: str, force: bool) -> dict[str, object]:
        if method != "qr":
            return {"ok": False, "error": "目前只支持扫码登录"}
        if not self.rt.can_interact:
            return {"ok": False, "error": "没有 interact 权限，不能请主人扫码"}
        if self.rt.online and self.rt.account and not force:
            return {"ok": True, "message": f"已经登录为 {self.rt.account.uname}，不用再扫"}
        if self._task is not None and not self._task.done():
            return {"ok": True, "message": "登录正在进行中，等主人扫码/确认，结果会通知你"}
        try:
            ticket = await self.rt.backend.login_qr_start()
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "二维码没有生成，等会儿再试"}
        if not ticket.url or not ticket.key:
            return {"ok": False, "error": "二维码没有生成，等会儿再试"}
        self._interaction = await self.rt.ctx.request_interaction(
            InteractionRequest(
                kind="display",
                title=_TITLE,
                message=(
                    "请主人用 B站 App 扫码，登录的是她使用的账号（默认是她自己的专用号）。"
                    "二维码 3 分钟内有效；扫完请在手机上确认。"
                ),
                qr=ticket.url,
                expires_in_s=180,
            )
        )
        started = self.rt.clock.now()
        self._task = asyncio.create_task(self._poll(ticket.key, self._interaction, started))
        return {
            "ok": True,
            "message": "二维码已经发给主人了，请主人用B站 App 扫码，扫完在手机上确认一下",
        }

    async def cancel(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        self._interaction = ""

    async def bootstrap(self) -> None:
        raw = await self.rt.ctx.get_secret("cookie")
        if raw:
            try:
                cookies = json.loads(raw)
            except json.JSONDecodeError:
                cookies = None
            if isinstance(cookies, dict):
                kept = {key: str(cookies[key]) for key in COOKIE_NAMES if cookies.get(key)}
                self.rt.cookies = kept
                await self.rt.backend.apply_cookies(kept)
                await self._confirm(kept, notify=True)
                return
        try:
            spi = await self.rt.backend.spi()
        except BiliError:
            return
        buvid = {}
        if spi.get("b_3"):
            buvid["buvid3"] = spi["b_3"]
        if spi.get("b_4"):
            buvid["buvid4"] = spi["b_4"]
        self._buvid = buvid

    async def _poll(self, key: str, interaction_id: str, started: datetime) -> None:
        failures = 0
        scanned = False
        try:
            while _elapsed(self.rt, started) < 180:
                try:
                    status = await self.rt.backend.login_qr_poll(key)
                except BiliError:
                    failures += 1
                    if failures >= 3:
                        await self.rt.ctx.close_interaction(
                            interaction_id, False, "登录请求失败了，等会儿再试"
                        )
                        return
                    await self.rt.clock.sleep(float(2**failures))
                    continue
                if status.code == 86101:
                    await self.rt.clock.sleep(2)
                    continue
                if status.code == 86090:
                    if not scanned:
                        scanned = True
                        await self.rt.ctx.update_interaction(
                            interaction_id, "已扫码，请在手机上确认"
                        )
                    await self.rt.clock.sleep(2)
                    continue
                if status.code == 86038:
                    await self.rt.ctx.close_interaction(
                        interaction_id, False, "二维码过期了，需要重新生成"
                    )
                    return
                if status.code == 0:
                    await self._accept(interaction_id, status.cookies)
                    return
                failures += 1
                if failures >= 3:
                    await self.rt.ctx.close_interaction(
                        interaction_id, False, "登录请求失败了，等会儿再试"
                    )
                    return
                await self.rt.clock.sleep(2)
            await self.rt.ctx.close_interaction(interaction_id, False, "二维码过期了，需要重新生成")
        except asyncio.CancelledError:
            raise
        finally:
            self._interaction = ""

    async def _accept(self, interaction_id: str, cookies: dict[str, str]) -> None:
        merged = {**self._buvid, **cookies}
        old = dict(self.rt.cookies)
        await self.rt.backend.apply_cookies(merged)
        try:
            account = await self.rt.backend.nav()
        except BiliError as error:
            await self.rt.backend.apply_cookies(old)
            self.rt.cookies = old
            await self.rt.ctx.close_interaction(interaction_id, False, "登录没有完成，没有保存")
            await self.rt.note_error(error)
            return
        if not account.online or not account.mid or not account.uname:
            await self.rt.backend.apply_cookies(old)
            self.rt.cookies = old
            await self.rt.ctx.close_interaction(interaction_id, False, "登录没有完成，没有保存")
            return
        expected = self.rt.settings.account.expected_mid
        if expected is not None and account.mid != expected:
            await self.rt.backend.apply_cookies(old)
            self.rt.cookies = old
            await self.rt.ctx.close_interaction(
                interaction_id,
                False,
                f"登录的账号是 {account.uname}（UID {account.mid}），"
                f"不是预期的 UID {expected}；没有保存。",
            )
            return
        fresh = await self.rt.backend.export_cookies()
        fresh.update({key: value for key, value in merged.items() if key not in fresh})
        self.rt.cookies = {key: fresh[key] for key in COOKIE_NAMES if fresh.get(key)}
        await self.rt.ctx.set_secret("cookie", json.dumps(self.rt.cookies, ensure_ascii=False))
        self.rt.account = account
        self.rt.online = True
        self.rt.store.set_account(account.mid, account.uname, True, account.blocked)
        note = ""
        owner_mid = self.rt.store.owner_mid()
        if self.rt.settings.account.role == "owner":
            self.rt.store.set_owner(
                account.mid, account.uname, "account", self.rt.clock.now().isoformat()
            )
        elif owner_mid is not None and account.mid == owner_mid:
            note = "这是主人自己的账号；建议给她一个专用小号，否则评论会用主人的身份发出去。"
        message = f"登录成功：{account.uname}"
        if note:
            message = f"{message}。{note}"
        await self.rt.ctx.close_interaction(interaction_id, True, message)
        log.info("登录成功：%s", account.uname)

    async def _confirm(self, cookies: dict[str, str], *, notify: bool) -> None:
        try:
            account = await self.rt.backend.nav()
        except BiliError as error:
            if notify:
                await self.rt.note_error(error)
            return
        if not account.online:
            self.rt.online = False
            if notify:
                await self.rt.notify_once("offline", "B站登录过期了，需要主人帮我重新登录")
            return
        self.rt.account = account
        self.rt.online = True
        self.rt.cookies = cookies
        self.rt.store.set_account(account.mid, account.uname, True, account.blocked)
        if self.rt.settings.account.role == "owner":
            self.rt.store.set_owner(
                account.mid, account.uname, "account", self.rt.clock.now().isoformat()
            )


def _elapsed(rt: Runtime, started: datetime) -> float:
    return (rt.clock.now() - started).total_seconds()
