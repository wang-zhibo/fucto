import asyncio
import json
import requests
import websockets
import uuid
import threading
from typing import Optional, List, Dict, Any
from pathlib import Path
# ========== ⚙️ 配置区 ==========
# cookies需要手动获取，登录后找到https://clerk.cto.new/v1/client/sessions/sess...请求的请求头，复制其中的cookies，以【__client=】开头

COOKIES_FILE = Path(__file__).with_name("cookies.txt")
_cookie_lock = threading.Lock()
_cookie_pool: List[str] = []
_cookie_index = 0
_cookie_mtime: Optional[float] = None
# COOKIES = COOKIES_FILE.read_text().strip()
AUTO_NEW_CHAT = True  # True=每次新建对话；False=复用上次 chat_id
CHAT_ID_CACHE_FILE = "chat_id.txt"
# ADAPTER = "ClaudeSonnet4_5"
ADAPTER = "GPT5"
# ==============================


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



def get_clerk_info():
    """
    获取 Clerk 会话信息、user_id、session_id
    """
    url = "https://clerk.cto.new/v1/me/organization_memberships"
    params = {
        "paginated": "true",
        "limit": "10",
        "offset": "0",
        "__clerk_api_version": "2025-04-10",
        "_clerk_js_version": "5.102.0",
    }
    
    cookie = get_cookie()

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
    print(f"🪪 Clerk session id: {session_id}")
    print(f"👤 WebSocket user token: {user_id}")

    return session_id, user_id


def get_jwt_from_clerk(session_id):
    """
    刷新 JWT
    """
    url = (
        f"https://clerk.cto.new/v1/client/sessions/{session_id}/tokens"
        "?__clerk_api_version=2025-04-10&_clerk_js_version=5.101.1"
    )
    cookie = get_cookie()
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0",
    }
    r = requests.post(url, headers=headers, data={})
    print("🔖 Clerk tokens status:", r.status_code)
    r.raise_for_status()
    jwt = r.json().get("jwt")
    print("✅ JWT 获取成功，长度:", len(jwt))
    return jwt




def create_new_chat(jwt, prompt="你好", adapter = ADAPTER):
    """
    创建一个新的 chat_id（UUID），并使用 /chat 发送首条消息。
    """
    chat_id = str(uuid.uuid4())  # 生成随机 chatHistoryId
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

    print(f"🆕 使用随机 chat_id 创建新对话: {chat_id}")
    r = requests.post(url, headers=headers, json=data)
    print("💬 创建对话 status:", r.status_code)
    if not r.ok:
        print("⚠️ 创建对话失败:", r.status_code, r.text)
        r.raise_for_status()
    else:
        print("✅ 新对话创建成功。")

    return chat_id


async def listen_ws(chat_id, ws_user_token):
    """
    连接 WebSocket 并拼接 AI 输出
    """
    ws_url = (
        f"wss://api.enginelabs.ai/engine-agent/chat-histories/{chat_id}"
        f"/buffer/stream?token={ws_user_token}"
    )
    print("🔌 连接:", ws_url)

    async with websockets.connect(ws_url, max_size=None) as ws:
        print("✅ WebSocket 已连接，等待响应...\n")

        buffer = ""
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
                        print(content, end="", flush=True)
                except Exception:
                    continue

            elif data.get("type") == "state" and not data["state"].get("inProgress"):
                print("\n\n--- 生成结束 ---\n")
                print("🤖 完整回答：\n", buffer.strip())
                print("\n-----------------\n")
                break


async def main():
    # 获取 Clerk 信息（session_id, user_id）
    session_id, ws_user_token = get_clerk_info()
    jwt = get_jwt_from_clerk(session_id)

    # 确定 chat_id（自动新建或读取缓存）
    if AUTO_NEW_CHAT:
        chat_id = create_new_chat(jwt)
        with open(CHAT_ID_CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(chat_id)
    else:
        try:
            with open(CHAT_ID_CACHE_FILE, "r", encoding="utf-8") as f:
                chat_id = f.read().strip()
                print(f"♻️ 复用 chat_id: {chat_id}")
        except FileNotFoundError:
            chat_id = create_new_chat(jwt)
            with open(CHAT_ID_CACHE_FILE, "w", encoding="utf-8") as f:
                f.write(chat_id)

    # 循环对话
    while True:
        prompt = input("You: ").strip()
        if not prompt or prompt.lower() in {"exit", "quit"}:
            break

        print(f"📨 发送 prompt: {prompt}")
        url = "https://api.enginelabs.ai/engine-agent/chat"
        headers = {
            "authorization": f"Bearer {jwt}",
            "content-type": "application/json",
            "origin": "https://cto.new",
        }
        payload = {
            "prompt": prompt,
            "chatHistoryId": chat_id,
            "adapterName": "ClaudeSonnet4_5",
        }
        r = requests.post(url, headers=headers, json=payload)
        print("POST", r.status_code, "\n")

        await listen_ws(chat_id, ws_user_token)


if __name__ == "__main__":
    asyncio.run(main())
