"""按墙上时钟看一段。心跳只报已经过去的秒数，画面留在工作目录。"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from koi.protocol import MediaFrame

from koi_bilibili.errors import BiliError, public_message
from koi_bilibili.frames import FrameError, ffmpeg_grab_url, prune, write_frame
from koi_bilibili.models import Cue, VideoDetail
from koi_bilibili.runtime import Runtime

log = logging.getLogger("koi_bilibili")

Grabber = Callable[[bytes, float, int, Path], Awaitable[bytes]]
UrlGrabber = Callable[[str, float, int], Awaitable[bytes]]


class Watcher:
    def __init__(
        self,
        rt: Runtime,
        grabber: Grabber | None = None,
        *,
        remote: UrlGrabber | None = None,
    ) -> None:
        self.rt = rt
        # 测试注入本地片段抽帧。正式路径按播放地址和片内秒数抽，不把整段下下来。
        self._grab = grabber
        self._remote = remote or ffmpeg_grab_url
        self._task: asyncio.Task[None] | None = None

    async def start(self, bvid: str, seconds: int | None, page: int) -> dict[str, object]:
        if self._task is not None and not self._task.done():
            return {"ok": False, "error": "正在看，先看完这个"}
        if not self.rt.online:
            return {"ok": False, "error": "还没登录，看不了"}
        try:
            video = await self.rt.backend.video(bvid)
        except BiliError as error:
            await self.rt.note_error(error)
            return {"ok": False, "error": "这个视频没打开"}
        part = _page(video, page)
        if part is None:
            return {"ok": False, "error": "没有这一P"}
        span = _span(self.rt, bvid, seconds, part[1])
        if isinstance(span, str):
            return {"ok": False, "error": span}
        resume, duration = span
        self._task = asyncio.create_task(self._run(video, part[0], resume, duration, page))
        if duration >= 60:
            wait = f"大约 {max(1, round(duration / 60))} 分钟"
        else:
            wait = f"大约 {duration} 秒"
        return {
            "ok": True,
            "pending": True,
            "message": (
                f"开始看了，{wait}后才会把画面和字幕给你。"
                "现在还没有看到内容。收到之前不要描述视频里发生了什么。"
            ),
        }

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(
        self, video: VideoDetail, cid: int, resume: int, duration: int, page: int
    ) -> None:
        frames: list[MediaFrame] = []
        elapsed = 0
        try:
            await self.rt.backend.click(video.aid, cid, video.bvid, part=page)
            url, blob = await self._source(video.bvid, cid)
            started = self.rt.clock.now()
            targets = _targets(
                duration, self.rt.settings.watch.frame_interval_s, self.rt.settings.watch.max_frames
            )
            grabbed = 0
            while True:
                elapsed = int((self.rt.clock.now() - started).total_seconds())
                if elapsed >= duration:
                    elapsed = duration
                    break
                await self.rt.clock.sleep(min(15, duration - elapsed))
                elapsed = min(duration, int((self.rt.clock.now() - started).total_seconds()))
                await self.rt.backend.heartbeat(
                    aid=video.aid,
                    cid=cid,
                    bvid=video.bvid,
                    played=elapsed,
                    realtime=elapsed,
                )
                while grabbed < len(targets) and targets[grabbed] <= elapsed:
                    frame = await self._frame(blob, url, video.bvid, resume + targets[grabbed])
                    if frame is not None:
                        frames.append(frame)
                    grabbed += 1
            await self.rt.backend.report_progress(video.aid, cid, resume + elapsed)
            self._remember(video.bvid, resume + elapsed, elapsed)
            cues = await self._cues(video.aid, cid, resume, resume + elapsed)
            await self._offer(video, cues, frames, elapsed, "")
        except asyncio.CancelledError:
            raise
        except BiliError as error:
            await self.rt.note_error(error)
            if elapsed:
                self._remember(video.bvid, resume + elapsed, elapsed)
            await self._offer(video, [], frames, elapsed, public_message(error))

    async def _source(self, bvid: str, cid: int) -> tuple[str, bytes]:
        try:
            url = await self.rt.backend.play_url(bvid, cid, self.rt.settings.watch.qn)
        except BiliError as error:
            log.info("播放地址没拿到：%s", error.code)
            return "", b""
        if self._grab is None:
            return url, b""
        try:
            return url, await self.rt.backend.fetch_bytes(url)
        except BiliError as error:
            log.info("片段没拿到：%s", error.code)
            return "", b""

    async def _frame(self, blob: bytes, url: str, bvid: str, at: int) -> MediaFrame | None:
        width = self.rt.settings.watch.frame_width
        tmp = self.rt.work_dir / "frames" / ".tmp"
        try:
            if self._grab is None:
                if not url:
                    return None
                data = await self._remote(url, float(at), width)
            else:
                if not blob:
                    return None
                data = await self._grab(blob, float(at), width, tmp)
            rel = write_frame(self.rt.work_dir, bvid, at, data)
        except (FrameError, OSError) as error:
            log.info("这一帧没抽到：%s", error)
            return None
        finally:
            if tmp.exists():
                for path in tmp.glob("*"):
                    path.unlink(missing_ok=True)
        prune(self.rt.work_dir)
        return MediaFrame(path=rel, t=float(at))

    async def _cues(self, aid: int, cid: int, start: int, end: int) -> list[Cue]:
        try:
            cues = await self.rt.backend.subtitles(aid, cid)
        except BiliError:
            return []
        return [cue for cue in cues if cue.end >= start and cue.start <= end and cue.text]

    def _remember(self, bvid: str, progress: int, watched: int) -> None:
        self.rt.store.set_watch_progress(bvid, progress)
        self.rt.store.add_watched(self.rt.clock.now().date().isoformat(), watched)

    async def _offer(
        self,
        video: VideoDetail,
        cues: list[Cue],
        frames: list[MediaFrame],
        elapsed: int,
        problem: str,
    ) -> None:
        lines = [cue.text for cue in cues]
        if lines:
            text = "\n".join(lines)
        elif frames:
            text = "这段没有字幕，你只看见画面"
        else:
            text = "这段没有字幕，画面也没抽到"
        if problem:
            text = f"看到 {elapsed} 秒就停了：{problem}\n{text}"
        text = f"《{video.title}》\n{text}"[:2000]
        minutes = elapsed / 60
        await self.rt.ctx.offer_media(text, frames, energy_cost=round(1.5 * minutes, 2))


def _page(video: VideoDetail, page: int) -> tuple[int, int] | None:
    if video.pages:
        if page < 1 or page > len(video.pages):
            return None
        part = video.pages[page - 1]
        return part.cid, part.duration_s or video.duration_s
    if page != 1:
        return None
    return 0, video.duration_s


def _span(rt: Runtime, bvid: str, seconds: int | None, length: int) -> tuple[int, int] | str:
    watch = rt.settings.watch
    resume = rt.store.watch_progress(bvid)
    if length and resume >= length:
        return "这个视频看到结尾了"
    remain_video = max(0, length - resume) if length else watch.max_watch_s_per_call
    day = rt.clock.now().date().isoformat()
    remain_day = watch.daily_watch_s - rt.store.watched_today(day)
    if remain_day <= 0:
        return "今天的观看时间用完了"
    wanted = watch.max_watch_s if seconds is None else seconds
    if wanted <= 0:
        return "观看时长不对"
    duration = min(wanted, watch.max_watch_s_per_call, remain_day, remain_video)
    if duration < 5:
        return "剩下的时间不够再看一段"
    return resume, duration


def _targets(duration: int, interval: int, limit: int) -> list[int]:
    times = list(range(interval, duration + 1, interval))[:limit]
    if not times:
        times = [duration]
    return times[:limit]
