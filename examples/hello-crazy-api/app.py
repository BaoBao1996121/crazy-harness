from __future__ import annotations

VERSION = "0.1.0"


def health() -> dict[str, str]:
    return {"status": "ok"}


def version() -> dict[str, str]:
    return {"version": VERSION}


try:
    from fastapi import FastAPI

    app = FastAPI(title="hello-crazy-api")

    @app.get("/health")
    def health_route() -> dict[str, str]:
        return health()

    @app.get("/version")
    def version_route() -> dict[str, str]:
        return version()

except ImportError:
    app = None
