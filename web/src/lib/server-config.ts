/**
 * Where the backend is, and the token to reach it with.
 *
 * Server-side only. The token must never be sent to the browser: the API is
 * unauthenticated apart from this header, and a page the user happens to be
 * visiting can POST to 127.0.0.1. Shipping the token to the client would hand
 * that page the one thing stopping it from driving the crawler
 * (docs/11_SECURITY_MODEL.md).
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

const ENV_FILE_KEYS = ["CRWALLM_API_TOKEN", "CRWALLM_API_HOST", "CRWALLM_API_PORT"] as const;

type EnvKey = (typeof ENV_FILE_KEYS)[number];

/**
 * Read the backend's own `.env`.
 *
 * The alternative is asking the operator to copy the token into a second file
 * and keep the two in step, which is a step that will be forgotten exactly
 * once and then debugged for twenty minutes. One process, one machine, one
 * config file.
 */
function fromProjectEnv(): Partial<Record<EnvKey, string>> {
  const out: Partial<Record<EnvKey, string>> = {};
  try {
    const text = readFileSync(join(process.cwd(), "..", ".env"), "utf8");
    for (const line of text.split(/\r?\n/)) {
      const match = /^\s*([A-Z_]+)\s*=\s*(.*)$/.exec(line);
      if (!match) continue;
      const [, key, rawValue] = match;
      if (!(ENV_FILE_KEYS as readonly string[]).includes(key)) continue;
      out[key as EnvKey] = rawValue.trim().replace(/^["']|["']$/g, "");
    }
  } catch {
    // No file is normal - the environment may be set directly.
  }
  return out;
}

const fileEnv = fromProjectEnv();

function setting(key: EnvKey, fallback: string): string {
  return process.env[key] ?? fileEnv[key] ?? fallback;
}

export const API_ORIGIN = `http://${setting("CRWALLM_API_HOST", "127.0.0.1")}:${setting(
  "CRWALLM_API_PORT",
  "8000",
)}`;

export const API_TOKEN = setting("CRWALLM_API_TOKEN", "");

export const TOKEN_HEADER = "X-CRWALLM-Token";

/**
 * The backend rejects any Host it does not recognise with a 421, which is what
 * blocks DNS rebinding. Proxied requests have to present one it knows.
 */
export const API_HOST_HEADER = setting("CRWALLM_API_HOST", "127.0.0.1");
