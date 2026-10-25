"""读取当前 MaiBot 实例的启动身份和 WebUI 端口，不加载框架。"""

from pathlib import Path

import json
import tomllib


def main() -> None:
    """校验本机启动配置，仅输出启动器需要的非敏感字段。"""
    path = Path(__file__).resolve().parents[1] / "config" / "bot_config.toml"
    with path.open("rb") as file:
        config = tomllib.load(file)
    nickname = config["bot"]["nickname"]
    webui = config["webui"]
    port = webui["port"]
    if not isinstance(nickname, str) or not nickname.strip():
        raise ValueError(f"{path}: bot.nickname 必须为非空字符串")
    if webui.get("enabled") is not True:
        raise ValueError(f"{path}: 启动器要求 webui.enabled = true")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(f"{path}: webui.port 必须为 1–65535 的整数")
    print(json.dumps({"nickname": nickname, "webui_port": port}))


if __name__ == "__main__":
    main()
