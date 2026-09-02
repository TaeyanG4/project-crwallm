"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { RecipeStatusChip } from "@/components/StatusChip";
import type { RecipeSummary } from "@/lib/types";

export default function RecipesPage() {
  const [recipes, setRecipes] = useState<RecipeSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .listRecipes()
      .then(setRecipes)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoaded(true));
  }, []);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-6">
        <div>
          <h1 className="text-lg font-semibold">레시피</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            무엇을 어떻게 뽑을지에 대한 선언. 만드는 것은 대화나{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">crwallm recipe adapt</code>{" "}
            에서 하고, 여기서는 무엇이 있고 얼마나 잘 동작하는지를 봅니다.
          </p>
        </div>

        {error && <p className="rounded-md bg-bad/10 px-3 py-2 text-sm text-bad">{error}</p>}

        {loaded && recipes.length === 0 && !error && (
          <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            레시피가 없습니다.
          </p>
        )}

        <div className="space-y-3">
          {recipes.map((recipe) => (
            <article key={recipe.name} className="space-y-2 rounded-lg border p-4">
              <div className="flex items-center gap-2">
                <h2 className="font-medium">{recipe.name}</h2>
                <span className="text-xs text-muted-foreground">v{recipe.version}</span>
                <RecipeStatusChip status={recipe.status} />

                {/* The evidence, next to the claim. "active" alone is an
                    assertion; a stale or thin measurement should be as
                    visible as the status that rests on it. */}
                <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                  {recipe.record_count} 레코드 · fill{" "}
                  {(recipe.mean_fill * 100).toFixed(0)}%
                </span>
              </div>

              <div className="flex flex-wrap gap-1.5">
                {recipe.field_names.map((name) => (
                  <span
                    key={name}
                    className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                  >
                    {name}
                  </span>
                ))}
              </div>

              <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[6rem_1fr]">
                <dt className="text-muted-foreground">컨테이너</dt>
                <dd className="font-mono">{recipe.container ?? "— (페이지당 1건)"}</dd>
                <dt className="text-muted-foreground">동작 도메인</dt>
                <dd className="font-mono">{recipe.allowed_domains.join(", ") || "제한 없음"}</dd>
                <dt className="text-muted-foreground">샘플</dt>
                <dd className="truncate">
                  <a
                    href={recipe.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-running hover:underline"
                  >
                    {recipe.source_url}
                  </a>
                </dd>
                <dt className="text-muted-foreground">측정</dt>
                <dd>
                  {recipe.measured_at
                    ? new Date(recipe.measured_at).toLocaleString()
                    : "측정된 적 없음"}
                </dd>
              </dl>
            </article>
          ))}
        </div>
      </div>
    </div>
  );
}
