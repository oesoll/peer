// peer for OmO: this session reports its state to peer and takes peer messages as user input.
//
// peer-session (behind peer spawn and sm) packs the session's identity (PEER_NAME, where peer lives, an sm holder
// socket) into OMO_PEER_IDENTITY: OmO runs the session in an RPC host that receives only OMO_*/SENPI_*/PI_* and a
// few basics. Without that identity (OmO started by hand) this extension does nothing: such a session has no name
// to answer to, and ten hand-started sessions in one folder would all register as the same folder name.
//
// State goes to peer the way Claude's hooks send it: one JSON event to `peer-hook --kind omo` per change, in order.
// Mail comes in on a unix socket this extension opens: one JSON line {id, text} in, {ok, mode} out
// (peer_delivery.omo_deliver). Install it by linking this file into ~/.omo/agent/extensions/.
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

function readIdentity() {
	try {
		const value = JSON.parse(process.env.OMO_PEER_IDENTITY || "");
		return value && value.PEER_BIN && value.PEER_STATE ? value : null;
	} catch {
		return null;
	}
}

export default function peer(pi) {
	const identity = readIdentity();
	if (!identity) return;
	const bin = identity.PEER_BIN;
	const state = identity.PEER_STATE;
	let sessionId = null;
	let cwd = process.cwd();
	let sock = null;
	let server = null;
	let ctxNow = null;
	let queue = Promise.resolve();

	// One event at a time: a later state must never be written before an earlier one.
	const report = (event) => {
		if (!sessionId) return;
		const body = JSON.stringify({ hook_event_name: event, session_id: sessionId, cwd, omo_socket: sock });
		queue = queue.then(
			() =>
				new Promise((resolve) => {
					let child;
					try {
						child = spawn(path.join(bin, "peer-hook"), ["--kind", "omo"], {
							env: { ...process.env, ...identity },
							stdio: ["pipe", "ignore", "ignore"],
						});
					} catch {
						return resolve();
					}
					const timer = setTimeout(() => {
						child.kill();
						resolve();
					}, 5000);
					const done = () => {
						clearTimeout(timer);
						resolve();
					};
					child.on("error", done);
					child.on("exit", done);
					child.stdin.on("error", () => {});
					child.stdin.end(body);
				}),
		);
		return queue;
	};

	const answer = (line) => {
		const { text } = JSON.parse(line);
		if (typeof text !== "string" || !text) throw new Error("empty message");
		const idle = !ctxNow || ctxNow.isIdle();
		// While a turn runs OmO requires a delivery mode; steer lands after the current tool calls.
		if (idle) pi.sendUserMessage(text);
		else pi.sendUserMessage(text, { deliverAs: "steer" });
		return { ok: true, mode: idle ? "start" : "steer" };
	};

	const listen = () => {
		const dir = path.join(state, "omo");
		fs.mkdirSync(dir, { recursive: true });
		sock = path.join(dir, `${process.pid}-${sessionId.slice(0, 12)}.sock`);
		try {
			fs.unlinkSync(sock);
		} catch {}
		server = net.createServer((conn) => {
			let buffer = "";
			conn.setEncoding("utf8");
			conn.on("error", () => {});
			conn.on("data", (chunk) => {
				buffer += chunk;
				const end = buffer.indexOf("\n");
				if (end < 0) return;
				let reply;
				try {
					reply = answer(buffer.slice(0, end));
				} catch (error) {
					reply = { ok: false, error: String((error && error.message) || error).slice(0, 200) };
				}
				conn.end(JSON.stringify(reply) + "\n");
			});
		});
		server.on("error", () => {});
		server.listen(sock);
	};

	const close = () => {
		if (server) server.close();
		server = null;
		if (sock) {
			try {
				fs.unlinkSync(sock);
			} catch {}
		}
	};

	pi.on("session_start", async (_event, ctx) => {
		ctxNow = ctx;
		sessionId = ctx.sessionManager.getSessionId();
		cwd = ctx.cwd || process.cwd();
		if (!sessionId) return;
		listen();
		report("SessionStart");
	});
	pi.on("agent_start", async (_event, ctx) => {
		ctxNow = ctx;
		report("UserPromptSubmit");
	});
	// agent_end can still be followed by a retry or a queued follow-up; settled is the real end of the run.
	pi.on("agent_settled", async (_event, ctx) => {
		ctxNow = ctx;
		report("Stop");
	});
	// Not awaited: shutdown handlers have a 2 s budget. If the host exits first, peer sees the process gone anyway.
	pi.on("session_shutdown", async () => {
		close();
		report("SessionEnd");
		sessionId = null;
	});
}
