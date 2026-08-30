from __future__ import annotations

import re


CATALOG_MARKER = "【RightCodes 生图接口知识】"
RIGHTCODES_DRAW_CATALOG_TEXT = """【RightCodes 生图接口知识】
资料来源：Right Code 官方文档 https://docs.right.codes/docs/rc_draw/ ，最近核对 2026-08-03。

基础信息：
- 绘图基础地址：https://www.rightapi.ai/draw
- 任务查询地址：https://www.rightapi.ai/v1/tasks/{task_id}，查询接口不带 /draw。
- 鉴权头：Authorization: Bearer sk-xxxxx
- 绘图请求使用异步流程：提交时带 async=true，取得 task_id 后轮询任务查询接口。

/v1/images/generations：
- POST https://www.rightapi.ai/draw/v1/images/generations
- model、prompt、async=true 必填；n、size、imageSize、image 可选。
- size 支持 1:1、16:9、9:16、4:3，或 1024x1024 这类像素串。
- imageSize 支持 1K、2K、4K。
- image 参考图应使用 data URL 数组。
- 示例 body：
{
  "model": "gpt-image-2",
  "prompt": "一只白猫",
  "n": 1,
  "size": "1:1",
  "imageSize": "1K",
  "async": true
}

/v1beta/models/{model}:generateContent：
- POST https://www.rightapi.ai/draw/v1beta/models/{model}:generateContent
- 请求体带 async=true，提示词放在 contents[].parts[].text。
- 比例和分辨率分别放在 generationConfig.imageConfig.aspectRatio 与 imageSize。
- 参考图使用 contents[].parts[].inline_data，包含 mime_type 和 base64 data。

/v1/tasks/{task_id}：
- GET https://www.rightapi.ai/v1/tasks/{task_id}
- queued / in_progress 继续轮询，completed 从 data 或 candidates 取图，failed 查看 error.message。
- Images 完成响应也可能直接返回 created 和 data，不带 status，此时直接从 data 取图。

已验证模型：
- gpt-image-2：$0.04/次，支持 1K。
- gpt-image-2-vip：$0.13/次，支持 1K。
- nano-banana-2-lite：$0.05/次，支持 1K。
- nano-banana-pro：$0.18/次，支持 1K、2K、4K。

回答约束：说明异步提交与任务轮询，不推荐旧域名或同步等待方式。JSON 可以保留缩进，但不要使用 Markdown 代码围栏。用户问 body 时直接给可复制 JSON。"""

_KEYWORDS = (
    "rightcodes",
    "right code",
    "right.codes",
    "docs.right.codes",
    "gpt-image-2",
    "gpt-image-2-vip",
    "nano-banana",
    "nano banana",
    "画图接口",
    "生图接口",
    "图片生成",
    "图像生成",
    "images/generations",
    "generatecontent",
    "v1/tasks",
    "1024x1024",
    "2048x2048",
    "4096x4096",
)


def should_inject_draw_catalog(query: str) -> bool:
    normalized = normalize_catalog_query(query)
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _KEYWORDS):
        return True
    if "size" in normalized and any(term in normalized for term in ("body", "json", "prompt", "model")):
        return True
    return bool(re.search(r"\b(?:1k|2k|4k)\b", normalized)) and any(
        term in normalized for term in ("生图", "画图", "图片", "图像", "draw")
    )


def normalize_catalog_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "").strip().lower())


def extract_current_query(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
            continue
        text = flatten_message_content(message.get("content"))
        target_matches = re.findall(r"^- 发言内容：(.+)$", text, flags=re.MULTILINE)
        if target_matches:
            return target_matches[-1].strip()
        if text.strip():
            return text.strip()
    return ""


def inject_catalog_into_messages(messages: object) -> list[dict[str, object]] | None:
    if not isinstance(messages, list):
        return None
    normalized = [dict(message) for message in messages if isinstance(message, dict)]
    if len(normalized) != len(messages):
        return None
    if any(CATALOG_MARKER in flatten_message_content(message.get("content")) for message in normalized):
        return normalized
    for message in normalized:
        if str(message.get("role") or "").lower() != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{content.rstrip()}\n\n{RIGHTCODES_DRAW_CATALOG_TEXT}"
            return normalized
    normalized.insert(0, {"role": "system", "content": RIGHTCODES_DRAW_CATALOG_TEXT})
    return normalized


def flatten_message_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and str(item.get("type") or "text").lower() == "text":
            parts.append(str(item.get("text") or ""))
    return "".join(parts)
