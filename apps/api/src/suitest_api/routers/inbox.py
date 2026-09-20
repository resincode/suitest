"""Inbox read endpoint — aggregated workspace notifications.

The Inbox screen (M1b) lists cards for gating failures, manual-run fails, MCP
health blips, flaky promotions, and agent generation/diagnosis events.

``WORKSPACE_INVITE`` (M1e-9) was the first real, non-stub kind: pending
invitations addressed to the caller's email, surfaced here so an
already-registered user can approve/decline in-app instead of hunting for the
invite email. Because the recipient is (by definition) not yet a member of
the target workspace, this endpoint only requires an authenticated session,
not membership of any particular workspace — unlike the rest of the ``_app``
shell it renders inside.

``DEPLOY_GATE_FAIL``, ``MANUAL_RUN_FAIL``, ``MCP_HEALTH``, and
``FLAKY_PROMOTION`` (follow-up to M1e-9) are now real too, scoped to the
caller's *current* workspace (``X-Workspace-Id``, resolved only if the caller
is actually a member — see ``_member_workspace_id``). ``AGENT_GENERATION``
and ``AGENT_DIAGNOSIS`` remain a wire-shape stub pending the M1d/M2 agent
event aggregator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from suitest_db.models.invitation import Invitation
from suitest_db.models.mcp_provider import McpProvider
from suitest_db.models.run import Run
from suitest_db.models.user import User
from suitest_db.repositories.defects import DefectRepo
from suitest_db.repositories.mcp_providers import McpProviderRepo
from suitest_db.repositories.projects import ProjectRepo
from suitest_db.repositories.requirements import RequirementRepo
from suitest_db.repositories.runs import RunRepo
from suitest_db.repositories.test_cases import TestCaseRepo
from suitest_db.repositories.workspace_members import WorkspaceMembershipRepo
from suitest_shared.domain.enums import Role, RunTrigger

from suitest_api.auth.db import get_async_session
from suitest_api.auth.manager import current_active_user
from suitest_api.deps.scope import TenantContext
from suitest_api.services.analytics_service import AnalyticsService
from suitest_api.services.invitation_service import InvitationService
from suitest_api.settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["inbox"])


InboxKind = Literal[
    "DEPLOY_GATE_FAIL",
    "MANUAL_RUN_FAIL",
    "MCP_HEALTH",
    "FLAKY_PROMOTION",
    "AGENT_GENERATION",
    "AGENT_DIAGNOSIS",
    "WORKSPACE_INVITE",
]

# CI-triggered runs gate a deploy; a MANUAL run is a human clicking "run now".
_GATE_TRIGGERS = (RunTrigger.WEBHOOK, RunTrigger.CI_PUSH, RunTrigger.CI_PR)
_MANUAL_TRIGGERS = (RunTrigger.MANUAL,)
_MAX_CARDS_PER_AGGREGATOR = 10


class InboxItem(BaseModel):
    """One notification card in the Inbox feed."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: InboxKind
    title: str
    body: str
    created_at: str = Field(alias="createdAt")
    # Optional back-link display id (e.g. a run/defect public id) for kinds
    # that need one. WORKSPACE_INVITE has no natural ref — the title already
    # names the workspace.
    ref: str | None = None
    status: Literal["unread", "read", "dismissed"] = "unread"
    # Only WORKSPACE_INVITE sets this today — the countdown badge on the card.
    expires_at: str | None = Field(default=None, alias="expiresAt")


class InboxResponse(BaseModel):
    """``GET /inbox`` envelope — items list + unread badge counter."""

    model_config = ConfigDict(populate_by_name=True)

    items: list[InboxItem] = Field(default_factory=list)
    unread_count: int = Field(default=0, alias="unreadCount")


def _invite_card(invitation: Invitation) -> InboxItem:
    who = invitation.creator.name if invitation.creator else "Someone"
    return InboxItem(
        id=invitation.id,
        kind="WORKSPACE_INVITE",
        title=f"{who} invited you to {invitation.workspace.name}",
        body=f"Join as {invitation.role.value.title()} — approve or decline below.",
        created_at=invitation.created_at.isoformat(),
        expires_at=invitation.expires_at.isoformat(),
    )


def _run_card(run: Run, *, kind: Literal["DEPLOY_GATE_FAIL", "MANUAL_RUN_FAIL"]) -> InboxItem:
    when = run.completed_at or run.created_at
    if kind == "DEPLOY_GATE_FAIL":
        title = f"Gate failed: {run.name}"
        body = f"{run.env} · {run.trigger.value.lower()} trigger · {run.status.value}"
    else:
        title = f"Run failed: {run.name}"
        body = f"{run.env} · manual run · {run.status.value}"
    return InboxItem(
        id=f"run:{run.id}",
        kind=kind,
        title=title,
        body=body,
        ref=run.public_id,
        created_at=when.isoformat(),
    )


def _mcp_health_card(provider: McpProvider) -> InboxItem:
    when = provider.last_health_at or provider.updated_at
    return InboxItem(
        id=f"mcp:{provider.id}",
        kind="MCP_HEALTH",
        title=f"{provider.name} is unreachable",
        body="Last health check reported 'down' — verify the endpoint and credentials.",
        created_at=when.isoformat(),
        ref=provider.name,
    )


async def _member_workspace_id(
    session: AsyncSession, user: User, x_workspace_id: str | None
) -> str | None:
    """Resolve ``X-Workspace-Id`` to a workspace the caller actually belongs
    to, or ``None``. Unlike ``require_workspace_membership`` this degrades
    silently instead of 400/403-ing: ``/inbox`` must keep working for a
    brand-new user with zero workspaces who only has a WORKSPACE_INVITE card
    to see, and for a foreign/stale header we simply omit workspace-scoped
    cards rather than fail the whole feed.
    """
    if not x_workspace_id:
        return None
    membership = await WorkspaceMembershipRepo(session).get(x_workspace_id, user.id)
    return x_workspace_id if membership is not None else None


async def _gate_and_run_cards(session: AsyncSession, workspace_id: str) -> list[InboxItem]:
    run_repo = RunRepo(session)
    gate_runs = await run_repo.list_failed_by_workspace(
        workspace_id, triggers=_GATE_TRIGGERS, limit=_MAX_CARDS_PER_AGGREGATOR
    )
    manual_runs = await run_repo.list_failed_by_workspace(
        workspace_id, triggers=_MANUAL_TRIGGERS, limit=_MAX_CARDS_PER_AGGREGATOR
    )
    return [_run_card(run, kind="DEPLOY_GATE_FAIL") for run in gate_runs] + [
        _run_card(run, kind="MANUAL_RUN_FAIL") for run in manual_runs
    ]


async def _mcp_health_cards(session: AsyncSession, workspace_id: str) -> list[InboxItem]:
    providers = await McpProviderRepo(session).list_unhealthy_by_workspace(
        workspace_id, limit=_MAX_CARDS_PER_AGGREGATOR
    )
    return [_mcp_health_card(provider) for provider in providers]


async def _flaky_cards(session: AsyncSession, workspace_id: str, user: User) -> list[InboxItem]:
    """``FLAKY_PROMOTION`` cards.

    CAVEAT: there is no persisted "became flaky" event to key a real
    "promotion" off of. This reuses the existing M1-26 rule
    (:meth:`AnalyticsService.flaky`: population variance > 0.2 over the last
    10 runs a case appeared in, >= 3 samples) and recomputes it fresh on every
    call. These cards mean "currently flaky", not "flagged just now" — the
    card's timestamp is the query time, not a promotion time.
    """
    ctx = TenantContext(workspace_id=workspace_id, user_id=str(user.id), role=Role.VIEWER)
    analytics = AnalyticsService(
        ctx,
        RunRepo(session),
        ProjectRepo(session),
        RequirementRepo(session),
        TestCaseRepo(session),
        DefectRepo(session),
    )
    projects = await ProjectRepo(session).list_by_workspace(workspace_id)
    now = datetime.now(UTC).isoformat()
    cards: list[InboxItem] = []
    for project in projects:
        for case in await analytics.flaky(project.id) or []:
            cards.append(
                InboxItem(
                    id=f"flaky:{project.id}:{case.case_id}",
                    kind="FLAKY_PROMOTION",
                    title=f"{case.public_id} looks flaky in {project.name}",
                    body=(
                        f"Flake rate {case.flake_rate:.0%} over the last "
                        f"{case.sample_size} runs it appeared in."
                    ),
                    created_at=now,
                    ref=case.public_id,
                )
            )
    return cards


@router.get("/inbox", response_model=InboxResponse)
async def list_inbox(
    user: User = Depends(current_active_user),
    status_filter: str = Query(default="all", alias="status"),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    session: AsyncSession = Depends(get_async_session),
) -> InboxResponse:
    """List inbox items for the caller.

    ``WORKSPACE_INVITE`` is cross-workspace (by the caller's email); the four
    workspace-scoped aggregators only run when ``X-Workspace-Id`` resolves to
    a workspace the caller is a member of. ``AGENT_GENERATION``/
    ``AGENT_DIAGNOSIS`` stay empty (no aggregator yet).
    """
    _ = status_filter
    settings = get_settings()
    invitation_service = InvitationService(
        session, web_url=settings.web_url, ttl_hours=settings.invite_ttl_hours
    )
    items = [_invite_card(inv) for inv in await invitation_service.list_my_invitations(actor=user)]

    workspace_id = await _member_workspace_id(session, user, x_workspace_id)
    if workspace_id is not None:
        items += await _gate_and_run_cards(session, workspace_id)
        items += await _mcp_health_cards(session, workspace_id)
        items += await _flaky_cards(session, workspace_id, user)

    items.sort(key=lambda item: item.created_at, reverse=True)
    return InboxResponse(items=items, unread_count=len(items))
