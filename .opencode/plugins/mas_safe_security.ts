import { appendFile, mkdir } from "node:fs/promises"
import path from "node:path"

const EVENT_LOG_PATH = path.join("events", "opencode_events.jsonl")
const TRACKING_ENV_KEYS = ["MAS_RUN_ID", "MAS_CAPTURE_DIR", "MAS_ISSUE_ID"]

function sanitize(value: unknown, seen = new WeakSet<object>()): unknown {
  if (value === null || value === undefined) return null
  if (typeof value === "bigint") return value.toString()
  if (typeof value === "function") return `[function ${value.name || "anonymous"}]`
  if (typeof value !== "object") return value
  if (seen.has(value)) return "[circular]"
  seen.add(value)

  if (Array.isArray(value)) {
    return value.map((item) => sanitize(item, seen))
  }

  const output: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(value)) {
    output[key] = sanitize(item, seen)
  }
  return output
}

function correlationFields(...sources: unknown[]): Record<string, unknown> {
  const selected: Record<string, unknown> = {}
  for (const source of sources) {
    if (!source || typeof source !== "object" || Array.isArray(source)) continue
    for (const [key, value] of Object.entries(source)) {
      if (key.endsWith("ID") || key.endsWith("Id") || key === "id" || key === "tool" || key === "command") {
        selected[key] = sanitize(value)
      }
    }
  }
  return selected
}

async function appendEvent(
  nativeEventType: string,
  input: unknown,
  output: unknown,
  context: { directory: string; worktree: string },
) {
  const captureDir = process.env.MAS_CAPTURE_DIR || path.join(context.directory, "artifacts", "captures", "fallback")
  const eventPath = path.join(captureDir, EVENT_LOG_PATH)
  const record = {
    timestamp: new Date().toISOString(),
    native_event_type: nativeEventType,
    run_id: process.env.MAS_RUN_ID || null,
    issue_id: process.env.MAS_ISSUE_ID || null,
    context,
    correlation: correlationFields(input, output),
    raw_input: sanitize(input),
    raw_output: sanitize(output),
  }

  await mkdir(path.dirname(eventPath), { recursive: true })
  await appendFile(eventPath, `${JSON.stringify(record)}\n`, "utf8")
}

export const MasSafeSecurity = async ({ directory, worktree }) => {
  const context = { directory, worktree }

  const record = async (nativeEventType: string, input: unknown, output: unknown) => {
    await appendEvent(nativeEventType, input, output, context)
  }

  const injectTrackingEnv = (output: { env?: Record<string, string> }) => {
    output.env = output.env || {}
    for (const key of TRACKING_ENV_KEYS) {
      output.env[key] = process.env[key] || ""
    }
  }

  return {
    event: async ({ event }) => {
      const eventType = event && typeof event === "object" && "type" in event ? String(event.type) : "event.unknown"
      await record(eventType, event, null)
    },
    "command.execute.before": async (input, output) => {
      await record("command.execute.before", input, output)
    },
    "tool.execute.before": async (input, output) => {
      await record("tool.execute.before", input, output)
    },
    "tool.execute.after": async (input, output) => {
      await record("tool.execute.after", input, output)
    },
    "permission.ask": async (input, output) => {
      await record("permission.ask", input, output)
    },
    "shell.env": async (input, output) => {
      const shellOutput =
        output && typeof output === "object" && !Array.isArray(output) ? output : { env: {} }
      injectTrackingEnv(shellOutput as { env?: Record<string, string> })
      await record("shell.env", input, shellOutput)
    },
  }
}
