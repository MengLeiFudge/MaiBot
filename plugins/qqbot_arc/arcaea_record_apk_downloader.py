from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Callable
from urllib.parse import urlsplit

import os
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


@dataclass(frozen=True, slots=True)
class ApkDownloadResult:
    version: str
    path: Path
    output: str


class ApkDownloadCancelled(RuntimeError):
    pass


class ArcaeaRecordApkDownloader:
    """Run the vendored arcaeaRecord command and atomically publish its APK."""

    def __init__(
        self,
        project_root: Path,
        target_dir: Path,
        *,
        maven_command: str = "",
        java_home: str = "",
        compile_timeout_seconds: float = 180.0,
        download_timeout_seconds: float = 900.0,
    ) -> None:
        self.project_root = Path(project_root)
        self.target_dir = Path(target_dir)
        self.maven_command = maven_command
        self.java_home = java_home
        self.compile_timeout_seconds = compile_timeout_seconds
        self.download_timeout_seconds = download_timeout_seconds

    def download_latest_apk(
        self,
        version: str,
        progress_callback: Callable[[str], None] | None = None,
        cancel_event: Event | None = None,
    ) -> ApkDownloadResult:
        if not self.project_root.is_dir():
            raise RuntimeError(f"arcaeaRecord 项目目录不存在：{self.project_root}")
        cancel = cancel_event or Event()
        env = self._build_env()
        self.target_dir.mkdir(parents=True, exist_ok=True)
        staging = self.target_dir / f".qqbot-arc-apk-{uuid.uuid4().hex}.tmp"
        staging.mkdir()
        try:
            maven_settings = self._write_maven_proxy_settings(staging, env)
            compile_command = [self._resolve_maven_command()]
            if maven_settings is not None:
                compile_command.extend(["-s", str(maven_settings)])
            compile_command.extend(["-q", "-DskipTests", "compile"])
            self._run_streaming(
                compile_command,
                timeout=self.compile_timeout_seconds,
                env=env,
                cancel_event=cancel,
            )
            output = self._run_streaming(
                [
                    self._resolve_java_command(),
                    "-cp",
                    str(self.project_root / "target" / "classes"),
                    "arc.record.Main",
                    "6",
                    str(staging),
                ],
                timeout=self.download_timeout_seconds,
                env=env,
                cancel_event=cancel,
                progress_callback=progress_callback,
            )
            downloaded = self._parse_downloaded_path(output)
            if not downloaded.is_absolute():
                downloaded = (self.project_root / downloaded).resolve()
            else:
                downloaded = downloaded.resolve()
            staging_root = staging.resolve()
            if downloaded.parent != staging_root or downloaded.suffix.lower() != ".apk":
                raise RuntimeError("arcaeaRecord 返回了临时下载目录外的无效 APK 路径")
            if not downloaded.is_file() or downloaded.stat().st_size <= 0:
                raise RuntimeError("arcaeaRecord 下载结果不是有效的非空 APK")
            destination = self.target_dir / downloaded.name
            os.replace(downloaded, destination)
            return ApkDownloadResult(version=version, path=destination, output=output)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        java_home = self.java_home or env.get("JAVA_HOME", "")
        if java_home:
            env["JAVA_HOME"] = java_home
            path_key = "Path" if os.name == "nt" else "PATH"
            env[path_key] = str(Path(java_home) / "bin") + os.pathsep + env.get(path_key, "")
        proxy_options = self._java_proxy_options(env)
        if proxy_options:
            env["MAVEN_OPTS"] = self._append_options(env.get("MAVEN_OPTS", ""), proxy_options)
            env["JAVA_TOOL_OPTIONS"] = self._append_options(env.get("JAVA_TOOL_OPTIONS", ""), proxy_options)
        return env

    @staticmethod
    def _proxy_address(env: dict[str, str]) -> tuple[str, int] | None:
        proxy_url = next(
            (
                str(env.get(name) or "").strip()
                for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
                if str(env.get(name) or "").strip()
            ),
            "",
        )
        if not proxy_url:
            return None
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    @classmethod
    def _java_proxy_options(cls, env: dict[str, str]) -> str:
        address = cls._proxy_address(env)
        if address is None:
            return ""
        host, port = address
        return (
            f"-Dhttp.proxyHost={host} -Dhttp.proxyPort={port} "
            f"-Dhttps.proxyHost={host} -Dhttps.proxyPort={port}"
        )

    @classmethod
    def _write_maven_proxy_settings(cls, staging: Path, env: dict[str, str]) -> Path | None:
        address = cls._proxy_address(env)
        if address is None:
            return None
        host, port = address
        settings = ET.Element("settings")
        proxies = ET.SubElement(settings, "proxies")
        non_proxy_hosts = cls._maven_non_proxy_hosts(env)
        for protocol in ("http", "https"):
            proxy = ET.SubElement(proxies, "proxy")
            ET.SubElement(proxy, "id").text = f"qqbot-{protocol}-proxy"
            ET.SubElement(proxy, "active").text = "true"
            ET.SubElement(proxy, "protocol").text = protocol
            ET.SubElement(proxy, "host").text = host
            ET.SubElement(proxy, "port").text = str(port)
            if non_proxy_hosts:
                ET.SubElement(proxy, "nonProxyHosts").text = non_proxy_hosts
        settings_path = staging / "maven-settings.xml"
        ET.ElementTree(settings).write(settings_path, encoding="utf-8", xml_declaration=True)
        return settings_path

    @staticmethod
    def _maven_non_proxy_hosts(env: dict[str, str]) -> str:
        raw = str(env.get("NO_PROXY") or env.get("no_proxy") or "").strip()
        return "|".join(item.strip() for item in raw.split(",") if item.strip())

    @staticmethod
    def _append_options(existing: str, addition: str) -> str:
        return " ".join(part for part in (existing.strip(), addition.strip()) if part)

    def _resolve_maven_command(self) -> str:
        return self.maven_command or shutil.which("mvn.cmd") or shutil.which("mvn") or "mvn"

    def _resolve_java_command(self) -> str:
        java_home = self.java_home or os.environ.get("JAVA_HOME", "")
        if java_home:
            return str(Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java"))
        return shutil.which("java") or "java"

    def _run_streaming(
        self,
        command: list[str],
        *,
        timeout: float,
        env: dict[str, str],
        cancel_event: Event,
        progress_callback: Callable[[str], None] | None = None,
    ) -> str:
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        lines: Queue[str | None] = Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        Thread(target=read_output, name="qqbot-arc-apk-output", daemon=True).start()
        deadline = time.monotonic() + timeout
        output: list[str] = []
        stream_closed = False
        try:
            while process.poll() is None or not stream_closed:
                if cancel_event.is_set():
                    raise ApkDownloadCancelled("Arcaea 安装包下载已取消")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"arcaeaRecord 执行超时（{timeout:g} 秒）")
                try:
                    line = lines.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                except Empty:
                    continue
                if line is None:
                    stream_closed = True
                    continue
                output.append(line)
                progress = self._parse_progress_message(line)
                if progress is not None and progress_callback is not None:
                    progress_callback(progress)
        except (ApkDownloadCancelled, RuntimeError):
            self._terminate(process)
            raise
        finally:
            process.stdout.close()
        return_code = process.wait(timeout=5)
        combined = "".join(output)
        if return_code != 0:
            detail = combined.strip()[-2000:]
            raise RuntimeError(
                "arcaeaRecord 执行失败：" + " ".join(command) + ("\n" + detail if detail else "")
            )
        return combined

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _parse_progress_message(line: str) -> str | None:
        parts = line.strip().split()
        if len(parts) != 4 or parts[0] != "APK_PROGRESS":
            return None
        return (
            f"{parts[1]}%（{ArcaeaRecordApkDownloader._format_bytes(parts[2])} / "
            f"{ArcaeaRecordApkDownloader._format_bytes(parts[3])}）"
        )

    @staticmethod
    def _format_bytes(raw_value: str) -> str:
        try:
            value = int(raw_value)
        except ValueError:
            return raw_value
        if value < 0:
            return "未知大小"
        mib = value / 1024 / 1024
        return f"{mib / 1024:.2f} GiB" if mib >= 1024 else f"{mib:.1f} MiB"

    @staticmethod
    def _parse_downloaded_path(output: str) -> Path:
        for line in reversed(output.splitlines()):
            if line.strip().startswith("APK_DOWNLOADED "):
                return Path(line.strip().removeprefix("APK_DOWNLOADED ").strip())
        raise RuntimeError("arcaeaRecord 未输出 APK_DOWNLOADED 路径")
