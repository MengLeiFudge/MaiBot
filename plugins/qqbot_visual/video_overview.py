"""先浏览整段时间轴，保留真实原图供主模型定位，再按需精读。"""
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import asyncio
import math


async def read_overview(
    video: Path, folder: Path, ffmpeg: str, start: float, end: float,
    command: Callable[..., Awaitable[bytes]],
) -> tuple[tuple[bytes, ...], dict[str, Any]]:
    """直接跳到全片各采样时刻，不逐帧扫描，也不把多张原图压成小格子。"""
    count = min(64, max(2, math.ceil((end - start) / 10) + 1))
    last = max(start, end - min(1.0, (end - start) / 4))
    times = [start + (last - start) * index / (count - 1) for index in range(count)]
    workers = asyncio.Semaphore(4)

    async def frame(index: int, at: float) -> tuple[bytes | None, dict[str, Any]]:
        path = folder / f'overview-{index:03}.jpg'
        try:
            async with workers:
                await command([
                    ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-threads', '1',
                    '-protocol_whitelist', 'file,pipe', '-ss', f'{at:.6f}', '-i', str(video),
                    '-an', '-sn', '-frames:v', '1', '-vf',
                    'scale=1920:1080:force_original_aspect_ratio=decrease',
                    '-q:v', '2', '-threads', '1', str(path),
                ], timeout=15, folder=folder)
                data = path.read_bytes()
                if not data.startswith(b'\xff\xd8\xff'):
                    raise ValueError('未得到有效JPEG画面')
                return data, {'at_seconds': round(at, 3), 'status': 'read'}
        except Exception as exc:
            return None, {'at_seconds': round(at, 3), 'status': 'unread', 'error_type': type(exc).__name__}

    tasks = [asyncio.create_task(frame(index, at)) for index, at in enumerate(times)]
    try:
        async with asyncio.timeout(60):
            frames = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    images, samples = [], []
    for data, sample in frames:
        if data is not None:
            images.append(data)
            sample['image_index'] = len(images)
        samples.append(sample)
    if not images:
        raise RuntimeError('整片概览没有取得可读画面，不能当作已经看过')
    return tuple(images), {'cells_total': len(images), 'mode': 'overview', 'samples': samples,
                           'requested_start_seconds': start, 'requested_end_seconds': end,
                           'sampling_complete': all(sample['status'] == 'read' for sample in samples)}


async def read_existing_subtitles(
    video: Path, folder: Path, ffmpeg: str, streams: list[Mapping[str, Any]],
    command: Callable[..., Awaitable[bytes]],
) -> tuple[str, str]:
    """先读已有字幕；无字幕时明确说明，密集OCR留给模型选择的细读片段。"""
    for suffix in ('.srt', '.vtt', '.ass', '.ssa'):
        sidecar = video.with_suffix(suffix)
        if sidecar.is_file():
            try:
                raw = await asyncio.to_thread(sidecar.read_bytes)
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'gb18030')
                if text.strip():
                    return f'同名字幕文件{suffix}', text
            except (OSError, UnicodeError) as exc:
                return '字幕未读取', f'现成字幕读取失败：{type(exc).__name__}；需要时细读对应片段。'
    text_codecs = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
    subtitles = [stream for stream in streams if stream.get('codec_type') == 'subtitle']
    for stream in subtitles:
        if stream.get('codec_name') not in text_codecs:
            continue
        try:
            output = folder / 'overview-subtitles.srt'
            await command([ffmpeg, '-hide_banner', '-v', 'error', '-nostdin',
                           '-protocol_whitelist', 'file,pipe', '-i', str(video),
                           '-map', f'0:{int(stream["index"])}', '-c:s', 'srt', str(output)], timeout=10, folder=folder)
            text = output.read_text('utf-8-sig')
            if text.strip():
                return '内嵌文字字幕', text
        except Exception as exc:
            return '字幕未读取', f'内嵌字幕读取失败：{type(exc).__name__}；需要时细读对应片段。'
    kind = '只有图像字幕轨' if subtitles else '没有现成文字字幕'
    return '无文字字幕', f'{kind}；概览先提供全片画面，细读需要的时间段时再做画面文字OCR。尚未转写音频，不能声称听见对白。'
