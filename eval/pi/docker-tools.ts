/**
 * Route Pi's default tools (read, bash, edit, write) into one existing Docker container.
 *
 * The Pi process and its provider key stay on the host; every file access and command runs
 * inside the container, which the harness starts with --network=none. The host environment
 * is never forwarded, so the provider key cannot reach commands run by the model.
 *
 * Env: CI_REPAIR_PI_CONTAINER (required), CI_REPAIR_PI_MAX_TURNS, CI_REPAIR_PI_COMMAND_SECONDS.
 */

import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
	type BashOperations,
	createBashTool,
	createEditTool,
	createReadTool,
	createWriteTool,
	type ReadOperations,
	type WriteOperations,
} from "@earendil-works/pi-coding-agent";

const WORKSPACE = "/workspace";
const container = process.env.CI_REPAIR_PI_CONTAINER ?? "";
const maxTurns = Number(process.env.CI_REPAIR_PI_MAX_TURNS ?? "30");
const commandSeconds = Number(process.env.CI_REPAIR_PI_COMMAND_SECONDS ?? "120");
const hostCwd = process.cwd();

/** Pi resolves tool paths against its host cwd; map that tree onto the container workspace. */
function guest(value: string): string {
	if (value === hostCwd) return WORKSPACE;
	if (value.startsWith(`${hostCwd}/`)) return `${WORKSPACE}/${value.slice(hostCwd.length + 1)}`;
	return value;
}

function docker(
	args: string[],
	options: { input?: string; onData?: (data: Buffer) => void; signal?: AbortSignal } = {},
): Promise<{ code: number | null; stdout: Buffer; stderr: Buffer }> {
	return new Promise((resolve, reject) => {
		// A minimal environment: never forward the host's provider credentials.
		const child = spawn("docker", args, {
			env: { PATH: process.env.PATH ?? "/usr/bin:/bin", HOME: process.env.HOME ?? "/" },
			stdio: [options.input === undefined ? "ignore" : "pipe", "pipe", "pipe"],
			signal: options.signal,
		});
		const out: Buffer[] = [];
		const err: Buffer[] = [];
		child.stdout.on("data", (chunk: Buffer) => (options.onData ? options.onData(chunk) : out.push(chunk)));
		child.stderr.on("data", (chunk: Buffer) => (options.onData ? options.onData(chunk) : err.push(chunk)));
		child.on("error", reject);
		child.on("close", (code) => resolve({ code, stdout: Buffer.concat(out), stderr: Buffer.concat(err) }));
		if (options.input !== undefined) child.stdin?.end(options.input);
	});
}

async function checked(args: string[], input?: string): Promise<Buffer> {
	const result = await docker(["exec", ...(input === undefined ? [] : ["-i"]), container, ...args], { input });
	if (result.code !== 0) throw new Error(result.stderr.toString().trim() || `exit ${result.code}`);
	return result.stdout;
}

const readOps: ReadOperations = {
	readFile: (file) => checked(["cat", "--", guest(file)]),
	access: async (file) => {
		await checked(["test", "-r", guest(file)]);
	},
};

const writeOps: WriteOperations = {
	writeFile: async (file, content) => {
		await checked(["sh", "-c", 'cat > "$1"', "sh", guest(file)], content);
	},
	mkdir: async (dir) => {
		await checked(["mkdir", "-p", "--", guest(dir)]);
	},
};

const bashOps: BashOperations = {
	exec: async (command, cwd, { onData, signal, timeout }) => {
		const seconds = Math.min(timeout && timeout > 0 ? timeout : commandSeconds, commandSeconds);
		const result = await docker(
			[
				"exec",
				"-w",
				guest(cwd),
				container,
				"timeout",
				"--signal=TERM",
				"--kill-after=2s",
				`${seconds}s`,
				"bash",
				"-lc",
				command,
			],
			{ onData, signal },
		);
		return { exitCode: result.code };
	},
};

export default function (pi: ExtensionAPI) {
	if (!container) throw new Error("CI_REPAIR_PI_CONTAINER is required");
	const tools = [
		createReadTool(WORKSPACE, { operations: readOps }),
		createWriteTool(WORKSPACE, { operations: writeOps }),
		createEditTool(WORKSPACE, { operations: { ...readOps, writeFile: writeOps.writeFile } }),
		createBashTool(WORKSPACE, { operations: bashOps }),
	];
	for (const tool of tools) pi.registerTool(tool);

	let turns = 0;
	pi.on("turn_end", async (_event, ctx) => {
		turns += 1;
		if (turns >= maxTurns) ctx.abort();
	});

	pi.on("before_agent_start", async (event) => {
		const line = `Current working directory: ${WORKSPACE} (isolated container without network access)`;
		return { systemPrompt: event.systemPrompt.replace(/Current working directory: .*/, line) };
	});
}
