// installed by kb setup: kb setup replaces this file while this line is in it
/**
 * kb session hooks: record session start, activity and end on the agent-kanban board.
 *
 * Each event runs `kb hook <event> --harness pi` with the pi session id. kb always exits 0,
 * prints nothing (except the prompt hints) and skips pi subagent children (PI_SUBAGENT_DEPTH); errors are swallowed here
 * so the board can never break a pi session.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// kb setup writes the command here: kb's absolute path, or python and kb on Windows.
const KB: string[] = @KB_COMMAND@;

export default function (pi: ExtensionAPI) {
	async function hook(event: "start" | "stop" | "end", ctx: ExtensionContext, source?: string) {
		try {
			await pi.exec(
				KB[0],
				[
					...KB.slice(1),
					"hook", event, "--harness", "pi",
					"--id", ctx.sessionManager.getSessionId(),
					"--cwd", ctx.cwd || process.cwd(),
					"--source", source ?? "",
				],
				{ timeout: 3000 },
			);
		} catch {
			// kb logs its own errors to hook-errors.log; nothing to do here.
		}
	}

	// Prompt hints: kb prints new requests and replies for this session name, once each.
	pi.on("before_agent_start", async (_event, ctx) => {
		try {
			const r = await pi.exec(
				KB[0],
				[...KB.slice(1), "hook", "prompt", "--harness", "pi", "--id", ctx.sessionManager.getSessionId(),
				 "--cwd", ctx.cwd || process.cwd()],
				{ timeout: 3000 },
			);
			const text = r.stdout.trim();
			if (r.code === 0 && text) {
				return { message: { customType: "kb-hints", content: text, display: true } };
			}
		} catch {
			// kb logs its own errors; a missing hint never blocks the prompt.
		}
		return undefined;
	});
	pi.on("session_start", async (event, ctx) => hook("start", ctx, event.reason));
	pi.on("agent_end", async (_event, ctx) => hook("stop", ctx));
	pi.on("session_shutdown", async (event, ctx) => hook("end", ctx, event.reason));
}
