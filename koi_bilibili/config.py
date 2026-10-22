"""插件设置。未知键直接报错；`owner` 例外，它不生效，只记一条警告。"""

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

log = logging.getLogger("koi_bilibili")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountSettings(_Model):
    role: Literal["dedicated", "owner"] = "dedicated"
    expected_mid: int | None = None


class FeedSettings(_Model):
    default_mode: Literal["recommend", "popular", "search"] = "recommend"
    page_size: int = Field(default=12, ge=1, le=20)
    max_age_days: int = Field(default=30, ge=0, le=3650)


class WatchSettings(_Model):
    max_watch_s: int = Field(default=180, ge=15, le=480)
    max_watch_s_per_call: int = Field(default=480, ge=15, le=480)
    daily_watch_s: int = Field(default=1200, ge=60, le=7200)
    frame_interval_s: int = Field(default=30, ge=5, le=120)
    max_frames: int = Field(default=8, ge=1, le=8)
    frame_width: int = Field(default=640, ge=160, le=1280)
    qn: int = Field(default=32, ge=16, le=120)


class LimitSettings(_Model):
    like_per_day: int = Field(default=20, ge=0, le=100)
    favorite_per_day: int = Field(default=10, ge=0, le=50)
    coin_per_day: int = Field(default=5, ge=0, le=10)
    coin_per_video: int = Field(default=2, ge=0, le=2)
    comment_per_day: int = Field(default=2, ge=0, le=5)
    comment_min_interval_s: int = Field(default=600, ge=0, le=86400)
    write_min_interval_s: float = Field(default=2, ge=0, le=60)
    write_max_interval_s: float = Field(default=6, ge=0, le=120)
    browse_per_hour: int = Field(default=6, ge=1, le=60)

    @model_validator(mode="after")
    def _interval(self) -> "LimitSettings":
        if self.write_min_interval_s > self.write_max_interval_s:
            raise ValueError("write_min_interval_s 不能大于 write_max_interval_s")
        return self


class CommentSettings(_Model):
    mode: Literal["approve", "quota", "off"] = "approve"
    max_chars: int = Field(default=120, ge=1, le=1000)
    max_lines: int = Field(default=2, ge=1, le=20)
    block_keywords: list[str] = Field(default_factory=list)


class InboxSettings(_Model):
    enabled: bool = True
    poll_s: int = Field(default=90, ge=30, le=600)
    max_items_per_poll: int = Field(default=20, ge=1, le=50)
    max_events_per_min: int = Field(default=6, ge=1, le=30)


class NetworkSettings(_Model):
    timeout_s: float = Field(default=20, ge=1, le=60)
    trust_env: bool = True


class Settings(_Model):
    account: AccountSettings = Field(default_factory=AccountSettings)
    feed: FeedSettings = Field(default_factory=FeedSettings)
    watch: WatchSettings = Field(default_factory=WatchSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    comments: CommentSettings = Field(default_factory=CommentSettings)
    inbox: InboxSettings = Field(default_factory=InboxSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)


def load_settings(raw: dict[str, Any] | None) -> Settings:
    data = dict(raw or {})
    if "owner" in data:
        log.warning("settings.owner 不生效，主人只能通过 bind_owner 的表单写入")
        data.pop("owner")
    return Settings.model_validate(data)
