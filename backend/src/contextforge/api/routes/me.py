"""The signed-in user's profile: instructions and preferences."""

from typing import Any

from fastapi import APIRouter, Depends

from contextforge.api.deps import ServiceDep, authorized
from contextforge.api.schemas import MePatch
from contextforge.core.entities import wire

router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(authorized)])


@router.get("")
def get_me(service: ServiceDep) -> dict[str, Any]:
    return wire(service.me())


@router.patch("")
def patch_me(body: MePatch, service: ServiceDep) -> dict[str, Any]:
    return wire(service.update_me(body.model_dump(exclude_unset=True)))
