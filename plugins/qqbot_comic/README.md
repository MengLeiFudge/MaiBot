# QQBot JM 漫画 PDF

MaiBot SDK 2.x 原生固定命令插件。完整匹配 `JM<数字>`（大小写不敏感，JM 与数字间可有空格），在普通聊天链路前完成好友检查、唯一命令仲裁、下载、缓存、AES 加密和私聊交付。插件默认启用并直接处理，不设 cutover 门禁。

## 行为

- 群聊和私聊均可触发；普通正文中的 JMID 不触发。
- 群聊先在共享 SQLite 中汇合各 MaiBot 实例的好友能力，再由好友可达实例调用 `qqbot.route.claim`。
- 同一 JMID 共享单个任务；不同 JMID 默认并发 2 个，FIFO 最多排队 50 个。
- 明文标准 PDF 与 `metadata.json` 持久保存到 `runtime_root/comic_pdf_cache`，默认 10 GiB LRU。
- 下载图片、构建产物和每次 AES 加密副本只写 SDK `runtime_dir/jmcomic`，结束后清理。
- 最终仅私聊发送：一次元数据和密码、全部加密 PDF 切片、`JM<id>发送完成`。所有本机 PDF 读取后编码为 OneBot `base64://` 文件段，不引用原消息。

双实例部署时把每个实例的 `routing.expected_workers` 设为实际会同时收到群消息的实例数；当前单实例保持默认 `1`。`config.toml` 是本机配置并被 `.gitignore` 排除，提交模板为 `config.example.toml`。

## Python 依赖

锁定版本：`jmcomic==2.7.2`、`img2pdf==0.6.1`、`pikepdf==10.11.0`。最小安装命令：

```bash
python -m pip install "jmcomic==2.7.2" "img2pdf==0.6.1" "pikepdf==10.11.0"
```
