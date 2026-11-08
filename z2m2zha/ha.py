"""Home Assistant API access with the Python standard library only.

Two ways in:
  * Supervisor proxy (HA OS / Supervised, run from an add-on such as Advanced SSH & Web Terminal
    with protection mode off): uses $SUPERVISOR_TOKEN, http://supervisor/core/api and
    ws://supervisor/core/websocket. This also unlocks the Supervisor API (add-on control).
  * Any HA (Container, Core, remote): set "ha": {"url": "http://host:8123"} in the config and put
    a long-lived access token in $HA_TOKEN (or the env var named by "ha.token_env").
"""
import base64
import json
import os
import secrets
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request


class HAError(RuntimeError):
    pass


def endpoints(cfg):
    """Return (rest_base, ws_url, token, supervisor_token_or_None)."""
    sup_token = os.environ.get("SUPERVISOR_TOKEN")
    url = cfg["ha"]["url"]
    if url:
        token = os.environ.get(cfg["ha"]["token_env"])
        if not token:
            raise SystemExit(f"set ${cfg['ha']['token_env']} to a long-lived access token for {url}")
        u = url.rstrip("/")
        ws = ("wss://" if u.startswith("https://") else "ws://") + u.split("://", 1)[1] + "/api/websocket"
        return u + "/api", ws, token, sup_token
    if sup_token:
        return "http://supervisor/core/api", "ws://supervisor/core/websocket", sup_token, sup_token
    raise SystemExit("no Home Assistant access. Either run inside an add-on where $SUPERVISOR_TOKEN is set\n"
                     "(for SSH add-ons use a login shell, e.g. bash -lc), or set ha.url in the config and\n"
                     "export HA_TOKEN=<long-lived access token>.")


class _WS:
    """Minimal RFC 6455 client (text frames, client masking, no extensions)."""

    def __init__(self, url, timeout, verify_tls=True):
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        s = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=u.hostname)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        s.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise HAError("websocket handshake failed: " + head.split(b"\r\n", 1)[0].decode("latin1"))
        self.s, self.b = s, bytearray(rest)

    def _exact(self, n):
        while len(self.b) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise HAError("websocket closed")
            self.b += chunk
        out = bytes(self.b[:n])
        del self.b[:n]
        return out

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._exact(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if b1 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
            if op == 8:
                raise HAError("server closed the websocket")
            if op == 9:  # ping -> pong
                self._send(data, 0xA)
                continue
            if op in (0, 1, 2):
                parts.append(data)
                if b0 & 0x80:
                    return json.loads(b"".join(parts).decode())

    def _send(self, data, opcode=0x1):
        head = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mk = secrets.token_bytes(4)
        self.s.sendall(bytes(head) + mk + bytes(x ^ mk[i % 4] for i, x in enumerate(data)))

    def send(self, obj):
        self._send(json.dumps(obj).encode())


class HA:
    """ha("config/entity_registry/list") runs a websocket command. ha.rest(...) calls the REST API."""

    def __init__(self, cfg, timeout=300):
        self.rest_base, ws_url, self.token, self.sup_token = endpoints(cfg)
        self.verify_tls = cfg["ha"].get("verify_tls", True)
        self.ws = _WS(ws_url, timeout, self.verify_tls)
        if self.ws.recv().get("type") != "auth_required":
            raise HAError("unexpected websocket greeting")
        self.ws.send({"type": "auth", "access_token": self.token})
        if self.ws.recv().get("type") != "auth_ok":
            raise HAError("websocket authentication failed (token)")
        self.i = 0

    def __call__(self, type_, **kw):
        self.i += 1
        self.ws.send({"id": self.i, "type": type_, **kw})
        while True:
            m = self.ws.recv()
            if m.get("id") == self.i and m.get("type") == "result":
                if not m["success"]:
                    raise HAError(f"{type_} -> {m['error']}")
                return m["result"]

    def rest(self, method, path, body=None, timeout=60):
        return _http(self.rest_base + path, method, body, self.token, timeout, self.verify_tls)


class Supervisor:
    """Supervisor API (add-on control). Only available inside an add-on with $SUPERVISOR_TOKEN."""

    def __init__(self):
        self.token = os.environ.get("SUPERVISOR_TOKEN")

    @property
    def available(self):
        return bool(self.token)

    def get(self, path):
        return _http("http://supervisor" + path, "GET", None, self.token, 60)["data"]

    def post(self, path, body=None, timeout=300):
        return _http("http://supervisor" + path, "POST", body, self.token, timeout)


def _http(url, method, body, token, timeout, verify_tls=True):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ctx = None
    if url.startswith("https://") and not verify_tls:
        ctx = ssl._create_unverified_context()  # noqa: S323  (opt-in via ha.verify_tls=false)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise HAError(f"{method} {url} -> HTTP {e.code}: {e.read()[:400].decode(errors='replace')}") from e
    return json.loads(raw) if raw else None
