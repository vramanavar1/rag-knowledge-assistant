"""Authentication and department-scoped authorization.

What this is
------------
A development stand-in: a static bearer-token table mapping tokens to a
principal with a department scope, so the access-control behaviour is real and
demonstrable end to end.  The *authorization* half -- department claims driving
a pre-filter inside the search query -- is exactly what production does.  Only
the *authentication* half is stubbed.

What production does instead
----------------------------
Microsoft Entra ID issues the token; the API validates it against the tenant's
JWKS (issuer, audience, expiry, signature) and reads group or app-role claims.
Those claims map to the same ``Principal.departments`` list this module
produces, so nothing downstream changes.  The mapping table becomes a group ->
department lookup in configuration.

Why the filter and not a post-filter
------------------------------------
See ``Retriever._filters_for``: trimming after retrieval lets documents the
caller cannot read occupy top-k slots, which both degrades their answers and
leaks the existence of those documents.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, status

from rag.ingest.metadata import DEPARTMENTS
from rag.models import Principal
from rag.observability.tracing import get_logger

log = get_logger(__name__)

# Demo tokens. Every one of these is public by design -- they grant access to a
# fictional corpus in a local process, and the README says so. Real deployments
# never ship a table like this.
_DEV_TOKENS: dict[str, Principal] = {
    "demo-admin": Principal(
        user_id="admin@northwindtraders.example",
        display_name="Avery Chen (IT Admin)",
        departments=["*"],
        role="admin",
    ),
}
for _department in DEPARTMENTS:
    _DEV_TOKENS[f"demo-{_department.lower()}"] = Principal(
        user_id=f"{_department.lower()}.user@northwindtraders.example",
        display_name=f"{_department} team member",
        departments=[_department],
        role="employee",
    )

ANONYMOUS = Principal(
    user_id="anonymous",
    display_name="Anonymous",
    departments=["*"],
    role="employee",
)


def available_tokens() -> dict[str, dict[str, object]]:
    """Advertised to the UI so the department picker can be built from it."""
    return {
        token: {
            "display_name": principal.display_name,
            "departments": principal.departments,
            "role": principal.role,
        }
        for token, principal in _DEV_TOKENS.items()
    }


def _allow_anonymous() -> bool:
    return (os.getenv("API_ALLOW_ANONYMOUS", "true").strip().lower()
            in {"1", "true", "yes", "on"})


def resolve_principal(authorization: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: bearer token -> principal."""
    if not authorization:
        if _allow_anonymous():
            return ANONYMOUS
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="expected 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = _DEV_TOKENS.get(token.strip())
    if principal is None:
        log.warning("rejected unknown bearer token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this operation requires the admin role",
        )
