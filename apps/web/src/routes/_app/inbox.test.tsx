import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/mocks/server";
import { routeTree } from "@/routeTree.gen";
import { CLOUD_CAPS, resetCaps, setCaps } from "@/test/capabilities";

function renderInbox() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: ["/inbox"] }),
    context: { queryClient },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const FIXTURE = {
  unreadCount: 2,
  items: [
    {
      id: "nf_01",
      kind: "DEPLOY_GATE_FAIL",
      title: "Gating suite failed on main",
      body: "Smoke @ main reported 2 failing steps.",
      ref: "RUN-1002",
      createdAt: "2026-05-27T11:13:40Z",
      status: "unread",
    },
    {
      id: "nf_02",
      kind: "AGENT_DIAGNOSIS",
      title: "Agent filed a defect",
      body: "DEF-201 auto-filed.",
      ref: "DEF-201",
      createdAt: "2026-05-26T09:00:00Z",
      status: "read",
    },
  ],
};

const INVITE_FIXTURE = {
  unreadCount: 1,
  items: [
    {
      id: "inv_01",
      kind: "WORKSPACE_INVITE",
      title: "Admin invited you to Acme",
      body: "Join as QA — approve or decline below.",
      createdAt: "2026-05-27T11:13:40Z",
      status: "unread",
    },
  ],
};

describe("Inbox screen", () => {
  beforeEach(() => {
    resetCaps();
    server.use(
      http.get("*/api/v1/auth/me", () =>
        HttpResponse.json({ id: "u_demo", email: "demo@suitest.dev", name: "Maya", memberships: [] }),
      ),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders the skeleton before /inbox resolves", async () => {
    server.use(
      http.get("*/api/v1/inbox", async () => {
        // Project `lib` target is ES2023 — no `Promise.withResolvers` yet.
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(FIXTURE);
      }),
    );
    renderInbox();
    expect(await screen.findByTestId("inbox-skeleton")).toBeInTheDocument();
  });

  it("ZERO tier: hides AI-typed cards (AGENT_*) and shows only deterministic items", async () => {
    server.use(http.get("*/api/v1/inbox", () => HttpResponse.json(FIXTURE)));
    renderInbox();
    const list = await screen.findByTestId("inbox-list", undefined, { timeout: 3000 });
    expect(list.querySelectorAll('[data-testid="inbox-card"]').length).toBe(1);
    expect(list.querySelector('[data-kind="DEPLOY_GATE_FAIL"]')).not.toBeNull();
    expect(list.querySelector('[data-kind="AGENT_DIAGNOSIS"]')).toBeNull();
  });

  it("CLOUD tier: renders both deterministic and agent cards", async () => {
    setCaps(CLOUD_CAPS);
    // Pin /capabilities so the RootLayout effect re-fetch doesn't downgrade
    // the seeded tier back to ZERO during the test.
    server.use(
      http.get("*/capabilities", () => HttpResponse.json(CLOUD_CAPS)),
      http.get("*/api/v1/capabilities", () => HttpResponse.json(CLOUD_CAPS)),
      http.get("*/api/v1/inbox", () => HttpResponse.json(FIXTURE)),
    );
    renderInbox();
    const list = await screen.findByTestId("inbox-list", undefined, { timeout: 3000 });
    expect(list.querySelectorAll('[data-testid="inbox-card"]').length).toBe(2);
    expect(list.querySelector('[data-kind="AGENT_DIAGNOSIS"]')).not.toBeNull();
  });

  it("renders the empty state when there are no items", async () => {
    server.use(
      http.get("*/api/v1/inbox", () => HttpResponse.json({ unreadCount: 0, items: [] })),
    );
    renderInbox();
    expect(
      await screen.findByText(/Inbox is empty/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument();
  });

  it("renders the error fallback when /inbox 500s", async () => {
    server.use(
      http.get("*/api/v1/inbox", () =>
        HttpResponse.json({ code: "BOOM", message: "nope" }, { status: 500 }),
      ),
    );
    renderInbox();
    expect(
      await screen.findByText(/Couldn't load inbox/i, undefined, { timeout: 3000 }),
    ).toBeInTheDocument();
  });

  it("renders the unread badge when there are unread items", async () => {
    server.use(http.get("*/api/v1/inbox", () => HttpResponse.json(FIXTURE)));
    renderInbox();
    expect(
      await screen.findByTestId("inbox-unread", undefined, { timeout: 3000 }),
    ).toHaveTextContent("2 unread");
  });

  it("renders a WORKSPACE_INVITE card and approves it (M1e-9)", async () => {
    let approved = false;
    server.use(
      // The invitee already belongs to one workspace here — an empty
      // membership list hits the app shell's onboarding gate, which
      // disables pointer events on the rest of the page.
      http.get("*/api/v1/auth/me", () =>
        HttpResponse.json({
          id: "u_demo",
          email: "demo@suitest.dev",
          name: "Maya",
          memberships: [{ workspace_id: "ws_1", role: "OWNER", workspace: { id: "ws_1", slug: "demo", name: "Demo" } }],
        }),
      ),
      http.get("*/api/v1/inbox", () =>
        HttpResponse.json(approved ? { unreadCount: 0, items: [] } : INVITE_FIXTURE),
      ),
      http.post("*/api/v1/invitations/inv_01/approve", () => {
        approved = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderInbox();
    const card = await screen.findByTestId("inbox-card", undefined, { timeout: 3000 });
    expect(card).toHaveTextContent("Admin invited you to Acme");

    await userEvent.click(screen.getByTestId("inbox-invite-approve"));

    await waitFor(() => expect(screen.queryByTestId("inbox-card")).not.toBeInTheDocument());
    expect(await screen.findByText(/Inbox is empty/i)).toBeInTheDocument();
  });

  it("declines a WORKSPACE_INVITE card", async () => {
    let declined = false;
    server.use(
      http.get("*/api/v1/auth/me", () =>
        HttpResponse.json({
          id: "u_demo",
          email: "demo@suitest.dev",
          name: "Maya",
          memberships: [{ workspace_id: "ws_1", role: "OWNER", workspace: { id: "ws_1", slug: "demo", name: "Demo" } }],
        }),
      ),
      http.get("*/api/v1/inbox", () =>
        HttpResponse.json(declined ? { unreadCount: 0, items: [] } : INVITE_FIXTURE),
      ),
      http.post("*/api/v1/invitations/inv_01/decline", () => {
        declined = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderInbox();
    await screen.findByTestId("inbox-card", undefined, { timeout: 3000 });

    await userEvent.click(screen.getByTestId("inbox-invite-decline"));

    await waitFor(() => expect(screen.queryByTestId("inbox-card")).not.toBeInTheDocument());
    expect(await screen.findByText(/Inbox is empty/i)).toBeInTheDocument();
  });
});
