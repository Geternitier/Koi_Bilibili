"""测试用的假后端和假核心。不连 B 站。"""

from typing import Any

from koi_bilibili.errors import BiliError
from koi_bilibili.models import (
    Account,
    Comment,
    Cue,
    Folder,
    MsgItem,
    MyFlags,
    QrStatus,
    QrTicket,
    UserCard,
    VideoCard,
    VideoDetail,
    VideoPage,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
BVID = "BV1xx411c7mD"


class Ctx:
    def __init__(self) -> None:
        self.plugin_id = "bilibili"
        self.events: list[Any] = []
        self.interactions: list[Any] = []
        self.updates: list[tuple[str, str]] = []
        self.closed: list[tuple[str, bool, str]] = []
        self.secrets: dict[str, str] = {}
        self.approvals: list[Any] = []
        self.media: list[tuple[str, list[Any], float]] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def request_interaction(self, request: Any) -> str:
        self.interactions.append(request)
        return f"i{len(self.interactions)}"

    async def update_interaction(self, interaction_id: str, message: str) -> None:
        self.updates.append((interaction_id, message))

    async def close_interaction(self, interaction_id: str, ok: bool, message: str = "") -> None:
        self.closed.append((interaction_id, ok, message))

    async def request_approval(self, request: Any) -> str:
        self.approvals.append(request)
        return f"a{len(self.approvals)}"

    async def get_secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    async def set_secret(self, name: str, value: str) -> None:
        self.secrets[name] = value

    async def delete_secret(self, name: str) -> None:
        self.secrets.pop(name, None)

    async def offer_media(self, text: str, frames: list[Any], *, energy_cost: float = 0.0) -> None:
        self.media.append((text, list(frames), energy_cost))


class FakeBili:
    def __init__(self) -> None:
        self.account = Account(online=True, mid=2, uname="小鱼", coins=5)
        self.qr: list[QrStatus] = []
        self.cookies: dict[str, str] = {}
        self.heartbeats: list[tuple[int, int]] = []
        self.clicks = 0
        self.progress: list[int] = []
        self.detail = VideoDetail(
            bvid=BVID,
            aid=100,
            title="给猫做一顿饭",
            desc="猫饭",
            up_mid=8,
            up_name="某某",
            tname="动物圈",
            duration_s=600,
            pubdate="2026-10-20",
            pages=[VideoPage(cid=7, duration_s=600)],
        )
        self.cards = [
            VideoCard(
                bvid=BVID,
                title="给猫做一顿饭",
                up="某某",
                duration_s=492,
                view=123000,
                like=8900,
                pubdate="2026-10-20",
                tags=["猫"],
                reason="你关注的人",
            )
        ]
        self.comments = [Comment(uname="路人", message="好可爱", like=3, ctime="2026-10-21")]
        self.users = [UserCard(mid=9, uname="主人昵称", sign="签名", level=4, fans=10)]
        self.liked = False
        self.coin_n = 0
        self.favored = False
        self.folders = [Folder(id=3, title="默认收藏夹")]
        self.cues = [Cue(10, 20, "猫探头了")]
        self.segment = b"segment-bytes"
        self.posted: list[tuple[int, str]] = []
        self.unread_counts = (0, 0)
        self.messages: list[MsgItem] = []
        self.fail: BiliError | None = None

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def apply_cookies(self, cookies: dict[str, str]) -> None:
        self.cookies = dict(cookies)

    async def export_cookies(self) -> dict[str, str]:
        return dict(self.cookies)

    async def spi(self) -> dict[str, str]:
        return {"b_3": "buvid-3", "b_4": "buvid-4"}

    async def login_qr_start(self) -> QrTicket:
        self._check()
        return QrTicket(url="https://passport.bilibili.com/qr/abc", key="key")

    async def login_qr_poll(self, key: str) -> QrStatus:
        self._check()
        if not self.qr:
            return QrStatus(86101)
        return self.qr.pop(0)

    async def nav(self) -> Account:
        self._check()
        if "SESSDATA" not in self.cookies:
            return Account(online=False)
        return self.account

    async def card(self, mid: int) -> UserCard:
        self._check()
        for user in self.users:
            if user.mid == mid:
                return user
        return UserCard(mid=mid, uname=f"用户{mid}")

    async def search_users(self, keyword: str) -> list[UserCard]:
        self._check()
        return [user for user in self.users if keyword.strip() in user.uname]

    async def feed(self, mode: str, *, query: str, limit: int, fresh_idx: int) -> list[VideoCard]:
        del mode, query, fresh_idx
        self._check()
        return self.cards[:limit]

    async def video(self, bvid: str) -> VideoDetail:
        self._check()
        if bvid != self.detail.bvid:
            raise BiliError(0, "没有这个视频")
        return self.detail

    async def my_flags(self, bvid: str, aid: int) -> MyFlags:
        del bvid, aid
        return MyFlags(liked=self.liked, coin=self.coin_n, favored=self.favored)

    async def comments(self, aid: int, limit: int) -> list[Comment]:
        del aid
        return self.comments[:limit]

    async def like(self, bvid: str, on: bool) -> None:
        del bvid
        self._check()
        self.liked = on

    async def coin(self, bvid: str, multiply: int) -> None:
        del bvid
        self._check()
        self.coin_n += multiply
        self.account = Account(
            online=self.account.online,
            mid=self.account.mid,
            uname=self.account.uname,
            coins=self.account.coins - multiply,
            blocked=self.account.blocked,
        )

    async def folders(self, mid: int) -> list[Folder]:
        del mid
        return list(self.folders)

    async def favorite(self, aid: int, folder_id: int, *, on: bool) -> None:
        del aid, folder_id
        self.favored = on

    async def comment(self, aid: int, text: str, *, root: int = 0, parent: int = 0) -> int:
        del root, parent
        self._check()
        self.posted.append((aid, text))
        return 42

    async def delete_comment(self, aid: int, rpid: int) -> None:
        del aid, rpid

    async def play_url(self, bvid: str, cid: int, qn: int) -> str:
        del bvid, cid, qn
        return "https://example.invalid/video"

    async def fetch_bytes(self, url: str) -> bytes:
        del url
        return self.segment

    async def subtitles(self, aid: int, cid: int) -> list[Cue]:
        del aid, cid
        return list(self.cues)

    async def click(self, aid: int, cid: int, bvid: str, *, part: int = 1) -> None:
        del aid, cid, bvid, part
        self.clicks += 1

    async def heartbeat(self, *, aid: int, cid: int, bvid: str, played: int, realtime: int) -> None:
        del aid, cid, bvid
        self.heartbeats.append((played, realtime))

    async def report_progress(self, aid: int, cid: int, progress: int) -> None:
        del aid, cid
        self.progress.append(progress)

    async def unread(self) -> tuple[int, int]:
        return self.unread_counts

    async def msgfeed(self, kind: str, cursor: str | None) -> list[MsgItem]:
        del cursor
        return [item for item in self.messages if item.kind == kind]

    async def aclose(self) -> None:
        return None


async def grab(video: bytes, at_s: float, width: int, tmp: Any) -> bytes:
    del video, width, tmp
    grab.calls.append(at_s)  # type: ignore[attr-defined]
    return JPEG


grab.calls = []  # type: ignore[attr-defined]
