"""Loopback relay for Claude policy decisions, with retries during control plane restarts."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("aiflow4.node.policy_relay")


class PolicyRelay:
    def __init__(self, control_url: str, retry_window_s: float = 60.0, client: httpx.Client | None = None,
                 sleep=time.sleep) -> None:
        self.control_url = control_url.rstrip("/")
        self.retry_window_s = retry_window_s
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self._sleep = sleep
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.app = self._create_app()

    @property
    def healthy(self) -> bool:
        return bool(self.server and self.server.started and self.thread and self.thread.is_alive())

    def health(self) -> dict[str, bool]:
        return {"enabled": True, "healthy": self.healthy}

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="AI Flow v4 node policy relay")

        @app.get("/healthz")
        def healthz() -> dict[str, bool]:
            return {"ok": True}

        @app.post("/v1/policy/decide")
        async def decide(request: Request, authorization: str = Header(default="")) -> Response:
            body = await request.body()
            return await asyncio.to_thread(self._forward, body, authorization)

        return app

    def _forward(self, body: bytes, authorization: str) -> Response:
        deadline = time.monotonic() + self.retry_window_s
        delay = 0.25
        last_response: httpx.Response | None = None
        last_error: httpx.RequestError | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_response is not None:
                    self._log_decision(last_response, body)
                    return self._response(last_response)
                log.error("policy relay could not reach control API within %.1f seconds: %s",
                          self.retry_window_s, str(last_error or "retry window expired")[:200])
                return JSONResponse(status_code=503, content={"error": "control policy API unavailable"})

            try:
                response = self.client.post(
                    f"{self.control_url}/v1/policy/decide",
                    content=body,
                    headers={"Authorization": authorization},
                    timeout=min(10.0, remaining),
                )
            except httpx.RequestError as e:
                last_error = e
            else:
                if response.status_code < 500:
                    self._log_decision(response, body)
                    return self._response(response)
                last_response = response

            self._sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(2.0, delay * 2)

    @staticmethod
    def _response(response: httpx.Response) -> Response:
        headers = {}
        if content_type := response.headers.get("content-type"):
            headers["content-type"] = content_type
        return Response(content=response.content, status_code=response.status_code, headers=headers)

    @staticmethod
    def _log_decision(response: httpx.Response, request_body: bytes) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        try:
            request = json.loads(request_body)
        except (TypeError, ValueError):
            request = {}
        if not isinstance(request, dict):
            request = {}
        output = payload.get("hookSpecificOutput") or {} if isinstance(payload, dict) else {}
        log.info("policy decision tool=%s decision=%s reason=%s", str(request.get("tool_name", ""))[:80],
                 str(output.get("permissionDecision", "unknown"))[:20],
                 str(output.get("permissionDecisionReason", ""))[:200])

    def start(self, host: str = "127.0.0.1", port: int = 8471, startup_timeout_s: float = 5.0) -> bool:
        if self.thread and self.thread.is_alive():
            return self.healthy
        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="aiflow4-policy-relay", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + startup_timeout_s
        while self.thread.is_alive() and not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.healthy:
            log.error("policy relay failed to start on %s:%d", host, port)
        else:
            log.info("policy relay listening on http://%s:%d", host, port)
        return self.healthy

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)
        if self._owns_client:
            self.client.close()
