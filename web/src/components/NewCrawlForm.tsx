"use client";

/**
 * Start a crawl by hand.
 *
 * The chat is the easy path; this is the one that stays honest. Every field
 * here maps to a CrawlSpec field, so what the form submits is what the CLI
 * would submit, and a crawl that misbehaves can be reproduced exactly.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { RecipeSummary } from "@/lib/types";

/**
 * Guess the scope from the seed.
 *
 * `allowed_domains` is required and unbounded crawls are refused, so leaving
 * it blank is not an option - but making the operator retype the host they
 * just pasted is friction with no safety in it. The guess is shown and
 * editable, never silent.
 */
function hostOf(seed: string): string {
  try {
    return new URL(seed.trim()).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

export function NewCrawlForm({ onSubmitted }: { onSubmitted?: () => void }) {
  const router = useRouter();
  const [seed, setSeed] = useState("");
  // `null` means "not edited yet", which is what lets the guess follow the
  // seed. Deriving the shown value beats copying one field into the other in
  // an effect: there is no render where the two disagree.
  const [typedDomains, setTypedDomains] = useState<string | null>(null);
  const [recipe, setRecipe] = useState("");
  const [mode, setMode] = useState<"collect" | "spider">("collect");
  const [maxPages, setMaxPages] = useState(50);
  const [maxDepth, setMaxDepth] = useState(2);
  const [recipes, setRecipes] = useState<RecipeSummary[]>([]);
  const domains = typedDomains ?? hostOf(seed);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listRecipes().then(setRecipes).catch(() => setRecipes([]));
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const job = await api.submit({
        seed_urls: seed
          .split(/[\s,]+/)
          .map((s) => s.trim())
          .filter(Boolean),
        allowed_domains: domains
          .split(/[\s,]+/)
          .map((s) => s.trim())
          .filter(Boolean),
        mode,
        // Spider mode requires it, and a collect run of one page does not.
        follow_links: mode === "spider" || maxDepth > 0,
        recipe: recipe || null,
        limits: { max_pages: maxPages, max_depth: maxDepth },
      });
      onSubmitted?.();
      router.push(`/jobs/${job.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">시드 URL</span>
          <input
            required
            value={seed}
            onChange={(e) => setSeed(e.target.value)}
            placeholder="https://example.com/products"
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </label>

        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">허용 도메인</span>
          <input
            required
            value={domains}
            onChange={(e) => setTypedDomains(e.target.value)}
            placeholder="example.com"
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">모드</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "collect" | "spider")}
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm"
          >
            <option value="collect">collect — 아는 페이지</option>
            <option value="spider">spider — 사이트 탐색</option>
          </select>
        </label>

        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">레시피</span>
          <select
            value={recipe}
            onChange={(e) => setRecipe(e.target.value)}
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm"
          >
            <option value="">없음 — 수집만</option>
            {recipes.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name} ({r.status})
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">최대 페이지</span>
          <input
            type="number"
            min={1}
            max={100000}
            value={maxPages}
            onChange={(e) => setMaxPages(Number(e.target.value))}
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm"
          />
        </label>

        <label className="space-y-1">
          <span className="text-xs font-medium text-muted-foreground">최대 깊이</span>
          <input
            type="number"
            min={0}
            max={20}
            value={maxDepth}
            onChange={(e) => setMaxDepth(Number(e.target.value))}
            className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm"
          />
        </label>
      </div>

      {!recipe && (
        <p className="text-xs text-muted-foreground">
          레시피 없이 돌리면 페이지는 가져오되 <strong>레코드는 나오지 않습니다</strong>. 구조를
          모르는 사이트라면 대화에서 먼저 레시피를 만드세요.
        </p>
      )}

      {error && (
        <p className="rounded-md bg-bad/10 px-3 py-2 text-sm text-bad">{error}</p>
      )}

      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
      >
        {busy ? "제출 중…" : "크롤 시작"}
      </button>
    </form>
  );
}
