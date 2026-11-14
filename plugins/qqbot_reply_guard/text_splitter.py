"""只调整公开正文的消息边界，保留全部文字、标点和顺序。"""

from __future__ import annotations

import re


# 完整链接、代码和成对引用中的句读不能成为消息边界。
_REPLY_TOKENS = re.compile(
    r"```.*?(?:```|\Z)|`[^`\r\n]*`|"
    r"https?://[^\s<>\"“”。！？，；]+|"
    r'"[^"\r\n]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|'
    r"\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|【[^【】]*】|\d+(?:[.,]\d+)+|"
    r"(?P<boundary>[。！？!?；;，,\r\n]+[ \t]*)",
    re.DOTALL,
)


def split_reply(text: str, max_parts: int) -> list[str]:
    """按自然句读分段并顺序合并到既有条数上限，不截断或改写正文。"""
    if max_parts < 1:
        raise ValueError("回复分段上限必须是正整数")
    if max_parts == 1 or (len(text) < 32 and "\n" not in text):
        return [text]

    sentences: list[str] = []
    start = 0
    for token in _REPLY_TOKENS.finditer(text):
        if token.lastgroup != "boundary":
            continue
        end = token.end()
        # 句首的空行留给下一段，不单独发送仅有空白的消息。
        if text[start:end].strip():
            sentences.append(text[start:end])
            start = end
    if start < len(text):
        tail = text[start:]
        if tail.strip() or not sentences:
            sentences.append(tail)
        else:
            sentences[-1] += tail
    if len(sentences) <= max_parts:
        return sentences or [text]

    parts: list[str] = []
    start = 0
    for index in range(max_parts):
        remaining_sentences = len(sentences) - start
        remaining_parts = max_parts - index
        count = (remaining_sentences + remaining_parts - 1) // remaining_parts
        parts.append("".join(sentences[start:start + count]))
        start += count
    return parts
