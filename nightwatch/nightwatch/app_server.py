from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any

from . import __version__
from .storage import redact


class AppServerProtocolError(RuntimeError):
    pass


class AppServerClient:
    """Small JSON-RPC client for the current Codex app-server stdio contract."""

    def __init__(self, binary: str | None = None, timeout: float = 8.0):
        self.binary = binary or os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
        self.timeout = timeout
        self.trace: list[dict[str, Any]] = []

    def rate_limits(self) -> dict[str, Any]:
        process = self._spawn()
        selector: selectors.BaseSelector | None = None
        try:
            assert process.stdin is not None and process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            self._send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "nightwatch", "version": __version__}, "capabilities": {"experimentalApi": False}}})
            self._wait_response(process, selector, 1)
            self._send(process, {"jsonrpc": "2.0", "method": "initialized"})
            # Codex 0.150.1 schema defines params as null and does not require
            # the key. Omitting it avoids sending an invalid empty object.
            self._send(process, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"})
            response = self._wait_response(process, selector, 2)
            return response.get("result")
        finally:
            if selector is not None:
                selector.close()
            self._shutdown(process)

    def _spawn(self) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen([self.binary, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            raise AppServerProtocolError(f"app-server spawn failed: {type(exc).__name__}") from exc

    def _send(self, process: subprocess.Popen[str], message: dict[str, Any]) -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise AppServerProtocolError("app-server stdin failed") from exc
        self.trace.append({"direction": "send", "id": message.get("id"), "method": message.get("method")})

    def _wait_response(self, process: subprocess.Popen[str], selector: selectors.BaseSelector, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            ready = selector.select(max(0.01, deadline - time.monotonic()))
            if not ready:
                if process.poll() is not None:
                    raise AppServerProtocolError(f"app-server exited before response id {request_id}")
                continue
            assert process.stdout is not None
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    raise AppServerProtocolError(f"app-server exited before response id {request_id}")
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.trace.append({"direction": "recv", "kind": "malformed"})
                continue
            if not isinstance(message, dict):
                self.trace.append({"direction": "recv", "kind": "non_object"})
                continue
            if "method" in message and "id" not in message:
                self.trace.append({"direction": "recv", "kind": "notification", "method": message.get("method")})
                continue
            if message.get("id") != request_id:
                self.trace.append({"direction": "recv", "kind": "other_response", "id": message.get("id")})
                continue
            self.trace.append({"direction": "recv", "kind": "response", "id": request_id})
            if message.get("error") is not None:
                raise AppServerProtocolError(f"app-server response id {request_id} returned error: {redact(message['error'])}")
            if "result" not in message:
                raise AppServerProtocolError(f"app-server response id {request_id} has no result")
            return message
        raise AppServerProtocolError(f"app-server response id {request_id} timed out")

    @staticmethod
    def _shutdown(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
