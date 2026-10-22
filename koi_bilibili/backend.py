"""B 站 Web 接口。路径和字段都收在这里，上面的工具不拼 URL。"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

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
from koi_bilibili.text import clean, clean_comment_body
from koi_bilibili.wbi import mixin_key, sign

log = logging.getLogger("koi_bilibili")

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
COOKIE_NAMES = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "b_nut",
)
_SEGMENT_CAP = 24 * 1024 * 1024


class BiliBackend(Protocol):
    async def apply_cookies(self, cookies: dict[str, str]) -> None: ...

    async def export_cookies(self) -> dict[str, str]: ...

    async def spi(self) -> dict[str, str]: ...

    async def login_qr_start(self) -> QrTicket: ...

    async def login_qr_poll(self, key: str) -> QrStatus: ...

    async def nav(self) -> Account: ...

    async def card(self, mid: int) -> UserCard: ...

    async def search_users(self, keyword: str) -> list[UserCard]: ...

    async def feed(
        self, mode: str, *, query: str, limit: int, fresh_idx: int
    ) -> list[VideoCard]: ...

    async def video(self, bvid: str) -> VideoDetail: ...

    async def my_flags(self, bvid: str, aid: int) -> MyFlags: ...

    async def comments(self, aid: int, limit: int) -> list[Comment]: ...

    async def like(self, bvid: str, on: bool) -> None: ...

    async def coin(self, bvid: str, multiply: int) -> None: ...

    async def folders(self, mid: int) -> list[Folder]: ...

    async def favorite(self, aid: int, folder_id: int, *, on: bool) -> None: ...

    async def comment(self, aid: int, text: str, *, root: int = 0, parent: int = 0) -> int: ...

    async def delete_comment(self, aid: int, rpid: int) -> None: ...

    async def play_url(self, bvid: str, cid: int, qn: int) -> str: ...

    async def fetch_bytes(self, url: str) -> bytes: ...

    async def subtitles(self, aid: int, cid: int) -> list[Cue]: ...

    async def click(self, aid: int, cid: int, bvid: str, *, part: int = 1) -> None: ...

    async def heartbeat(
        self, *, aid: int, cid: int, bvid: str, played: int, realtime: int
    ) -> None: ...

    async def report_progress(self, aid: int, cid: int, progress: int) -> None: ...

    async def unread(self) -> tuple[int, int]: ...

    async def msgfeed(self, kind: str, cursor: str | None) -> list[MsgItem]: ...

    async def aclose(self) -> None: ...


class WebBiliBackend:
    """httpx 直连 Web 接口。不依赖 bilibili-api-python，插件可以保持 MIT。"""

    def __init__(self, *, timeout_s: float = 20, trust_env: bool = True) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            },
            timeout=timeout_s,
            trust_env=trust_env,
            follow_redirects=True,
        )
        self._mixin = ""
        self._cookies: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def apply_cookies(self, cookies: dict[str, str]) -> None:
        self._cookies = {key: value for key, value in cookies.items() if key in COOKIE_NAMES}
        self._client.cookies.clear()
        for key, value in self._cookies.items():
            self._client.cookies.set(key, value, domain=".bilibili.com")

    async def export_cookies(self) -> dict[str, str]:
        found = dict(self._cookies)
        for key in COOKIE_NAMES:
            value = self._client.cookies.get(key)
            if value:
                found[key] = value
        return found

    async def spi(self) -> dict[str, str]:
        data = await self._get(f"{API}/x/frontend/finger/spi", signed=False)
        return {key: str(data.get(key) or "") for key in ("b_3", "b_4") if data.get(key)}

    async def login_qr_start(self) -> QrTicket:
        data = await self._get(f"{PASSPORT}/x/passport-login/web/qrcode/generate", signed=False)
        return QrTicket(url=str(data.get("url") or ""), key=str(data.get("qrcode_key") or ""))

    async def login_qr_poll(self, key: str) -> QrStatus:
        url = f"{PASSPORT}/x/passport-login/web/qrcode/poll?{urlencode({'qrcode_key': key})}"
        response = await self._client.get(url)
        body = _json(response)
        _raise_if(body)
        data = body.get("data") or {}
        code = int(data.get("code") if data.get("code") is not None else body.get("code") or 0)
        cookies = await self.export_cookies() if code == 0 else {}
        return QrStatus(code=code, cookies=cookies, message=str(data.get("message") or ""))

    async def nav(self) -> Account:
        data = await self._get(f"{API}/x/web-interface/nav", signed=False)
        self._remember_wbi(data.get("wbi_img") or {})
        return _account(data)

    async def card(self, mid: int) -> UserCard:
        data = await self._get(f"{API}/x/web-interface/card", {"mid": mid}, signed=False)
        card = data.get("card") or data
        return _user(card)

    async def search_users(self, keyword: str) -> list[UserCard]:
        data = await self._get(
            f"{API}/x/web-interface/wbi/search/type",
            {"search_type": "bili_user", "keyword": keyword},
        )
        rows = (data.get("result") or []) if isinstance(data, dict) else []
        return [_user(row) for row in rows if isinstance(row, dict)]

    async def feed(self, mode: str, *, query: str, limit: int, fresh_idx: int) -> list[VideoCard]:
        if mode == "popular":
            data = await self._get(
                f"{API}/x/web-interface/popular", {"ps": limit, "pn": 1}, signed=False
            )
            rows = data.get("list") or []
        elif mode == "search":
            data = await self._get(
                f"{API}/x/web-interface/wbi/search/type",
                {"search_type": "video", "keyword": query},
            )
            rows = data.get("result") or []
        else:
            data = await self._get(
                f"{API}/x/web-interface/wbi/index/top/feed/rcmd",
                {"ps": limit, "fresh_idx": fresh_idx, "fresh_type": 4},
            )
            rows = data.get("item") or []
        cards = [_card(row) for row in rows if isinstance(row, dict)]
        return [card for card in cards if card.bvid and card.goto == "av"][:limit]

    async def video(self, bvid: str) -> VideoDetail:
        data = await self._get(f"{API}/x/web-interface/view", {"bvid": bvid}, signed=False)
        return _detail(data)

    async def my_flags(self, bvid: str, aid: int) -> MyFlags:
        liked = favored = False
        coin_n = 0
        try:
            like_data = await self._get(
                f"{API}/x/web-interface/archive/has/like", {"bvid": bvid}, signed=False
            )
            liked = int(like_data if isinstance(like_data, int) else like_data or 0) == 1
        except BiliError:
            liked = False
        try:
            coin_data = await self._get(
                f"{API}/x/web-interface/archive/coins", {"bvid": bvid}, signed=False
            )
            coin_n = int((coin_data or {}).get("multiply") or 0)
        except BiliError:
            coin_n = 0
        try:
            fav = await self._get(f"{API}/x/v2/fav/video/favoured", {"aid": aid}, signed=False)
            favored = bool((fav or {}).get("favoured"))
        except BiliError:
            favored = False
        return MyFlags(liked=liked, coin=coin_n, favored=favored)

    async def comments(self, aid: int, limit: int) -> list[Comment]:
        try:
            data = await self._get(
                f"{API}/x/v2/reply",
                {"type": 1, "oid": aid, "sort": 2, "ps": limit, "pn": 1},
                signed=False,
            )
        except BiliError:
            data = await self._get(
                f"{API}/x/v2/reply/wbi/main",
                {"type": 1, "oid": aid, "mode": 3, "ps": limit},
            )
        replies = data.get("replies") or []
        out = []
        for row in replies:
            if not isinstance(row, dict):
                continue
            member = row.get("member") or {}
            content = row.get("content") or {}
            out.append(
                Comment(
                    uname=clean(str(member.get("uname") or ""), limit=40),
                    message=clean_comment_body(str(content.get("message") or "")),
                    like=int(row.get("like") or 0),
                    ctime=_day(row.get("ctime")),
                )
            )
        return out[:limit]

    async def like(self, bvid: str, on: bool) -> None:
        await self._post(
            f"{API}/x/web-interface/archive/like",
            {"bvid": bvid, "like": 1 if on else 2, "csrf": self._csrf()},
        )

    async def coin(self, bvid: str, multiply: int) -> None:
        await self._post(
            f"{API}/x/web-interface/coin/add",
            {
                "bvid": bvid,
                "multiply": multiply,
                "select_like": 0,
                "csrf": self._csrf(),
            },
        )

    async def folders(self, mid: int) -> list[Folder]:
        data = await self._get(
            f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": mid}, signed=False
        )
        rows = data.get("list") or []
        return [
            Folder(id=int(row.get("id") or 0), title=clean(str(row.get("title") or ""), limit=40))
            for row in rows
            if isinstance(row, dict)
        ]

    async def favorite(self, aid: int, folder_id: int, *, on: bool) -> None:
        field = "add_media_ids" if on else "del_media_ids"
        await self._post(
            f"{API}/x/v3/fav/resource/deal",
            {"rid": aid, "type": 2, field: folder_id, "csrf": self._csrf()},
        )

    async def comment(self, aid: int, text: str, *, root: int = 0, parent: int = 0) -> int:
        payload: dict[str, object] = {
            "type": 1,
            "oid": aid,
            "message": text,
            "plat": 1,
            "csrf": self._csrf(),
        }
        if root:
            payload["root"] = root
            payload["parent"] = parent or root
        data = await self._post(f"{API}/x/v2/reply/add", payload)
        return int((data or {}).get("rpid") or 0)

    async def delete_comment(self, aid: int, rpid: int) -> None:
        await self._post(
            f"{API}/x/v2/reply/del",
            {"type": 1, "oid": aid, "rpid": rpid, "csrf": self._csrf()},
        )

    async def play_url(self, bvid: str, cid: int, qn: int) -> str:
        for quality in (qn, 16) if qn != 16 else (16,):
            data = await self._get(
                f"{API}/x/player/wbi/playurl",
                {"bvid": bvid, "cid": cid, "qn": quality, "fnval": 16, "fourk": 0},
            )
            url = _video_url(data)
            if url:
                return url
        raise BiliError(0, "拿不到播放地址")

    async def fetch_bytes(self, url: str) -> bytes:
        response = await self._client.get(url, headers={"Range": f"bytes=0-{_SEGMENT_CAP - 1}"})
        response.raise_for_status()
        return response.content[:_SEGMENT_CAP]

    async def subtitles(self, aid: int, cid: int) -> list[Cue]:
        data = await self._get(f"{API}/x/player/wbi/v2", {"aid": aid, "cid": cid})
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        rows = (subtitle or {}).get("subtitles") or []
        if not rows:
            return []
        url = str(rows[0].get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return []
        body = _json(await self._client.get(url))
        cues = []
        for item in body.get("body") or []:
            if not isinstance(item, dict):
                continue
            cues.append(
                Cue(
                    start=float(item.get("from") or 0),
                    end=float(item.get("to") or 0),
                    text=clean(str(item.get("content") or ""), limit=200),
                )
            )
        return cues

    async def click(self, aid: int, cid: int, bvid: str, *, part: int = 1) -> None:
        # 缺 part / mid / 时间戳时这个接口直接回 -400，观看会在 0 秒停住。
        now = int(time.time())
        await self._post(
            f"{API}/x/click-interface/click/web/h5",
            {
                "aid": aid,
                "cid": cid,
                "bvid": bvid,
                "part": max(1, part),
                "mid": self._cookies.get("DedeUserID") or "0",
                "lv": 0,
                "ftime": now - 30,
                "stime": now,
                "jsonp": "jsonp",
                "type": 3,
                "sub_type": 0,
                "csrf": self._csrf(),
            },
        )

    async def heartbeat(self, *, aid: int, cid: int, bvid: str, played: int, realtime: int) -> None:
        await self._post(
            f"{API}/x/click-interface/web/heartbeat",
            {
                "aid": aid,
                "cid": cid,
                "bvid": bvid,
                "played_time": played,
                "realtime": realtime,
                "csrf": self._csrf(),
            },
        )

    async def report_progress(self, aid: int, cid: int, progress: int) -> None:
        await self._post(
            f"{API}/x/v2/history/report",
            {"aid": aid, "cid": cid, "progress": progress, "csrf": self._csrf()},
        )

    async def unread(self) -> tuple[int, int]:
        data = await self._get(f"{API}/x/msgfeed/unread", signed=False)
        return int(data.get("reply") or 0), int(data.get("at") or 0)

    async def msgfeed(self, kind: str, cursor: str | None) -> list[MsgItem]:
        params: dict[str, object] = {}
        if cursor:
            params["id"] = cursor
        data = await self._get(f"{API}/x/msgfeed/{kind}", params, signed=False)
        items = data.get("items") or []
        out = []
        for row in items:
            if isinstance(row, dict):
                item = _msg(kind, row)
                if item is not None:
                    out.append(item)
        return out

    def _csrf(self) -> str:
        token = self._cookies.get("bili_jct") or self._client.cookies.get("bili_jct")
        if not token:
            raise BiliError(-101, "没有 csrf")
        return str(token)

    def _remember_wbi(self, images: dict[str, Any]) -> None:
        img = _wbi_key(str(images.get("img_url") or ""))
        sub = _wbi_key(str(images.get("sub_url") or ""))
        if len(img + sub) >= 64:
            self._mixin = mixin_key(img, sub)

    async def _get(
        self, url: str, params: dict[str, object] | None = None, *, signed: bool = True
    ) -> Any:
        query = dict(params or {})
        if signed:
            query = sign(query, self._mixin, int(datetime.now().timestamp()))
        response = await self._client.get(url, params=query)
        body = _json(response)
        _raise_if(body)
        return body.get("data")

    async def _post(self, url: str, form: dict[str, object]) -> Any:
        response = await self._client.post(url, data=form)
        body = _json(response)
        _raise_if(body)
        return body.get("data")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise BiliError(response.status_code, "B站返回的不是 json") from exc
    if not isinstance(body, dict):
        raise BiliError(0, "B站返回的结构不对")
    return body


def _raise_if(body: dict[str, Any]) -> None:
    data = body.get("data")
    if isinstance(data, dict) and data.get("v_voucher"):
        raise BiliError(-352, "需要验证")
    code = int(body.get("code") or 0)
    if code != 0:
        raise BiliError(code, str(body.get("message") or ""))


def _wbi_key(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _account(data: dict[str, Any]) -> Account:
    wbi = data.get("wbi_img") or {}
    wallet = data.get("wallet") or {}
    return Account(
        online=bool(data.get("isLogin")),
        mid=int(data.get("mid") or 0),
        uname=clean(str(data.get("uname") or ""), limit=40),
        coins=int(wallet.get("bcoin_balance") or data.get("money") or 0),
        img_key=_wbi_key(str(wbi.get("img_url") or "")),
        sub_key=_wbi_key(str(wbi.get("sub_url") or "")),
    )


def _user(row: dict[str, Any]) -> UserCard:
    official = row.get("official_verify") or row.get("official") or {}
    title = official.get("desc") if isinstance(official, dict) else ""
    return UserCard(
        mid=int(row.get("mid") or 0),
        uname=clean(str(row.get("uname") or ""), limit=40),
        sign=clean(str(row.get("sign") or ""), limit=80),
        level=int(row.get("level") or 0),
        fans=int(row.get("fans") or 0),
        official=clean(str(title or ""), limit=40),
    )


def _card(row: dict[str, Any]) -> VideoCard:
    owner = row.get("owner") or {}
    stat = row.get("stat") or {}
    reason = row.get("rcmd_reason") or {}
    return VideoCard(
        bvid=str(row.get("bvid") or ""),
        title=clean(str(row.get("title") or ""), limit=80),
        up=clean(str(owner.get("name") or row.get("author") or row.get("uname") or ""), limit=40),
        duration_s=_duration(row.get("duration")),
        view=int(stat.get("view") or row.get("play") or 0),
        like=int(stat.get("like") or row.get("like") or 0),
        coin=int(stat.get("coin") or 0),
        favorite=int(stat.get("favorite") or row.get("favorites") or 0),
        reply=int(stat.get("reply") or row.get("review") or 0),
        pubdate=_day(row.get("pubdate")),
        tags=_tags(row),
        reason=clean(str(reason.get("content") or ""), limit=40),
        goto=str(row.get("goto") or "av"),
    )


def _detail(data: dict[str, Any]) -> VideoDetail:
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    pages = [
        VideoPage(cid=int(page.get("cid") or 0), duration_s=int(page.get("duration") or 0))
        for page in data.get("pages") or []
        if isinstance(page, dict)
    ]
    return VideoDetail(
        bvid=str(data.get("bvid") or ""),
        aid=int(data.get("aid") or 0),
        title=clean(str(data.get("title") or ""), limit=80),
        desc=clean(str(data.get("desc") or ""), limit=500),
        up_mid=int(owner.get("mid") or 0),
        up_name=clean(str(owner.get("name") or ""), limit=40),
        tname=clean(str(data.get("tname") or ""), limit=40),
        duration_s=int(data.get("duration") or 0),
        pubdate=_day(data.get("pubdate")),
        view=int(stat.get("view") or 0),
        like=int(stat.get("like") or 0),
        coin=int(stat.get("coin") or 0),
        favorite=int(stat.get("favorite") or 0),
        reply=int(stat.get("reply") or 0),
        pages=pages,
        tags=[clean(str(tag), limit=20) for tag in data.get("tags") or []],
        business=bool(data.get("business_info")),
    )


def _msg(kind: str, row: dict[str, Any]) -> MsgItem | None:
    user = row.get("user") or {}
    item = row.get("item") or {}
    return MsgItem(
        id=str(row.get("id") or ""),
        kind=kind,
        sender_mid=int(user.get("mid") or 0),
        sender_name=clean(str(user.get("nickname") or ""), limit=40),
        text=clean(str(item.get("source_content") or ""), limit=500),
        bvid=str(item.get("bvid") or ""),
        root_id=str(item.get("root_id") or item.get("target_id") or ""),
        source_id=str(item.get("source_id") or row.get("id") or ""),
        title=clean(str(item.get("title") or ""), limit=80),
        business_id=int(item.get("business_id") or 1),
    )


def _video_url(data: dict[str, Any]) -> str:
    dash = data.get("dash") or {}
    videos = dash.get("video") or []
    if videos and isinstance(videos[0], dict):
        return str(videos[0].get("baseUrl") or videos[0].get("base_url") or "")
    durl = data.get("durl") or []
    if durl and isinstance(durl[0], dict):
        return str(durl[0].get("url") or "")
    return ""


def _day(value: object) -> str:
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp).astimezone().date().isoformat()


def _duration(value: object) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "")
    if ":" not in text:
        try:
            return int(text or 0)
        except ValueError:
            return 0
    parts = [int(part) for part in text.split(":") if part.isdigit()]
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _tags(row: dict[str, Any]) -> list[str]:
    raw = row.get("tag") or row.get("tags") or []
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part]
    return [clean(str(tag), limit=20) for tag in raw][:5]
