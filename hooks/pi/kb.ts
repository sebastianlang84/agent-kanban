// installed by kb setup: kb setup replaces this file while this line is in it
/**
 * kb session hooks: record session start, activity and end on the agent-kanban board.
 *
 * Each event runs `kb hook <event> --harness pi` with the pi session id. kb always exits 0,
 * prints only the start context and the prompt hints, and skips pi subagent children
 * (PI_SUBAGENT_DEPTH); errors are swallowed here so the board can never break a pi session.
 * pi sets no session variable itself, so the adapter exports PI_SESSION_ID and
 * KB_SESSION=pi:<id>: the agent's own `kb` shell calls then find this session.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// kb setup writes the command here: kb's absolute path, or python and kb on Windows.
const KB: string[] = @KB_COMMAND@;

export default function (pi: ExtensionAPI) {
	let context = "";

	async function hook(event: "start" | "stop" | "end" | "prompt", ctx: ExtensionContext,
		source?: string): Promise<string> {
		try {
			const r = await pi.exec(
				KB[0],
				[
					...KB.slice(1),
					"hook", event, "--harness", "pi", "--format", "text",
					"--id", ctx.sessionManager.getSessionId(),
					"--cwd", ctx.cwd || process.cwd(),
					...(event === "prompt" ? [] : ["--source", source ?? ""]),
				],
				{ timeout: 3000 },
			);
			return r.code === 0 ? r.stdout.trim() : "";
		} catch {
			// kb logs its own errors to hook-errors.log; nothing to do here.
			return "";
		}
	}

	pi.on("session_start", async (event, ctx) => {
		const id = ctx.sessionManager.getSessionId();
		if (id) {
			process.env.PI_SESSION_ID = id;
			process.env.KB_SESSION = `pi:${id}`;
		}
		context = await hook("start", ctx, event.reason);
	});

	// Start context into the system prompt; new requests and replies as a message, once each.
	pi.on("before_agent_start", async (event, ctx) => {
		const news = await hook("prompt", ctx);
		if (!context && !news) return undefined;
		return {
			...(context ? { systemPrompt: `${event.systemPrompt}\n\n${context}` } : {}),
			...(news ? { message: { customType: "kb-hints", content: news, display: true } } : {}),
		};
	});
	pi.on("agent_end", async (_event, ctx) => { await hook("stop", ctx); });
	pi.on("session_shutdown", async (event, ctx) => { await hook("end", ctx, event.reason); });
}
