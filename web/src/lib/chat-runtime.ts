"use client";

/**
 * assistant-ui talking to the crawler's own agent loop.
 *
 * `ChatModelAdapter` is assistant-ui's documented seam for a backend that is
 * not the Vercel AI SDK, which is what made it the right library here: the
 * backend is Python, and nothing about the AI SDK's data-stream protocol was
 * worth reimplementing in FastAPI to get streaming chat.
 *
 * The mapping falls out cleanly, which is the sign the seam is in the right
 * place. The agent emits three things and assistant-ui already renders all
 * three:
 *
 *   thinking          -> reasoning part   (collapsed rationale)
 *   action.started    -> tool-call part   (spinner while it runs)
 *   action.finished   -> the same part, with its result
 *   answer            -> text part
 *
 * Each `yield` is the *whole* message so far, not a delta - that is the
 * adapter contract, and it is why the parts array is rebuilt each time rather
 * than appended to in place.
 */

import type { ChatModelAdapter, ChatModelRunResult } from "@assistant-ui/react";

/** Mirrors `crwallm.llm.agent`'s event dataclasses. */
type AgentEvent =
  | { type: "thinking"; text: string }
  | { type: "action.started"; action: string; detail: string }
  | {
      type: "action.finished";
      action: string;
      ok: boolean;
      summary: string;
      data: Record<string, unknown>;
    }
  | { type: "answer"; text: string };

type Part = NonNullable<ChatModelRunResult["content"]>[number];

/** Flatten assistant-ui's message parts back to the plain text the API takes. */
function textOf(message: { content: readonly { type: string; text?: string }[] }): string {
  return message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text ?? "")
    .join("\n")
    .trim();
}

async function* parseSse(response: Response): AsyncGenerator<AgentEvent> {
  const reader = response.body?.getReader();
  if (!reader) return;

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line. A chunk can split one anywhere,
    // so only whole frames are taken and the remainder stays buffered.
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");

      for (const line of frame.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try {
          yield JSON.parse(line.slice(6)) as AgentEvent;
        } catch {
          // A frame we cannot parse is not worth ending the turn over.
        }
      }
    }
  }
}

export const crawlerAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal }) {
    const history = messages.slice(0, -1).map((message) => ({
      role: message.role === "user" ? ("user" as const) : ("assistant" as const),
      content: textOf(message as never),
    }));
    const latest = textOf(messages[messages.length - 1] as never);

    const response = await fetch("/api/crwallm/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: latest, history }),
      signal: abortSignal,
    });

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        /* the status line stands */
      }
      yield { content: [{ type: "text", text: `요청이 실패했습니다: ${detail}` }] };
      return;
    }

    const parts: Part[] = [];
    // Tool calls are keyed by action so `finished` can find the `started`
    // part and fill in its result, rather than appending a second card.
    const pending = new Map<string, number>();
    let step = 0;

    for await (const event of parseSse(response)) {
      switch (event.type) {
        case "thinking":
          parts.push({ type: "reasoning", text: event.text });
          break;

        case "action.started": {
          step += 1;
          pending.set(event.action, parts.length);
          parts.push({
            type: "tool-call",
            toolCallId: `${event.action}-${step}`,
            toolName: event.action,
            args: { target: event.detail },
            argsText: JSON.stringify({ target: event.detail }),
          });
          break;
        }

        case "action.finished": {
          const index = pending.get(event.action);
          if (index === undefined) break;
          pending.delete(event.action);
          const started = parts[index] as Extract<Part, { type: "tool-call" }>;
          parts[index] = {
            ...started,
            result: { summary: event.summary, ...event.data },
            isError: !event.ok,
          };
          break;
        }

        case "answer":
          parts.push({ type: "text", text: event.text });
          break;
      }

      yield { content: [...parts] };
    }

    // A turn that produced only tool calls still has to end as a message with
    // text in it, or the thread shows an assistant bubble with nothing in it.
    if (!parts.some((part) => part.type === "text")) {
      parts.push({ type: "text", text: "(응답 없이 끝났습니다)" });
      yield { content: [...parts] };
    }
  },
};
