"""蓝铃的本机文件操作；个人工作区与蓝铃项目目录均可读写。"""

from __future__ import annotations

from hashlib import sha256
from itertools import islice
from pathlib import Path, PureWindowsPath
from typing import Any

import os
import re
import stat
import tempfile
import uuid


WORKSPACE_ROOT = Path("D:/project/lanling")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_WRITE_BYTES = 256 * 1024
MAX_COPY_BYTES = 80 * 1024 * 1024
MAX_RESULT_CHARS = 16000
PAGE_SIZE = 100
_RESERVED_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)(?: *\.|$)", re.IGNORECASE)


class Workspace:
    """提供本地读取及两处目录的完整文件变更；调用方先授权并串行调用。"""

    def __init__(self) -> None:
        """仅在实际运行机器的 Windows 路径语义下启用文件工具。"""
        if os.name != "nt":
            raise RuntimeError("蓝铃工作区文件工具需要在 Windows 中运行。")
        self.root = WORKSPACE_ROOT
        self.write_roots = (self.root, PROJECT_ROOT)
        for root in self.write_roots:
            self._reject_reparse(root)
            if not root.is_dir():
                raise ValueError(f"可写目录不存在：{root}，请先创建该目录。")

    def _local_path(self, value: str) -> Path:
        """相对路径从个人工作区解释，支持 WSL 盘符路径，拒绝 Windows 特殊路径。"""
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError("路径必须是 1–2048 个字符的本地路径。")
        if mounted := re.match(r"^/mnt/([A-Za-z])(?:/|$)", value):
            value = f"{mounted.group(1).upper()}:/{value[mounted.end():]}"
        windows = PureWindowsPath(value)
        if windows.drive and (len(windows.drive) != 2 or windows.drive[1] != ":" or not windows.drive[0].isascii() or not windows.drive[0].isalpha()):
            raise ValueError("仅支持本地盘符路径，不支持 UNC 或设备路径。")
        if (windows.drive or windows.root) and not windows.is_absolute():
            raise ValueError("带盘符的路径必须完整，例如 D:\\Documents\\notes.txt。")
        parts = windows.parts[1:] if windows.is_absolute() else windows.parts
        for part in parts:
            if part in (".", ".."):
                continue
            if (part.endswith((" ", ".")) or _RESERVED_NAME.match(part)
                    or any(ord(char) < 32 or char in '<>:"|?*' for char in part)):
                raise ValueError("路径包含设备名称、备用数据流或无效的 Windows 文件名。")
        path = Path(windows)
        return Path(os.path.abspath(path if path.is_absolute() else self.root / path))

    @staticmethod
    def _reject_reparse(path: Path) -> None:
        """检查已有的每一级路径，写入不经过符号链接、目录联接或其他重解析点。"""
        for item in reversed((path, *path.parents)):
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"写入路径经过链接或重解析点，已拒绝：{item}")

    def _write_path(self, value: str) -> Path:
        """两处根目录内不按文件用途限制写入；链接不得把目标转到其他目录。"""
        path = self._local_path(value)
        if not any(path.is_relative_to(root) for root in self.write_roots):
            raise ValueError("写入只能发生在个人工作区或蓝铃项目目录内：" + "、".join(map(str, self.write_roots)))
        self._reject_reparse(path)
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root.resolve(strict=True)) for root in self.write_roots):
            raise ValueError("实际写入位置超出已开放的两处目录，已拒绝。")
        return path

    def _read_path(self, value: str) -> Path:
        """解析读取目标；链接可以读取，但实际目标仍须使用普通盘符路径。"""
        path = self._local_path(value).resolve(strict=True)
        return self._local_path(str(path))

    @staticmethod
    def _read_bytes(path: Path, limit: int) -> bytes:
        """只读有限大小的普通文件，打开后再次核对类型与实际字节数。"""
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError(f"需要不超过 {limit} 字节的普通文件：{path}")
        with path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("读取对象不是普通文件。")
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"文件超出本次读取上限 {limit} 字节。")
        return data

    def list_directory(self, path: str, offset: int) -> dict[str, Any]:
        """按目录原生顺序分页，最多返回 100 项，不递归扫描电脑。"""
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("目录 offset 必须在 0–100000 之间。")
        target = self._read_path(path)
        entries = []
        with os.scandir(target) as directory:
            page = list(islice(directory, offset, offset + PAGE_SIZE + 1))
            for item in page[:PAGE_SIZE]:
                info = item.stat(follow_symlinks=False)
                linked = bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                entries.append({"name": item.name, "type": "link" if linked else "directory" if stat.S_ISDIR(info.st_mode) else "file", "bytes": info.st_size})
        return {"path": str(target), "entries": entries, "next_offset": offset + PAGE_SIZE if len(page) > PAGE_SIZE else None}

    def read_text(self, path: str, start_line: int, max_lines: int) -> dict[str, Any]:
        """读取 UTF-8、带 BOM 的 UTF-16 或 GB18030 文本，附覆盖校验所需摘要。"""
        if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 300:
            raise ValueError("start_line 从 1 开始，max_lines 必须在 1–300 之间。")
        target = self._read_path(path)
        data = self._read_bytes(target, MAX_TEXT_BYTES)
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            if b"\0" in data:
                raise ValueError("该文件不是支持的文本格式；可以复制到工作区保存原件。")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
        lines = text.splitlines()
        selected = []
        size = 0
        for line in lines[start_line - 1:start_line - 1 + max_lines]:
            extra = len(line) + (1 if selected else 0)
            if size + extra > MAX_RESULT_CHARS:
                if not selected:
                    raise ValueError("单行超过 16000 字符，本工具无法完整返回该行。")
                break
            selected.append(line)
            size += extra
        next_line = start_line + len(selected)
        return {"path": str(target), "sha256": sha256(data).hexdigest(), "total_lines": len(lines),
                "start_line": start_line, "next_line": next_line if next_line <= len(lines) else None,
                "text": "\n".join(selected)}

    def _check_version(self, target: Path, expected: str) -> None:
        """覆盖前检查原文件；首次创建必须留空，已有文件必须先读取得摘要。"""
        if target.exists():
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("覆盖已有文件前先读取它，并传入返回的 sha256。")
            if sha256(self._read_bytes(target, MAX_TEXT_BYTES)).hexdigest() != expected:
                raise ValueError("文件已变化，请重新读取后再保存。")
        elif expected:
            raise ValueError("原文件已不存在，请检查路径后按新文件保存。")

    def write_text(self, path: str, content: str, expected_sha256: str) -> dict[str, Any]:
        """原子提交 UTF-8 文本；替换目录项而不原地改写硬链接指向的内容。"""
        if not isinstance(content, str) or not isinstance(expected_sha256, str):
            raise ValueError("content 和 expected_sha256 必须是字符串。")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ValueError("单次写入不能超过 256 KiB，请拆成多个资料文件。")
        target = self._write_path(path)
        self._check_version(target, expected_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_path(str(target))
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".lanling-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._write_path(str(target))
            self._check_version(target, expected_sha256)
            if expected_sha256:
                os.replace(temporary, target)
            else:
                # Windows rename 在目标已存在时失败，首次创建不覆盖并发出现的文件。
                temporary.rename(target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"path": str(target), "bytes": len(data), "sha256": sha256(data).hexdigest()}

    def _copy_file(self, source: Path, target: Path) -> dict[str, Any]:
        """只读取得来源文件，分块复制到可写目录，再以不覆盖方式提交目标。"""
        if not source.is_file() or source.stat().st_size > MAX_COPY_BYTES:
            raise ValueError("复制来源必须是不超过 80 MiB 的普通文件。")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_path(str(target))
        total = 0
        digest = sha256()
        temporary: Path | None = None
        try:
            with source.open("rb") as incoming, tempfile.NamedTemporaryFile(dir=target.parent, prefix=".lanling-", delete=False) as output:
                temporary = Path(output.name)
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise ValueError("复制来源不是普通文件。")
                while block := incoming.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_COPY_BYTES:
                        raise ValueError("复制来源超出 80 MiB 上限。")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            self._write_path(str(target))
            temporary.rename(target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"path": str(target), "bytes": total, "sha256": digest.hexdigest()}

    def manage(self, action: str, path: str, destination: str) -> dict[str, Any]:
        """管理两处目录的文件；归档统一进入个人工作区 .trash，保留恢复路径。"""
        if action not in ("mkdir", "copy", "move", "trash"):
            raise ValueError("action 只能是 mkdir、copy、move 或 trash。")
        if action == "mkdir":
            target = self._write_path(path)
            target.mkdir(parents=True, exist_ok=True)
            self._write_path(str(target))
            return {"path": str(target), "type": "directory"}
        if action == "copy":
            source = self._read_path(path)
        else:
            source = self._write_path(path)
            if source in self.write_roots:
                raise ValueError("不能移动或归档已授权的根目录。")
            source.stat()
        if action == "trash":
            destination = str(self.root / ".trash" / uuid.uuid4().hex / source.name)
        target = self._write_path(destination)
        if target in self.write_roots or target.exists():
            raise ValueError("目标已存在，请指定个人工作区或蓝铃项目内的新路径。")
        if action == "copy":
            return self._copy_file(source, target)
        if target.is_relative_to(source):
            raise ValueError("不能把目录移到它自身内部。")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_path(str(source))
        self._write_path(str(target))
        source.rename(target)
        return {"from": str(source), "path": str(target), "action": action}
