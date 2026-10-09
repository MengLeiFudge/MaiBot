from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
import asyncio
import json

from aiohttp import ClientError, ClientSession, ClientTimeout


@dataclass(frozen=True, slots=True)
class SharedKnowledgeResult:
    """共享 DSP 检索服务的一次有界响应。"""

    available: bool
    matched: bool
    evidence: str = ""
    hit_count: int = 0
    error_type: str = ""


class SharedDspKnowledgeClient:
    """调用云栖持有的 localhost DSP 向量检索服务。"""

    def __init__(self, endpoint: str, timeout_seconds: float, max_evidence_chars: int) -> None:
        parsed = urlsplit(str(endpoint or "").strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path
        ):
            raise ValueError("共享 DSP 知识接口必须是无凭据的 localhost HTTP 地址")
        self.endpoint = parsed.geturl()
        self.timeout_seconds = max(1.0, min(30.0, float(timeout_seconds)))
        self.max_evidence_chars = max(800, min(12_000, int(max_evidence_chars)))

    async def search(self, query: str, group_id: str) -> SharedKnowledgeResult:
        """提交查询并验证服务端返回的证据边界。

        Args:
            query: 当前 Replyer 请求中的最后一条用户查询。
            group_id: 由结构化接收事件确认的 QQ 群号，私聊时为空。

        Returns:
            服务可用性、DSP 命中状态和可注入证据。
        """

        timeout = ClientTimeout(total=self.timeout_seconds)
        try:
            async with ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(
                    self.endpoint,
                    json={"query": query, "group_id": group_id},
                ) as response:
                    body = await response.content.read(64 * 1024 + 1)
                    if len(body) > 64 * 1024:
                        return SharedKnowledgeResult(
                            available=False,
                            matched=False,
                            error_type="ResponseTooLarge",
                        )
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, dict):
                        return SharedKnowledgeResult(
                            available=False,
                            matched=False,
                            error_type="InvalidPayload",
                        )
                    matched = bool(payload.get("matched", False))
                    if response.status != 200 or not bool(payload.get("ok", False)):
                        return SharedKnowledgeResult(
                            available=False,
                            matched=matched,
                            error_type=str(payload.get("error", "HttpError") or "HttpError"),
                        )
                    evidence = str(payload.get("evidence", "") or "").strip()
                    if len(evidence) > self.max_evidence_chars:
                        return SharedKnowledgeResult(
                            available=False,
                            matched=matched,
                            error_type="EvidenceTooLarge",
                        )
                    hit_count_raw = payload.get("hit_count", 0)
                    hit_count = int(hit_count_raw) if isinstance(hit_count_raw, int) else 0
                    return SharedKnowledgeResult(
                        available=True,
                        matched=matched,
                        evidence=evidence,
                        hit_count=max(0, hit_count),
                    )
        except asyncio.TimeoutError:
            return SharedKnowledgeResult(
                available=False,
                matched=False,
                error_type="TimeoutError",
            )
        except (ClientError, TypeError, ValueError) as exc:
            return SharedKnowledgeResult(
                available=False,
                matched=False,
                error_type=type(exc).__name__,
            )
