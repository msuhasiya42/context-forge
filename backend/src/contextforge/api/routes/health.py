"""Liveness and which build, providers and limits this process runs with. No auth."""

import os

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    build_info: dict[str, str] = request.app.state.build_info
    return {"status": "ok", "processId": str(os.getpid()), **build_info}
