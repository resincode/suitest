"""Tests for ``GET /api/v1/inbox``.

Six kinds (``DEPLOY_GATE_FAIL``, ``MANUAL_RUN_FAIL``, ``MCP_HEALTH``,
``FLAKY_PROMOTION``, ``AGENT_GENERATION``, ``AGENT_DIAGNOSIS``) remain a wire
shape stub (CRITICAL C4) — no aggregator exists yet. ``WORKSPACE_INVITE``
(M1e-9) is the first real kind: pending invites addressed to the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from suitest_shared.domain.enums import Role

if TYPE_CHECKING:
    from api_harness import ApiDb


@pytest.mark.asyncio
async def test_inbox_returns_empty_envelope_with_no_pending_invites(api_db: ApiDb) -> None:
    user = await api_db.seed_user(email="inbox@example.com")
    ws = await api_db.member_workspace(user, slug="inbox-ws")
    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": ws.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["unreadCount"] == 0


@pytest.mark.asyncio
async def test_inbox_does_not_require_workspace_membership(api_db: ApiDb) -> None:
    """Pending invites are addressed to the caller's email, not scoped to a
    workspace they already belong to — the endpoint only needs a session."""
    user = await api_db.seed_user(email="inbox-403@example.com")
    other = await api_db.seed_workspace(slug="inbox-403-other", name="Other")
    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": other.id})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_inbox_lists_pending_workspace_invite(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    async with api_db.client(admin) as admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    async with api_db.client(alice) as alice_client:
        resp = await alice_client.get("/api/v1/inbox")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unreadCount"] == 1
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["id"] == invitation_id
        assert item["kind"] == "WORKSPACE_INVITE"
        assert item["title"] == "Admin invited you to Acme"

        approved = await alice_client.post(f"/api/v1/invitations/{invitation_id}/approve")
        assert approved.status_code == 204

        after = await alice_client.get("/api/v1/inbox")
        assert after.json()["items"] == []
