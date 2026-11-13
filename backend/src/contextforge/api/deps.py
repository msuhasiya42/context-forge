"""What routes get injected: the application service, the settings, and the auth check.

``create_app`` puts the service and settings on ``app.state``; these read them back per request.
"""

import hmac
from typing import Annotated

from fastapi import Depends, Header, Request

from contextforge.core.services.application import Application, ServiceError
from contextforge.core.settings import Settings


def get_service(request: Request) -> Application:
    service: Application = request.app.state.service
    return service


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


ServiceDep = Annotated[Application, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def authorized(
    settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Without a configured dev token every caller is the dev user; with one, it is required."""
    if settings.dev_token is not None and not hmac.compare_digest(
        authorization or "", f"Bearer {settings.dev_token.get_secret_value()}"
    ):
        raise ServiceError(401, "unauthenticated", "A valid development token is required.")
