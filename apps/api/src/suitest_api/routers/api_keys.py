"""Programmatic API keys — Settings → API keys / MCP setup (docs/API.md §3.x).

Surface:

* ``GET    /api/v1/workspaces/:id/api-keys``        — list live keys (members)
* ``POST   /api/v1/workspaces/:id/api-keys``        — mint a key, plaintext once (ADMIN+)
* ``DELETE /api/v1/workspaces/:id/api-keys/:keyId`` — revoke (ADMIN+)

Keys authenticate MCP / SDK / CI clients to this workspace. Only the SHA-256
hash is stored; the plaintext is returned exactly once, on creation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from suitest_db.models.api_key import ApiKey
from suitest_shared.domain.enums import Role

from suitest_api.auth.db import get_async_session
from suitest_api.deps.api_key import ApiKeyPrincipal, require_api_key
from suitest_api.deps.role import require_role
from suitest_api.deps.scope import TenantContext
from suitest_api.deps.tier import workspace_llm_status
from suitest_api.schemas.api_keys import (
    ApiKeyCreated,
    ApiKeyCreateRequest,
    ApiKeyItem,
    ApiKeyList,
    ApiKeyWhoami,
)
from suitest_api.services import api_key_service

router = APIRouter(prefix="/api/v1", tags=["api-keys"])

#: Minting/revoking a key is QA+; VIEWER is excluded because a key authenticates
#: as ``Role.QA`` (see ``deps/api_key.py``) — letting read-only members mint one
#: would be a privilege escalation.
_WRITER_ROLES = {Role.QA, Role.ADMIN, Role.OWNER}
_ADMIN_ROLES = {Role.ADMIN, Role.OWNER}


@router.get("/api-keys/whoami", response_model=ApiKeyWhoami)
async def whoami(
    principal: ApiKeyPrincipal = Depends(require_api_key),
    session: AsyncSession = Depends(get_async_session),
) -> ApiKeyWhoami:
    """Verify a key: returns the workspace it authenticates to. 401 if invalid.

    Used by the CLI / MCP setup to confirm ``SUITEST_API_KEY`` works before use.
    """
    return ApiKeyWhoami(
        workspace_id=principal.workspace_id,
        key_id=principal.key_id,
        key_name=principal.key_name,
        llm_status=await workspace_llm_status(session, principal.workspace_id),
    )


def _to_item(row: ApiKey, *, include_key: bool) -> ApiKeyItem:
    return ApiKeyItem(
        id=row.id,
        name=row.name,
        key_prefix=row.key_prefix,
        # Decrypted full token so its owner (or an admin) can re-copy it. Only
        # the admin surface and the creating response ever carry it.
        key=row.key_encrypted if include_key else None,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


@router.get("/workspaces/{workspaceId}/api-keys", response_model=ApiKeyList)
async def list_keys(
    # QA+ may list keys. The list carries the decrypted token for keys created
    # before the encrypted column, so non-admins see only their own keys —
    # ``key`` stays populated for the admin surface only.
    ctx: TenantContext = Depends(require_role(_WRITER_ROLES)),
    session: AsyncSession = Depends(get_async_session),
) -> ApiKeyList:
    admin = ctx.role in _ADMIN_ROLES
    rows = await api_key_service.list_api_keys(
        session, ctx.workspace_id, created_by=None if admin else ctx.user_id
    )
    return ApiKeyList(items=[_to_item(r, include_key=admin) for r in rows])


@router.post(
    "/workspaces/{workspaceId}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    body: ApiKeyCreateRequest,
    ctx: TenantContext = Depends(require_role(_WRITER_ROLES)),
    session: AsyncSession = Depends(get_async_session),
) -> ApiKeyCreated:
    row, token = await api_key_service.create_api_key(
        session,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        name=body.name,
        expires_in_days=body.expires_in_days,
    )
    await session.commit()
    item = _to_item(row, include_key=True)
    # ``item.key`` already carries the token (decrypted from the row); ensure the
    # freshly-minted plaintext is returned even if encryption is unconfigured.
    return ApiKeyCreated(**{**item.model_dump(), "key": token})


@router.delete(
    "/workspaces/{workspaceId}/api-keys/{keyId}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_key(
    keyId: str,
    ctx: TenantContext = Depends(require_role(_WRITER_ROLES)),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    row = await api_key_service.revoke_api_key(
        session,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        key_id=keyId,
        owner_id=None if ctx.role in _ADMIN_ROLES else ctx.user_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api key not found")
    await session.commit()
