"""浏览、视频详情、热门评论。"""

from datetime import datetime, timedelta

from koi_bilibili.errors import BiliError
from koi_bilibili.models import VideoCard, VideoDetail
from koi_bilibili.runtime import Runtime


class Browse:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def feed(self, mode: str, query: str, limit: int) -> dict[str, object]:
        if mode not in ("recommend", "popular", "search"):
            mode = self.rt.settings.feed.default_mode
        if mode == "search" and not query.strip():
            return {"ok": False, "error": "搜索要给关键词"}
        hour = self.rt.clock.now().strftime("%Y-%m-%dT%H")
        if self.rt.store.quota_total(hour, "browse") >= self.rt.settings.limits.browse_per_hour:
            return {"ok": False, "error": "这一小时浏览够了，过会儿再看"}
        fresh = self.rt.store.next_fresh_idx()
        try:
            cards = await self.rt.backend.feed(
                mode, query=query.strip(), limit=limit, fresh_idx=fresh
            )
        except BiliError as error:
            await self.rt.note_error(error)
            if mode == "recommend" and error.code == -101:
                try:
                    cards = await self.rt.backend.feed(
                        "popular", query="", limit=limit, fresh_idx=fresh
                    )
                except BiliError as again:
                    await self.rt.note_error(again)
                    return {"ok": False, "error": "现在刷不了"}
                mode = "popular"
                note = "不是个性化推荐"
            else:
                return {"ok": False, "error": "现在刷不了"}
        else:
            note = ""
        if mode == "recommend":
            cards = [card for card in cards if not _too_old(card, self.rt)]
        self.rt.store.add_quota(hour, "browse", "", 1)
        items = [_item(card) for card in cards[:limit]]
        payload: dict[str, object] = {
            "ok": True,
            "mode": mode,
            "items": _fit(items),
            "next": f"fresh_idx={fresh + 1}",
        }
        if note:
            payload["message"] = note
        return payload

    async def view(self, bvid: str) -> dict[str, object]:
        try:
            video = await self.rt.backend.video(bvid)
            flags = await self.rt.backend.my_flags(bvid, video.aid)
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "这个视频没打开"}
        return {"ok": True, "video": _video(video), "my": _flags(flags)}

    async def comments(self, bvid: str, limit: int) -> dict[str, object]:
        try:
            video = await self.rt.backend.video(bvid)
            rows = await self.rt.backend.comments(video.aid, limit)
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "评论没读到"}
        items = [
            {"uname": row.uname, "message": row.message[:200], "like": row.like, "ctime": row.ctime}
            for row in rows[:limit]
        ]
        hidden = max(0, len(rows) - len(_fit_comments(items)))
        shown = _fit_comments(items)
        payload: dict[str, object] = {"ok": True, "bvid": bvid, "comments": shown}
        if hidden:
            payload["message"] = f"还有 {hidden} 条"
        return payload


def _too_old(card: VideoCard, rt: Runtime) -> bool:
    if not card.pubdate:
        return False
    try:
        published = datetime.fromisoformat(card.pubdate).date()
    except ValueError:
        return False
    return rt.clock.now().date() - published > timedelta(days=rt.settings.feed.max_age_days)


def _item(card: VideoCard) -> dict[str, object]:
    return {
        "bvid": card.bvid,
        "title": card.title,
        "up": card.up,
        "duration": _clock(card.duration_s),
        "view": _compact(card.view),
        "like": _compact(card.like),
        "coin": _compact(card.coin),
        "favorite": _compact(card.favorite),
        "reply": _compact(card.reply),
        "pubdate": card.pubdate,
        "tags": card.tags,
        "reason": card.reason,
    }


def _video(video: VideoDetail) -> dict[str, object]:
    return {
        "bvid": video.bvid,
        "aid": video.aid,
        "title": video.title,
        "up": {"mid": video.up_mid, "name": video.up_name},
        "tname": video.tname,
        "duration": _clock(video.duration_s),
        "pubdate": video.pubdate,
        "desc": video.desc,
        "stat": {
            "view": video.view,
            "like": video.like,
            "coin": video.coin,
            "favorite": video.favorite,
            "reply": video.reply,
        },
    }


def _flags(flags: object) -> dict[str, object]:
    return {"liked": flags.liked, "coin": flags.coin, "favored": flags.favored}  # type: ignore[attr-defined]


def _compact(value: int) -> str:
    if value >= 10000:
        text = f"{value / 10000:.1f}"
        if text.endswith(".0"):
            text = text[:-2]
        return text + "万"
    return str(value)


def _clock(seconds: int) -> str:
    minutes, sec = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _fit(items: list[dict[str, object]]) -> list[dict[str, object]]:
    kept: list[dict[str, object]] = []
    total = 0
    for item in items:
        size = len(str(item))
        if kept and total + size > 3000:
            break
        kept.append(item)
        total += size
    return kept


def _fit_comments(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return _fit(items)
