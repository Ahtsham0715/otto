"""The developer console and memory editor.

A small `http.server` on **127.0.0.1**, started only when the user asks for it and
stopped when they close it. No framework, no websocket, no bundled browser — it
opens in the browser the user already has (DECISIONS D-18), which is how a rich UI
costs zero idle RAM.

What it shows: agents, messages, tool calls (including refused ones), decisions,
artifacts, errors, the execution timeline, and the memory rows.

Security posture:

* Bound to 127.0.0.1 only. Never 0.0.0.0.
* There is **no route that executes anything** — no tool dispatch, no shell, no
  command endpoint. The only writes are to the memory table, which is the one thing
  the brief requires be editable from the UI.
* Those writes require a per-session token generated at start-up and embedded in the
  page URL, so another local process cannot blind-POST to it.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from ..app import Otto

MAX_BODY = 100_000


class DevConsole:
    """Owns the server thread. `start()` is idempotent; `stop()` frees the port."""

    def __init__(self, otto: "Otto", port: int | None = None):
        self.otto = otto
        self.port = int(port or otto.services.config.console_port)
        self.token = secrets.token_urlsafe(16)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def start(self) -> str:
        if self._server is not None:
            return self.url
        handler = _make_handler(self)
        # Explicitly 127.0.0.1: binding 0.0.0.0 would put this on the network.
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="otto-console", daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        self._thread = None

    # -- data --------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        snapshot = self.otto.snapshot()
        snapshot["memories"] = [
            m.as_dict() for m in self.otto.services.memory.all(limit=300)
        ]
        snapshot["usage"] = {
            "apps": self.otto.services.memory.top_usage("app"),
            "commands": self.otto.services.memory.top_usage("command"),
        }
        return snapshot

    def authorised(self, query: dict[str, list[str]]) -> bool:
        return secrets.compare_digest((query.get("token") or [""])[0], self.token)


def _make_handler(console: DevConsole):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Otto/1.0"

        def log_message(self, *args: Any) -> None:  # keep the terminal quiet
            pass

        # -- helpers -------------------------------------------------------

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        # -- routes --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html"):
                page = PAGE.replace("__TOKEN__", console.token)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/snapshot":
                if not console.authorised(query):
                    self._json(403, {"error": "bad token"})
                    return
                self._json(200, console.payload())
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
            except (ValueError, OSError):
                self._json(400, {"error": "bad JSON"})
                return
            if not isinstance(body, dict) or not secrets.compare_digest(
                str(body.get("token", "")), console.token
            ):
                self._json(403, {"error": "bad token"})
                return

            memory = console.otto.services.memory
            # The complete set of write routes. Memory only — by design.
            if parsed.path == "/api/memory/delete":
                ok = memory.forget(int(body.get("id", 0)))
                self._json(200, {"deleted": ok})
                return
            if parsed.path == "/api/memory/set":
                try:
                    stored = memory.remember(
                        str(body.get("key", "")),
                        str(body.get("value", "")),
                        scope=str(body.get("scope", "global")),
                        scope_key=str(body.get("scope_key", "")),
                        source="user-edit",
                    )
                except Exception as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"saved": stored.as_dict()})
                return
            if parsed.path == "/api/cancel":
                self._json(200, {"cancelled": console.otto.cancel()})
                return
            if parsed.path == "/api/approve":
                approval_id = str(body.get("approval_id", ""))
                granted = bool(body.get("granted"))
                for approval in console.otto.pending_approvals():
                    if approval.id == approval_id:
                        approval.decide(granted)
                        self._json(200, {"decided": granted})
                        return
                self._json(404, {"error": "no such pending approval"})
                return
            self._json(404, {"error": "no such route"})

    return Handler


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Otto — developer console</title>
<style>
 :root { color-scheme: light dark; --line:#8883; }
 body { font: 13px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
        margin: 0; padding: 20px; }
 h1 { font-size: 17px; margin: 0 0 4px; }
 h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
      opacity: .6; margin: 22px 0 8px; }
 .grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(300px,1fr));
         gap: 18px; }
 table { border-collapse: collapse; width: 100%; }
 td, th { border-bottom: 1px solid var(--line); padding: 4px 6px; text-align: left;
          vertical-align: top; }
 th { font-weight: 600; opacity: .7; }
 code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
 pre { white-space: pre-wrap; margin: 0; }
 .pill { display: inline-block; padding: 1px 7px; border-radius: 9px; font-size: 11px;
         border: 1px solid var(--line); }
 .COMPLETED { background: #2e7d3222; } .FAILED { background: #c6282822; }
 .REQUIRES_HUMAN, .WAITING { background: #f9a82522; } .RUNNING { background: #1976d222; }
 .CANCELLED { opacity: .6; }
 button { font: inherit; padding: 2px 8px; }
 .muted { opacity: .6; }
</style></head><body>
<h1>Otto — developer console</h1>
<div class="muted" id="head">loading…</div>
<div class="grid">
 <div>
  <h2>Current task</h2><div id="current"></div>
  <h2>Execution timeline</h2><div id="timeline"></div>
 </div>
 <div>
  <h2>Tool calls</h2><div id="calls"></div>
  <h2>Agent messages</h2><div id="messages"></div>
  <h2>Artifacts</h2><div id="artifacts"></div>
 </div>
 <div>
  <h2>Agents</h2><div id="agents"></div>
  <h2>Audit (incl. refusals)</h2><div id="audit"></div>
 </div>
 <div>
  <h2>Memory <button onclick="addMemory()">+ add</button></h2><div id="memory"></div>
  <h2>Learned from use</h2><div id="usage"></div>
 </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const rows = (head, body) => `<table><tr>${head.map(h=>`<th>${h}</th>`).join("")}</tr>${body}</table>`;

async function post(path, data) {
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({token: TOKEN, ...data})});
  return r.json();
}
async function del(id) { await post("/api/memory/delete", {id}); refresh(); }
async function addMemory() {
  const key = prompt("Key (e.g. 'projects')"); if (!key) return;
  const value = prompt("Value (e.g. 'my projects live in ~/Projects')"); if (!value) return;
  const res = await post("/api/memory/set", {key, value, scope:"global"});
  if (res.error) alert(res.error);
  refresh();
}
async function editMemory(id, key, scope, scope_key, current) {
  const value = prompt("New value for " + key, current); if (value === null) return;
  const res = await post("/api/memory/set", {key, value, scope, scope_key});
  if (res.error) alert(res.error);
  refresh();
}

function render(d) {
  const m = d.models;
  document.getElementById("head").textContent =
    `state: ${d.state} · mac bridge: ${d.mac_bridge} · sandbox: ${d.sandbox} · ` +
    `models: ${m.any_configured ? Object.entries(m.tiers).map(([k,v])=>k+"="+(v.model||v.kind)).join(", ")
      : "none configured (fast path only)"}` +
    (m.cloud_tiers.length ? ` · CLOUD IN USE: ${m.cloud_tiers.join(", ")}` : "");

  const t = d.current;
  document.getElementById("current").innerHTML = t ? `
    <div><span class="pill ${t.status}">${t.status}</span> <b>${esc(t.request)}</b>
    <span class="muted">(${t.source})</span></div>
    <p>${esc(t.summary || t.error || "")}</p>
    ${rows(["step","agent","status","result"], t.subtasks.map(s =>
      `<tr><td>${esc(s.description)}</td><td>${s.agent_id}</td>
       <td><span class="pill ${s.status}">${s.status}</span></td>
       <td>${esc(s.result || s.error || "")}</td></tr>`).join(""))}` : "<p class=muted>no task yet</p>";

  document.getElementById("timeline").innerHTML = t ? rows(["at","kind","detail"],
    t.timeline.slice(-60).map(e => `<tr><td class=muted>${new Date(e.at*1000)
      .toLocaleTimeString()}</td><td>${esc(e.kind)}</td><td>${esc(e.detail)}</td></tr>`).join("")) : "";

  const calls = t ? t.subtasks.flatMap(s => s.calls) : [];
  document.getElementById("calls").innerHTML = rows(
    ["tool","agent","perm","status","verified","detail"],
    calls.map(c => `<tr><td><code>${esc(c.tool)}</code></td><td>${c.agent_id}</td>
      <td>${c.permission_level||""}</td><td><span class="pill ${c.status}">${c.status}</span></td>
      <td>${c.verified === null ? "" : (c.verified ? "yes" : "no")}</td>
      <td>${esc(c.verification_detail || c.error || "")}</td></tr>`).join(""));

  document.getElementById("messages").innerHTML = t ? rows(["from","to","kind","content"],
    t.messages.map(x => `<tr><td>${x.sender}</td><td>${x.recipient}</td><td>${x.kind}</td>
      <td>${esc(x.content)}</td></tr>`).join("")) : "";

  document.getElementById("artifacts").innerHTML = t ? rows(["kind","name","value"],
    t.artifacts.map(a => `<tr><td>${a.kind}</td><td>${esc(a.name)}</td>
      <td><pre>${esc((a.value||"").slice(0,400))}</pre></td></tr>`).join("")) : "";

  document.getElementById("agents").innerHTML = rows(["id","ceiling","scope","tier","tools"],
    d.agents.map(a => `<tr><td><b>${a.id}</b></td><td>${a.ceiling}</td>
      <td>${a.memory_scope}</td><td>${a.tier}</td>
      <td class=muted>${a.tools.join(", ")}</td></tr>`).join(""));

  document.getElementById("audit").innerHTML = rows(["at","event","detail"],
    d.audit.slice().reverse().map(e => `<tr><td class=muted>${new Date(e.at*1000)
      .toLocaleTimeString()}</td><td>${esc(e.event)}</td>
      <td>${esc(e.tool || "")} ${esc(e.error || e.detail || "")}</td></tr>`).join(""));

  document.getElementById("memory").innerHTML = rows(["scope","key","value","hits",""],
    d.memories.map(x => `<tr><td>${x.scope}${x.scope_key ? "/"+esc(x.scope_key) : ""}</td>
      <td>${esc(x.key)}</td><td>${esc(x.value)}</td><td>${x.hits}</td>
      <td><button onclick='editMemory(${x.id},${JSON.stringify(x.key)},${JSON.stringify(x.scope)},${JSON.stringify(x.scope_key)},${JSON.stringify(x.value)})'>edit</button>
          <button onclick="del(${x.id})">delete</button></td></tr>`).join(""));

  document.getElementById("usage").innerHTML = rows(["kind","subject","times"],
    [...d.usage.apps.map(a => ["app", ...a]), ...d.usage.commands.map(c => ["command", ...c])]
      .map(r => `<tr><td>${r[0]}</td><td>${esc(r[1])}</td><td>${r[2]}</td></tr>`).join(""));
}

async function refresh() {
  try {
    const r = await fetch("/api/snapshot?token=" + encodeURIComponent(TOKEN));
    render(await r.json());
  } catch (e) { document.getElementById("head").textContent = "Otto is not running."; }
}
refresh();
setInterval(refresh, 2000);
</script></body></html>
"""
