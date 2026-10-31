"""Export the OpenAPI documents of both agents into docs/openapi/.

    uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "openapi"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DEVOPS_AGENT_HOME", tempfile.mkdtemp())
    from devops_local.daemon import create_app as local_app
    from devops_local.service import LocalAgent
    from devops_server.main import create_app as server_app

    docs = {
        "server-agent.json": server_app(init_db=False).openapi(),
        "local-agent.json": local_app(LocalAgent(), background=False).openapi(),
    }
    for name, doc in docs.items():
        (OUT / name).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote docs/openapi/{name} ({len(doc.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
