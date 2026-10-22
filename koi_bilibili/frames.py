"""画面文件。视频片段只在抽帧时落在临时目录，用完就删。"""

import asyncio
import re
import shutil
from pathlib import Path

FRAME_BUDGET = 50 * 1024 * 1024

_BVID = re.compile(r"^BV[0-9A-Za-z]+$")


class FrameError(Exception):
    pass


def frame_path(bvid: str, second: int) -> str:
    if _BVID.fullmatch(bvid) is None:
        raise FrameError("bvid 不合法")
    return f"frames/{bvid}/{max(0, second):04d}.jpg"


def write_frame(root: Path, bvid: str, second: int, data: bytes) -> str:
    rel = frame_path(bvid, second)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def prune(root: Path, budget: int = FRAME_BUDGET) -> None:
    base = root / "frames"
    if not base.is_dir():
        return
    files = [path for path in base.rglob("*.jpg") if path.is_file()]
    groups: dict[Path, list[Path]] = {}
    for path in files:
        groups.setdefault(path.parent, []).append(path)
    total = sum(path.stat().st_size for path in files)
    for folder in sorted(groups, key=lambda item: item.stat().st_mtime):
        if total <= budget:
            break
        for path in groups[folder]:
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:
        return None
    try:
        return get_ffmpeg_exe()
    except OSError:
        return None


async def _ffmpeg_frame(ffmpeg: str, args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 25)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise FrameError("抽帧超时") from None
    if proc.returncode != 0 or not out.startswith(b"\xff\xd8"):
        raise FrameError("抽帧失败")
    return out


async def ffmpeg_grab_url(url: str, at_s: float, width: int) -> bytes:
    """按片内的秒数向播放地址要一帧。地址里的鉴权不写进日志。"""
    from koi_bilibili.backend import UA

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FrameError("没有 ffmpeg，抽不了画面")
    return await _ffmpeg_frame(
        ffmpeg,
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, at_s):.3f}",
            "-user_agent",
            UA,
            "-headers",
            "Referer: https://www.bilibili.com/\r\n",
            "-i",
            url,
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
    )


async def ffmpeg_grab(video: bytes, at_s: float, width: int, tmp: Path) -> bytes:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FrameError("没有 ffmpeg，抽不了画面")
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "segment.bin"
    src.write_bytes(video)
    try:
        return await _ffmpeg_frame(
            ffmpeg,
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, at_s):.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                f"scale={width}:-2",
                "-f",
                "image2",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
        )
    finally:
        src.unlink(missing_ok=True)
