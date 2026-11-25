"""Standard-library HTTP adapter for customer-service queries and ingestion."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .engine import LossService
from .store import Store

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>观赛损失客服台</title>
<style>
body{font-family:system-ui,"Noto Sans CJK SC",sans-serif;margin:2rem;max-width:1100px}
input,button{font-size:1rem;padding:.45rem;margin-right:.4rem}
pre{background:#f6f8fa;padding:1rem;border-radius:8px;overflow:auto;white-space:pre-wrap}
.muted{color:#666}
</style></head><body>
<h1>观赛损失与补偿客服台</h1>
<p>输入订单号查询按当时材料形成的决定、校准依据、确定/可能片段、回执和可补缺口。</p>
<div>
 <input id="order" placeholder="例如 b2" value="b2">
 <input id="at" placeholder="as_of 逻辑时钟（可空）" style="width:16rem">
 <button onclick="queryOrder()">查询订单</button>
 <button onclick="queryQueue()">复核队列</button>
</div>
<p class="muted">接口：<code>GET /api/order?order=b2[&at=10250]</code>、<code>GET /api/queue</code>、<code>POST /api/ingest</code>。</p>
<pre id="out">等待查询…</pre>
<script>
function show(x){document.getElementById('out').textContent=JSON.stringify(x,null,2);}
async function queryOrder(){
 const o=document.getElementById('order').value.trim();
 const at=document.getElementById('at').value.trim();
 let u='/api/order?order='+encodeURIComponent(o);
 if(at)u+='&at='+encodeURIComponent(at);
 const r=await fetch(u); show(await r.json());
}
async function queryQueue(){const r=await fetch('/api/queue');show(await r.json());}
</script>
</body></html>
"""


def create_handler(db_path: str | Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LossService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep test output clean; errors are still returned to the client.
            return

        def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = INDEX_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/health":
                self._json(200, {"ok": True})
                return
            query = parse_qs(parsed.query)
            try:
                with Store(db_path) as store:
                    service = LossService.from_store(store)
                    if parsed.path == "/api/order":
                        order = query.get("order", [""])[0]
                        if not order:
                            self._json(400, {"error": "缺少 order"})
                            return
                        at = int(query["at"][0]) if query.get("at") else None
                        self._json(200, service.order(order, at))
                        return
                    if parsed.path == "/api/queue":
                        at = int(query["at"][0]) if query.get("at") else None
                        self._json(200, service.queue(at))
                        return
            except KeyError as error:
                message = error.args[0] if error.args else str(error)
                self._json(404, {"error": message})
                return
            except (ValueError, TypeError) as error:
                self._json(400, {"error": str(error)})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/ingest":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                with Store(db_path) as store:
                    if "directory" in payload:
                        inserted = store.ingest_directory(payload["directory"], batch=payload.get("batch"))
                    elif "table" in payload and "records" in payload:
                        inserted = {
                            payload["table"]: store.ingest_json_records(
                                payload["table"], payload["records"]
                            )
                        }
                    elif "tables" in payload:
                        inserted = store.ingest_tables(payload["tables"], batch=payload.get("batch"))
                    else:
                        raise ValueError("需要 directory、table+records 或 tables")
                self._json(200, {"inserted": inserted})
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})

    return Handler


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), create_handler(db_path))
    return server
