// Pi adapter for kb (Claude Code and Codex use command hooks, see README).
// Symlink into ~/.pi/agent/extensions/. It exports KB_SESSION so the agent's `kb` shell
// calls find this session, and runs `kb hook` at session start, each prompt, each answer
// and shutdown. Hooks record; failures never reach the session.
import { execFileSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function hook(event: string, sessionId: string, cwd: string): string {
  try {
    return execFileSync("kb", ["hook", event, "--harness", "pi", "--format", "text"], {
      input: JSON.stringify({ session_id: sessionId, cwd }),
      encoding: "utf8",
      timeout: 3000,
    }).trim();
  } catch {
    return "";
  }
}

export default function kbBoard(pi: ExtensionAPI) {
  let sessionId = "";
  let context = "";

  pi.on("session_start", async (_event, ctx) => {
    sessionId = ctx.sessionManager.getSessionId();
    if (!sessionId) return;
    process.env.KB_SESSION = `pi:${sessionId}`;
    context = hook("start", sessionId, ctx.cwd);
  });

  pi.on("before_agent_start", async (event, ctx) => {
    if (!sessionId) return;
    const news = hook("prompt", sessionId, ctx.cwd);
    return {
      ...(context ? { systemPrompt: `${event.systemPrompt}\n\n${context}` } : {}),
      ...(news ? { message: { customType: "kb", content: news, display: true } } : {}),
    };
  });

  pi.on("agent_end", async (_event, ctx) => {
    if (sessionId) hook("stop", sessionId, ctx.cwd);
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (sessionId) hook("end", sessionId, ctx.cwd);
  });
}
