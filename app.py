#!/usr/bin/env python3
"""Convenience entry point: ``python app.py`` starts the customer-service HTTP API."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loss_service.web import serve  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    db = os.environ.get("LOSS_DB", str(Path(__file__).resolve().parent / ".runtime" / "loss.sqlite3"))
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    # First boot imports the shipped sample only when the database is empty.
    from loss_service.store import Store  # noqa: E402
    from intake import ROOT  # noqa: E402

    with Store(db) as store:
        if not store.table("programs"):
            store.ingest_directory(ROOT / "sample")
    httpd = serve(db, host, port)
    print(f"customer service on http://{host}:{port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
