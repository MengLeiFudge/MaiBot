"""Read timestamped screen text from owned video samples with Windows OCR."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
import json
import os
import re


async def read_screen_subtitles(
    video: Path, folder: Path, ffmpeg: str, duration: float,
    command: Callable[..., Awaitable[bytes]],
) -> tuple[Path | None, str]:
    """Extract screen text without relying on small storyboard cells.

    Args:
        video: Validated video file belonging to the current message.
        folder: Owned temporary workspace, removed by the reader.
        ffmpeg: Existing local FFmpeg executable.
        duration: Video duration in seconds, bounded by the reader.
        command: Existing bounded subprocess runner with tree cancellation.

    Returns:
        An SRT file when text exists, plus timestamped text and coverage facts.

    Raises:
        RuntimeError: Windows OCR is unavailable or returned invalid results.
        TimeoutError: Frame extraction or OCR exceeded its subprocess budget.
    """
    if os.name != "nt":
        raise RuntimeError("画面字幕OCR需要Windows中文识别组件")
    frames = folder / "subtitle-frames"
    frames.mkdir()
    await command([
        ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
        "-i", str(video), "-an", "-sn", "-vf",
        "fps=2:start_time=0,scale=1280:720:force_original_aspect_ratio=decrease",
        "-frames:v", "1200", "-q:v", "3", str(frames / "frame-%05d.jpg"),
    ], timeout=30, folder=folder)
    output = folder / "screen-text.json"
    await command([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(Path(__file__).with_name("screen-subtitles.ps1")),
        "-FramesPath", str(frames), "-OutputPath", str(output), "-BudgetSeconds", "55",
    ], timeout=65, folder=folder)
    result = json.loads(output.read_text(encoding="utf-8-sig"))
    samples = result["frames"]
    if (not isinstance(samples, list) or not 0 < len(samples) <= 1200
            or result["processed"] != len(samples) or result["expected"] < len(samples)):
        raise RuntimeError("Windows OCR返回的采样覆盖信息无效")
    segments: list[dict] = []
    active: dict[str, dict] = {}
    for sample in samples:
        at = float(sample["at"])
        current: dict[str, dict] = {}
        for line in sample["lines"]:
            text = " ".join(str(line).split())
            text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff，。！？、；：])", "", text)
            key = re.sub(r"[\s，。！？、；：,.!?;:…·]+", "", text).casefold()
            if len(key) < 2 or key in current:
                continue
            previous = active.get(key)
            if previous is not None and at - previous["to"] <= 0.51:
                segment = previous
                segment["to"] = min(duration, at + 0.5)
            else:
                segment = {"from": at, "to": min(duration, at + 0.5), "text": text}
                segments.append(segment)
            current[key] = segment
        active = current
    segments.sort(key=lambda segment: segment["from"])
    coverage = min(duration, float(samples[-1]["at"]) + 0.5)
    complete = result["status"] == "complete" and result["processed"] == result["expected"]
    report = (
        f"画面文字OCR：{'采样完成' if complete else '采样未完成'}；间隔0.5秒；"
        f"覆盖0–{coverage:.2f}/{duration:.2f}秒，共{len(samples)}/{result['expected']}帧。"
        "以下文字来自原视频画面，可能包含歌词、片名和水印；请结合画面区分。"
        "OCR可能错字，短于采样间隔的文字可能遗漏，不能把未读到说成原视频模糊，也不能声称已完整听取音频。\n"
    )
    blocks: list[str] = []
    timeline: list[str] = []
    for segment in segments:
        times = []
        for value in (segment["from"], segment["to"]):
            milliseconds = max(0, round(value * 1000))
            times.append(f"{milliseconds // 3600000:02d}:{milliseconds // 60000 % 60:02d}:"
                         f"{milliseconds // 1000 % 60:02d},{milliseconds % 1000:03d}")
        entry = f"[{segment['from']:.1f}–{segment['to']:.1f}秒] {segment['text']}"
        if sum(map(len, timeline)) + len(entry) > 32000:
            report += "文字结果达到32000字符上限，后续部分未附入，不能声称已逐句读完。\n"
            break
        timeline.append(entry)
        blocks.append(f"{len(blocks) + 1}\n{times[0]} --> {times[1]}\n{segment['text']}\n")
    if not blocks:
        return None, report + "未取得可用画面文字。"
    subtitle = folder / "screen-text.srt"
    subtitle.write_text("\n".join(blocks), encoding="utf-8")
    return subtitle, report + "\n".join(timeline)
