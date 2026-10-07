"""Live dashboard: python dashboard.py  ->  http://localhost:8000"""
import html
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA = Path(os.getenv("INTELLIDOC_DATA", "data"))


def lines(name, n=15):
    p = DATA / name
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines()[-n:]][::-1]


def records():
    db = DATA / "intellidoc.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as c:
        return c.execute("SELECT system, source, data, created_at FROM records "
                         "ORDER BY id DESC LIMIT 15").fetchall()


def table(headers, rows):
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                   for r in rows) or f"<tr><td colspan={len(headers)}>none yet</td></tr>"
    return f"<table><tr>{th}</tr>{body}</table>"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ev = [(e["ts"][11:19], e["node"], e["source"], e["status"], e["doc_type"],
               e["retries"], "; ".join(e["errors"])[:80]) for e in lines("events.jsonl", 20)]
        rq = [(e["queued_at"][11:19], e["source"], e["doc_type"], "; ".join(e["errors"])[:100])
              for e in lines("review_queue.jsonl")]
        page = f"""<meta http-equiv=refresh content=3><title>IntelliDoc</title>
<style>body{{font:14px sans-serif;margin:2rem}}table{{border-collapse:collapse;margin-bottom:2rem}}
td,th{{border:1px solid #ccc;padding:4px 10px;text-align:left}}th{{background:#eee}}</style>
<h2>Live events</h2>{table(['time','node','source','status','type','retries','last error'], ev)}
<h2>Integrated records</h2>{table(['system','source','data','created'], records())}
<h2>Human review queue</h2>{table(['queued','source','type','errors'], rq)}"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(page.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("Dashboard on http://localhost:8000")
    HTTPServer(("", 8000), H).serve_forever()
