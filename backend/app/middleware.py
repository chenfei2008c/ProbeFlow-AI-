"""Bound actual API request bytes before framework JSON parsing."""

from fastapi.responses import JSONResponse

from app.models import uid


class RequestBodyLimit:
    def __init__(self, app, limit: int = 12 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/api/")
            or scope["method"] in {"GET", "HEAD", "OPTIONS"}
        ):
            await self.app(scope, receive, send)
            return
        content = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            if len(content) + len(part) > self.limit:
                response = JSONResponse(
                    {
                        "code": "REQUEST_TOO_LARGE",
                        "message": "请求数据过大",
                        "retryable": False,
                        "request_id": scope.get("state", {}).get("request_id", uid()),
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            content.extend(part)
            if not message.get("more_body", False):
                break
        forwarded = False

        async def replay():
            nonlocal forwarded
            if not forwarded:
                forwarded = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
