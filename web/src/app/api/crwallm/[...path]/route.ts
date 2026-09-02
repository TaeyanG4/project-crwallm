/**
 * Proxy to the FastAPI backend.
 *
 * Everything the browser asks for goes through here rather than straight to
 * :8000, for two reasons. The token is injected server-side, so it never
 * reaches the browser. And same-origin requests need no CORS, which means the
 * backend's origin allowlist can stay as narrow as it is.
 *
 * Streaming is passed through untouched: `GET /api/jobs/{id}/stream` is an
 * SSE feed, and buffering it here would turn a live crawl into one delivery
 * at the end.
 */

import { NextRequest } from "next/server";
import { API_HOST_HEADER, API_ORIGIN, API_TOKEN, TOKEN_HEADER } from "@/lib/server-config";

/** Node, not edge: the config is read from the filesystem. */
export const runtime = "nodejs";

/**
 * Never cache. A job list that is thirty seconds stale looks like a worker
 * that has stopped.
 */
export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, path: string[]): Promise<Response> {
  const target = new URL(`/api/${path.join("/")}`, API_ORIGIN);
  target.search = request.nextUrl.search;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);

  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);

  // Resuming an interrupted event stream: the browser sends where it got to.
  const lastEventId = request.headers.get("last-event-id");
  if (lastEventId) headers.set("last-event-id", lastEventId);

  headers.set("host", API_HOST_HEADER);
  if (API_TOKEN) headers.set(TOKEN_HEADER, API_TOKEN);

  const method = request.method;
  const body = method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      // A crawl's event stream stays open for as long as the crawl runs.
      signal: request.signal,
      cache: "no-store",
      // @ts-expect-error - undici option, not in the DOM fetch types
      duplex: "half",
    });
  } catch (error) {
    // The usual cause by far: the API is not running. Say so, rather than
    // letting a generic 500 send the operator looking at the wrong thing.
    return Response.json(
      {
        detail:
          `cannot reach the crawler API at ${API_ORIGIN} - ` +
          `is it running? (crwallm serve)`,
        cause: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers();
  // `content-disposition` matters: without it an export arrives as a document
  // the browser tries to render rather than a file it saves, and the
  // filename the backend chose is lost.
  for (const key of ["content-type", "cache-control", "content-disposition"]) {
    const value = upstream.headers.get(key);
    if (value) responseHeaders.set(key, value);
  }
  // Whatever the upstream said, an event stream must not be buffered on the
  // way through.
  if (upstream.headers.get("content-type")?.includes("text/event-stream")) {
    responseHeaders.set("cache-control", "no-cache, no-transform");
    responseHeaders.set("x-accel-buffering", "no");
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

/**
 * `RouteContext` is generated from the route literal and is globally
 * available - no import, and it stays correct if this file moves.
 */
type Context = RouteContext<"/api/crwallm/[...path]">;

export async function GET(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}

export async function DELETE(request: NextRequest, context: Context) {
  return proxy(request, (await context.params).path);
}
