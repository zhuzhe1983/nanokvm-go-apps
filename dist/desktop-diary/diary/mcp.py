#!/usr/bin/env python3
"""Local stdio bridge to this NanoKVM's certificate-pinned MCP endpoint.

No third-party dependencies. Credentials never enter config, argv, or diagnostics.
The device is a request/response tools server; unsolicited server requests are
not supported. Every failed request is reported once, never automatically retried.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
import time

from urllib.parse import urlsplit

_endpoint = urlsplit(os.environ.get("DIARY_MCP_URL", "https://127.0.0.1/api/mcp"))
if _endpoint.scheme != "https" or not _endpoint.hostname or _endpoint.username:
    raise ValueError("MCP URL must be an HTTPS endpoint without credentials")
HOST = _endpoint.hostname
PORT = _endpoint.port or 443
ENDPOINT = _endpoint.path or "/api/mcp"
CERT_SHA256 = os.environ.get("DIARY_MCP_CERT_SHA256", "")
if not CERT_SHA256 and HOST in ("127.0.0.1", "localhost"):
    _cert = Path("/etc/kvm/server.crt")
    if _cert.exists():
        CERT_SHA256 = hashlib.sha256(ssl.PEM_cert_to_DER_cert(_cert.read_text())).hexdigest()
TOKEN_PATH = Path(os.environ.get("DIARY_MCP_TOKEN_FILE", "/etc/nanokvm-agent/mcp-token"))
TIMEOUT = 45
MAX_BODY = 32 * 1024 * 1024
PROTOCOL_VERSION = "2025-03-26"


class BridgeError(Exception):
    """An intentionally credential-free diagnostic."""


def read_token(path=TOKEN_PATH):
    """Read only a regular, owner-only file without following a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise BridgeError("Token file must be owned by the current user.")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise BridgeError("Token file must have owner-only permissions (0600).")
            value = stream.read(65537).strip()
    except BridgeError:
        raise
    except (OSError, UnicodeError):
        raise BridgeError("Cannot read the protected NanoKVM token file.") from None
    if not value or len(value) > 65536 or any(ch in value for ch in "\r\n"):
        raise BridgeError("NanoKVM token file is empty or invalid.")
    return value


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The factory certificate has CN=localhost and no SAN. Authenticate the
        # exact pinned DER certificate instead of trusting arbitrary certificates.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(HOST, PORT, timeout=TIMEOUT, context=context)

    def connect(self):
        if not CERT_SHA256:
            raise BridgeError("Configure the NanoKVM certificate fingerprint first.")
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, CERT_SHA256):
            self.close()
            raise BridgeError("NanoKVM certificate changed; connection refused before authentication.")


def decode_json(data):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise BridgeError("NanoKVM returned invalid JSON.") from None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise BridgeError("NanoKVM returned an invalid JSON-RPC envelope.")
    return value


def parse_sse(response, expected_id, deadline, set_timeout=None):
    """Read bounded SSE frames until the matching JSON-RPC response arrives."""
    data_lines = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("NanoKVM response timed out.")
        if set_timeout:
            set_timeout(remaining)
        line = response.readline(MAX_BODY - total + 1)
        total += len(line)
        if total > MAX_BODY:
            raise BridgeError("NanoKVM response exceeds the size limit.")
        if not line or line in (b"\n", b"\r\n"):
            if data_lines:
                message = decode_json(b"\n".join(data_lines))
                data_lines = []
                if message.get("id") == expected_id and (
                    "result" in message or "error" in message
                ):
                    return message
                if "method" in message and "id" in message:
                    raise BridgeError("NanoKVM requested an unsupported client callback.")
            if not line:
                raise BridgeError("NanoKVM stream ended without a matching response.")
            continue
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data = line[5:]
            data_lines.append(data[1:] if data.startswith(b" ") else data)


class Remote:
    def __init__(self, token=None, connection_factory=PinnedHTTPSConnection):
        self.token = token if token is not None else (os.environ.get("DIARY_MCP_API_KEY") or read_token())
        self.connection_factory = connection_factory
        self.connection = None
        self.session_id = None
        self.protocol = PROTOCOL_VERSION
        self.initialized = False

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def request(self, message):
        expects_reply = "id" in message
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        deadline = time.monotonic() + TIMEOUT
        try:
            if self.connection is None:
                self.connection = self.connection_factory()
                # Explicit connect ensures the certificate is checked BEFORE the
                # HTTP request, including its Authorization header, can be sent.
                self.connection.connect()
            self.connection.request("POST", ENDPOINT, body=body, headers=headers)
            response = self.connection.getresponse()
            if response.status not in (200, 202, 204):
                status = response.status
                self.close()
                raise BridgeError("NanoKVM HTTP request failed (status %d)." % status)
            session = response.getheader("Mcp-Session-Id")
            if session:
                self.session_id = session
            if not expects_reply:
                # Notifications have no JSON-RPC response. Do not wait on an SSE
                # stream a nonstandard server might leave open for them.
                if response.status in (202, 204) or response.getheader("Content-Length") == "0":
                    response.read()
                else:
                    self.close()
                return None
            if response.status in (202, 204):
                self.close()
                raise BridgeError("NanoKVM accepted a request without returning a result.")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "text/event-stream":
                sock = getattr(self.connection, "sock", None)
                result = parse_sse(response, message["id"], deadline, sock.settimeout if sock else None)
                self.close()  # An SSE response may continue indefinitely.
            else:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise BridgeError("NanoKVM response exceeds the size limit.")
                result = decode_json(data)
                if result.get("id") != message["id"] or not ("result" in result or "error" in result):
                    raise BridgeError("NanoKVM returned a mismatched response.")
            if message.get("method") == "initialize" and "result" in result:
                negotiated = result["result"].get("protocolVersion")
                if isinstance(negotiated, str):
                    self.protocol = negotiated
                self.initialized = True
            return result
        except BridgeError:
            self.close()
            raise
        except (socket.timeout, TimeoutError):
            self.close()
            raise BridgeError("NanoKVM response timed out; request was not retried.") from None
        except (OSError, http.client.HTTPException, ValueError):
            self.close()
            raise BridgeError("NanoKVM transport failed; request was not retried.") from None

    def initialize(self):
        reply = self.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "desktop-diary", "version": "1.0.0"},
        }})
        if "error" in reply:
            raise BridgeError("NanoKVM rejected MCP initialization.")
        self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
