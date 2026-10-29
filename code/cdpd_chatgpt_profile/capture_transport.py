"""Narrow direct CDP read transport, based on pinned v0.2.1 collector.

No public arbitrary CDP/JS API, page creation, navigation, UI mutation, browser
launch, connection retry, or fallback target. Real use requires a separately
authorized live keeper/coordinator; execution tests inject a local fake.
"""
from __future__ import annotations
import errno
import http.client
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from cdpd.collector import CaptureError
from chatgpt_web_api.target_guard import derive_browser_epoch
from .spool import strict_json
from .structure_script import capture_script

BROWSER_METHODS = frozenset({"Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget"})
SESSION_METHODS = frozenset({"Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"})
_SOCKET_LOSS_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("ECONNRESET", "ECONNABORTED", "EPIPE", "ENOTCONN", "ENETRESET", "ESHUTDOWN")
    if hasattr(errno, name)
)
_WINSOCK_LOSS_CODES = frozenset({10052, 10053, 10054, 10057, 10058})


class ReadOnlyCaptureConnection:
    def __init__(self, endpoint: str, timeout: float) -> None:
        if endpoint != "http://127.0.0.1:9222":
            raise CaptureError("ENDPOINT_NOT_ALLOWED")
        self.endpoint, self.deadline = endpoint, time.monotonic() + timeout
        self.socket = None
        self.sequence = 0
        self.audit: list[str] = []
        self.metadata_audit: list[str] = []
        try:
            ws = self._discovery()
            self.browser_id = derive_browser_epoch(ws)
            self._ws = ws
            import websocket
            self.socket = websocket.create_connection(ws, timeout=self.remaining(), suppress_origin=True,
                                                       http_no_proxy=["127.0.0.1"])
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CDP_CONNECT_FAILED") from None

    def remaining(self) -> float:
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise CaptureError("CAPTURE_DEADLINE")
        return value

    def _get(self, path: str, maximum: int) -> Any:
        self.metadata_audit.append("HTTP_VERSION" if path == "/json/version" else "HTTP_TARGET_LIST")
        conn = http.client.HTTPConnection("127.0.0.1", 9222, timeout=self.remaining())
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            if response.status != 200:
                raise CaptureError("CDP_DISCOVERY_FAILED")
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise CaptureError("CDP_DISCOVERY_TOO_LARGE")
            return strict_json(raw, max_bytes=maximum)
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CDP_DISCOVERY_FAILED") from None
        finally:
            conn.close()

    def _discovery(self) -> str:
        value = self._get("/json/version", 65536)
        if type(value) is not dict or type(value.get("webSocketDebuggerUrl")) is not str:
            raise CaptureError("CDP_DISCOVERY_FAILED")
        ws = value["webSocketDebuggerUrl"]
        parsed = urlsplit(ws)
        if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != 9222
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.path.startswith("/devtools/browser/")):
            raise CaptureError("ENDPOINT_REDIRECT_REJECTED")
        return ws

    def identity(self) -> dict[str, Any]:
        """Fresh discovery metadata only. Never inspect another target's DOM."""
        try:
            epoch = derive_browser_epoch(self._discovery())
            values = self._get("/json/list", 1048576)
            if type(values) is not list or len(values) > 1000:
                raise CaptureError("TARGET_BINDING_INVALID")
            targets = []
            for item in values:
                if type(item) is not dict:
                    raise CaptureError("TARGET_BINDING_INVALID")
                targets.append({"targetId": item.get("id"), "type": item.get("type"),
                                "url": item.get("url"), "subtype": item.get("subtype", "")})
            return {"browser_id": epoch, "targets": targets}
        except CaptureError:
            raise
        except Exception:
            raise CaptureError("CDP_DISCOVERY_FAILED") from None

    def send(self, method: str, params: dict[str, Any], session: str | None = None) -> dict[str, Any]:
        if method not in (SESSION_METHODS if session is not None else BROWSER_METHODS):
            raise CaptureError("COMMAND_NOT_ALLOWED")
        if method == "Runtime.evaluate":
            expression = params.get("expression")
            match = re.search(r"const maxNodes = (\d+);", expression) if type(expression) is str else None
            if not match or expression != capture_script(int(match.group(1))):
                raise CaptureError("SCRIPT_NOT_ALLOWED")
            if params.get("returnByValue") is not True or params.get("awaitPromise") is not False:
                raise CaptureError("SCRIPT_NOT_ALLOWED")
        self.audit.append(method)
        self.sequence += 1
        packet = {"id": self.sequence, "method": method, "params": params}
        if session is not None:
            packet["sessionId"] = session
        try:
            self.socket.settimeout(self.remaining())
            self.socket.send(json.dumps(packet, allow_nan=False))
            while True:
                self.socket.settimeout(self.remaining())
                raw = self.socket.recv()
                if raw == "" or raw == b"":
                    raise CaptureError("DISCONNECTED")
                if type(raw) is str:
                    raw = raw.encode("utf-8")
                if type(raw) is not bytes or len(raw) > 2097152:
                    raise CaptureError("CDP_RESPONSE_TOO_LARGE")
                message = strict_json(raw, max_bytes=2097152)
                if type(message) is not dict:
                    raise CaptureError("CDP_COMMAND_FAILED")
                if type(message.get("id")) is not int or message.get("id") != self.sequence:
                    continue
                if ((session is None and "sessionId" in message)
                        or (session is not None and message.get("sessionId") != session)):
                    raise CaptureError("TARGET_BINDING_INVALID")
                if "error" in message or type(message.get("result", {})) is not dict:
                    raise CaptureError("CDP_COMMAND_FAILED")
                return message.get("result", {})
        except CaptureError:
            raise
        except Exception as error:
            import websocket
            # websocket-client can propagate native send/recv losses directly.
            # Only known loss of this established channel is terminal; timeout,
            # generic command errors, preconnect and cleanup retain their policy.
            native_loss = isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
            if isinstance(error, OSError):
                error_number = error.errno
                windows_error = getattr(error, "winerror", None)
                native_loss = native_loss or (
                    type(error_number) is int
                    and error_number in (_SOCKET_LOSS_ERRNOS | _WINSOCK_LOSS_CODES)
                ) or (type(windows_error) is int and windows_error in _WINSOCK_LOSS_CODES)
            if isinstance(error, websocket.WebSocketConnectionClosedException) or native_loss:
                raise CaptureError("DISCONNECTED") from None
            raise CaptureError("CDP_COMMAND_FAILED") from None

    def close(self, session: str | None) -> None:
        try:
            if session and self.socket and self.deadline > time.monotonic():
                self.send("Target.detachFromTarget", {"sessionId": session})
        except Exception:
            pass
        finally:
            if self.socket:
                try:
                    self.socket.close(timeout=0.2)
                except Exception:
                    pass
