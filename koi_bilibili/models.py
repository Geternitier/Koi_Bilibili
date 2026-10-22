"""B 站接口在插件内部的形状。字段缺了就用默认值，不让一条脏数据把进程打崩。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    online: bool
    mid: int = 0
    uname: str = ""
    coins: int = 0
    blocked: bool = False
    img_key: str = ""
    sub_key: str = ""


@dataclass(frozen=True)
class UserCard:
    mid: int
    uname: str
    sign: str = ""
    level: int = 0
    fans: int = 0
    official: str = ""


@dataclass(frozen=True)
class VideoCard:
    bvid: str
    title: str
    up: str
    duration_s: int = 0
    view: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    reply: int = 0
    pubdate: str = ""
    tags: list[str] = field(default_factory=list)
    reason: str = ""
    goto: str = "av"


@dataclass(frozen=True)
class VideoPage:
    cid: int
    duration_s: int


@dataclass(frozen=True)
class VideoDetail:
    bvid: str
    aid: int
    title: str
    desc: str
    up_mid: int
    up_name: str
    tname: str = ""
    duration_s: int = 0
    pubdate: str = ""
    view: int = 0
    like: int = 0
    coin: int = 0
    favorite: int = 0
    reply: int = 0
    pages: list[VideoPage] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    business: bool = False


@dataclass(frozen=True)
class MyFlags:
    liked: bool = False
    coin: int = 0
    favored: bool = False


@dataclass(frozen=True)
class Comment:
    uname: str
    message: str
    like: int = 0
    ctime: str = ""


@dataclass(frozen=True)
class QrTicket:
    url: str
    key: str


@dataclass(frozen=True)
class QrStatus:
    """code：0 成功，86038 过期，86090 已扫未确认，86101 未扫。"""

    code: int
    cookies: dict[str, str] = field(default_factory=dict)
    message: str = ""


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Folder:
    id: int
    title: str


@dataclass(frozen=True)
class MsgItem:
    id: str
    kind: str
    sender_mid: int
    sender_name: str
    text: str
    bvid: str
    root_id: str
    source_id: str
    title: str
    business_id: int = 1
