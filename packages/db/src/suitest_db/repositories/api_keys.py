"""Repository for programmatic API keys.

Query surface only — token generation, hashing, and audit belong to the API
service layer. Keys are looked up by their SHA-256 hash (never by plaintext).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from suitest_db.ids import new_id
from suitest_db.models.api_key import ApiKey

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class ApiKeyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        workspace_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        key_encrypted: str,
        created_by: uuid.UUID | None,
        expires_at: datetime | None,
    ) -> ApiKey:
        row = ApiKey(
            id=new_id(),
            workspace_id=workspace_id,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            key_encrypted=key_encrypted,
            created_by=created_by,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_active(
        self, workspace_id: str, created_by: uuid.UUID | None = None
    ) -> Sequence[ApiKey]:
        """Non-revoked keys for a workspace, newest first.

        ``created_by`` narrows the result to one creator's keys — the QA
        scoping: members may list what they minted, admins see everything.
        """
        stmt = select(ApiKey).where(
            ApiKey.workspace_id == workspace_id, ApiKey.revoked_at.is_(None)
        )
        if created_by is not None:
            stmt = stmt.where(ApiKey.created_by == created_by)
        result = await self.session.scalars(stmt.order_by(ApiKey.created_at.desc()))
        return result.all()

    async def get_by_id(self, workspace_id: str, key_id: str) -> ApiKey | None:
        row: ApiKey | None = await self.session.scalar(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.workspace_id == workspace_id)
        )
        return row

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        """Look up a live key by hash for authentication (any workspace).

        Returns ``None`` when the key is unknown, revoked, or expired — the
        caller treats all three identically (401), never leaking which.
        """
        row = await self.session.scalar(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            return None
        return row

    async def revoke(
        self, workspace_id: str, key_id: str, created_by: uuid.UUID | None = None
    ) -> ApiKey | None:
        """Revoke a key; ``created_by`` restricts revocation to the owner's keys.

        Returns ``None`` when the key does not exist, is already revoked, or
        (when ``created_by`` is set) belongs to someone else — callers surface
        all three as 404 without distinguishing them.
        """
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.workspace_id == workspace_id)
        if created_by is not None:
            stmt = stmt.where(ApiKey.created_by == created_by)
        row = await self.session.scalar(stmt)
        if row is None or row.revoked_at is not None:
            return None
        row.revoked_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def touch_last_used(self, key: ApiKey) -> None:
        key.last_used_at = datetime.now(UTC)
        await self.session.flush()
