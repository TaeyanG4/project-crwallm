"use client";

/**
 * The conversation.
 *
 * Built from assistant-ui primitives rather than a drop-in chat component,
 * which is the whole reason for choosing a primitives library: the interesting
 * part of a turn here is not the prose, it is the tool calls - what page was
 * inspected, what the recipe scored, which job got queued. Those get real
 * cards with the numbers on them, and the model's rationale is collapsed
 * because it is context, not the answer.
 */

import { AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive, ThreadPrimitive, useLocalRuntime } from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import Link from "next/link";
import { crawlerAdapter } from "@/lib/chat-runtime";
import { cn } from "@/lib/format";

const SUGGESTIONS = [
  "https://quotes.toscrape.com/ 에서 명언과 작가를 모아줘",
  "https://news.ycombinator.com/ 첫 페이지 구조를 봐줘",
] as const;

export default function ChatPage() {
  const runtime = useLocalRuntime(crawlerAdapter);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex h-full min-h-0 flex-col">
        <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-5 px-6 py-6">
            <ThreadPrimitive.Empty>
              <div className="space-y-4 py-16 text-center">
                <h1 className="text-xl font-semibold">무엇을 수집할까요?</h1>
                <p className="text-sm text-muted-foreground">
                  URL과 원하는 데이터를 말하면 페이지를 살펴보고, 레시피를 만들고, 크롤을
                  돌립니다.
                </p>
                <div className="flex flex-col items-center gap-2 pt-2">
                  {SUGGESTIONS.map((text) => (
                    <ThreadPrimitive.Suggestion
                      key={text}
                      prompt={text}
                      method="replace"
                      autoSend
                      className="rounded-lg border px-3 py-2 text-left text-sm transition-colors hover:bg-muted"
                    >
                      {text}
                    </ThreadPrimitive.Suggestion>
                  ))}
                </div>
              </div>
            </ThreadPrimitive.Empty>

            <ThreadPrimitive.Messages
              components={{ UserMessage, AssistantMessage }}
            />
          </div>
        </ThreadPrimitive.Viewport>

        <div className="shrink-0 border-t px-6 py-3">
          <ComposerPrimitive.Root className="mx-auto flex max-w-3xl items-end gap-2">
            <ComposerPrimitive.Input
              autoFocus
              rows={1}
              placeholder="URL과 원하는 데이터를 적어주세요"
              className="max-h-40 flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
            />
            <ThreadPrimitive.If running={false}>
              <ComposerPrimitive.Send className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground">
                보내기
              </ComposerPrimitive.Send>
            </ThreadPrimitive.If>
            <ThreadPrimitive.If running>
              <ComposerPrimitive.Cancel className="rounded-lg border px-4 py-2 text-sm">
                중지
              </ComposerPrimitive.Cancel>
            </ThreadPrimitive.If>
          </ComposerPrimitive.Root>
        </div>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end">
      <div className="max-w-[80%] rounded-2xl bg-muted px-4 py-2 text-sm">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="space-y-2">
      <MessagePrimitive.Parts
        components={{
          Text: AssistantText,
          Reasoning: Rationale,
          tools: { Fallback: ToolCard },
        }}
      />
    </MessagePrimitive.Root>
  );
}

/**
 * Markdown, not plain text.
 *
 * The model answers with lists and bold labels because that is how it writes
 * a set of extracted rows, and rendering that literally shows the reader
 * asterisks instead of a list.
 */
function AssistantText() {
  return (
    <div className="space-y-2 text-sm leading-relaxed [&_a]:text-running [&_a]:underline [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-xs [&_li]:ml-4 [&_li]:list-disc [&_ol_li]:list-decimal [&_strong]:font-semibold">
      <MarkdownTextPrimitive />
    </div>
  );
}

/**
 * The model's one-line reason for a step.
 *
 * Small and grey on purpose: useful when a turn goes somewhere unexpected,
 * noise the rest of the time.
 */
function Rationale({ text }: { text: string }) {
  return <p className="border-l-2 pl-3 text-xs text-muted-foreground">{text}</p>;
}

const TOOL_LABELS: Record<string, string> = {
  inspect: "페이지 살펴보기",
  make_recipe: "레시피 만들기",
  crawl: "크롤 실행",
};

type ToolResult = {
  summary?: string;
  job_id?: string;
  name?: string;
  score?: number;
  records?: number;
  fill?: string;
  fields?: string[];
  container?: string;
  columns?: unknown[];
  recipe?: string | null;
};

function ToolCard({
  toolName,
  args,
  result,
  isError,
}: {
  toolName: string;
  args: Record<string, unknown>;
  result?: unknown;
  isError?: boolean;
}) {
  const done = result !== undefined;
  const data = (result ?? {}) as ToolResult;
  const target = String(args?.target ?? "");

  return (
    <div
      className={cn(
        "space-y-1.5 rounded-lg border p-3 text-sm",
        isError && "border-bad/40 bg-bad/5",
      )}
    >
      <div className="flex items-center gap-2">
        {!done && (
          <span className="size-1.5 animate-pulse rounded-full bg-running" aria-hidden />
        )}
        <span className="font-medium">{TOOL_LABELS[toolName] ?? toolName}</span>
        {target && (
          <span className="truncate font-mono text-xs text-muted-foreground">{target}</span>
        )}
      </div>

      {done && data.summary && (
        <p className={cn("text-xs", isError ? "text-bad" : "text-muted-foreground")}>
          {data.summary}
        </p>
      )}

      {/* A queued job is the one result worth a link: it is where the data
          actually shows up, and the crawl is still running when this renders. */}
      {done && data.job_id && (
        <Link
          href={`/jobs/${data.job_id}`}
          className="inline-block text-xs text-running hover:underline"
        >
          실행 화면 열기 →
        </Link>
      )}

      {done && data.fields && data.fields.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-0.5">
          {data.fields.map((field) => (
            <span key={field} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
              {field}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
