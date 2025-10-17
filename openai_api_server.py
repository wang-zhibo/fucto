"""
OpenAI 格式兼容的 API 服务器
提供标准的 /v1/chat/completions 接口,桥接到现有的 CTO.NEW AI 服务
"""
import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any
import requests
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


# ========== 配置 ==========
COOKIES_FILE = Path(__file__).with_name("cookies.txt")
_cookie_lock = threading.Lock()
_cookie_pool: List[str] = []
_cookie_index = 0
_cookie_mtime: Optional[float] = None


def _load_cookie_pool() -> None:
    """Load cookie list from disk and reset round-robin index."""
    global _cookie_pool, _cookie_index, _cookie_mtime
    try:
        current_mtime = COOKIES_FILE.stat().st_mtime
        raw_lines = COOKIES_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cookie file not found: {COOKIES_FILE}") from exc

    cookies = [line.strip() for line in raw_lines if line.strip() and not line.strip().startswith("#")]
    if not cookies:
        raise RuntimeError(f"No cookies defined in {COOKIES_FILE}")

    _cookie_pool = cookies
    _cookie_index = 0
    _cookie_mtime = current_mtime


def get_cookie() -> str:
    """Return next cookie using round-robin rotation."""
    global _cookie_index
    with _cookie_lock:
        try:
            current_mtime = COOKIES_FILE.stat().st_mtime
        except FileNotFoundError as exc:
            raise RuntimeError(f"Cookie file not found: {COOKIES_FILE}") from exc

        if not _cookie_pool or _cookie_mtime != current_mtime:
            _load_cookie_pool()

        cookie = _cookie_pool[_cookie_index]
        _cookie_index = (_cookie_index + 1) % len(_cookie_pool)
        return cookie

# 模型映射:将 OpenAI 模型名称映射到 CTO.NEW 的 adapter
MODEL_MAPPING = {
    "gpt-5": "GPT5",
    "claude-sonnet-4-5": "ClaudeSonnet4_5",
}

DEFAULT_ADAPTER = "ClaudeSonnet4_5"


# ========== Pydantic Models ==========
class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: bool = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionChoice(BaseModel):
    index: int
    message: Message
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage


# ========== FastAPI App ==========
app = FastAPI(title="OpenAI Compatible API", version="1.0.0")


# ========== 辅助函数 ==========
def get_clerk_info(cookie: str):
    """获取 Clerk 会话信息"""
    url = "https://clerk.cto.new/v1/me/organization_memberships"
    params = {
        "paginated": "true",
        "limit": "10",
        "offset": "0",
        "__clerk_api_version": "2025-04-10",
        "_clerk_js_version": "5.102.0",
    }
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0",
    }

    r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    data = r.json()

    session_id = data["client"]["last_active_session_id"]
    user_id = data["client"]["sessions"][0]["user"]["id"]

    return session_id, user_id


def get_jwt_from_clerk(session_id: str, cookie: str) -> str:
    """刷新 JWT"""
    url = (
        f"https://clerk.cto.new/v1/client/sessions/{session_id}/tokens"
        "?__clerk_api_version=2025-04-10&_clerk_js_version=5.101.1"
    )
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0",
    }
    r = requests.post(url, headers=headers, data={})
    r.raise_for_status()
    jwt = r.json().get("jwt")
    return jwt


def create_chat(jwt: str, prompt: str, adapter: str) -> str:
    """创建新的聊天会话"""
    chat_id = str(uuid.uuid4())
    url = "https://api.enginelabs.ai/engine-agent/chat"
    headers = {
        "authorization": f"Bearer {jwt}",
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://cto.new",
        "referer": "https://cto.new",
    }
    data = {
        "prompt": prompt,
        "chatHistoryId": chat_id,
        "adapterName": adapter
    }

    r = requests.post(url, headers=headers, json=data)
    r.raise_for_status()

    return chat_id


async def get_ai_response(chat_id: str, ws_user_token: str) -> str:
    """从 WebSocket 获取完整的 AI 响应"""
    ws_url = (
        f"wss://api.enginelabs.ai/engine-agent/chat-histories/{chat_id}"
        f"/buffer/stream?token={ws_user_token}"
    )

    buffer = ""
    async with websockets.connect(ws_url, max_size=None) as ws:
        async for msg in ws:
            try:
                data = json.loads(msg)
            except Exception:
                continue

            if data.get("type") == "update" and data.get("buffer"):
                try:
                    inner = json.loads(data["buffer"])
                    if inner.get("type") == "chat":
                        content = inner.get("chat", {}).get("content", "")
                        buffer += content
                except Exception:
                    continue

            elif data.get("type") == "state" and not data["state"].get("inProgress"):
                break

    return buffer.strip()


async def stream_ai_response(chat_id: str, ws_user_token: str):
    """流式输出 AI 响应"""
    ws_url = (
        f"wss://api.enginelabs.ai/engine-agent/chat-histories/{chat_id}"
        f"/buffer/stream?token={ws_user_token}"
    )

    async with websockets.connect(ws_url, max_size=None) as ws:
        async for msg in ws:
            try:
                data = json.loads(msg)
            except Exception:
                continue

            if data.get("type") == "update" and data.get("buffer"):
                try:
                    inner = json.loads(data["buffer"])
                    if inner.get("type") == "chat":
                        content = inner.get("chat", {}).get("content", "")
                        if content:
                            # OpenAI 流式格式
                            chunk = {
                                "id": f"chatcmpl-{chat_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "gpt-4",
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": content},
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                except Exception:
                    continue

            elif data.get("type") == "state" and not data["state"].get("inProgress"):
                # 发送结束标记
                final_chunk = {
                    "id": f"chatcmpl-{chat_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4",
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                break


# ========== API Routes ==========
@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "OpenAI Compatible API Server",
        "endpoints": {
            "chat": "/v1/chat/completions",
            "models": "/v1/models"
        }
    }


@app.get("/v1/models")
async def list_models():
    """列出可用模型"""
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "cto-new"
            }
            for model_name in MODEL_MAPPING.keys()
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    OpenAI 兼容的聊天完成接口
    支持流式和非流式响应
    """
    try:
        cookie = get_cookie()
        # 获取认证信息
        session_id, ws_user_token = get_clerk_info(cookie)
        jwt = get_jwt_from_clerk(session_id, cookie)

        # 确定使用的 adapter
        adapter = MODEL_MAPPING.get(request.model, DEFAULT_ADAPTER)

        # 提取最后一条用户消息作为 prompt
        user_messages = [msg for msg in request.messages if msg.role == "user"]
        if not user_messages:
            raise HTTPException(status_code=400, detail="No user message found")

        prompt = user_messages[-1].content

        # 创建聊天会话
        chat_id = create_chat(jwt, prompt, adapter)

        # 流式响应
        if request.stream:
            return StreamingResponse(
                stream_ai_response(chat_id, ws_user_token),
                media_type="text/event-stream"
            )

        # 非流式响应
        response_content = await get_ai_response(chat_id, ws_user_token)

        return ChatCompletionResponse(
            id=f"chatcmpl-{chat_id}",
            created=int(time.time()),
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=Message(role="assistant", content=response_content),
                    finish_reason="stop"
                )
            ],
            usage=Usage(
                prompt_tokens=len(prompt) // 4,  # 粗略估算
                completion_tokens=len(response_content) // 4,
                total_tokens=(len(prompt) + len(response_content)) // 4
            )
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
