# ima 桌面客户端（公开精简版 / public 分支）

基于 PySide6 的 ima 桌面客户端，此分支为公开使用的精简版本：
知识库仅支持**浏览与下载**，文件修改类操作（重命名/删除/移动等）请使用 ima 官方客户端。

## 功能

- **对话**：接入 ima LLM，支持模型选择与流式输出
- **智能体记忆**：记忆文件化管理（多文件 / 导入 / 删除），对话时自动注入、形成的记忆自动写回
- **知识库浏览**：树形浏览 ima 知识库、批量下载（含自动续期登录态）
- **上传**：本地文件上传到知识库（走官方 OpenAPI）
- **笔记**：新建 Markdown 笔记并可加入知识库，查看 / 导出笔记正文（走官方 OpenAPI）
- **本地知识库**：挂载任意本地文件夹，层级树浏览、文件变动自动刷新、一键推送到 ima 知识库

## 运行

```bash
pip install PySide6 requests
python main.py
```

首次启动在「设置」中粘贴浏览器抓取的 `x-ima-cookie` / `x-ima-bkn`，
以及官方 OpenAPI 的 `clientid` / `apikey`。
凭据保存在 `data/config.json`（已 gitignore），该目录请勿分享。

## 打包

```bash
pyinstaller ima_desktop.spec --distpath dist --workpath build --noconfirm
```
