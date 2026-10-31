# 视频分镜

在聊天中发送可公开访问的视频链接，或带下载地址的 QQ 视频附件。`qqbot_visual_video` 在公开接收 Hook 中下载视频，沿完整时间线抽取九帧，拼成一张带格号、时间戳的图片。现有 `vlm` 模型任务读取拼图后，画面描述追加到原消息的副本，随后进入正常聊天上下文。聊天触发规则、图片门控和表情收集继续使用原有配置。

支持 B站长链、b23 短链、YouTube、抖音、小红书、微博、腾讯视频、西瓜等域名，以及 HTTP/HTTPS 视频文件直链。网页解析由 yt-dlp 提供；站点要求登录、风控或不支持匿名下载时，会标注视频未读取。插件不保存登录凭据。QQ 视频的下载 URL 由本地 `napcat_adapter` 的 `_build_video_text_segment` 保留，缺少地址时提示改发链接或重传。

## 安装与配置

插件清单声明 Pillow、aiohttp、yt-dlp 和 `ffmpeg-binaries`。使用本项目虚拟环境安装这些依赖。FFmpeg 和 ffprobe 也可放在本插件的 `tools/ffmpeg/bin/` 或 PATH 中；本机工具文件不进 Git。

插件配置顶层的 `video_enabled` 默认开启，可在 `config.toml` 的 `[plugin]` 前设置：

```toml
video_enabled = true

[plugin]
enabled = true
config_version = "0.1.0"
```

模型使用现有 `model_task_config.vlm`，需要该任务配置的模型实际支持图像。视频描述通过公开 `llm.generate` 调用，无需增加模型提供商或填写另一份密钥。

## 处理边界

- 一条消息最多处理一个视频，每台机器人同时处理一个任务。
- 视频不超过80 MiB、10分钟、4000万像素；网页视频选择不超过480像素的可用档位。
- 九格图片每格长边420像素，覆盖完整视频。离散帧无法呈现所有快速动作。
- 总处理预算100秒，下载45秒，视觉描述40秒；接收 Hook 为清理任务预留额外时间。
- 只提供画面，不读取音轨。描述会保留时间范围与采样信息，不能声称听过对白或音乐。
- 成功描述最多缓存64条、6小时，仅存进程内存。临时视频与帧在成功、失败或取消后清理；卸载等待任务和子进程结束。

下载、解析、抽帧或模型失败时，原消息追加带原因的 `[视频未读取]` 标记，明确不应根据标题或其他图片编造内容。日志保留工具退出码和经过 URL 脱敏的诊断，不记录视频内容。

拼图实现复用 [Netsysn/maibot-video-storyboard](https://github.com/Netsysn/maibot-video-storyboard)；固定来源和 MIT 许可见 [vendor/NOTICE.md](vendor/NOTICE.md)。
