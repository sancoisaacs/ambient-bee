#!/usr/bin/env python3
"""
Ambient Bee Local App
A dependency-free local dashboard for the existing Ambient Bee engine.

Run:
    python bee_web.py

Then open:
    http://127.0.0.1:8787

The server binds to localhost only. It does not expose the memory vault to the network.
"""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import ambient_bee as bee

HOST = "127.0.0.1"
PORT = 8787


def db_rows():
    con = bee.db()
    try:
        recordings = con.execute(
            """SELECT r.id, r.file, r.recorded_at, r.duration_s, r.status,
                      m.summary, m.todos, m.people, m.followups, m.decisions, m.tags
               FROM recordings r
               LEFT JOIN memories m ON m.rec_id=r.id
               WHERE r.status='ok'
               ORDER BY r.recorded_at DESC"""
        ).fetchall()
        todos = con.execute(
            "SELECT id,text,created,done_at,explicit FROM todos ORDER BY done_at IS NOT NULL, created DESC"
        ).fetchall()
        return recordings, todos
    finally:
        con.close()


def state():
    recordings, todos = db_rows()
    open_todos = [dict(t) for t in todos if not t["done_at"]]
    return {
        "recordings": len(recordings),
        "hours": round(sum((r["duration_s"] or 0) for r in recordings) / 3600, 2),
        "open_todos": len(open_todos),
        "latest": dict(recordings[0]) if recordings else None,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, content, content_type="application/json; charset=utf-8"):
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = Path(__file__).with_name("web").joinpath("index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, json.dumps(state(), ensure_ascii=False))
        if u.path == "/api/daily":
            date = parse_qs(u.query).get("date", [""])[0]
            path = bee.VAULT_DIR / "Daily" / f"{date}.md"
            if not date or not path.exists():
                return self._send(404, json.dumps({"error": "Daily log not found"}))
            return self._send(200, json.dumps({"date": date, "markdown": path.read_text(encoding="utf-8")}, ensure_ascii=False))
        if u.path == "/api/todos":
            _, todos = db_rows()
            return self._send(200, json.dumps([dict(t) for t in todos], ensure_ascii=False))
        if u.path == "/api/search":
            q = parse_qs(u.query).get("q", [""])[0].strip()
            if not q:
                return self._send(200, "[]")
            con = bee.db()
            try:
                hits = bee.search(con, q, k=12)
                out = [dict(h) for h in hits]
            finally:
                con.close()
            return self._send(200, json.dumps(out, ensure_ascii=False))
        if u.path == "/api/speak":
            text = parse_qs(u.query).get("text", [""])[0][:600]
            if text:
                bee._speak(text)
            return self._send(200, '{"ok":true}')
        if u.path == "/api/brief":
            con = bee.db()
            try:
                result = bee.brief(con)
            finally:
                con.close()
            return self._send(200, json.dumps({"text": result}, ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/run":
            def worker():
                bee.run_once()
            threading.Thread(target=worker, daemon=True).start()
            return self._send(202, '{"ok":true,"message":"Bee run started"}')
        if u.path == "/api/done":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            tid = int(body.get("id", 0))
            con = bee.db()
            try:
                bee.done_todo(con, tid)
            finally:
                con.close()
            return self._send(200, '{"ok":true}')
        return self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    print(f"🐝 Ambient Bee Local App → http://{HOST}:{PORT}")
    print("Localhost only. Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
