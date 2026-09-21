"""FastAPI application factory, lifespan management, and static file serving for AGYM Council."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agym import __version__ as core_version
from agym.council import __version__ as council_version
from agym.council.api.routes import router as api_router
from agym.council.api.security import CouncilSecurityMiddleware, IdempotencyMiddleware
from agym.council.providers.base import ProviderAdapter
from agym.council.recovery import reconcile_startup_crashes
from agym.council.storage import get_connection, init_db


def get_web_dist_dir() -> Path:
    """Return path to packaged production web distribution directory."""
    return Path(__file__).resolve().parent.parent / "web_dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize database and reconcile crash state on startup."""
    db_path = getattr(app.state, "db_path", None)
    provider_adapter = getattr(app.state, "provider_adapter", None)

    # 1. Initialize SQLite tables and indexes
    conn = init_db(db_path)
    try:
        # 2. Run startup crash reconciliation for any in-flight attempts
        try:
            reconciled = await reconcile_startup_crashes(conn, provider=provider_adapter)
            if reconciled:
                print(f"agym-council: Startup recovery reconciled {len(reconciled)} in-flight attempt(s).")
        except Exception as exc:
            print(f"agym-council: Startup crash reconciliation warning: {exc}")
    finally:
        conn.close()

    yield

    # Cleanup on shutdown if needed


def create_app(
    db_path: Path | str | None = None,
    provider_adapter: ProviderAdapter | None = None,
    web_dist_dir: Path | str | None = None,
) -> FastAPI:
    """Create and configure the local loopback AGYM Council FastAPI application."""
    app = FastAPI(
        title="AGYM Council API",
        version=council_version,
        description="Local multi-account, multi-worker AI council platform for Google Antigravity CLI.",
        lifespan=lifespan,
    )

    # Store configurations in app.state
    app.state.db_path = db_path
    app.state.provider_adapter = provider_adapter

    # Middlewares (applied in reverse order of execution)
    # 1. Idempotency middleware for mutating requests
    app.add_middleware(IdempotencyMiddleware, db_path=db_path)

    # 2. Security middleware: enforces loopback, origin validation, and X-Council-Session
    app.add_middleware(CouncilSecurityMiddleware, db_path=db_path)

    # 3. Restrictive CORS middleware limited strictly to loopback origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Include REST API routes
    app.include_router(api_router)

    # Web UI static assets
    dist_dir = Path(web_dist_dir).resolve() if web_dist_dir else get_web_dist_dir()
    index_file = dist_dir / "index.html"

    if dist_dir.is_dir() and index_file.is_file():
        # Mount assets subdirectory if present
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # Fallback route for SPA HTML5 history routing
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> Any:
            # If requesting an existing static file directly
            candidate = dist_dir / full_path
            if candidate.is_file() and not full_path.startswith("api/"):
                return FileResponse(candidate)
            # Default to index.html for client-side routing
            return FileResponse(index_file)
    else:
        # Minimal built-in welcome page when web_dist is not yet built
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def index_fallback() -> str:
            return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AGYM Council v{council_version}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; line-height: 1.6; color: #24292e; }}
    h1 {{ color: #1a73e8; }}
    code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
    a {{ color: #1a73e8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .card {{ border: 1px solid #e1e4e8; border-radius: 8px; padding: 16px; margin: 16px 0; background: #fafbfc; }}
  </style>
</head>
<body>
  <h1>AGYM Council</h1>
  <p>Local multi-account, multi-worker AI council service for Google Antigravity CLI.</p>
  <div class="card">
    <h3>Service Status: Running (127.0.0.1)</h3>
    <p>Version: <code>v{council_version}</code> (Core: <code>v{core_version}</code>)</p>
    <ul>
      <li><a href="/api/health">Health Check (<code>/api/health</code>)</a></li>
      <li><a href="/api/presets">Workflow Presets (<code>/api/presets</code>)</a></li>
      <li><a href="/docs">Interactive OpenAPI Documentation (<code>/docs</code>)</a></li>
    </ul>
  </div>
</body>
</html>"""

    return app


# Default singleton application instance for uvicorn
app = create_app()
