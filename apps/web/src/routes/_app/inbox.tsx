import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { formatRelativeTime } from "@/lib/date";
import {
  AlertTriangle,
  Bot,
  Inbox as InboxIcon,
  PlugZap,
  ShieldAlert,
  Sparkles,
  TriangleAlert,
  UserPlus,
  type LucideIcon,
} from "lucide-react";
import { Suspense, useState } from "react";
import { useTranslation } from "react-i18next";

import { Gated } from "@/components/gating/Gated";
import { InboxSkeleton } from "@/components/inbox/skeleton";
import { EmptyState } from "@/components/shared/EmptyState";
import { ErrorBoundary } from "@/components/shared/ErrorBoundary";
import { Button } from "@/components/ui/button";
import { approveInvitation, declineInvitation } from "@/lib/api-client";
import {
  isZeroSafeKind,
  useInbox,
  type InboxItem,
  type InboxItemKind,
} from "@/hooks/use-inbox";
import { useCapabilities } from "@/stores/use-capabilities";

function kindMeta(kind: InboxItemKind): { icon: LucideIcon; tone: string; label: string } {
  switch (kind) {
    case "DEPLOY_GATE_FAIL":
      return { icon: ShieldAlert, tone: "text-red", label: "Gating" };
    case "FLAKY_PROMOTION":
      return { icon: TriangleAlert, tone: "text-amber", label: "Flaky" };
    case "MANUAL_RUN_FAIL":
      return { icon: AlertTriangle, tone: "text-red", label: "Run" };
    case "MCP_HEALTH":
      return { icon: PlugZap, tone: "text-amber", label: "MCP" };
    case "AGENT_GENERATION":
      return { icon: Bot, tone: "text-violet", label: "Agent" };
    case "AGENT_DIAGNOSIS":
      return { icon: Sparkles, tone: "text-violet", label: "Agent" };
    case "WORKSPACE_INVITE":
      return { icon: UserPlus, tone: "text-accent", label: "Invite" };
  }
}

/** Approve/decline actions for a `WORKSPACE_INVITE` card (M1e-9). The other
 * six kinds have no aggregator yet, so their "Review"/"Dismiss" buttons stay
 * disabled placeholders below. */
function InviteActions({ invitationId }: { invitationId: string }): React.ReactElement {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const invalidate = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: ["inbox"] });

  const approve = useMutation({
    mutationFn: () => approveInvitation(invitationId),
    onSuccess: invalidate,
    onError: () => setError("Could not approve. Try again."),
  });
  const decline = useMutation({
    mutationFn: () => declineInvitation(invitationId),
    onSuccess: invalidate,
    onError: () => setError("Could not decline. Try again."),
  });

  const pending = approve.isPending || decline.isPending;

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1.5">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={pending}
          onClick={() => approve.mutate()}
          data-testid="inbox-invite-approve"
        >
          Approve
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={pending}
          onClick={() => decline.mutate()}
          data-testid="inbox-invite-decline"
        >
          Decline
        </Button>
      </div>
      {error ? <p className="text-[11px] text-red">{error}</p> : null}
    </div>
  );
}

function NotificationCard({ item }: { item: InboxItem }): React.ReactElement {
  const meta = kindMeta(item.kind);
  const Icon = meta.icon;
  return (
    <article
      data-testid="inbox-card"
      data-kind={item.kind}
      data-read={item.status === "read" ? "true" : "false"}
      className="flex items-start gap-3 rounded-md border border-border bg-bg-elev-1 p-[14px]"
    >
      <span
        className={`mt-0.5 inline-flex h-7 w-7 items-center justify-center rounded-full bg-bg-elev-2 ${meta.tone}`}
        aria-hidden="true"
      >
        <Icon className="h-3.5 w-3.5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-[13px] font-semibold text-fg-1">{item.title}</h3>
          <span className="font-mono text-[10.5px] text-fg-5">
            {formatRelativeTime(item.createdAt)}
          </span>
        </div>
        <p className="text-[12.5px] text-fg-3">{item.body}</p>
        <div className="mt-1 flex items-center justify-between">
          <span className="font-mono text-[10.5px] text-fg-5">
            {meta.label}
            {item.ref ? ` · ${item.ref}` : ""}
          </span>
          {item.kind === "WORKSPACE_INVITE" ? (
            <InviteActions invitationId={item.id} />
          ) : (
            <div className="flex items-center gap-1.5">
              <Button type="button" size="sm" variant="outline" disabled>
                Review
              </Button>
              <Button type="button" size="sm" variant="ghost" disabled>
                Dismiss
              </Button>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}

function InboxList(): React.ReactElement {
  const { data } = useInbox("all");
  const llmReady = useCapabilities((s) => s.capabilities?.llm.status === "ready");
  const visible = data.items.filter((item) => {
    if (!llmReady) return isZeroSafeKind(item.kind);
    return true;
  });

  if (visible.length === 0) {
    return (
      <EmptyState
        icon={InboxIcon}
        title="Inbox is empty"
        subtitle="Nothing needs attention."
      />
    );
  }

  return (
    <div className="flex flex-col gap-[14px]" data-testid="inbox-list">
      {visible.map((item) => {
        if (item.kind === "AGENT_DIAGNOSIS" || item.kind === "AGENT_GENERATION") {
          return (
            <Gated key={item.id} feature="ai_panel" fallback={null}>
              <NotificationCard item={item} />
            </Gated>
          );
        }
        return <NotificationCard key={item.id} item={item} />;
      })}
    </div>
  );
}

function UnreadBadge(): React.ReactElement | null {
  const { data } = useInbox("all");
  if (data.unreadCount === 0) return null;
  return (
    <span
      data-testid="inbox-unread"
      className="inline-flex items-center rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent"
    >
      {data.unreadCount} unread
    </span>
  );
}

function InboxHeader(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <header className="flex items-center justify-between" data-testid="inbox-header">
      <div className="flex items-center gap-2.5">
        <h2 className="text-[20px] font-semibold tracking-[-.01em] text-fg-1">{t("inbox.title")}</h2>
        <Suspense fallback={null}>
          <UnreadBadge />
        </Suspense>
      </div>
    </header>
  );
}

function InboxError({ reset }: { reset: () => void }): React.ReactElement {
  return (
    <EmptyState
      icon={AlertTriangle}
      title="Couldn't load inbox"
      action={{ label: "Retry", onClick: reset }}
    />
  );
}

function Inbox(): React.ReactElement {
  return (
    <section className="flex flex-col gap-4" data-testid="inbox-screen">
      <ErrorBoundary fallback={({ reset }) => <InboxError reset={reset} />}>
        <InboxHeader />
        <Suspense fallback={<InboxSkeleton />}>
          <InboxList />
        </Suspense>
      </ErrorBoundary>
    </section>
  );
}

export const Route = createFileRoute("/_app/inbox")({
  component: Inbox,
  staticData: { title: "Inbox" },
});
