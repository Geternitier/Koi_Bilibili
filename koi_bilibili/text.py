"""文本清洗和评论发布前的检查。"""

import re

from koi_bilibili.config import Settings

_TAG = re.compile(r"<[^>]+>")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\ufeff]")
_LINK = re.compile(r"https?://|www\.|b23\.tv|bilibili\.com", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CONTACT = re.compile(r"微信|vx|weixin|qq", re.IGNORECASE)
_SPACE = re.compile(r"\s+")

# 结构检查解决不了的明显招嫖、赌博、诈骗。其余词放 settings.comments.block_keywords。
_BUILTIN_BLOCK = ("色情", "裸聊", "赌博", "博彩", "代刷", "刷量", "兼职日结")


def clean(text: str, *, limit: int = 2000) -> str:
    raw = _CTRL.sub("", _TAG.sub("", text or ""))
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1] + "…"


def clean_comment_body(text: str) -> str:
    """表情和图片收成记号，正文仍要再过发布检查。"""
    body = text or ""
    body = re.sub(r"\[.*?\]", "[表情]", body)
    body = body.replace("<img", "[图片]<img")
    return clean(body, limit=200)


def normalize(text: str) -> str:
    folded = clean(text, limit=1000).lower()
    return _SPACE.sub("", re.sub(r"[^\w\u4e00-\u9fff]+", "", folded))


def comment_problem(
    text: str,
    settings: Settings,
    *,
    title: str = "",
    business: bool = False,
    own_video: bool = False,
    max_chars: int | None = None,
    max_lines: int | None = None,
) -> str | None:
    if business:
        return "这像是广告视频，不在下面评论"
    if own_video:
        return "这是自己的视频，不自己评论"
    title_hit = _hit(title, settings)
    if title_hit:
        return f"视频标题里有「{title_hit}」，不评论"
    limit = max_chars if max_chars is not None else settings.comments.max_chars
    lines_limit = max_lines if max_lines is not None else settings.comments.max_lines
    body = clean(text, limit=limit + 20)
    if not body:
        return "评论是空的"
    if len(body) > limit:
        return f"评论超过 {limit} 字了，缩短一点"
    lines = [line for line in body.split("\n") if line.strip()]
    if len(lines) > lines_limit:
        return f"评论最多 {lines_limit} 行"
    if _LINK.search(body):
        return "这条评论里有链接，公开评论区不适合发；可以改成只说感受"
    if "@" in body:
        return "评论里不要 @"
    if _PHONE.search(body) or _EMAIL.search(body) or _CONTACT.search(body):
        return "评论里不要留联系方式"
    if "主人" in body:
        return "评论里不要提到主人，公开评论区不适合说这个"
    hit = _hit(body, settings)
    if hit:
        return f"这句话不适合发在公开评论区：含有「{hit}」"
    return None


def _hit(text: str, settings: Settings) -> str | None:
    folded = text.casefold()
    for word in (*_BUILTIN_BLOCK, *settings.comments.block_keywords):
        token = word.strip()
        if token and token.casefold() in folded:
            return token
    return None
