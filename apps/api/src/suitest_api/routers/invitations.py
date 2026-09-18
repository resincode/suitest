"""M1e invitation routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from suitest_db.models.user import User
from suitest_shared.domain.enums import Role

from suitest_api.auth.db import get_async_session
from suitest_api.auth.manager import auth_backend, current_active_user, get_jwt_strategy
from suitest_api.services.invitation_service import (
    InvitationConflictError,
    InvitationEmailMismatchError,
    InvitationForbiddenError,
    InvitationNotFoundError,
    InvitationService,
)
from suitest_api.settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["invitations"])


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: Role


class InvitationOut(BaseModel):
    id: str
    email: str
    role: Role
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    link: str | None = None


class InvitationListEnvelope(BaseModel):
    items: list[InvitationOut]


class InvitationValidateResponse(BaseModel):
    email: str
    role: Role
    workspace_name: str
    expires_at: datetime


class InvitationLookupResponse(BaseModel):
    """``GET .../invitations/lookup`` — exists/name only, never an id or any
    other field, so the invite composer's autocomplete cannot be used to
    enumerate accounts beyond "does this exact email have one"."""

    exists: bool
    name: str | None = None


class MyInvitationOut(BaseModel):
    """One pending invite addressed to the caller, for the Inbox surface."""

    id: str
    workspace_id: str
    workspace_name: str
    role: Role
    invited_by: str | None = None
    expires_at: datetime


class MyInvitationListEnvelope(BaseModel):
    items: list[MyInvitationOut]


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1)
    # The invited address. Accepting only works when this matches the
    # invitation — the link is personal, not a shared signup link.
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)


class AcceptInviteResponse(BaseModel):
    ok: bool = True
    # False when the invited email already owns an active account: the
    # membership was attached, but the caller must sign in with their
    # existing credentials (no session cookie is issued).
    requires_login: bool = False


def _service(session: AsyncSession) -> InvitationService:
    settings = get_settings()
    return InvitationService(
        session,
        web_url=settings.web_url,
        ttl_hours=settings.invite_ttl_hours,
    )


@router.post(
    "/workspaces/{workspace_id}/invitations",
    response_model=InvitationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    workspace_id: str,
    body: InvitationCreateRequest,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> InvitationOut:
    try:
        outcome = await _service(session).create_invitation(
            workspace_id=workspace_id,
            email=str(body.email),
            role=body.role,
            actor=user,
        )
    except InvitationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    except InvitationConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already a member"
        ) from exc
    await session.commit()
    inv = outcome.invitation
    return InvitationOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
        link=outcome.link,
    )


@router.get(
    "/workspaces/{workspace_id}/invitations",
    response_model=InvitationListEnvelope,
)
async def list_invitations(
    workspace_id: str,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> InvitationListEnvelope:
    try:
        rows = await _service(session).list_invitations(workspace_id=workspace_id, actor=user)
    except InvitationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    return InvitationListEnvelope(
        items=[
            InvitationOut(
                id=row.id,
                email=row.email,
                role=row.role,
                expires_at=row.expires_at,
                accepted_at=row.accepted_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]
    )


@router.get(
    "/workspaces/{workspace_id}/invitations/lookup",
    response_model=InvitationLookupResponse,
)
async def lookup_invitee(
    workspace_id: str,
    email: str = Query(min_length=3),
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> InvitationLookupResponse:
    """Does ``email`` already have a registered account? Backs the invite
    composer's autocomplete confirmation chip. Gated identically to invite
    creation (ADMIN/OWNER of this workspace) — see ``lookup_user`` docstring."""
    try:
        name = await _service(session).lookup_user(
            workspace_id=workspace_id, email=email, actor=user
        )
    except InvitationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    return InvitationLookupResponse(exists=name is not None, name=name)


@router.get(
    "/invitations/validate",
    response_model=InvitationValidateResponse,
)
async def validate_invitation(
    token: str = Query(min_length=1),
    session: AsyncSession = Depends(get_async_session),
) -> InvitationValidateResponse:
    try:
        inv = await _service(session).validate_token(token)
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    return InvitationValidateResponse(
        email=inv.email,
        role=inv.role,
        workspace_name=inv.workspace.name,
        expires_at=inv.expires_at,
    )


@router.post("/invitations/{invitation_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: str,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    try:
        await _service(session).revoke(invitation_id=invitation_id, actor=user)
    except InvitationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/{invitation_id}/resend", response_model=InvitationOut)
async def resend_invitation(
    invitation_id: str,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> InvitationOut:
    try:
        outcome = await _service(session).resend(invitation_id=invitation_id, actor=user)
    except InvitationForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    await session.commit()
    inv = outcome.invitation
    return InvitationOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
        link=outcome.link,
    )


@router.post(
    "/auth/accept-invite",
    response_model=AcceptInviteResponse,
)
async def accept_invitation(
    body: AcceptInviteRequest,
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    try:
        outcome = await _service(session).accept(
            token=body.token,
            email=body.email,
            name=body.name,
            password=body.password,
        )
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    except InvitationEmailMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invitation was issued to a different email address.",
        ) from exc
    await session.commit()
    if not outcome.issues_session:
        # The invited email already owns an active account. The membership is
        # attached, but no session cookie is issued: the invitee signs in with
        # their existing credentials. Never reset that account from a link.
        return JSONResponse(
            content=AcceptInviteResponse(ok=True, requires_login=True).model_dump(),
            status_code=status.HTTP_200_OK,
        )
    # FastAPI-Users' CookieTransport login yields a 204 carrying only the
    # Set-Cookie header. The accept-invite contract returns a JSON body
    # (``AcceptInviteResponse``) AND sets the session cookie, so build a 200
    # JSON response and graft the auth cookie onto it.
    login_response = await auth_backend.login(get_jwt_strategy(), outcome.user)
    response = JSONResponse(
        content=AcceptInviteResponse(ok=True, requires_login=False).model_dump(),
        status_code=status.HTTP_200_OK,
    )
    for key, value in login_response.raw_headers:
        if key.lower() == b"set-cookie":
            response.raw_headers.append((key, value))
    return response


@router.get(
    "/invitations/mine",
    response_model=MyInvitationListEnvelope,
)
async def list_my_invitations(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> MyInvitationListEnvelope:
    """Pending invites addressed to the caller's email, across workspaces.

    Feeds the Inbox ``WORKSPACE_INVITE`` cards (M1e-9) so an already-verified
    user can approve/decline in-app instead of hunting for the invite email.
    """
    rows = await _service(session).list_my_invitations(actor=user)
    return MyInvitationListEnvelope(
        items=[
            MyInvitationOut(
                id=row.id,
                workspace_id=row.workspace_id,
                workspace_name=row.workspace.name,
                role=row.role,
                invited_by=row.creator.name if row.creator else None,
                expires_at=row.expires_at,
            )
            for row in rows
        ]
    )


@router.post("/invitations/{invitation_id}/approve", status_code=status.HTTP_204_NO_CONTENT)
async def approve_invitation(
    invitation_id: str,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """In-app approve: the caller is already authenticated as the invitee, so
    this skips the token/password/"set your name" detour ``/auth/accept-invite``
    needs for an anonymous link click."""
    try:
        await _service(session).approve(invitation_id=invitation_id, actor=user)
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    except InvitationEmailMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invitation was issued to a different email address.",
        ) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/{invitation_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invitation(
    invitation_id: str,
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    try:
        await _service(session).decline(invitation_id=invitation_id, actor=user)
    except InvitationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invite not found"
        ) from exc
    except InvitationEmailMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This invitation was issued to a different email address.",
        ) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
