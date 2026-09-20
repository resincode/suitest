import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { CopyButton } from "@/components/shared/CopyButton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ApiError,
  createInvitation,
  type InvitationOut,
  type InvitationStatus,
  invitationStatus,
  listInvitations,
  listMembers,
  lookupInviteEmail,
  resendInvitation,
  revokeInvitation,
  type Role,
} from "@/lib/api-client";
import { useWorkspaceStream } from "@/lib/ws-client";

/** Debounce delay before an in-flight email is checked against existing
 * accounts (M1e-9 autocomplete chip) — long enough to skip mid-typing. */
const EMAIL_LOOKUP_DEBOUNCE_MS = 400;

/** Invite creation is limited to ADMIN/QA/VIEWER — OWNER stays a separate action. */
const INVITE_ROLES: Role[] = ["ADMIN", "QA", "VIEWER"];

/** Roles allowed to manage invitations (OWNER + ADMIN). */
function canManageInvites(role: string | undefined): boolean {
  return role === "OWNER" || role === "ADMIN";
}

const STATUS_STYLE: Record<InvitationStatus, string> = {
  pending: "text-amber",
  accepted: "text-accent",
  revoked: "text-fg-4",
  declined: "text-fg-4",
  expired: "text-red",
};

const STATUS_KEY: Record<InvitationStatus, string> = {
  pending: "members.statusPending",
  accepted: "members.statusAccepted",
  revoked: "members.statusRevoked",
  declined: "members.statusDeclined",
  expired: "members.statusExpired",
};

interface MembersPanelProps {
  workspaceId: string;
  /** Current user's role in this workspace; gates the Invite affordances. */
  currentRole: string | undefined;
}

export function MembersPanel({ workspaceId, currentRole }: MembersPanelProps): React.ReactElement {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const isAdmin = canManageInvites(currentRole);

  const membersQuery = useQuery({
    queryKey: ["workspace", workspaceId, "members"],
    queryFn: () => listMembers(workspaceId),
  });

  const invitesQuery = useQuery({
    queryKey: ["workspace", workspaceId, "invitations"],
    queryFn: () => listInvitations(workspaceId),
    enabled: isAdmin,
  });

  const [inviteOpen, setInviteOpen] = useState(false);
  const [created, setCreated] = useState<InvitationOut | null>(null);

  const invalidateInvites = (): void => {
    void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId, "invitations"] });
  };

  const invalidateMembers = (): void => {
    void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId, "members"] });
  };

  // M1e-9 follow-up: the invitee's approve/decline resolves out-of-band (a
  // different browser tab/session) — refresh both lists live instead of
  // waiting for this admin to reload the page.
  useWorkspaceStream((e) => {
    if (e.event === "invitation.resolved") {
      invalidateInvites();
      invalidateMembers();
    }
  });

  const revokeMutation = useMutation({
    mutationFn: (id: string) => revokeInvitation(id),
    onSuccess: invalidateInvites,
  });

  const resendMutation = useMutation({
    mutationFn: (id: string) => resendInvitation(id),
    onSuccess: (res) => {
      if (res.link) {
        setCreated(res);
      }
      invalidateInvites();
    },
  });

  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-fg-1">{t("members.title")}</h2>
          {isAdmin ? (
            <button
              type="button"
              onClick={() => {
                setCreated(null);
                setInviteOpen(true);
              }}
              className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-[13px] font-medium text-accent-fg hover:opacity-90"
              data-testid="invite-button"
            >
              {t("members.inviteButton")}
            </button>
          ) : null}
        </div>

        {membersQuery.isError ? (
          <p
            role="alert"
            className="rounded-md border border-red/30 bg-red/10 px-3 py-2 text-[12.5px] text-red"
          >
            {t("members.loadError")}
          </p>
        ) : (
          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-left text-[13px]">
              <thead className="bg-bg-elev-2 text-[11px] uppercase tracking-[0.07em] text-fg-4">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("members.columnMember")}</th>
                  <th className="px-3 py-2 font-medium">{t("members.columnEmail")}</th>
                  <th className="px-3 py-2 font-medium">{t("members.columnRole")}</th>
                </tr>
              </thead>
              <tbody>
                {(membersQuery.data ?? []).map((m) => (
                  <tr key={m.user_id} className="border-t border-border" data-testid="member-row">
                    <td className="px-3 py-2 text-fg-1">{m.name}</td>
                    <td className="px-3 py-2 text-fg-3">{m.email}</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-fg-1">{m.role}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {isAdmin ? (
        <section className="space-y-3">
          <h2 className="text-[15px] font-semibold text-fg-1">{t("members.pendingTitle")}</h2>
          {invitesQuery.isError ? (
            <p
              role="alert"
              className="rounded-md border border-red/30 bg-red/10 px-3 py-2 text-[12.5px] text-red"
            >
              {t("members.pendingLoadError")}
            </p>
          ) : (invitesQuery.data ?? []).length === 0 ? (
            <p className="text-[13px] text-fg-4">{t("members.pendingEmpty")}</p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border">
              <table className="w-full text-left text-[13px]">
                <thead className="bg-bg-elev-2 text-[11px] uppercase tracking-[0.07em] text-fg-4">
                  <tr>
                    <th className="px-3 py-2 font-medium">{t("members.columnEmail")}</th>
                    <th className="px-3 py-2 font-medium">{t("members.columnRole")}</th>
                    <th className="px-3 py-2 font-medium">{t("members.columnStatus")}</th>
                    <th className="px-3 py-2 text-right font-medium">
                      {t("members.columnActions")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {(invitesQuery.data ?? []).map((inv) => {
                    const status = invitationStatus(inv);
                    return (
                      <tr key={inv.id} className="border-t border-border" data-testid="invite-row">
                        <td className="px-3 py-2 text-fg-1">{inv.email}</td>
                        <td className="px-3 py-2 font-mono text-[12px] text-fg-1">{inv.role}</td>
                        <td className={`px-3 py-2 font-medium ${STATUS_STYLE[status]}`}>
                          {t(STATUS_KEY[status])}
                        </td>
                        <td className="px-3 py-2">
                          {status === "pending" ? (
                            <div className="flex items-center justify-end gap-2">
                              <button
                                type="button"
                                disabled={resendMutation.isPending}
                                onClick={() => resendMutation.mutate(inv.id)}
                                className="rounded-md px-2 py-1 text-[12px] font-medium text-fg-1 hover:bg-bg-elev-2 disabled:opacity-50"
                                data-testid={`resend-${inv.id}`}
                              >
                                {t("members.resend")}
                              </button>
                              <button
                                type="button"
                                disabled={revokeMutation.isPending}
                                onClick={() => revokeMutation.mutate(inv.id)}
                                className="rounded-md px-2 py-1 text-[12px] font-medium text-red hover:bg-red/10 disabled:opacity-50"
                                data-testid={`revoke-${inv.id}`}
                              >
                                {t("members.revoke")}
                              </button>
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {created?.link ? (
            <div
              className="space-y-2 rounded-lg border border-border bg-bg-elev-1 p-4"
              data-testid="invite-link-panel"
            >
              <p className="text-[12.5px] font-medium text-fg-1">
                {t("members.linkTitle", { email: created.email })}
              </p>
              <p className="text-[12px] text-fg-4">
                {t("members.linkWarning", { email: created.email })}
              </p>
              <div className="flex items-center gap-2">
                <code className="flex-1 truncate rounded-md border border-border bg-bg-base px-3 py-2 font-mono text-[12px] text-fg-1">
                  {created.link}
                </code>
                <CopyButton value={created.link} label={t("members.copyLink")} />
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <InviteModal
        open={inviteOpen}
        workspaceId={workspaceId}
        onOpenChange={setInviteOpen}
        onCreated={(inv) => {
          if (inv.link) {
            setCreated(inv);
          }
          invalidateInvites();
        }}
      />
    </div>
  );
}

interface InviteModalProps {
  open: boolean;
  workspaceId: string;
  onOpenChange: (open: boolean) => void;
  onCreated: (inv: InvitationOut) => void;
}

function InviteModal({
  open,
  workspaceId,
  onOpenChange,
  onCreated,
}: InviteModalProps): React.ReactElement {
  const { t } = useTranslation();
  const [email, setEmail] = useState("");
  const [debouncedEmail, setDebouncedEmail] = useState("");
  const [role, setRole] = useState<Role>("QA");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(
      () => setDebouncedEmail(email.trim().toLowerCase()),
      EMAIL_LOOKUP_DEBOUNCE_MS,
    );
    return () => clearTimeout(timer);
  }, [email]);

  const isPlausibleEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(debouncedEmail);
  const lookup = useQuery({
    queryKey: ["invite-lookup", workspaceId, debouncedEmail] as const,
    queryFn: () => lookupInviteEmail(workspaceId, debouncedEmail),
    enabled: isPlausibleEmail,
  });

  const createMutation = useMutation({
    mutationFn: () => createInvitation(workspaceId, { email, role }),
    onSuccess: (res) => {
      onCreated(res);
      setEmail("");
      setRole("QA");
      setError(null);
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setError(t("members.conflictError"));
        return;
      }
      setError(t("members.genericError"));
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("members.inviteModalTitle")}</DialogTitle>
          <DialogDescription>{t("members.inviteModalDescription")}</DialogDescription>
        </DialogHeader>

        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            createMutation.mutate();
          }}
        >
          <div className="space-y-2">
            <label htmlFor="invite-email" className="text-[12.5px] font-medium text-fg-1">
              {t("members.emailLabel")}
            </label>
            <input
              id="invite-email"
              name="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-border bg-bg-base px-3 py-2 text-[13px] text-fg-1 outline-none focus:border-accent"
            />
            {isPlausibleEmail && lookup.data?.exists ? (
              <p
                data-testid="invite-lookup-match"
                className="rounded-md border border-accent/20 bg-accent/10 px-2.5 py-1.5 text-[12px] text-accent"
              >
                {t("members.lookupMatch", { name: lookup.data.name })}
              </p>
            ) : null}
          </div>

          <div className="space-y-2">
            <label htmlFor="invite-role" className="text-[12.5px] font-medium text-fg-1">
              {t("members.roleLabel")}
            </label>
            <select
              id="invite-role"
              name="role"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              className="w-full rounded-md border border-border bg-bg-base px-3 py-2 text-[13px] text-fg-1 outline-none focus:border-accent"
            >
              {INVITE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>

          {error ? (
            <p
              role="alert"
              className="rounded-md border border-red/30 bg-red/10 px-3 py-2 text-[12.5px] text-red"
            >
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="inline-flex h-9 items-center rounded-md bg-accent px-4 text-[13px] font-medium text-accent-fg hover:opacity-90 disabled:opacity-60"
              data-testid="invite-submit"
            >
              {createMutation.isPending ? t("members.creating") : t("members.createInvitation")}
            </button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
