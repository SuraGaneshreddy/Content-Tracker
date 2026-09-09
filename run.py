#!/usr/bin/env python3
"""
Development launcher.

    python3 run.py

Reads HOST / PORT from .env (default 0.0.0.0:8000), creates the database if it
is missing, seeds the reference data, and starts uvicorn with auto-reload.

For production, run uvicorn directly instead — see README.md §10:

    uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
"""

from __future__ import annotations

import sys


def main() -> int:
    # Importing config loads .env before anything else reads the environment.
    from app.config import settings

    from app.database import init_db

    init_db()

    if settings.secret_key in ("", "insecure-dev-key-change-me") and settings.is_production:
        print(
            "\nERROR: APP_ENV=production but SECRET_KEY is still the placeholder.\n"
            "Generate one with:\n"
            '    python3 -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            "and put it in .env\n",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    reload = not settings.is_production
    print(f"\n  📚 {settings.app_name}")
    print(f"     http://localhost:{settings.port}   (env: {settings.app_env})")
    print(f"     database: {settings.database_url}")
    print("     Ctrl+C to stop\n")

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
