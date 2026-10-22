"""协议门面：能力分发、生命周期。B 站的细节在各个服务里。"""

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from koi.protocol import Approval, Capability, Interaction, Manifest

from koi_bilibili.actions import Actions
from koi_bilibili.auth import AuthManager
from koi_bilibili.backend import WebBiliBackend
from koi_bilibili.browse import Browse
from koi_bilibili.clock import Clock, SystemClock
from koi_bilibili.config import load_settings
from koi_bilibili.errors import BiliError, PluginError, public_message
from koi_bilibili.grant import Grant
from koi_bilibili.inbox import Inbox
from koi_bilibili.owner import OwnerBinding
from koi_bilibili.runtime import Runtime
from koi_bilibili.store import Store
from koi_bilibili.watch import Grabber, Watcher

log = logging.getLogger("koi_bilibili")

_BVID = re.compile(r"^BV[0-9A-Za-z]+$")
_LOGIN = "B站扫码登录"
_OWNER = "确认主人的B站账号"
_GRANT = "B站自主互动授权"


def _capabilities() -> list[Capability]:
    return [
        Capability(
            name="login",
            description="登录B站。需要主人用B站 App 扫码；二维码会出现在网页的「等你处理」里。",
            parameters={"method": "qr", "force": "是否强制重新登录，默认 false"},
            energy_cost=1,
            timeout_s=20,
        ),
        Capability(
            name="bind_owner",
            description=(
                "绑定主人的B站账号。先问主人要用户名或空间链接，插件会解析成 UID 并请主人确认。"
            ),
            parameters={
                "query": "主人的B站用户名、空间链接或 UID",
                "force": "是否重新绑定，默认 false",
            },
            energy_cost=1,
            timeout_s=20,
        ),
        Capability(
            name="grant_autonomy",
            description="请主人授予B站自主互动额度（点赞/收藏/投币/评论），有期限、有上限、可撤销。",
            parameters={"days": "授权天数，1–30，默认 30"},
            energy_cost=1,
            timeout_s=20,
        ),
        Capability(
            name="browse_feed",
            description="刷B站：看推荐、热门，或按关键词搜索视频。返回标题、UP、时长、播放和互动数。",
            parameters={
                "mode": "recommend（默认）/ popular / search",
                "query": "search 时的关键词；recommend/popular 时留空",
                "limit": "返回多少条，1–20，默认 12",
            },
            energy_cost=3,
            timeout_s=30,
        ),
        Capability(
            name="view_video",
            description="看一个视频的详细信息：简介、UP、分区、时长、播放和互动数据，以及你有没有点赞、投币、收藏。",
            parameters={"bvid": "视频 BV 号"},
            energy_cost=1,
            timeout_s=20,
        ),
        Capability(
            name="read_comments",
            description="看一个视频的热门评论。只读取，不回复。",
            parameters={"bvid": "视频 BV 号", "limit": "条数，1–20，默认 10"},
            energy_cost=1,
            timeout_s=20,
        ),
        Capability(
            name="watch_video",
            description=(
                "按真实时间看一段视频。马上返回，这时还没有画面。"
                "看完后才会把抽到的画面和这段字幕给你。"
            ),
            parameters={
                "bvid": "视频 BV 号",
                "seconds": "想看的秒数。不填则用默认时长，不超过单次上限",
                "page": "分 P，默认 1",
            },
            energy_cost=2,
            timeout_s=30,
        ),
        Capability(
            name="like_video",
            description="给视频点赞。看到喜欢的就可以点；已经点过会告诉你。",
            parameters={"bvid": "视频 BV 号"},
            energy_cost=0.5,
            timeout_s=20,
        ),
        Capability(
            name="coin_video",
            description="给视频投币（1 或 2 枚）。硬币不可退，只有真正喜欢才投；每天有预算。",
            parameters={"bvid": "视频 BV 号", "multiply": "1 或 2，默认 1"},
            energy_cost=0.5,
            timeout_s=20,
        ),
        Capability(
            name="favorite_video",
            description="把视频收藏到默认收藏夹。想以后再看就收藏。",
            parameters={"bvid": "视频 BV 号"},
            energy_cost=0.5,
            timeout_s=20,
        ),
        Capability(
            name="comment_video",
            description="在视频下发表一条短评论。评论是公开的。可能需要主人批准。",
            parameters={"bvid": "视频 BV 号", "text": "评论内容，最多 120 字"},
            energy_cost=2,
            timeout_s=20,
        ),
        Capability(
            name="send_message",
            description="回复B站评论。私信还没有接入。",
            parameters={"text": "内容", "conversation": "会话", "message_id": "评论 id"},
            tool=False,
            timeout_s=20,
        ),
    ]


class BilibiliPlugin:
    def __init__(
        self,
        *,
        backend: Any = None,
        clock: Clock | None = None,
        grabber: Grabber | None = None,
        permissions: list[str] | None = None,
        secrets: list[str] | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self._backend = backend
        self._clock = clock or SystemClock()
        self._grabber = grabber
        self._permissions = ["interact"] if permissions is None else list(permissions)
        self._secrets = ["cookie"] if secrets is None else list(secrets)
        self._work_dir = work_dir
        self._raw_settings: dict[str, Any] = {}
        self._broken = ""
        self._bootstrap: asyncio.Task[None] | None = None
        self.manifest = Manifest(
            id="bilibili",
            version="0.1.0",
            description="B站：扫码登录、主人绑定、浏览与按真实时间观看",
            max_trust="owner",
            permissions=["interact"],
            secrets=["cookie"],
            capabilities=_capabilities(),
        )
        self.rt: Runtime | None = None
        self.auth: AuthManager | None = None
        self.owner: OwnerBinding | None = None
        self.browse: Browse | None = None
        self.watch: Watcher | None = None
        self.actions: Actions | None = None
        self.inbox: Inbox | None = None
        self.grant: Grant | None = None

    def configure(self, settings: dict[str, Any]) -> None:
        self._raw_settings = dict(settings or {})

    async def start(self, ctx: Any) -> None:
        settings = load_settings(self._raw_settings)
        work = self._work_dir or Path.cwd()
        backend = self._backend or WebBiliBackend(
            timeout_s=settings.network.timeout_s,
            trust_env=settings.network.trust_env,
        )
        self.rt = Runtime(
            ctx=ctx,
            settings=settings,
            store=Store(work / "state.sqlite3"),
            backend=backend,
            clock=self._clock,
            work_dir=work,
            can_interact="interact" in self._permissions,
        )
        self.auth = AuthManager(self.rt)
        self.owner = OwnerBinding(self.rt)
        self.browse = Browse(self.rt)
        self.watch = Watcher(self.rt, self._grabber)
        self.actions = Actions(self.rt)
        self.inbox = Inbox(self.rt)
        self.grant = Grant(self.rt)
        if "cookie" not in self._secrets:
            self._broken = "配置里没有 cookie 凭据，B站插件起不来"
            await self.rt.notify(self._broken)
            return
        if not self.rt.can_interact:
            await self.rt.notify("没有 interact 权限，登录、绑定和授权用不了")
        self._bootstrap = asyncio.create_task(self.auth.bootstrap())
        self.inbox.start()

    async def settle(self) -> None:
        if self._bootstrap is not None:
            await self._bootstrap

    async def stop(self) -> None:
        if self.watch is not None:
            await self.watch.stop()
        if self.inbox is not None:
            await self.inbox.stop()
        if self.auth is not None:
            await self.auth.cancel()
        if self._bootstrap is not None:
            self._bootstrap.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bootstrap
        if self.rt is not None:
            await self.rt.backend.aclose()
            self.rt.store.close()

    async def call(self, capability: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._broken:
            return {"ok": False, "error": self._broken}
        if self.rt is None:
            return {"ok": False, "error": "插件还没启动"}
        try:
            return await self._dispatch(capability, args or {})
        except PluginError as error:
            return {"ok": False, "error": error.message}
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": public_message(error)}
        except Exception:
            log.exception("能力 %s 失败", capability)
            return {"ok": False, "error": "插件出错了"}

    async def on_approval(self, approval: Approval) -> None:
        if self.actions is not None:
            self.actions.on_approval(approval)

    async def on_interaction(self, interaction: Interaction, values: dict[str, str] | None) -> None:
        if interaction.title == _LOGIN and self.auth is not None:
            await self.auth.cancel()
        elif interaction.title == _OWNER and self.owner is not None:
            await self.owner.submit(values)
        elif interaction.title == _GRANT and self.grant is not None:
            await self.grant.submit(values)

    async def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.rt and self.auth and self.owner and self.browse and self.watch
        assert self.actions and self.grant
        rt = self.rt
        if name == "login":
            return await self.auth.login(str(args.get("method") or "qr"), _flag(args.get("force")))
        if name == "bind_owner":
            return await self.owner.bind(str(args.get("query") or ""), _flag(args.get("force")))
        if name == "grant_autonomy":
            return await self.grant.request(_number(args.get("days"), 30, 1, 30))
        if name == "browse_feed":
            mode = str(args.get("mode") or rt.settings.feed.default_mode)
            limit = _number(args.get("limit"), rt.settings.feed.page_size, 1, 20)
            return await self.browse.feed(mode, str(args.get("query") or ""), limit)
        if name == "view_video":
            return await self.browse.view(_bvid(args))
        if name == "read_comments":
            return await self.browse.comments(_bvid(args), _number(args.get("limit"), 10, 1, 20))
        if name == "watch_video":
            raw_seconds = args.get("seconds")
            seconds = None if raw_seconds in (None, "") else _number(raw_seconds, 0, 1, 480)
            page = _number(args.get("page"), 1, 1, 50)
            return await self.watch.start(_bvid(args), seconds, page)
        if name == "like_video":
            return await self.actions.like(_bvid(args))
        if name == "coin_video":
            return await self.actions.coin(_bvid(args), _number(args.get("multiply"), 1, 1, 2))
        if name == "favorite_video":
            return await self.actions.favorite(_bvid(args))
        if name == "comment_video":
            return await self.actions.comment(_bvid(args), str(args.get("text") or ""))
        if name == "send_message":
            return await self.actions.send_reply(
                str(args.get("text") or ""),
                str(args.get("conversation") or ""),
                str(args.get("message_id") or ""),
                system=_flag(args.get("system")),
            )
        return {"ok": False, "error": "没有这个能力"}


def _bvid(args: dict[str, Any]) -> str:
    bvid = str(args.get("bvid") or "")
    if _BVID.fullmatch(bvid) is None:
        raise PluginError("bvid 不对")
    return bvid


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "是")
    return bool(value)


def _number(value: object, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise PluginError("参数不是数字") from error
    return min(hi, max(lo, number))
