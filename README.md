# CTO OpenAI 兼容 API 项目说明

本项目提供一个与 OpenAI 接口兼容的 FastAPI 服务，并包含一个用于直接测试 WebSocket 接口的示例脚本。

## 项目结构

- `openai_api_server.py`：FastAPI 实现的 `/v1/chat/completions` 兼容服务，支持轮询多个 Cookie 并与 CTO.NEW 的后端服务通信。
- `websocket_example.py`：命令行交互示例，演示如何直接通过 HTTP + WebSocket 与引擎交互并获取实时响应。
- `requirements.txt`：运行所需的第三方依赖列表。
- `cookies.txt`（需手动创建）：按行存放可用的 Cookie 字符串，服务会自动轮询使用。

## 快速开始

1. **环境准备**
   - 推荐使用 Python 3.10+。
   - （可选）创建并激活虚拟环境。
   - 执行 `pip install -r requirements.txt` 安装依赖。

2. **配置 Cookie**
   - 登录网站，进行抓包，找到https://clerk.cto.new/v1/client/sessions/sess...请求的请求头，复制其中的cookies，以【__client=】开头
   - 在项目根目录创建 `cookies.txt`。
   - 将多个 Cookie 字符串按行写入文件，可添加 `#` 开头的注释行。
   - 每次请求将自动轮询使用不同的 Cookie，实现简单的负载均衡。

3. **启动 API 服务**
   ```bash
   uvicorn openai_api_server:app --host 0.0.0.0 --port 8000
   ```
   - FastAPI 服务会提供 `/v1/chat/completions` 与 `/v1/models` 两个主要端点。
   - 默认返回格式与 OpenAI Chat Completions 兼容，可直接被现有客户端使用。

4. **运行 WebSocket 示例**
   ```bash
   python websocket_example.py
   ```
   - 首次运行会在当前目录保存 `chat_id.txt`，以便选择复用或新建对话。
   - 根据提示输入消息，可实时获取模型回复。

## 编码与本地化

- 所有源文件及配置文件请使用 UTF-8 编码保存，特别是在保存含中文的文本时。
- 如果在 Windows 终端中出现乱码，请确认终端编码（如 PowerShell 的 `chcp 65001`）与文件编码一致。

## 常见问题

- **Cookie 失效**：出现 401 或 403 时，更新 `cookies.txt` 中的条目后保存即可继续使用，无需重启服务。
- **依赖缺失**：确保在正确的虚拟环境中执行安装命令；必要时重新安装 `websockets`、`fastapi` 等包。

欢迎根据业务需求扩展路由或增加模型适配逻辑，所有修改建议保持 UTF-8 编码以保证跨平台兼容。
