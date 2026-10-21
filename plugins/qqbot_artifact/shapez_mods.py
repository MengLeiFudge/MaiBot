"""通过 NapCat 将一代模组的当前版本同步到指定群文件夹。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile

GROUP_ID = 1163635014
FOLDER_NAME = "s1-MOD-官方模组网站"
MOD_ROOT = Path("D:/project/shapez/ShapezToolkit/data/shapez1/mods" if os.name == "nt" else "/mnt/d/project/shapez/ShapezToolkit/data/shapez1/mods")


class ShapezModPublisher:
    """串行管理模组 ID 与群文件的对应关系，确认新版后才删除旧版。"""

    def __init__(self, call_api: Any, self_id: str, runtime_root: Path) -> None:
        """绑定已解包的协议调用、当前账号和不纳入版本控制的状态目录。"""
        self._call_api = call_api
        self._self_id = self_id
        self._root = runtime_root / "shapez1_mods"
        self._lock = asyncio.Lock()

    async def _call(self, action: str, **params: Any) -> dict[str, Any]:
        """限制协议调用时间，拒绝错误结果后再更新本地状态。"""
        result = await asyncio.wait_for(self._call_api(action, **params), timeout=900 if action == "upload_group_file" else 30)
        if result is None and action == "delete_group_file":
            return {}
        if not isinstance(result, dict):
            raise RuntimeError(f"{action}：OneBot 没有返回结果对象。")
        if result.get("status") == "failed" or result.get("retcode", 0) != 0:
            raise RuntimeError(f"{action}：OneBot 拒绝了操作。")
        data = result.get("data") if "status" in result or "retcode" in result else result
        return data if isinstance(data, dict) else {}

    async def _files(self, folder_id: str) -> dict[str, dict[str, Any]]:
        """读取指定文件夹，格式错误不能当作目录为空。"""
        result = await self._call("get_group_files_by_folder", group_id=GROUP_ID, folder_id=folder_id, file_count=10000)
        files = result.get("files")
        if not isinstance(files, list) or any(not isinstance(item, dict) or not item.get("file_id") for item in files):
            raise RuntimeError("OneBot 返回的群文件列表无效。")
        if len(files) >= 10000:
            raise RuntimeError("群文件列表达到读取上限，无法确认完整性。")
        indexed = {}
        for item in files:
            if any(item.get(key) is None for key in ("file_name", "file_size", "upload_time", "uploader")):
                raise RuntimeError("群文件身份信息不完整。")
            # NapCat 每次查询重新生成 file_id，该值仅用于本轮实际操作。
            identity = [str(item[key]) for key in ("file_name", "file_size", "upload_time", "uploader")]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            if key in indexed:
                raise RuntimeError("群文件存在无法唯一区分的同名记录，已停止同步，请先整理该目录。")
            indexed[key] = item
        return indexed

    def _confirm(self, state: dict[str, Any], path: Path, files: dict[str, dict[str, Any]]) -> None:
        """用成功上传回执和唯一新增条目确认新版，再将旧版加入待删除记录。"""
        pending = state.get("pending")
        if not pending:
            return
        if not pending.get("qq_file_id"):
            raise RuntimeError("上次上传未返回成功回执，已保留旧版并停止重传；请核对群文件及 pending 记录。")
        candidates = [(key, item) for key, item in files.items()
                      if key not in pending["baseline"] and str(item.get("uploader")) == self._self_id
                      and item.get("file_name") == pending["name"] and item.get("file_size") == pending["bytes"]]
        if len(candidates) != 1:
            raise RuntimeError("上传结果尚未唯一确认，已保留旧版并停止重传；稍后执行 mods publish，若仍失败请核对群文件。")
        key, _ = candidates[0]
        previous = state.get("current")
        if previous and previous["remote_key"] != key:
            state["obsolete"].append(previous)
        state["current"] = {field: pending[field] for field in ("file_id", "name", "sha256", "bytes", "qq_file_id")}
        state["current"]["remote_key"] = key
        state["pending"] = None
        _save_state(path, state)

    async def sync(self, payload: Any) -> dict[str, Any]:
        """同步单个当前或明确废弃模组；上传结果不确定时保留现场并报错。"""
        if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] <= 0:
            raise ValueError("需要正整数模组 ID。")
        retired = payload.get("retired", False)
        if type(retired) is not bool:
            raise ValueError("retired 必须是布尔值。")
        if not self._self_id.isdigit() or int(self._self_id) <= 0:
            raise RuntimeError("当前机器人账号不可用。")
        async with self._lock:
            root = await self._call("get_group_root_files", group_id=GROUP_ID, file_count=10000)
            folders = root.get("folders")
            if not isinstance(folders, list):
                raise RuntimeError("OneBot 没有返回群文件夹列表。")
            matches = [item for item in folders if isinstance(item, dict) and item.get("folder_name") == FOLDER_NAME]
            if len(matches) != 1 or not matches[0].get("folder_id"):
                raise RuntimeError(f"群 {GROUP_ID} 中无法唯一找到文件夹 {FOLDER_NAME}；请检查目录名称。")
            folder_id = str(matches[0]["folder_id"])
            scope = hashlib.sha256(f"{self._self_id}:{GROUP_ID}:{folder_id}".encode()).hexdigest()
            path = self._root / scope / f"{payload['id']}.json"
            state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "id": payload["id"], "group_id": GROUP_ID, "folder_id": folder_id,
                "current": None, "pending": None, "obsolete": [],
            }
            if (not isinstance(state, dict) or state.get("id") != payload["id"]
                    or state.get("folder_id") != folder_id or not isinstance(state.get("obsolete"), list)):
                raise ValueError(f"模组上传状态无效：{path}")
            files = await self._files(folder_id)
            self._confirm(state, path, files)
            result = {"id": payload["id"], "group_id": GROUP_ID, "folder_id": folder_id,
                      "uploaded": 0, "skipped": 0, "deleted": 0}
            current = state.get("current")
            if retired:
                if current:
                    state["obsolete"].append(current)
                    state["current"] = None
                    _save_state(path, state)
            else:
                package, content = await asyncio.to_thread(_read_package, payload)
                remote = files.get(current["remote_key"]) if current else None
                if (current and current["sha256"] == payload["sha256"] and current["name"] == payload["name"]
                        and remote and str(remote.get("uploader")) == self._self_id
                        and remote.get("file_name") == current["name"] and remote.get("file_size") == current["bytes"]):
                    current["file_id"] = payload["file_id"]
                    _save_state(path, state)
                    result["skipped"] = 1
                else:
                    # 先保存上传意图；超时不能自动触发第二次上传，防止重复群文件。
                    pending = {"file_id": payload["file_id"], "name": payload["name"],
                               "sha256": payload["sha256"], "bytes": len(content), "qq_file_id": "",
                               "source": str(package), "baseline": list(files)}
                    state["pending"] = pending
                    _save_state(path, state)
                    uploaded = await self._call("upload_group_file", group_id=GROUP_ID, folder_id=folder_id,
                                                name=payload["name"], file="base64://" + base64.b64encode(content).decode("ascii"))
                    pending["qq_file_id"] = str(uploaded.get("file_id") or "")
                    _save_state(path, state)
                    files = await self._files(folder_id)
                    self._confirm(state, path, files)
                    result["uploaded"] = 1
            # 仅删除本次文件夹中仍属于当前机器人的已跟踪文件。
            for old in list(state["obsolete"]):
                remote = files.get(old["remote_key"])
                if remote:
                    if str(remote.get("uploader")) != self._self_id or remote.get("file_name") != old["name"]:
                        raise RuntimeError("旧群文件归属与记录不符，已停止删除。")
                    params = {"group_id": GROUP_ID, "file_id": remote["file_id"]}
                    if "busid" in remote:
                        params["busid"] = remote["busid"]
                    await self._call("delete_group_file", **params)
                    files = await self._files(folder_id)
                    if old["remote_key"] in files:
                        raise RuntimeError("旧群文件删除尚未确认，已保留待删记录。")
                    result["deleted"] += 1
                state["obsolete"].remove(old)
                _save_state(path, state)
            return result


def _read_package(payload: dict[str, Any]) -> tuple[Path, bytes]:
    """读取工具箱目录中的 ZIP 为不可变上传内容，并核对清单哈希。"""
    if type(payload.get("file_id")) is not int or payload["file_id"] <= 0:
        raise ValueError("需要正整数 mod.io 文件 ID。")
    name = payload.get("name")
    if (not isinstance(name, str) or not name.endswith(".zip") or "_" not in name
            or len(name) > 200 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
        raise ValueError("上传名称必须为模组名_版本.zip。")
    digest = payload.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("需要小写 SHA-256。")
    raw = payload.get("path")
    if not isinstance(raw, str):
        raise ValueError("需要本地模组路径。")
    if os.name == "nt" and raw.startswith("/mnt/d/"):
        raw = "D:/" + raw[7:]
    elif os.name != "nt" and raw.replace("\\", "/").lower().startswith("d:/"):
        raw = "/mnt/d/" + raw.replace("\\", "/")[3:]
    path = Path(raw).resolve()
    if path.parent != (MOD_ROOT / "downloads").resolve() or path.suffix.lower() != ".zip":
        raise ValueError("模组必须位于 ShapezToolkit 的 downloads 目录。")
    with path.open("rb") as stream:
        content = stream.read(64 * 1024 * 1024 + 1)
    if len(content) > 64 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("模组超过 64 MiB 或 SHA-256 已改变，请重新生成清单。")
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError("模组不是有效 ZIP。")
    return path, content


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """原子保存上传意图与结果，写入中断时保留上一份完整状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
