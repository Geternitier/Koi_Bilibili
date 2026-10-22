"""主人绑定。昵称只用来找候选，写进状态的只有主人在表单里确认的 mid。"""

import re

from koi.protocol import InteractionField, InteractionRequest

from koi_bilibili.errors import BiliError
from koi_bilibili.models import UserCard
from koi_bilibili.runtime import Runtime

_TITLE = "确认主人的B站账号"
_MID = re.compile(r"^\d{1,12}$")
_SPACE = re.compile(r"bilibili\.com/(?:space/)?(\d{1,12})")


class OwnerBinding:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._pending = ""

    async def bind(self, query: str, force: bool) -> dict[str, object]:
        if self.rt.settings.account.role == "owner":
            return {"ok": True, "message": "当前账号就是主人自己的号，不需要绑定"}
        if not self.rt.can_interact:
            return {"ok": False, "error": "没有 interact 权限，不能请主人确认"}
        if self.rt.store.owner_mid() and not force:
            return {"ok": True, "message": f"已经绑定主人 {self.rt.store.owner_uname()}"}
        text = (query or "").strip()
        if not text:
            return {"ok": False, "error": "先问主人要 B 站用户名、空间链接或 UID"}
        if self._pending:
            return {"ok": False, "error": "先完成网页上正在处理的确认"}
        try:
            card, many = await self._resolve(text)
        except BiliError as error:
            await self.rt.note_error(error)
            if error.code in (-352, -412):
                return {"ok": False, "error": "B 站搜索被拦截了，请主人直接给空间链接或 UID"}
            return {"ok": False, "error": "没找到这个用户；请主人给空间链接或 UID"}
        if many:
            return {"ok": True, "candidates": [_public(item) for item in many]}
        if card is None:
            return {"ok": False, "error": "没找到这个用户；请主人给空间链接或 UID"}
        self._pending = await self.rt.ctx.request_interaction(
            InteractionRequest(
                kind="form",
                title=_TITLE,
                message=(
                    f"找到：{card.uname}（UID {card.mid}）。"
                    "请主人输入这个 UID 确认；如果不对，输入正确的 UID。"
                ),
                fields=[
                    InteractionField(
                        name="mid",
                        label="主人的 B 站 UID",
                        pattern=r"\d{1,12}",
                        placeholder=str(card.mid),
                    )
                ],
                expires_in_s=300,
            )
        )
        return {"ok": True, "message": "确认框已经发给主人了"}

    async def submit(self, values: dict[str, str] | None) -> None:
        interaction = self._pending
        self._pending = ""
        if not interaction:
            return
        if not values or not values.get("mid"):
            await self.rt.ctx.close_interaction(interaction, False, "主人没有确认，绑定保持原样")
            return
        raw = values["mid"].strip()
        if _MID.fullmatch(raw) is None:
            await self.rt.ctx.close_interaction(interaction, False, "UID 不对，没有改绑定")
            return
        mid = int(raw)
        account_mid = self.rt.account.mid if self.rt.account else self.rt.store.account_mid()
        if account_mid and mid == account_mid and self.rt.settings.account.role == "dedicated":
            await self.rt.ctx.close_interaction(
                interaction, False, "这是她自己登录的账号，主人需要用另一个号"
            )
            return
        try:
            card = await self.rt.backend.card(mid)
        except BiliError:
            await self.rt.ctx.close_interaction(interaction, False, "这个 UID 没查到人，没有改绑定")
            return
        if not card.mid or not card.uname:
            await self.rt.ctx.close_interaction(interaction, False, "这个 UID 没查到人，没有改绑定")
            return
        self.rt.store.set_owner(card.mid, card.uname, "form", self.rt.clock.now().isoformat())
        await self.rt.ctx.close_interaction(interaction, True, f"已绑定主人：{card.uname}")

    async def _resolve(self, query: str) -> tuple[UserCard | None, list[UserCard]]:
        space = _SPACE.search(query)
        if _MID.fullmatch(query) or space:
            mid = int(space.group(1) if space else query)
            return await self.rt.backend.card(mid), []
        found = await self.rt.backend.search_users(query)
        ranked = _rank(query.strip(), found)
        exact = [item for item in ranked if item.uname.strip() == query.strip()]
        if len(exact) == 1:
            return exact[0], []
        folded = query.strip().casefold()
        insensitive = [item for item in ranked if item.uname.strip().casefold() == folded]
        if len(exact) == 0 and len(insensitive) == 1:
            return insensitive[0], []
        if not ranked:
            return None, []
        return None, ranked[:5]


def _rank(query: str, cards: list[UserCard]) -> list[UserCard]:
    folded = query.casefold()

    def key(card: UserCard) -> tuple[int, int]:
        name = card.uname.strip()
        if name == query:
            return (0, 0)
        if name.casefold() == folded:
            return (1, 0)
        if folded in name.casefold():
            return (2, 0)
        return (3, 0)

    return sorted(cards, key=key)


def _public(card: UserCard) -> dict[str, object]:
    return {
        "uname": card.uname,
        "sign": card.sign,
        "level": card.level,
        "fans": card.fans,
        "official_verify": card.official,
    }
