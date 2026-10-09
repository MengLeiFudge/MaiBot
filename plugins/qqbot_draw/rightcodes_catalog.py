from __future__ import annotations

import re


CATALOG_MARKER = "【RightCodes 生图接口知识】"
RIGHTCODES_DRAW_CATALOG_TEXT = """【RightCodes 生图接口知识】

当前 bot 生图功能：
- 唯一可用模型为 gpt-image-2.5，每张 40 积分，对应人民币 0.04 元。
- 标准指令：文生图 提示词；图生图 提示词（同条附图或引用图片）；头像生图 [@某人] 提示词。无头像目标时使用发送者头像，真实艾特须紧跟头像生图。生图模型 / 生图价格；查看积分；积分排行。
- 以“生成”开头即直接调用生图，例如“生成一只白猫的图片”或“生成一只白猫”；允许不带空格、多行提示词，不要求图片后缀，群聊无需 @，按同一价格扣积分。只有“生成”而无提示词时不扣积分，提示补充内容。
- 旧用户保存的模型自动归一为 gpt-image-2.5，积分余额不变。
- 群消息继续累计积分；从第一张开始扣费，失败或超时原额退款。
- 普通标准指令原样提交提示词，不调用聊天模型改写。图生图使用单张原图，当前附件优先于引用图片；原图获取失败不扣积分。普通旧生成/棉花糖生图变体保留条件改写与最多3张参考图。
- 生成豆豆眼头像：固定将发送者本人的头像转为豆豆眼，直接使用完整固定预设。原有生图入口及自然语言改图请求含“豆豆眼”并指定头像或图片时，也完全换用该预设，不改写、不拼接。自然语言仍须识别为实际绘图意图，来源不明确先询问。
- 所有开工通知都会明确提示词来源：用户话语（无预设），或豆豆眼预设（完整固定原文）。
- 私聊或明确唤醒后的自然语言请求（如“画只猫”“帮我把我的头像改成水彩”）会先解析为文生图/图生图；完整请求直接开工并回显提示词和来源，歧义先询问，不扣积分。
- 普通聊天回复只能介绍用法或引导明确指令，不得声称已开始绘图、已扣分或已交付；实际执行由生图插件负责。
- 成品统一先保存再发送；保存失败仍尝试交付，并提示未保存。保存和交付均失败时退积分，不重复生成。

仅在用户询问接口时说明以下技术信息：
- bot 使用本机 CPA 的 OpenAI Images 接口，API 根地址为 http://127.0.0.1:8317/v1。
- 鉴权使用 CPA 客户端 API Key；HC/RC 渠道选择与优先级由 CPA 配置管理，各渠道的 gpt-image-2.5 模型记录需要 image: true 才能使用 Images 接口。
- 文生图：POST /images/generations，JSON body 示例：
{
  "model": "gpt-image-2.5",
  "prompt": "一只白猫",
  "n": 1,
  "size": "1024x1024",
  "response_format": "b64_json",
  "output_format": "png"
}
- 参考图：POST /images/edits，multipart/form-data 单张使用 image 文件字段，多张使用 image[] 文件字段，其余文本参数与文生图相同。
- 同一次 CPA 请求等待最终图片。RC 官方绘图接口另使用 /draw/v1/images/generations、JSON image 数组、async=true 和任务轮询；不能将该协议与当前 CPA Images 接口混用。
- 从返回 data[].b64_json 或 data[].url 取得图片；默认尺寸为 1024x1024，不承诺其它尺寸。

回答约束：模型、价格和用法问题只回答当前 bot 的功能，说明价格时使用人民币“元”。接口 body 问题直接给可复制 JSON，保留缩进，不使用 Markdown 代码围栏。"""

_KEYWORDS = (
    "rightcodes",
    "right code",
    "right.codes",
    "docs.right.codes",
    "gpt-image",
    "nano-banana",
    "nano banana",
    "画图接口",
    "画", "生图", "绘图", "头像", "改图",
    "生图接口",
    "图片生成",
    "图像生成",
    "images/generations",
    "images/edits",
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
