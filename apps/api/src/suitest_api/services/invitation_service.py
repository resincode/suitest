"""Invitation business logic."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi_users.password import PasswordHelper
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from suitest_db.audit import write_audit
from suitest_db.models.invitation import Invitation
from suitest_db.models.tenancy import Membership
from suitest_db.models.user import User
from suitest_db.repositories.invitations import InvitationRepository
from suitest_db.repositories.workspace_members import WorkspaceMembershipRepo
from suitest_shared.domain.enums import Role

ALLOWED_INVITE_ROLES = {Role.ADMIN, Role.QA, Role.VIEWER}


class InvitationError(Exception):
    """Base invitation service error."""


class InvitationForbiddenError(InvitationError):
    """Caller cannot manage invitations for this workspace."""


class InvitationConflictError(InvitationError):
    """Invite cannot be created because target is already a member."""


class InvitationNotFoundError(InvitationError):
    """Invite/token not found or inactive."""


class InvitationEmailMismatchError(InvitationError):
    """Accepting requires the invited email — the link is personal."""


@dataclass(frozen=True)
class InvitationLink:
    invitation: Invitation
    raw_token: str
    link: str


@dataclass(frozen=True)
class AcceptOutcome:
    """Result of accepting an invitation."""

    user: User
    """The account the invitation resolved to."""

    issues_session: bool
    """True when the caller authenticated as this user now (fresh account or
    claimed placeholder) — a session cookie may be issued. False when the
    email already belongs to an active account: the invitee must sign in with
    their existing credentials instead."""


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest for a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_invite_token() -> str:
    """Generate a URL-safe invite token."""
    return secrets.token_urlsafe(32)


class InvitationService:
    """Workspace invitation workflow."""

    def __init__(self, session: AsyncSession, *, web_url: str, ttl_hours: int) -> None:
        self.session = session
        self.web_url = web_url.rstrip("/")
        self.ttl_hours = ttl_hours
        self.repo = InvitationRepository(session)
        self.memberships = WorkspaceMembershipRepo(session)

    async def _ensure_manager(self, workspace_id: str, user: User) -> None:
        if user.is_superuser:
            return
        membership = await self.memberships.get(workspace_id, user.id)
        if membership is None or membership.role not in {Role.ADMIN, Role.OWNER}:
            raise InvitationForbiddenError

    @staticmethod
    def _is_placeholder_account(user: User) -> bool:
        """A row created by "add member by email": inactive, unverified, and
        carrying the unusable ``!``-prefixed hash from
        :func:`create_placeholder_user`. Never a real, sign-in-capable
        account — safe to ignore when deciding whether an email "already has
        an account" for invite UX (autocomplete, in-app approval)."""
        return user.hashed_password.startswith("!") and not user.is_active and not user.is_verified

    async def lookup_user(self, *, workspace_id: str, email: str, actor: User) -> str | None:
        """Return the display name of a real registered account for ``email``.

        ``None`` when no account exists, or the only match is an unclaimed
        placeholder. Gated the same as invite creation (``_ensure_manager``)
        so this never becomes a workspace-agnostic email oracle: it tells an
        admin nothing they could not already learn by sending the invite.
        """
        await self._ensure_manager(workspace_id, actor)
        user = await self.memberships.find_user_by_email(email.strip().lower())
        if user is None or self._is_placeholder_account(user):
            return None
        return user.name

    async def create_invitation(
        self, *, workspace_id: str, email: str, role: Role, actor: User
    ) -> InvitationLink:
        await self._ensure_manager(workspace_id, actor)
        if role not in ALLOWED_INVITE_ROLES:
            raise InvitationForbiddenError
        normalized = email.strip().lower()
        existing = await self.memberships.find_user_by_email(normalized)
        if existing is not None:
            membership = await self.memberships.get(workspace_id, existing.id)
            if membership is not None:
                raise InvitationConflictError
        token = new_invite_token()
        invitation = await self.repo.create(
            workspace_id=workspace_id,
            email=normalized,
            role=role,
            token_hash=hash_token(token),
            ttl_hours=self.ttl_hours,
            created_by=actor.id,
        )
        await write_audit(
            self.session,
            workspace_id=workspace_id,
            user_id=str(actor.id),
            action="invitation.create",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": invitation.email, "role": role.value},
        )
        await self.session.flush()
        return InvitationLink(invitation=invitation, raw_token=token, link=self._link(token))

    async def list_invitations(self, *, workspace_id: str, actor: User) -> list[Invitation]:
        await self._ensure_manager(workspace_id, actor)
        return await self.repo.list_for_workspace(workspace_id)

    async def list_my_invitations(self, *, actor: User) -> list[Invitation]:
        """Pending invites addressed to ``actor``'s email, across workspaces.

        Backs the in-app "pending invites" surface (Inbox, M1e-9): ``actor``
        is not yet a member of the target workspace(s), so this cannot be a
        workspace-scoped read like :meth:`list_invitations`.
        """
        return await self.repo.list_for_email(actor.email)

    async def validate_token(self, token: str) -> Invitation:
        invitation = await self.repo.get_active_by_token_hash(hash_token(token))
        if invitation is None:
            raise InvitationNotFoundError
        return invitation

    async def revoke(self, *, invitation_id: str, actor: User) -> None:
        invitation = await self.repo.get_by_id(invitation_id)
        if invitation is None:
            raise InvitationNotFoundError
        await self._ensure_manager(invitation.workspace_id, actor)
        await self.repo.revoke(invitation)
        await write_audit(
            self.session,
            workspace_id=invitation.workspace_id,
            user_id=str(actor.id),
            action="invitation.revoke",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": invitation.email},
        )
        await self.session.flush()

    async def resend(self, *, invitation_id: str, actor: User) -> InvitationLink:
        invitation = await self.repo.get_by_id(invitation_id)
        if invitation is None:
            raise InvitationNotFoundError
        await self._ensure_manager(invitation.workspace_id, actor)
        token = new_invite_token()
        await self.repo.resend(invitation, token_hash=hash_token(token), ttl_hours=self.ttl_hours)
        await write_audit(
            self.session,
            workspace_id=invitation.workspace_id,
            user_id=str(actor.id),
            action="invitation.resend",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"email": invitation.email},
        )
        await self.session.flush()
        return InvitationLink(invitation=invitation, raw_token=token, link=self._link(token))

    async def accept(self, *, token: str, email: str, name: str, password: str) -> AcceptOutcome:
        """Accept a personal invitation.

        ``email`` must equal the invited address: the link is personal, and
        the account it creates/claims is keyed to that address. Without this
        check anyone holding a forwarded link could register (or take over)
        as the invitee — the reported "B ends up recorded as A" incident.

        An active account for that email is NEVER modified here (no password
        reset, no activation): accepting links you to the workspace, it does
        not prove you own the account. Only a fresh account or an inactive
        placeholder (created by direct member add, unusable ``!``-prefixed
        hash) may be claimed with the link's password.
        """
        invitation = await self.validate_token(token)
        normalized = email.strip().lower()
        if normalized != invitation.email.lower():
            raise InvitationEmailMismatchError

        existing = await self.session.scalar(
            select(User).where(func.lower(User.email) == invitation.email.lower())
        )
        # A claimable placeholder is ONLY a row created by "add member by email":
        # inactive, unverified, and carrying the unusable ``!``-prefixed hash
        # (``create_placeholder_user``). A legitimate disabled account, or an
        # active account still awaiting verification, has a real password hash
        # and must go through the existing-account sign-in path instead.
        placeholder = existing is not None and self._is_placeholder_account(existing)
        if existing is None:
            user = User(
                id=uuid.uuid4(),
                email=invitation.email,
                hashed_password=PasswordHelper().hash(password),
                is_active=True,
                is_superuser=False,
                is_verified=True,
                name=name.strip(),
            )
            self.session.add(user)
            await self.session.flush()
            issues_session = True
        elif placeholder:
            user = existing
            user.is_active = True
            user.is_verified = True
            user.hashed_password = PasswordHelper().hash(password)
            user.must_change_password = False
            if not user.name:
                user.name = name.strip()
            issues_session = True
        else:
            # Active account: attach the membership, but require sign-in —
            # accepting the invite must not reset or reactivate the account.
            user = existing
            issues_session = False
            if not user.name:
                user.name = name.strip()
        membership = await self.memberships.get(invitation.workspace_id, user.id)
        if membership is None:
            self.session.add(
                Membership(
                    workspace_id=invitation.workspace_id,
                    user_id=user.id,
                    role=invitation.role,
                )
            )
        await self.repo.mark_accepted(invitation)
        await write_audit(
            self.session,
            workspace_id=invitation.workspace_id,
            user_id=str(user.id),
            action="invitation.accept",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"role": invitation.role.value},
        )
        await self.session.flush()
        return AcceptOutcome(user=user, issues_session=issues_session)

    async def approve(self, *, invitation_id: str, actor: User) -> Membership:
        """Approve a pending invite as the already-authenticated invitee.

        The in-app counterpart to :meth:`accept`: no token, no password, no
        "set your name" detour — the caller already has a real session. The
        email-match check is the same invariant :meth:`accept` enforces for
        the token path (M1e security fix): the invited address is the only
        one that may claim it, checked against the *authenticated* user, not
        anything client-supplied.
        """
        invitation = await self.repo.get_active_by_id(invitation_id)
        if invitation is None:
            raise InvitationNotFoundError
        if actor.email.strip().lower() != invitation.email.lower():
            raise InvitationEmailMismatchError
        membership = await self.memberships.get(invitation.workspace_id, actor.id)
        if membership is None:
            membership = Membership(
                workspace_id=invitation.workspace_id,
                user_id=actor.id,
                role=invitation.role,
            )
            self.session.add(membership)
        await self.repo.mark_accepted(invitation)
        await write_audit(
            self.session,
            workspace_id=invitation.workspace_id,
            user_id=str(actor.id),
            action="invitation.approve",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"role": invitation.role.value},
        )
        await self.session.flush()
        return membership

    async def decline(self, *, invitation_id: str, actor: User) -> Invitation:
        """Decline a pending invite as the already-authenticated invitee."""
        invitation = await self.repo.get_active_by_id(invitation_id)
        if invitation is None:
            raise InvitationNotFoundError
        if actor.email.strip().lower() != invitation.email.lower():
            raise InvitationEmailMismatchError
        await self.repo.mark_declined(invitation)
        await write_audit(
            self.session,
            workspace_id=invitation.workspace_id,
            user_id=str(actor.id),
            action="invitation.decline",
            resource_type="invitation",
            resource_id=invitation.id,
            metadata={"role": invitation.role.value},
        )
        await self.session.flush()
        return invitation

    def _link(self, token: str) -> str:
        return f"{self.web_url}/accept-invite?token={token}"
