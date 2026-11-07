"""Integration entities (implementation plan sections 3.7 to 3.9). No secret ever appears here."""

from enum import StrEnum
from typing import Any

from contextforge.core.entities import Entity


class TransportKind(StrEnum):
    MCP_HTTP = "mcp_http"
    MCP_STDIO = "mcp_stdio"
    NATIVE = "native"


class IntegrationTransport(Entity):
    kind: TransportKind
    server_url: str | None = None


class AuthKind(StrEnum):
    OAUTH2 = "oauth2"
    API_KEY = "api_key"
    NONE = "none"


class IntegrationAuth(Entity):
    kind: AuthKind
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    scopes: list[str]


class Integration(Entity):
    """Developer-maintained provider configuration checked into the repository."""

    integration_id: str
    name: str
    transport: IntegrationTransport
    auth: IntegrationAuth
    # Secret store name for the OAuth client secret, never the secret itself.
    client_credential_ref: str | None = None
    enabled: bool


class ConnectionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class IntegrationConnection(Entity):
    """One user's connected account. Unique on (integration_id, user_id, external_account_id)."""

    connection_id: str
    user_id: str
    integration_id: str
    external_account_id: str
    granted_scopes: list[str] = []
    status: ConnectionStatus
    # Reference to stored tokens; the worker resolves it at call time.
    credential_ref: str
    created_at: str
    updated_at: str


class ToolPermission(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class IntegrationTool(Entity):
    """One operation a provider offers. Unique on (integration_id, name)."""

    # Derived as "integration_id:name".
    integration_tool_id: str
    integration_id: str
    # The server's own tool name, used on tools/call.
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    permission: ToolPermission
    # Empty means any active connection.
    required_scopes: list[str] = []
    enabled: bool
    created_at: str
