"""Tests for ``GET /api/v1/inbox``.

``WORKSPACE_INVITE`` (M1e-9) was the first real kind: pending invites
addressed to the caller. ``DEPLOY_GATE_FAIL``, ``MANUAL_RUN_FAIL``,
``MCP_HEALTH``, and ``FLAKY_PROMOTION`` (M1e-9 follow-up) are now real too,
scoped to the caller's ``X-Workspace-Id`` when it resolves to an actual
membership. ``AGENT_GENERATION``/``AGENT_DIAGNOSIS`` remain a wire-shape stub.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from suitest_db.models.case import TestCase
from suitest_db.models.invitation import Invitation
from suitest_db.models.mcp_provider import McpProvider
from suitest_db.models.project import Project, Suite
from suitest_db.models.run import Run, RunStep
from suitest_shared.domain.enums import (
    CaseSource,
    McpTransport,
    Role,
    RunStatus,
    RunTrigger,
    StepOutcome,
)

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


@pytest.mark.asyncio
async def test_inbox_invite_card_includes_expiry(api_db: ApiDb) -> None:
    admin = await api_db.seed_user(email="exp-admin@example.com", name="Admin")
    alice = await api_db.seed_user(email="exp-alice@example.com", name="Alice")
    ws = await api_db.seed_workspace(slug="exp-ws", name="Acme")
    await api_db.seed_membership(workspace_id=ws.id, user_id=admin.id, role=Role.ADMIN)

    async with api_db.client(admin) as admin_client:
        created = await admin_client.post(
            f"/api/v1/workspaces/{ws.id}/invitations",
            json={"email": "exp-alice@example.com", "role": "QA"},
        )
        invitation_id = created.json()["id"]

    async with api_db.client(alice) as alice_client:
        resp = await alice_client.get("/api/v1/inbox")
    item = resp.json()["items"][0]

    async with api_db.maker() as session:
        row = await session.get(Invitation, invitation_id)
        assert row is not None
        assert item["expiresAt"] == row.expires_at.isoformat()


@pytest.mark.asyncio
async def test_inbox_lists_deploy_gate_and_manual_run_failures(api_db: ApiDb) -> None:
    """CI-triggered FAIL/ERROR runs surface as ``DEPLOY_GATE_FAIL``; a MANUAL
    trigger surfaces as ``MANUAL_RUN_FAIL``. A PASS run and a failing run in a
    workspace the caller does not belong to must never appear."""
    user = await api_db.seed_user(email="agg-runs@example.com")
    ws = await api_db.member_workspace(user, slug="agg-runs-ws")
    proj = Project(workspace_id=ws.id, slug="agg-runs-proj", name="Agg")
    await api_db.add_all([proj])

    gate_fail = Run(
        public_id="R-GATE1",
        project_id=proj.id,
        name="ci build",
        trigger=RunTrigger.CI_PUSH,
        status=RunStatus.FAIL,
    )
    manual_fail = Run(
        public_id="R-MANUAL1",
        project_id=proj.id,
        name="smoke run",
        trigger=RunTrigger.MANUAL,
        status=RunStatus.ERROR,
    )
    manual_pass = Run(
        public_id="R-PASS1",
        project_id=proj.id,
        name="passing run",
        trigger=RunTrigger.MANUAL,
        status=RunStatus.PASS,
    )
    await api_db.add_all([gate_fail, manual_fail, manual_pass])

    other_ws = await api_db.seed_workspace(slug="agg-other-ws", name="Other")
    other_proj = Project(workspace_id=other_ws.id, slug="agg-other-proj", name="Other")
    await api_db.add_all([other_proj])
    other_fail = Run(
        public_id="R-OTHER1",
        project_id=other_proj.id,
        name="other fail",
        trigger=RunTrigger.CI_PUSH,
        status=RunStatus.FAIL,
    )
    await api_db.add_all([other_fail])

    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": ws.id})
    assert resp.status_code == 200
    refs = {(item["kind"], item["ref"]) for item in resp.json()["items"]}
    assert ("DEPLOY_GATE_FAIL", "R-GATE1") in refs
    assert ("MANUAL_RUN_FAIL", "R-MANUAL1") in refs
    assert not any(ref == "R-PASS1" for _, ref in refs)
    assert not any(ref == "R-OTHER1" for _, ref in refs)


@pytest.mark.asyncio
async def test_inbox_lists_unhealthy_mcp_provider_only(api_db: ApiDb) -> None:
    """A ``down`` workspace provider surfaces; a healthy one and a bundled
    (global, ``workspace_id`` NULL) provider do not."""
    user = await api_db.seed_user(email="agg-mcp@example.com")
    ws = await api_db.member_workspace(user, slug="agg-mcp-ws")

    down = McpProvider(
        workspace_id=ws.id,
        name="flaky-mcp",
        kind="custom",
        endpoint="http://example.test",
        transport=McpTransport.STDIO,
        health_status="down",
    )
    healthy = McpProvider(
        workspace_id=ws.id,
        name="ok-mcp",
        kind="custom",
        endpoint="http://example.test",
        transport=McpTransport.STDIO,
        health_status="ok",
    )
    bundled_down = McpProvider(
        workspace_id=None,
        name="bundled-mcp",
        kind="custom",
        endpoint="http://example.test",
        transport=McpTransport.STDIO,
        health_status="down",
    )
    await api_db.add_all([down, healthy, bundled_down])

    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": ws.id})
    mcp_items = [item for item in resp.json()["items"] if item["kind"] == "MCP_HEALTH"]
    assert len(mcp_items) == 1
    assert mcp_items[0]["ref"] == "flaky-mcp"


@pytest.mark.asyncio
async def test_inbox_lists_flaky_promotion_card(api_db: ApiDb) -> None:
    """Reuses the existing M1-26 flaky rule (variance > 0.2, >= 3 samples)."""
    user = await api_db.seed_user(email="agg-flaky@example.com")
    ws = await api_db.member_workspace(user, slug="agg-flaky-ws")
    proj = Project(workspace_id=ws.id, slug="agg-flaky-proj", name="Flaky")
    await api_db.add_all([proj])
    suite = Suite(project_id=proj.id, name="S", order=0)
    await api_db.add_all([suite])
    case = TestCase(
        suite_id=suite.id, public_id="TC-INBOXFLAKE", name="flaky", source=CaseSource.MANUAL
    )
    await api_db.add_all([case])
    outcomes = [
        StepOutcome.PASS,
        StepOutcome.FAIL,
        StepOutcome.PASS,
        StepOutcome.FAIL,
        StepOutcome.PASS,
    ]
    for i, outcome in enumerate(outcomes):
        run = Run(
            public_id=f"R-IBFL{i}",
            project_id=proj.id,
            name="run",
            trigger=RunTrigger.MANUAL,
            status=RunStatus.PASS,
        )
        await api_db.add_all([run])
        await api_db.add_all(
            [RunStep(run_id=run.id, case_id=case.id, step_order=1, outcome=outcome)]
        )

    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": ws.id})
    flaky_items = [item for item in resp.json()["items"] if item["kind"] == "FLAKY_PROMOTION"]
    assert len(flaky_items) == 1
    assert flaky_items[0]["ref"] == "TC-INBOXFLAKE"


@pytest.mark.asyncio
async def test_inbox_omits_workspace_scoped_cards_for_non_member_workspace(api_db: ApiDb) -> None:
    """A caller sending ``X-Workspace-Id`` for a workspace they do not belong
    to must not see that workspace's run/health/flaky cards — the header is
    resolved only after a real membership check (see ``_member_workspace_id``)."""
    user = await api_db.seed_user(email="agg-nonmember@example.com")
    foreign_ws = await api_db.seed_workspace(slug="agg-nonmember-ws", name="Foreign")
    proj = Project(workspace_id=foreign_ws.id, slug="agg-foreign-proj", name="Foreign")
    await api_db.add_all([proj])
    fail_run = Run(
        public_id="R-FOREIGN1",
        project_id=proj.id,
        name="fail",
        trigger=RunTrigger.CI_PUSH,
        status=RunStatus.FAIL,
    )
    await api_db.add_all([fail_run])

    async with api_db.client(user) as c:
        resp = await c.get("/api/v1/inbox", headers={"X-Workspace-Id": foreign_ws.id})
    assert resp.status_code == 200
    assert resp.json()["items"] == []
