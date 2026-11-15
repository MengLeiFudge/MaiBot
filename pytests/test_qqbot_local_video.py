"""隔离验证长视频先浏览整段时间轴，再按问题精读；不连接模型或QQ。"""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import asyncio
import json

from maibot_sdk.context import PluginContext
from PIL import Image

from plugins.qqbot_visual import video_reader as module
from plugins.qqbot_visual.plugin import QQBotVisualPlugin
from plugins.qqbot_visual.video_overview import read_existing_subtitles, read_overview
from plugins.qqbot_visual.video_reader import VideoError, VideoOverview, VideoReader, VideoSource
from pytests.test_qqbot_public_replies import BOT, SESSION, PrivateHost


def picture(format='JPEG'):
    stream = BytesIO()
    Image.new('RGB', (32, 18), 'blue').save(stream, format)
    return stream.getvalue()


class VideoWorkflowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / 'movie.mp4'
        self.video.write_bytes(b'local-file')
        self.reader = VideoReader(self.root / 'work')
        self.addAsyncCleanup(self.reader.close)
        self.duration = 6786.0
        self.reader._download = AsyncMock(return_value=(self.video, 'test-video', None))
        self.metadata = {'format': {'duration': self.duration}, 'streams': [
            {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080}]}

    async def command(self, arguments, **kwargs):
        if '-show_entries' in arguments:
            return json.dumps(self.metadata).encode()
        request = Path(arguments[-1])
        options = json.loads(request.read_text('utf-8'))
        folder = request.parent
        (folder / 'frame.png').write_bytes(picture('PNG'))
        (folder / 'result.json').write_text(json.dumps({
            'images': ['frame.png'], 'manifest': '真实分镜', 'evidence': {'cells_total': 1}, 'transcript': 'none'}))
        self.assertEqual(options['from'], 1800)
        self.assertEqual(options['to'], 1815)
        return b''

    async def test_long_auto_reads_full_timeline_without_dense_ocr(self):
        overview = AsyncMock(return_value=((picture(),), {'cells_total': 1, 'samples': [], 'sampling_complete': True}))
        subtitles = AsyncMock(return_value=('已有字幕', '保留完整字幕内容'))
        with patch.object(module, '_command', self.command), patch.object(module, 'read_overview', overview), \
                patch.object(module, 'read_existing_subtitles', subtitles), patch.object(module, 'read_screen_subtitles', AsyncMock()) as ocr:
            result = await self.reader.analyze(VideoSource(str(self.video), local_path=self.video, mode='auto'))
        self.assertEqual((result.mode, result.mime_type), ('overview', 'image/jpeg'))
        self.assertEqual((result.start_seconds, result.end_seconds), (0, self.duration))
        self.assertEqual(overview.await_args.args[3:5], (0, self.duration))
        self.assertIn('保留完整字幕内容', result.prompt)
        ocr.assert_not_awaited()

    async def test_explicit_detail_keeps_existing_storyboard_and_ocr(self):
        with patch.object(module, '_command', self.command), \
                patch.object(module, 'read_overview', AsyncMock()) as overview, \
                patch.object(module, 'read_screen_subtitles', AsyncMock(return_value=(None, '没有画面字幕'))) as ocr:
            result = await self.reader.analyze(VideoSource(str(self.video), local_path=self.video,
                                                          mode='detail', start_seconds=1800, end_seconds=1815))
        self.assertEqual((result.mode, result.mime_type), ('detail', 'image/png'))
        self.assertEqual((result.start_seconds, result.end_seconds), (1800, 1815))
        overview.assert_not_awaited()
        self.assertEqual(ocr.await_args.kwargs['start_seconds'], 1800)

    async def test_auto_explicit_long_range_is_an_overview(self):
        overview = AsyncMock(return_value=((picture(),), {'cells_total': 1, 'samples': []}))
        with patch.object(module, '_command', self.command), patch.object(module, 'read_overview', overview), \
                patch.object(module, 'read_existing_subtitles', AsyncMock(return_value=('none', ''))):
            result = await self.reader.analyze(VideoSource(str(self.video), local_path=self.video, mode='auto', end_seconds=1800))
        self.assertEqual(result.mode, 'overview')
        self.assertEqual(result.end_seconds, 1800)

    async def test_cache_separates_mode_and_invalidates_changed_file(self):
        self.reader._analyze = AsyncMock(return_value=VideoOverview((picture(),), '', {'cells_total': 1}, 'test', self.duration, 'none'))
        source = VideoSource(str(self.video), local_path=self.video, mode='auto')
        await self.reader.analyze(source)
        await self.reader.analyze(source)
        self.assertEqual(self.reader._analyze.await_count, 1)
        await self.reader.analyze(VideoSource(str(self.video), local_path=self.video, mode='overview'))
        self.video.write_bytes(b'changed-original-file')
        await self.reader.analyze(source)
        self.assertEqual(self.reader._analyze.await_count, 3)

    async def test_invalid_modes_times_and_oversized_detail_are_explicit_errors(self):
        for values in [{'mode': 'unknown'}, {'start_seconds': float('nan')},
                       {'start_seconds': 10, 'end_seconds': 5}, {'mode': 'detail', 'end_seconds': 601}]:
            with self.subTest(values=values), self.assertRaises(VideoError):
                await self.reader.analyze(VideoSource(str(self.video), local_path=self.video, **values))


class TimelineSamplingTests(IsolatedAsyncioTestCase):
    async def test_samples_cover_start_middle_end_and_keep_a_missing_frame_explicit(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            async def command(arguments, **kwargs):
                target = Path(arguments[-1])
                if target.stem == 'overview-032':
                    raise OSError('isolated decode failure')
                target.write_bytes(picture())
                return b''
            images, evidence = await read_overview(folder / 'movie.mp4', folder, 'ffmpeg', 0, 6786, command)
        self.assertEqual(len(images), 63)
        self.assertEqual(evidence['samples'][0]['at_seconds'], 0)
        self.assertEqual(evidence['samples'][-1]['at_seconds'], 6785)
        self.assertEqual(evidence['samples'][32]['status'], 'unread')
        self.assertNotIn('image_index', evidence['samples'][32])
        self.assertFalse(evidence['sampling_complete'])
        self.assertEqual(evidence['samples'][-1]['image_index'], 63)

    async def test_cancellation_joins_all_frame_workers(self):
        running = 0
        started = asyncio.Event()
        async def command(arguments, **kwargs):
            nonlocal running
            running += 1
            started.set()
            try:
                await asyncio.Future()
            finally:
                running -= 1
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            task = asyncio.create_task(read_overview(folder / 'movie.mp4', folder, 'ffmpeg', 0, 6786, command))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(running, 0)

    async def test_existing_sidecar_is_read_without_ocr_or_truncation(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            content = '1\n00:00:01,000 --> 00:00:03,000\n' + '完整的字幕内容' * 6000
            (folder / 'movie.srt').write_text(content, encoding='utf-8-sig', newline='\n')
            command = AsyncMock()
            source, text = await read_existing_subtitles(folder / 'movie.mp4', folder, 'ffmpeg', [], command)
        self.assertEqual(text, content)
        self.assertIn('.srt', source)
        command.assert_not_awaited()

    async def test_embedded_text_subtitle_is_extracted_and_missing_audio_is_honest(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            async def command(arguments, **kwargs):
                self.assertEqual(arguments[arguments.index('-map') + 1], '0:2')
                Path(arguments[-1]).write_text('内嵌字幕原文', encoding='utf-8')
                return b''
            source, text = await read_existing_subtitles(folder / 'movie.mp4', folder, 'ffmpeg',
                                                        [{'index': 2, 'codec_type': 'subtitle', 'codec_name': 'subrip'}], command)
            self.assertEqual(text, '内嵌字幕原文')
            empty_source, empty_text = await read_existing_subtitles(folder / 'movie.mp4', folder, 'ffmpeg', [], AsyncMock())
        self.assertIn('内嵌', source)
        self.assertIn('无文字字幕', empty_source)
        self.assertIn('尚未转写音频', empty_text)


class LocalVideoAccessTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        host = PrivateHost()
        self.plugin = QQBotVisualPlugin()
        self.plugin.set_plugin_config({'plugin': {'enabled': True, 'config_version': '0.2.0'}})
        self.plugin._set_context(PluginContext('mlj.qqbot-visual', host.rpc))
        self.plugin._bot_id = BOT
        self.reader = SimpleNamespace(analyze=AsyncMock(return_value=VideoOverview((picture(),), '时间清单',
            {'cells_total': 1, 'samples': [], 'sampling_complete': True}, 'test', 6786, 'none', '', 0, 6786, 'overview', 'image/jpeg')),
            close=AsyncMock())
        self.plugin._video_reader = self.reader
        self.addAsyncCleanup(self.plugin.on_unload)

    async def test_owner_default_gets_whole_overview_and_native_jpeg(self):
        result = await self.plugin.watch_local_video(path='D:/Videos/movie.mp4', stream_id=SESSION)
        self.assertTrue(result['success'])
        self.assertEqual(result['video']['mode'], 'overview')
        self.assertIsNone(result['video']['next_start_seconds'])
        self.assertEqual(result['content_items'][0]['mime_type'], 'image/jpeg')
        self.assertEqual(self.reader.analyze.await_args.args[0].mode, 'auto')

    async def test_other_chat_and_group_never_read_a_local_file(self):
        for context in [{'stream_id': 'other-chat'}, {'stream_id': SESSION, 'group_id': '123'}]:
            result = await self.plugin.watch_local_video(path='D:/Videos/movie.mp4', **context)
            self.assertFalse(result['success'])
        self.reader.analyze.assert_not_awaited()
