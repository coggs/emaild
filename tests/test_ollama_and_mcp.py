import httpx

from emaild.llm.providers import OllamaProvider


def test_ollama_chat_payload(monkeypatch):
    seen = {}

    def fake_post(url, json, timeout):
        seen["url"], seen["body"] = url, json
        return httpx.Response(200, json={"message": {"content": "hi"}, "prompt_eval_count": 5, "eval_count": 2},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    p = OllamaProvider("http://h:11434/", "gemma4", num_ctx=8192)
    r = p.chat([{"role": "user", "content": "x"}], schema={"type": "object"})
    assert seen["url"] == "http://h:11434/api/chat"
    assert seen["body"]["format"] == {"type": "object"}
    assert seen["body"]["options"]["num_ctx"] == 8192
    assert r.text == "hi" and r.prompt_tokens == 5 and r.is_local


def test_mcp_tools_registered():
    import asyncio

    from emaild import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"ask", "search", "get_thread", "show_raw", "sync_status"} <= names


def test_web_app_imports():
    from emaild.web.app import app
    paths = {r.path for r in app.routes}
    assert {"/healthz", "/oauth/google/start", "/oauth/google/callback", "/"} <= paths
