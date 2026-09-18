"""Inbox read endpoint — aggregated workspace notifications.

The Inbox screen (M1b) lists cards for gating failures, manual-run fails, MCP
health blips, flaky promotions, and agent generation/diagnosis events. Those
six kinds remain a wire-shape stub — the M1d/M2 event aggregator that fills
them has not landed yet.

``WORKSPACE_INVITE`` (M1e-9) is the first real, non-stub kind: pending
invitations addressed to the caller's email, surfaced here so an
already-registered user can approve/decline in-app instead of hunting for the
invite email. Because the recipient is (by definition) not yet a member of
the target workspace, this endpoint only requires an authenticated session,
not membership of any particular workspace — unlike the rest of the ``_app``
shell it renders inside.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from suitest_db.models.invitation import Invitation
from suitest_db.models.user import User

from suitest_api.auth.db import get_async_session
from suitest_api.auth.manager import current_active_user
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
    )


@router.get("/inbox", response_model=InboxResponse)
async def list_inbox(
    user: User = Depends(current_active_user),
    status_filter: str = Query(default="all", alias="status"),
    session: AsyncSession = Depends(get_async_session),
) -> InboxResponse:
    """List inbox items for the caller.

    The six pre-existing kinds stay empty (no aggregator yet); pending
    workspace invites (``WORKSPACE_INVITE``) are real.
    """
    _ = status_filter
    settings = get_settings()
    service = InvitationService(
        session, web_url=settings.web_url, ttl_hours=settings.invite_ttl_hours
    )
    invitations = await service.list_my_invitations(actor=user)
    items = [_invite_card(inv) for inv in invitations]
    return InboxResponse(items=items, unread_count=len(items))
