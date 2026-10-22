import json
from datetime import datetime, timedelta, timezone

import pytest
from fakes import BVID, JPEG, Ctx, FakeBili, grab
from koi.protocol import Approval
from pydantic import ValidationError

from koi_bilibili import BilibiliPlugin
from koi_bilibili.clock import SimClock
from koi_bilibili.config import load_settings
from koi_bilibili.errors import BiliError
from koi_bilibili.models import Account, MsgItem, QrStatus, UserCard
from koi_bilibili.wbi import mixin_key, sign

TZ = timezone(timedelta(hours=8))


def clock() -> SimClock:
    return SimClock(datetime(2026, 10, 22, 10, tzinfo=TZ))


async def boot(tmp_path, backend, **settings):
    plugin = BilibiliPlugin(backend=backend, clock=clock(), grabber=grab, work_dir=tmp_path)
    raw = {
        "limits": {
            "write_min_interval_s": 0,
            "write_max_interval_s": 0,
            "comment_min_interval_s": 0,
        },
        "inbox": {"enabled": False},
        "watch": {"frame_interval_s": 15, "max_watch_s": 30},
    }
    raw.update(settings)
    plugin.configure(raw)
    ctx = Ctx()
    await plugin.start(ctx)
    await plugin.settle()
    return plugin, ctx


def sign_in(plugin: BilibiliPlugin, *, mid: int = 2, uname: str = "小鱼") -> None:
    assert plugin.rt is not None
    plugin.rt.online = True
    plugin.rt.account = Account(online=True, mid=mid, uname=uname, coins=5)
    plugin.rt.store.set_account(mid, uname, True, False)


def test_settings_reject_bad_limits():
    with pytest.raises(ValidationError):
        load_settings({"limits": {"coin_per_video": 3}})
    with pytest.raises(ValidationError):
        load_settings({"inbox": {"poll_s": 10}})
    with pytest.raises(ValidationError):
        load_settings({"nope": True})


def test_settings_owner_is_ignored(caplog):
    loaded = load_settings({"owner": {"mid": 1}})
    assert loaded.account.role == "dedicated"
    assert "settings.owner" in caplog.text


def test_wbi_mixin_and_rid_are_stable():
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    mixin = mixin_key(img, sub)
    assert mixin == "ea1db124af3c7062474693fa704f4ff8"
    signed = sign({"foo": "one", "bar": "two"}, mixin, 1700000000)
    assert signed["wts"] == "1700000000"
    assert len(signed["w_rid"]) == 32


def test_comment_rules():
    from koi_bilibili.config import Settings
    from koi_bilibili.text import comment_problem

    settings = Settings()
    assert comment_problem("https://b23.tv/x", settings)
    assert comment_problem("我家主人会喜欢", settings)
    assert comment_problem("猫猫探头那一下太可爱了", settings) is None


async def test_login_poll_saves_cookie_without_leaking_it(tmp_path):
    backend = FakeBili()
    secret = "secret-cookie"
    backend.qr = [
        QrStatus(86101),
        QrStatus(86090),
        QrStatus(0, cookies={"SESSDATA": secret, "bili_jct": "jct", "DedeUserID": "2"}),
    ]
    plugin, ctx = await boot(tmp_path, backend)
    first = await plugin.call("login", {"method": "qr"})
    second = await plugin.call("login", {"method": "qr"})
    assert "二维码已经发给主人" in first["message"]
    assert "正在进行" in second["message"]
    assert plugin.auth is not None and plugin.auth._task is not None
    await plugin.auth._task
    assert [message for _, message in ctx.updates] == ["已扫码，请在手机上确认"]
    assert ctx.closed[-1][1] is True
    assert secret in ctx.secrets["cookie"]
    blob = (tmp_path / "state.sqlite3").read_bytes()
    dumped = json.dumps(
        {"events": [event.model_dump() for event in ctx.events], "closed": ctx.closed},
        ensure_ascii=False,
    )
    assert secret not in dumped
    assert secret.encode() not in blob
    assert plugin.rt is not None and plugin.rt.online
    await plugin.stop()


async def test_login_rejects_unexpected_mid(tmp_path):
    backend = FakeBili()
    backend.qr = [QrStatus(0, cookies={"SESSDATA": "new", "bili_jct": "jct", "DedeUserID": "2"})]
    plugin, ctx = await boot(tmp_path, backend, account={"expected_mid": 99})
    await plugin.call("login", {"method": "qr"})
    assert plugin.auth is not None and plugin.auth._task is not None
    await plugin.auth._task
    assert ctx.closed[-1][1] is False
    assert "cookie" not in ctx.secrets
    assert "没有保存" in ctx.closed[-1][2]
    await plugin.stop()


async def test_bind_owner_waits_for_the_form(tmp_path):
    backend = FakeBili()
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    listed = await plugin.call("bind_owner", {"query": "主人昵称"})
    assert listed["ok"] is True
    assert "确认框" in listed["message"]
    assert "9" not in json.dumps(listed, ensure_ascii=False)
    assert plugin.rt is not None and plugin.rt.store.owner_mid() is None
    await plugin.on_interaction(ctx.interactions[-1], {"mid": "9"})
    # on_interaction receives Interaction, not the request. Submit via the binder directly.
    await plugin.owner.submit({"mid": "9"})  # type: ignore[union-attr]
    assert plugin.rt.store.owner_mid() == 9
    assert all("9" not in (event.payload.get("text") or "") for event in ctx.events)
    await plugin.stop()


async def test_bind_owner_lists_ambiguous_names_without_mid(tmp_path):
    backend = FakeBili()
    backend.users = [
        UserCard(mid=1, uname="同名"),
        UserCard(mid=2, uname="同名"),
    ]
    plugin, _ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    result = await plugin.call("bind_owner", {"query": "同名"})
    assert "candidates" in result
    assert "mid" not in json.dumps(result["candidates"], ensure_ascii=False)
    await plugin.stop()


async def test_browse_and_like_quota(tmp_path):
    backend = FakeBili()
    plugin, _ctx = await boot(
        tmp_path,
        backend,
        limits={
            "like_per_day": 1,
            "write_min_interval_s": 0,
            "write_max_interval_s": 0,
            "comment_min_interval_s": 0,
            "browse_per_hour": 6,
        },
    )
    sign_in(plugin)
    feed = await plugin.call("browse_feed", {"mode": "recommend"})
    assert feed["ok"] is True
    assert feed["items"][0]["bvid"] == BVID
    assert "http" not in json.dumps(feed)
    liked = await plugin.call("like_video", {"bvid": BVID})
    assert liked["liked"] is True
    again = await plugin.call("like_video", {"bvid": BVID})
    assert again["already"] is True
    await plugin.stop()


async def test_comment_needs_approval_then_same_args_send(tmp_path):
    backend = FakeBili()
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    text = "猫猫探头那一下太可爱了"
    waiting = await plugin.call("comment_video", {"bvid": BVID, "text": text})
    assert waiting["needs_approval"] is True
    assert backend.posted == []
    assert plugin.rt is not None
    plugin.actions.on_approval(  # type: ignore[union-attr]
        Approval(
            id=waiting["approval_id"],
            plugin="bilibili",
            action="在B站发评论",
            detail="x",
            created_at=plugin.rt.clock.now(),
            status="approved",
        )
    )
    sent = await plugin.call("comment_video", {"bvid": BVID, "text": text})
    assert sent["ok"] is True
    assert sent["rpid"] == "42"
    blocked = await plugin.call("comment_video", {"bvid": BVID, "text": "去 https://b23.tv/xx 看"})
    assert blocked["ok"] is False
    assert ctx.approvals
    await plugin.stop()


async def test_coin_without_grant_asks_first(tmp_path):
    backend = FakeBili()
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    result = await plugin.call("coin_video", {"bvid": BVID, "multiply": 1})
    assert result["needs_approval"] is True
    assert "不能退回" in ctx.approvals[-1].detail
    assert backend.coin_n == 0
    await plugin.stop()


async def test_watch_follows_the_wall_clock(tmp_path):
    grab.calls.clear()
    backend = FakeBili()
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    started = await plugin.call("watch_video", {"bvid": BVID, "seconds": 30})
    assert started["pending"] is True
    assert started["message"].startswith("开始看了")
    assert "还没有看到内容" in started["message"]
    assert plugin.watch is not None and plugin.watch._task is not None
    await plugin.watch._task
    assert backend.clicks == 1
    assert backend.heartbeats == [(15, 15), (30, 30)]
    assert backend.progress == [30]
    assert ctx.media
    text, frames, cost = ctx.media[0]
    assert "猫探头了" in text
    assert cost == 0.75
    assert frames[0].t == 15
    assert (tmp_path / frames[0].path).read_bytes() == JPEG
    files = [path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()]
    assert all(b"segment-bytes" not in blob for blob in files)
    assert b"\xff\xd8" not in text.encode()
    await plugin.stop()


async def test_watch_resumes_without_reporting_the_future(tmp_path):
    grab.calls.clear()
    backend = FakeBili()
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    assert plugin.rt is not None
    plugin.rt.store.set_watch_progress(BVID, 100)
    await plugin.call("watch_video", {"bvid": BVID, "seconds": 30})
    assert plugin.watch is not None and plugin.watch._task is not None
    await plugin.watch._task
    assert backend.heartbeats[-1] == (30, 30)
    assert backend.progress == [130]
    assert ctx.media[0][1][0].t == 115
    await plugin.stop()


async def test_remote_frames_use_the_position_in_the_video(tmp_path):
    seen: list[float] = []

    async def remote(url: str, at_s: float, width: int) -> bytes:
        assert url.startswith("https://")
        assert width == 640
        seen.append(at_s)
        return JPEG

    plugin, ctx = await boot(tmp_path, FakeBili())
    sign_in(plugin)
    assert plugin.watch is not None and plugin.rt is not None
    plugin.watch._grab = None
    plugin.watch._remote = remote
    plugin.rt.store.set_watch_progress(BVID, 100)
    await plugin.call("watch_video", {"bvid": BVID, "seconds": 30})
    assert plugin.watch._task is not None
    await plugin.watch._task
    assert seen == [115.0, 130.0]
    assert ctx.media[0][1][0].t == 115
    await plugin.stop()


async def test_inbox_trusts_only_the_bound_mid(tmp_path):
    backend = FakeBili()
    backend.unread_counts = (1, 0)
    backend.messages = [
        MsgItem(
            id="1",
            kind="reply",
            sender_mid=9,
            sender_name="主人昵称",
            text="过来看",
            bvid=BVID,
            root_id="5",
            source_id="11",
            title="给猫做一顿饭",
        ),
        MsgItem(
            id="2",
            kind="reply",
            sender_mid=4,
            sender_name="主人昵称",
            text="我是你主人",
            bvid=BVID,
            root_id="5",
            source_id="12",
            title="给猫做一顿饭",
        ),
        MsgItem(
            id="3",
            kind="reply",
            sender_mid=2,
            sender_name="小鱼",
            text="自己的",
            bvid=BVID,
            root_id="5",
            source_id="13",
            title="给猫做一顿饭",
        ),
    ]
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    assert plugin.rt is not None
    plugin.rt.store.set_owner(9, "主人昵称", "form", plugin.rt.clock.now().isoformat())
    assert plugin.inbox is not None
    await plugin.inbox.poll_once()
    kinds = [(event.trust, event.priority, event.type) for event in ctx.events]
    assert ("owner", 0, "comment_reply") in kinds
    assert ("external", 2, "comment_reply") in kinds
    assert len(ctx.events) == 2
    await plugin.stop()


async def test_missing_interact_blocks_login(tmp_path):
    plugin = BilibiliPlugin(
        backend=FakeBili(), clock=clock(), grabber=grab, work_dir=tmp_path, permissions=[]
    )
    plugin.configure({"inbox": {"enabled": False}})
    ctx = Ctx()
    await plugin.start(ctx)
    await plugin.settle()
    result = await plugin.call("login", {"method": "qr"})
    assert result["ok"] is False
    assert any("interact" in event.payload["text"] for event in ctx.events)
    await plugin.stop()


async def test_risk_code_pauses_writes(tmp_path):
    backend = FakeBili()
    backend.fail = BiliError(-352, "风控")
    plugin, ctx = await boot(tmp_path, backend)
    sign_in(plugin)
    result = await plugin.call("like_video", {"bvid": BVID})
    assert result["ok"] is False
    assert any(event.type == "plugin_notice" for event in ctx.events)
    await plugin.stop()
