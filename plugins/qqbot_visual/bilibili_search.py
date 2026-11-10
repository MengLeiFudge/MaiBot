"""Search public Bilibili videos without sharing the bot's proxy settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import asyncio
import html
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp


SEARCH_ENDPOINT = "https://api.bilibili.com/x/web-interface/search/type"
SEARCH_TIMEOUT_SECONDS = 30
MAX_QUERY_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 240
_BVID_PATTERN = re.compile(r"(?<![A-Za-z0-9])BV[0-9A-Za-z]{10}(?![A-Za-z0-9])", re.IGNORECASE)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


class BilibiliSearchError(RuntimeError):
    """B站搜索未能返回可用结果。"""


@dataclass(frozen=True, slots=True)
class BilibiliVideoResult:
    """一个已清理、可交给模型继续阅读的视频候选。"""

    bvid: str
    title: str
    author: str
    duration: str
    url: str
    description: str
    play_count: int
    review_count: int
    pubdate: str

    def as_dict(self) -> dict[str, object]:
        """返回不含 B站原始 HTML 的结构化结果。"""
        return {
            "bvid": self.bvid,
            "title": self.title,
            "author": self.author,
            "duration": self.duration,
            "url": self.url,
            "description": self.description,
            "play_count": self.play_count,
            "review_count": self.review_count,
            "pubdate": self.pubdate,
        }


def _canonical_bvid(value: str) -> str:
    """只规范 BV 前缀，保留正文的大小写；BVID 正文区分大小写。"""
    return "BV" + value[2:]


def normalize_bilibili_reference(reference: str) -> str:
    """把裸 BV 号变成可交给 yt-dlp 的 B站页面 URL。"""
    normalized = html.unescape(reference.replace("\\/", "/")).strip()
    if not normalized:
        raise BilibiliSearchError("B站视频地址不能为空，请提供视频链接或 BV 号")
    match = _BVID_PATTERN.fullmatch(normalized)
    if match is not None:
        return f"https://www.bilibili.com/video/{_canonical_bvid(match.group(0))}"
    return normalized


def is_bilibili_url(url: str) -> bool:
    """判断地址是否属于允许交给视频读取器的B站域名。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in (
        "bilibili.com", "b23.tv", "bili2233.cn",
    ))


def _clean_text(value: Any, *, limit: int) -> str:
    """移除搜索接口返回的关键词高亮标签并限制外部文本长度。"""
    text = html.unescape(str(value or ""))
    text = _HTML_TAG_PATTERN.sub("", text)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _as_non_negative_int(value: Any) -> int:
    """把接口中的播放量等字段转换成稳定的非负整数。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _format_pubdate(value: Any) -> str:
    """把 Unix 时间戳转换成简短日期；异常数据保持为空。"""
    try:
        timestamp = int(value or 0)
        if timestamp <= 0:
            return ""
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _result_from_payload(item: Any) -> BilibiliVideoResult | None:
    """校验单条搜索结果，缺少稳定 BV 号的项目直接跳过。"""
    if not isinstance(item, dict):
        return None
    bvid = str(item.get("bvid") or "").strip()
    if _BVID_PATTERN.fullmatch(bvid) is None:
        arcurl = str(item.get("arcurl") or "")
        match = _BVID_PATTERN.search(arcurl)
        if match is None:
            return None
        bvid = match.group(0)
    bvid = _canonical_bvid(bvid)
    return BilibiliVideoResult(
        bvid=bvid,
        title=_clean_text(item.get("title"), limit=160) or bvid,
        author=_clean_text(item.get("author") or item.get("uname"), limit=80) or "未知作者",
        duration=_clean_text(item.get("duration"), limit=20) or "未知",
        url=f"https://www.bilibili.com/video/{bvid}",
        description=_clean_text(item.get("description"), limit=MAX_DESCRIPTION_LENGTH),
        play_count=_as_non_negative_int(item.get("play")),
        review_count=_as_non_negative_int(item.get("review")),
        pubdate=_format_pubdate(item.get("pubdate")),
    )


class BilibiliSearch:
    """调用 B站公开视频搜索接口；请求固定直连，不采纳本机代理环境变量。"""

    async def search(self, query: str, *, page: int = 1, limit: int = 5) -> list[BilibiliVideoResult]:
        """按相关度搜索公开视频，并只保留有稳定 BV 号的结果。"""
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query or len(normalized_query) > MAX_QUERY_LENGTH:
            raise BilibiliSearchError(f"搜索关键词不能为空且最多{MAX_QUERY_LENGTH}个字符")
        if type(page) is not int or not 1 <= page <= 10:
            raise BilibiliSearchError("搜索页码必须是1到10之间的整数")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise BilibiliSearchError("搜索结果数量必须是1到10之间的整数")

        params = urlencode({
            "search_type": "video",
            "keyword": normalized_query,
            "page": page,
            "page_size": limit,
        })
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://search.bilibili.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        }
        timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_SECONDS, connect=10, sock_read=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=False) as session:
                async with session.get(f"{SEARCH_ENDPOINT}?{params}", allow_redirects=False) as response:
                    if response.status != 200:
                        raise BilibiliSearchError(f"B站搜索接口返回HTTP {response.status}")
                    payload = await response.json(content_type=None)
        except BilibiliSearchError:
            raise
        except asyncio.TimeoutError as exc:
            raise BilibiliSearchError("B站搜索超时，请稍后重试") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise BilibiliSearchError("B站搜索连接失败，请稍后重试") from exc

        if not isinstance(payload, dict):
            raise BilibiliSearchError("B站搜索返回格式无效")
        code = payload.get("code")
        if code != 0:
            message = _clean_text(payload.get("message"), limit=120)
            if code == -412:
                raise BilibiliSearchError("B站暂时拒绝了搜索请求，请稍后重试")
            raise BilibiliSearchError(message or f"B站搜索未成功（错误码{code}）")
        data = payload.get("data")
        raw_results = data.get("result") if isinstance(data, dict) else None
        if raw_results is None:
            return []
        if not isinstance(raw_results, list):
            raise BilibiliSearchError("B站搜索结果格式无效")
        results: list[BilibiliVideoResult] = []
        for item in raw_results:
            result = _result_from_payload(item)
            if result is not None:
                results.append(result)
            if len(results) >= limit:
                break
        return results
