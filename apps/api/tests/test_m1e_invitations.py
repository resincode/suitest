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
from suitest_db.models.audit import AuditLog
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
        known = await client.get(
            f"/api/v1/workspaces/{ws.id}/invitations/lookup?email=alice@example.com"
        )
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
        inbox = await alice_client.get("/api/v1/inbox")
        assert inbox.status_code == 200
        items = inbox.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == invitation_id
        assert items[0]["kind"] == "WORKSPACE_INVITE"
        assert "Acme" in items[0]["title"]

        approved = await alice_client.post(f"/api/v1/invitations/{invitation_id}/approve")
        assert approved.status_code == 204

        # Resolved invites drop out of the inbox.
        inbox_after = await alice_client.get("/api/v1/inbox")
        assert inbox_after.json()["items"] == []

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == alice.id
            )
        )
        assert membership is not None
        assert membership.role == Role.QA

        audit_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.approve",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert audit_row is not None
        assert audit_row.workspace_id == ws.id
        assert audit_row.user_id == alice.id


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
            select(Membership).where(Membership.workspace_id == ws.id, Membership.user_id == bob.id)
        )
        assert membership is None


@pytest.mark.asyncio
async def test_decline_invitation_marks_declined_and_hides_from_inbox(api_db: ApiDb) -> None:
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

        inbox_after = await alice_client.get("/api/v1/inbox")
        assert inbox_after.json()["items"] == []

        # Declining twice is a 404, not a silent no-op: the state is terminal.
        again = await alice_client.post(f"/api/v1/invitations/{invitation_id}/decline")
        assert again.status_code == 404

    # The admin must be able to see the decline — it is not "still pending".
    admin_client_2 = await _client_for(api_db, admin)
    async with admin_client_2:
        listing = await admin_client_2.get(f"/api/v1/workspaces/{ws.id}/invitations")
        row = next(item for item in listing.json()["items"] if item["id"] == invitation_id)
        assert row["declined_at"] is not None
        assert row["accepted_at"] is None
        assert row["revoked_at"] is None

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == alice.id
            )
        )
        assert membership is None

        audit_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.decline",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert audit_row is not None
        assert audit_row.user_id == alice.id


@pytest.mark.asyncio
async def test_declined_invitation_cannot_be_accepted_via_stale_email_link(
    api_db: ApiDb,
) -> None:
    """A decline in the Inbox must invalidate the original email link too —
    otherwise a forwarded/cached link still grants access after the invitee
    said no."""
    admin = await api_db.seed_user(email="admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="acme", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "carol@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]
        token = created.json()["link"].split("token=", 1)[1]

    # Carol registers, declines in-app, then the original email link — which
    # she (or anyone who intercepted it) might still hold — must be dead too.
    carol = await api_db.seed_user(email="carol@example.com", name="Carol")
    carol_client = await _client_for(api_db, carol)
    async with carol_client:
        declined = await carol_client.post(f"/api/v1/invitations/{invitation_id}/decline")
        assert declined.status_code == 204

    public_client = await _client_for(api_db, None)
    async with public_client:
        validated = await public_client.get(f"/api/v1/invitations/validate?token={token}")
        assert validated.status_code == 404

        accepted = await public_client.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": token,
                "email": "carol@example.com",
                "name": "Carol",
                "password": "whatever123",
            },
        )
        assert accepted.status_code == 404

    async with api_db.maker() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == ws.id, Membership.user_id == carol.id
            )
        )
        assert membership is None


@pytest.mark.asyncio
async def test_create_invitation_writes_audit_row(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="audit-create-admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="audit-create-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    client = await _client_for(api_db, admin)
    async with client:
        created = await client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    async with api_db.maker() as session:
        audit_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.create",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert audit_row is not None
        assert audit_row.workspace_id == ws.id
        assert audit_row.user_id == admin.id
        assert audit_row.metadata_json is not None
        assert audit_row.metadata_json["email"] == "qa@example.com"


@pytest.mark.asyncio
async def test_revoke_and_resend_invitation_write_audit_rows(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="audit-rr-admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="audit-rr-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    client = await _client_for(api_db, admin)
    async with client:
        created = await client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

        resent = await client.post(f"/api/v1/invitations/{invitation_id}/resend")
        assert resent.status_code == 200

        revoked = await client.post(f"/api/v1/invitations/{invitation_id}/revoke")
        assert revoked.status_code == 204

    async with api_db.maker() as session:
        resend_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.resend",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert resend_row is not None
        assert resend_row.user_id == admin.id

        revoke_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.revoke",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert revoke_row is not None
        assert revoke_row.user_id == admin.id


@pytest.mark.asyncio
async def test_accept_invite_writes_audit_row(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="audit-accept-admin@example.com", name="Admin")
    ws = await api_db.seed_workspace(slug="audit-accept-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)
    authed = await _client_for(api_db, admin)
    async with authed:
        created = await authed.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "qa-accept@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]
    token = created.json()["link"].split("token=", 1)[1]

    public = await _client_for(api_db, None)
    async with public:
        accepted = await public.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": token,
                "email": "qa-accept@example.com",
                "name": "QA User",
                "password": "secret123",
            },
        )
        assert accepted.status_code == 200

    async with api_db.maker() as session:
        user = await session.scalar(select(User).filter_by(email="qa-accept@example.com"))
        assert user is not None
        audit_row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "invitation.accept",
                AuditLog.resource_id == invitation_id,
            )
        )
        assert audit_row is not None
        assert audit_row.workspace_id == ws.id
        assert audit_row.user_id == user.id


# ---------------------------------------------------------------------------
# WS broadcast — invitation.resolved (M1e-9 follow-up)
# ---------------------------------------------------------------------------


# mypy: warn_unused_ignores=False
async def _drain_one(pubsub: object, received: list[bytes]) -> None:
    await pubsub.get_message(ignore_subscribe_messages=False, timeout=1.0)  # type: ignore[attr-defined]
    for _ in range(5):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)  # type: ignore[attr-defined]
        if msg is not None:
            received.append(msg["data"])
            return


@pytest.mark.asyncio
async def test_approve_invitation_emits_ws_event(api_db: ApiDb) -> None:
    """Approving publishes ``invitation.resolved`` so the inviting admin's
    Members panel can refresh live instead of on a manual reload."""
    import fakeredis
    import fakeredis.aioredis
    from asgi_lifespan import LifespanManager

    admin = await api_db.seed_user(email="ws-approve-admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="ws-approve-alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="ws-approve-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "ws-approve-alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    server = fakeredis.FakeServer()
    redis_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    received: list[bytes] = []

    app = api_db.app_for(alice)
    app.state.ws_redis = redis_client
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"workspace:{ws.id}")

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            approved = await c.post(f"/api/v1/invitations/{invitation_id}/approve")
            assert approved.status_code == 204
            await _drain_one(pubsub, received)

    await pubsub.aclose()  # type: ignore[no-untyped-call]
    await redis_client.aclose()  # type: ignore[no-untyped-call]
    assert received, "WS publish must reach the workspace:<id> channel"
    decoded = received[0].decode()
    assert "invitation.resolved" in decoded
    assert "approved" in decoded


@pytest.mark.asyncio
async def test_decline_invitation_emits_ws_event(api_db: ApiDb) -> None:
    import fakeredis
    import fakeredis.aioredis
    from asgi_lifespan import LifespanManager

    admin = await api_db.seed_user(email="ws-decline-admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="ws-decline-alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="ws-decline-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    admin_client = await _client_for(api_db, admin)
    async with admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "ws-decline-alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    server = fakeredis.FakeServer()
    redis_client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    received: list[bytes] = []

    app = api_db.app_for(alice)
    app.state.ws_redis = redis_client
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"workspace:{ws.id}")

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            declined = await c.post(f"/api/v1/invitations/{invitation_id}/decline")
            assert declined.status_code == 204
            await _drain_one(pubsub, received)

    await pubsub.aclose()  # type: ignore[no-untyped-call]
    await redis_client.aclose()  # type: ignore[no-untyped-call]
    assert received, "WS publish must reach the workspace:<id> channel"
    decoded = received[0].decode()
    assert "invitation.resolved" in decoded
    assert "declined" in decoded
