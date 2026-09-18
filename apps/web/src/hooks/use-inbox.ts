import { useSuspenseQuery, type UseSuspenseQueryResult } from "@tanstack/react-query";

import { api } from "@/lib/api-client";
import type { components } from "@/lib/api-types";

/**
 * Inbox notification kinds. Six of them (everything but `WORKSPACE_INVITE`)
 * remain a wire-shape stub server-side — no aggregator exists yet, so the
 * backend never returns them; the UI still renders their icon/label so a
 * screenshot or Storybook fixture stays meaningful ahead of that work.
 *
 * These types are generated from `packages/shared/openapi.json`
 * (`InboxItem`/`InboxResponse`/`InboxKind` in `routers/inbox.py`) — the wire
 * shape now has one real backend, so it is no longer modeled locally.
 */
export type InboxItemKind = components["schemas"]["InboxItem"]["kind"];
export type InboxItem = components["schemas"]["InboxItem"];

export interface InboxPage {
  items: InboxItem[];
  unreadCount: number;
}

export function useInbox(status: "all" | "unread" = "all"): UseSuspenseQueryResult<InboxPage> {
  return useSuspenseQuery({
    queryKey: ["inbox", status] as const,
    queryFn: async () => {
      const res = await api.get<components["schemas"]["InboxResponse"]>("/inbox", {
        params: { status },
      });
      return { items: res.data.items ?? [], unreadCount: res.data.unreadCount ?? 0 };
    },
  });
}

/** Item kinds backed by deterministic signals only — visible in ZERO. */
export function isZeroSafeKind(kind: InboxItemKind): boolean {
  switch (kind) {
    case "DEPLOY_GATE_FAIL":
    case "FLAKY_PROMOTION":
    case "MANUAL_RUN_FAIL":
    case "MCP_HEALTH":
    case "WORKSPACE_INVITE":
      return true;
    case "AGENT_GENERATION":
    case "AGENT_DIAGNOSIS":
      return false;
  }
}
