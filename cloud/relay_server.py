#!/usr/bin/env python3
"""
relay_server.py — the hosted RELAY HUB for the manager cloud control plane.

This is the ONE new networking primitive the hosted product needs (see
../PLAN.md). It is a dumb, security-neutral message mover: it holds a
persistent outbound long-poll from each machine's `relay_client.py`, mirrors
the METADATA that client pushes up, and queues commands the dashboard sends
down. It executes NOTHING and never sees code — the machine is the trust
boundary, not this server (a compromised relay cannot, by itself, run
anything: arming is local-only, enforced in relay_client.py).

Transport is plain HTTP long-poll (not WebSocket) on purpose: the daemon
dials OUT to here with stdlib urllib (works behind NAT, no inbound ports),
and this server is stdlib http.server — so the whole package keeps its
zero-pip-install property. WebSocket is a later latency optimization.

Phase 0 scope: in-memory store, dev tokens accepted as-is. Real per-user
hashed/revocable tokens + Postgres are Phase 2 (see PLAN.md).

Run:  python3 relay_server.py [--port 8787]
"""
import argparse
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_MAX_WAIT = 25          # seconds a long-poll is held before returning empty
MACHINE_STALE_AFTER = 60    # seconds without a poll/state before "offline"

_LOCK = threading.Lock()
# token -> machine record. Dev tokens are accepted as-is (Phase 2 hashes them).
_MACHINES = {}
_CMD_SEQ = 0


def _machine(token):
    """Get-or-create the record for a machine token (caller holds _LOCK)."""
    m = _MACHINES.get(token)
    if m is None:
        m = {"name": None, "state": {}, "state_ts": 0,
             "queue": deque(), "results": {}, "event": threading.Event(),
             "last_seen": 0}
        _MACHINES[token] = m
    return m


def _online(m):
    return (time.time() - m["last_seen"]) < MACHINE_STALE_AFTER


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    # ---- machine side (the daemon's relay_client dials these) ----

    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        if p in ("/", "/index.html"):
            return self._serve_dashboard()
        if p == "/relay/poll":
            # Long-poll: block until a command is queued for this machine, or
            # POLL_MAX_WAIT elapses (then return empty so the client re-polls).
            token = (qs.get("token") or [""])[0]
            if not token:
                return self._send(400, {"error": "token required"})
            with _LOCK:
                m = _machine(token)
                m["last_seen"] = time.time()
                if m["queue"]:
                    return self._send(200, {"command": m["queue"].popleft()})
                ev = m["event"]
                ev.clear()
            got = ev.wait(POLL_MAX_WAIT)
            with _LOCK:
                m = _machine(token)
                m["last_seen"] = time.time()
                if got and m["queue"]:
                    return self._send(200, {"command": m["queue"].popleft()})
            return self._send(200, {"command": None})
        if p == "/dash/state":
            # what a dashboard renders — the metadata the machine last pushed up
            token = (qs.get("token") or [""])[0]
            with _LOCK:
                m = _MACHINES.get(token)
                if not m:
                    return self._send(200, {"online": False, "state": {}, "name": None})
                return self._send(200, {"online": _online(m), "name": m["name"],
                                        "state": m["state"], "state_ts": m["state_ts"]})
        if p == "/dash/result":
            token = (qs.get("token") or [""])[0]
            cid = (qs.get("cmd_id") or [""])[0]
            with _LOCK:
                m = _MACHINES.get(token) or {}
                return self._send(200, {"result": (m.get("results") or {}).get(cid)})
        if p == "/relay/machines":
            # dev/debug: list known machines (no secrets)
            with _LOCK:
                out = [{"name": m["name"], "online": _online(m),
                        "queued": len(m["queue"])} for m in _MACHINES.values()]
            return self._send(200, {"machines": out})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()
        token = (body.get("token") or "").strip()
        if p == "/relay/state":
            # the machine pushes its filtered metadata snapshot up
            if not token:
                return self._send(400, {"error": "token required"})
            with _LOCK:
                m = _machine(token)
                m["last_seen"] = time.time()
                if body.get("name"):
                    m["name"] = body["name"]
                m["state"] = body.get("state", {})
                m["state_ts"] = time.time()
            return self._send(200, {"ok": True})
        if p == "/relay/ack":
            cid = str(body.get("cmd_id", ""))
            with _LOCK:
                m = _machine(token)
                m["results"][cid] = body.get("result")
            return self._send(200, {"ok": True})
        if p == "/dash/command":
            # the dashboard (user side) queues a command for the machine
            if not token:
                return self._send(400, {"error": "token required"})
            global _CMD_SEQ
            with _LOCK:
                _CMD_SEQ += 1
                cid = str(_CMD_SEQ)
                cmd = {"id": cid, "type": body.get("type", ""),
                       "payload": body.get("payload", {}), "ts": time.time()}
                m = _machine(token)
                m["queue"].append(cmd)
                m["event"].set()      # wake the held long-poll immediately
            return self._send(200, {"ok": True, "id": cid})
        return self._send(404, {"error": "not found"})

    def _serve_dashboard(self):
        path = os.path.join(HERE, "dashboard.html")
        try:
            with open(path, "rb") as f:
                return self._send(200, f.read(), "text/html")
        except OSError:
            return self._send(200, "<h1>relay up</h1>", "text/html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("RELAY_PORT", "8787")))
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"relay up: http://127.0.0.1:{args.port}   (dashboard at /)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
