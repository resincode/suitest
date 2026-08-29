import { describe, expect, it } from "vitest";

import {
  MCP_INSTALL_DOCS_URL,
} from "@/components/mcp/ConnectIdeDialog";
import {
  IDE_CLIENTS,
  claudeCmd,
  installCmd,
  mcpJson,
} from "@/components/mcp/connect-ide-commands";

describe("ConnectIdeDialog commands", () => {
  it("uses the npx package, not the stale python entry point", () => {
    expect(claudeCmd()).toContain("npx -y @suiflex/suitest-mcp");
    expect(claudeCmd()).not.toContain("python -m suitest_lifecycle.mcp_server");

    const json = mcpJson();
    expect(json).toContain('"command": "npx"');
    expect(json).toContain('"@suiflex/suitest-mcp"');
    expect(json).not.toContain("suitest_lifecycle.mcp_server");
  });

  it("generates per-IDE installer commands with the right --client target", () => {
    expect(installCmd(IDE_CLIENTS.claude)).toBe(
      "npx -y @suiflex/suitest-mcp install --client claude-code",
    );
    expect(installCmd(IDE_CLIENTS.cursor)).toBe(
      "npx -y @suiflex/suitest-mcp install --client cursor",
    );
    expect(installCmd(IDE_CLIENTS.windsurf)).toBe(
      "npx -y @suiflex/suitest-mcp install --client windsurf",
    );
  });

  it("links the IDE setup docs to the public MCP install guide", async () => {
    expect(MCP_INSTALL_DOCS_URL).toBe(
      "https://suitest.suiflex.dev/docs/install/mcp-server/",
    );
  });
});
