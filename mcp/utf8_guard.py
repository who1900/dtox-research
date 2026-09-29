"""ASGI guard: turn a non-UTF-8 request body into a JSON-RPC parse error.

The MCP SDK calls json.loads(body) on the raw bytes; a body in cp1251/latin-1
(typical for `curl -d` typed in a Windows shell) raises UnicodeDecodeError and
comes back as an opaque -32603 "Error handling POST request" with HTTP 500.
"""
import json

MAX_BODY = 4 * 1024 * 1024

_ERROR = json.dumps({
    "jsonrpc": "2.0", "id": None,
    "error": {"code": -32700,
              "message": "Parse error: request body is not valid UTF-8. "
                         "Send JSON encoded as UTF-8 (e.g. curl --data-binary @file.json)."},
}).encode()


class Utf8Guard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        chunks, size, more = [], 0, True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                return await self.app(scope, receive, send)
            chunks.append(msg.get("body", b""))
            size += len(chunks[-1])
            more = msg.get("more_body", False)
            if size > MAX_BODY:
                break
        body = b"".join(chunks)
        if size <= MAX_BODY:
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(_ERROR)).encode())]})
                await send({"type": "http.response.body", "body": _ERROR})
                return
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
