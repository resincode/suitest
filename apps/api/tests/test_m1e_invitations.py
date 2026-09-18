"""M1e invitation endpoint tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_harness import ApiDb
from fastapi_users.password import PasswordHelper
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from suitest_api.auth.db import get_async_session
from suitest_api.auth.manager import current_active_user
from suitest_api.main import create_app
from suitest_db.models.tenancy import Membership
from suitest_db.models.user import User
from suitest_shared.domain.enums import Role


async def _client_for(api_db: ApiDb, user: User | None) -> AsyncClient:
    app = create_app()

    async def _override_session() -> AsyncIterator[object]:
        async with api_db.maker() as session:
            yield session

    async def _override_current_user() -> User:
        assert user is not None
        async with api_db.maker() as session:
            db_user = await session.get(User, user.id)
            assert db_user is not None
            return db_user

    app.dependency_overrides[get_async_session] = _override_session
    if user is not None:
        app.dependency_overrides[current_active_user] = _override_current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_admin_can_create_validate_resend_revoke_invitation(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    client = await _client_for(api_db, admin)
    async with client:
        created = await client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["link"].startswith("http://localhost:3000/accept-invite?token=")
        token = payload["link"].split("token=", 1)[1]

        validated = await client.get(f"/api/v1/invitations/validate?token={token}")
        assert validated.status_code == 200
        assert validated.json()["email"] == "qa@example.com"

        resent = await client.post(f"/api/v1/invitations/{payload['id']}/resend")
        assert resent.status_code == 200
        new_token = resent.json()["link"].split("token=", 1)[1]
        assert new_token != token

        revoked = await client.post(f"/api/v1/invitations/{payload['id']}/revoke")
        assert revoked.status_code == 204

        invalid = await client.get(f"/api/v1/invitations/validate?token={new_token}")
        assert invalid.status_code == 404


@pytest.mark.asyncio
async def test_viewer_cannot_create_invitation(api_db: ApiDb) -> None:
    viewer = await api_db.seed_user(email="viewer@example.com", name="Viewer")
    ws = await api_db.member_workspace(viewer, slug="acme", name="Acme")
    client = await _client_for(api_db, viewer)
    async with client:
        response = await client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_accept_invite_creates_user_membership_and_session(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)
    authed = await _client_for(api_db, admin)
    async with authed:
        created = await authed.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )
    token = created.json()["link"].split("token=", 1)[1]

    public = await _client_for(api_db, None)
    async with public:
        accepted = await public.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": token,
                "email": "qa@example.com",
                "name": "QA User",
                "password": "secret123",
            },
        )

    assert accepted.status_code == 200
    assert "set-cookie" in accepted.headers
    async with api_db.maker() as session:
        user = await session.scalar(select(User).filter_by(email="qa@example.com"))
        assert user is not None
        assert user.name == "QA User"
        assert PasswordHelper().verify_and_update("secret123", user.hashed_password)[0]
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == user.id
            )
        )
        assert membership is not None
        assert membership.role == Role.QA


@pytest.mark.asyncio
async def test_invite_rejects_existing_workspace_member(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    existing = await api_db.seed_user(email="qa@example.com", name="QA")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)
    await api_db.seed_membership(workspace_id=ws.id, user_id=existing.id, role=Role.QA)

    client = await _client_for(api_db, admin)
    async with client:
        response = await client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_accept_invite_requires_matching_email(api_db: ApiDb) -> None:
    """A forwarded link must not let B register as A: email must match."""
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)
    authed = await _client_for(api_db, admin)
    async with authed:
        created = await authed.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "QA"},
        )
    token = created.json()["link"].split("token=", 1)[1]

    public = await _client_for(api_db, None)
    async with public:
        response = await public.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": token,
                "email": "bob@example.com",
                "name": "Bob",
                "password": "secret123",
            },
        )

    assert response.status_code == 403
    async with api_db.maker() as session:
        assert await session.scalar(select(User).filter_by(email="bob@example.com")) is None


@pytest.mark.asyncio
async def test_accept_invite_existing_active_account_requires_login(api_db: ApiDb) -> None:
    """An active account is linked, never taken over: no reset, no session."""
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    existing = await api_db.seed_user(email="alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)
    original_hash = existing.hashed_password
    authed = await _client_for(api_db, admin)
    async with authed:
        created = await authed.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "QA"},
        )
    token = created.json()["link"].split("token=", 1)[1]

    public = await _client_for(api_db, None)
    async with public:
        response = await public.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": token,
                "email": "alice@example.com",
                "name": "Alice Hijacker",
                "password": "attacker-pass",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["requires_login"] is True
    assert "set-cookie" not in response.headers
    async with api_db.maker() as session:
        user = await session.get(User, existing.id)
        assert user is not None
        # Password and name are untouched by the invite acceptance.
        assert user.hashed_password == original_hash
        assert user.name == "Alice"
        assert user.is_active
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == user.id
            )
        )
        assert membership is not None
        assert membership.role == Role.QA


@pytest.mark.asyncio
async def test_lookup_invitee_reports_existing_and_unknown_email(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    await api_db.seed_user(email="alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    client = await _client_for(api_db, admin)
    async with client:
        known = await client.get(f"/api/v1/workspaces/{ws.id}/invitations/lookup?email=alice@example.com")
        assert known.status_code == 200
        assert known.json() == {"exists": True, "name": "Alice"}

        unknown = await client.get(
            f"/api/v1/workspaces/{ws.id}/invitations/lookup?email=nobody@example.com"
        )
        assert unknown.status_code == 200
        assert unknown.json() == {"exists": False, "name": None}


@pytest.mark.asyncio
async def test_lookup_invitee_forbidden_for_non_manager(api_db: ApiDb) -> None:
    viewer = await api_db.seed_user(email="viewer@example.com", name="Viewer")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=viewer.id, role=Role.VIEWER)

    client = await _client_for(api_db, viewer)
    async with client:
        response = await client.get(
            f"/api/v1/workspaces/{ws.id}/invitations/lookup?email=anyone@example.com"
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_approve_invitation_grants_membership_without_password(api_db: ApiDb) -> None:
    """The in-app path for an already-registered invitee: no token, no
    password, no "set your name" detour — just an authenticated approve."""
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    alice_client = await _client_for(api_db, alice)
    async with alice_client:
        mine = await alice_client.get("/api/v1/invitations/mine")
        assert mine.status_code == 200
        items = mine.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == invitation_id
        assert items[0]["workspace_name"] == "Acme"
        assert items[0]["role"] == "QA"
        assert items[0]["invited_by"] == "Admin"

        approved = await alice_client.post(f"/api/v1/invitations/{invitation_id}/approve")
        assert approved.status_code == 204

        # Resolved invites drop out of "mine".
        mine_after = await alice_client.get("/api/v1/invitations/mine")
        assert mine_after.json()["items"] == []

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == alice.id
            )
        )
        assert membership is not None
        assert membership.role == Role.QA


@pytest.mark.asyncio
async def test_approve_invitation_rejects_mismatched_email(api_db: ApiDb) -> None:
    """The M1e email-binding invariant also protects the in-app approve path:
    only the invited address may claim the invite, never whoever is logged in."""
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    await api_db.seed_user(email="alice@example.com", name="Alice")
    bob = await api_db.seed_user(email="bob@example.com", name="Bob")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    bob_client = await _client_for(api_db, bob)
    async with bob_client:
        response = await bob_client.post(f"/api/v1/invitations/{invitation_id}/approve")
    assert response.status_code == 403

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == bob.id
            )
        )
        assert membership is None


@pytest.mark.asyncio
async def test_decline_invitation_marks_declined_and_hides_from_mine(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "alice@example.com", "role": "VIEWER"},
        )
        invitation_id = created.json()["id"]

    alice_client = await _client_for(api_db, alice)
    async with alice_client:
        declined = await alice_client.post(f"/api/v1/invitations/{invitation_id}/decline")
        assert declined.status_code == 204

        mine_after = await alice_client.get("/api/v1/invitations/mine")
        assert mine_after.json()["items"] == []

        # Declining twice is a 404, not a silent no-op: the state is terminal.
        again = await alice_client.post(f"/api/v1/invitations/{invitation_id}/decline")
        assert again.status_code == 404

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == alice.id
            )
        )
        assert membership is None
