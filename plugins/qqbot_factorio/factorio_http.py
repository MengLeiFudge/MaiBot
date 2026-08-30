from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from http.client import HTTPResponse
from json import JSONDecodeError, loads
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


LATEST_RELEASES_URL = "https://factorio.com/api/latest-releases"
DOWNLOAD_URL_TEMPLATE = "https://www.factorio.com/get-download/{version}/expansion/win64"
_USER_AGENT = "qqbot-maibot-factorio-download-link/1.0"
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class FactorioErrorCode(StrEnum):
    NOT_CONFIGURED = "not_configured"
    VERSION_API = "version_api"
    INVALID_CREDENTIALS = "invalid_credentials"
    FORBIDDEN = "forbidden"
    PACKAGE_NOT_FOUND = "package_not_found"
    DOWNLOAD_API = "download_api"
    NETWORK = "network"
    TIMEOUT = "timeout"


class FactorioDownloadError(RuntimeError):
    """A user-safe, classified Factorio API failure."""

    def __init__(self, code: FactorioErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FactorioDownloadLink:
    version: str
    url: str


class _Response(Protocol):
    status: int
    url: str

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


OpenRequest = Callable[[Request, float], _Response]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: HTTPResponse,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _open_with(opener: OpenerDirector) -> OpenRequest:
    return lambda request, timeout: opener.open(request, timeout=timeout)


def fetch_stable_space_age_version(
    *,
    timeout_seconds: float = 30.0,
    open_request: OpenRequest | None = None,
) -> str:
    request = Request(LATEST_RELEASES_URL, headers={"User-Agent": _USER_AGENT})
    open_request = open_request or _open_with(build_opener())
    try:
        with open_request(request, timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.VERSION_API,
            f"Factorio 版本接口返回 HTTP {exc.code}",
        ) from exc
    except TimeoutError as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.TIMEOUT,
            "连接 Factorio 版本接口超时",
        ) from exc
    except (URLError, OSError) as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.NETWORK,
            "无法连接 Factorio 版本接口",
        ) from exc

    try:
        data = loads(raw)
    except (JSONDecodeError, UnicodeError) as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.VERSION_API,
            "Factorio 版本接口返回内容不是有效 JSON",
        ) from exc
    stable = data.get("stable") if isinstance(data, dict) else None
    version = stable.get("expansion") if isinstance(stable, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise FactorioDownloadError(
            FactorioErrorCode.VERSION_API,
            "Factorio 版本接口缺少 stable.expansion 版本号",
        )
    return version.strip()


def fetch_factorio_space_age_windows_link(
    username: str,
    token: str,
    *,
    timeout_seconds: float = 30.0,
    version_open_request: OpenRequest | None = None,
    download_open_request: OpenRequest | None = None,
) -> FactorioDownloadLink:
    username = username.strip()
    token = token.strip()
    if not username or not token:
        raise FactorioDownloadError(
            FactorioErrorCode.NOT_CONFIGURED,
            "Factorio 下载凭据尚未配置，请在插件配置中填写 username 和 token",
        )

    version = fetch_stable_space_age_version(
        timeout_seconds=timeout_seconds,
        open_request=version_open_request,
    )
    base_url = DOWNLOAD_URL_TEMPLATE.format(version=version)
    authenticated_url = f"{base_url}?{urlencode({'username': username, 'token': token})}"
    request = Request(authenticated_url, headers={"User-Agent": _USER_AGENT})
    download_open_request = download_open_request or _open_with(build_opener(_NoRedirectHandler()))
    try:
        with download_open_request(request, timeout_seconds) as response:
            if 200 <= response.status < 300 and response.url != authenticated_url:
                return FactorioDownloadLink(version=version, url=response.url)
            raise FactorioDownloadError(
                FactorioErrorCode.DOWNLOAD_API,
                f"Factorio 下载接口返回 HTTP {response.status}，但没有提供下载重定向",
            )
    except HTTPError as exc:
        if exc.code in _REDIRECT_CODES:
            location = exc.headers.get("Location", "").strip()
            if location:
                return FactorioDownloadLink(version=version, url=urljoin(base_url, location))
            raise FactorioDownloadError(
                FactorioErrorCode.DOWNLOAD_API,
                "Factorio 下载接口返回重定向，但没有提供下载地址",
            ) from exc
        if exc.code == 401:
            raise FactorioDownloadError(
                FactorioErrorCode.INVALID_CREDENTIALS,
                "Factorio 凭据无效",
            ) from exc
        if exc.code == 403:
            raise FactorioDownloadError(
                FactorioErrorCode.FORBIDDEN,
                "Factorio 账号没有 Space Age 下载权限",
            ) from exc
        if exc.code == 404:
            raise FactorioDownloadError(
                FactorioErrorCode.PACKAGE_NOT_FOUND,
                "Factorio 官网没有提供当前版本的 Space Age Windows 安装包",
            ) from exc
        raise FactorioDownloadError(
            FactorioErrorCode.DOWNLOAD_API,
            f"Factorio 下载接口返回 HTTP {exc.code}",
        ) from exc
    except TimeoutError as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.TIMEOUT,
            "连接 Factorio 下载接口超时",
        ) from exc
    except (URLError, OSError) as exc:
        raise FactorioDownloadError(
            FactorioErrorCode.NETWORK,
            "无法连接 Factorio 下载接口",
        ) from exc
