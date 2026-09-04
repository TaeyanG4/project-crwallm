/*
 * 레시피: what this tool has learned about which sites.
 *
 * A recipe is the difference between understanding a site once and
 * understanding it every time. Making one can cost a model call; running it
 * never does, and that is the whole economics of the tool.
 *
 * Recipes are YAML files on disk, so this view needs no database - the only
 * screen besides 모으기 that works with Docker switched off.
 *
 * **Making** one is still `crwallm recipe adapt`: it asks a model to name the
 * columns, scores several candidates and takes minutes. **Proving** one is
 * here, because that is a single fetch and a score with no model in it - and
 * a screen that can list recipes but never check one turns 동작 확인됨 into a
 * word nobody can verify. 활성화 re-measures first for the same reason: the
 * numbers already in the file were true whenever they were taken.
 */

const recipesState = { open: null, busy: false };

/* The three the schema actually defines. Guessed names were shown as-is until
 * a real list came back reading "candidate", which tells a Korean-speaking
 * user nothing at all. */
const RECIPE_STATUS = {
  candidate: "확인 전",
  active: "동작 확인됨",
  deprecated: "물러남",
};

async function loadRecipes() {
  const empty = $("recipes-empty");
  try {
    const recipes = await rest.get("/api/recipes");
    renderRecipes(recipes);
    empty.hidden = recipes.length > 0;
    if (!recipes.length) {
      empty.textContent =
        "아직 레시피가 없습니다. 터미널에서 crwallm recipe adapt <이름> --url <주소> 로 만듭니다.";
    }
  } catch (err) {
    empty.hidden = false;
    empty.textContent = `레시피를 읽지 못했습니다: ${err.message || err}`;
  }
}

function renderRecipes(recipes) {
  const head = $("recipes-table").querySelector("thead");
  const body = $("recipes-table").querySelector("tbody");
  head.replaceChildren();
  body.replaceChildren();

  const headRow = document.createElement("tr");
  for (const name of ["이름", "상태", "어디서", "뽑은 건수", "채움", ""]) {
    const th = document.createElement("th");
    th.textContent = name;
    headRow.append(th);
  }
  head.append(headRow);

  for (const recipe of recipes) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.textContent = recipe.name;
    tr.append(name);

    const status = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `chip chip-${recipe.status}`;
    chip.textContent = RECIPE_STATUS[recipe.status] || recipe.status;
    status.append(chip);
    tr.append(status);

    const source = document.createElement("td");
    source.textContent = (recipe.allowed_domains || []).join(", ") || recipe.source;
    tr.append(source);

    const count = document.createElement("td");
    // 0 and "never measured" are different answers and the second one is the
    // one that explains why activate refused.
    count.textContent = recipe.measured_at == null ? "측정 안 함" : String(recipe.record_count);
    tr.append(count);

    const fill = document.createElement("td");
    // mean_fill is a computed average and comes back 0.0 when nothing was ever
    // measured, so it cannot say for itself whether it means anything. Only
    // measured_at can, and both columns have to agree - "측정 안 함" beside
    // "0%" reads as a recipe that found nothing rather than one never run.
    fill.textContent =
      recipe.measured_at == null ? "" : `${Math.round((recipe.mean_fill || 0) * 100)}%`;
    tr.append(fill);

    const actions = document.createElement("td");
    const open = document.createElement("button");
    open.textContent = "열기";
    open.addEventListener("click", () => openRecipe(recipe.name));
    actions.append(open);
    tr.append(actions);

    body.append(tr);
  }
}

async function openRecipe(name) {
  recipesState.open = name;
  $("recipe-detail").hidden = false;
  $("recipe-detail-title").textContent = name;
  $("recipe-report").hidden = true;
  $("recipe-detail-error").hidden = true;

  try {
    const recipe = await rest.get(`/api/recipes/${encodeURIComponent(name)}`);
    const bits = [
      `source: ${recipe.source}`,
      recipe.container ? `container: ${recipe.container}` : "",
      recipe.source_url ? `기준 페이지: ${recipe.source_url}` : "",
    ].filter(Boolean);
    $("recipe-detail-meta").textContent = bits.join("  ·  ");
    renderRecipeFields(recipe.fields || []);
  } catch (err) {
    $("recipe-detail-meta").textContent = String(err.message || err);
    $("recipe-fields").querySelector("tbody").replaceChildren();
  }
  $("recipe-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderRecipeFields(fields) {
  const head = $("recipe-fields").querySelector("thead");
  const body = $("recipe-fields").querySelector("tbody");
  head.replaceChildren();
  body.replaceChildren();

  const headRow = document.createElement("tr");
  for (const name of ["이름", "어디서", "종류", "다듬기"]) {
    const th = document.createElement("th");
    th.textContent = name;
    headRow.append(th);
  }
  head.append(headRow);

  for (const field of fields) {
    const tr = document.createElement("tr");
    for (const value of [
      field.name,
      field.selector,
      field.type || "text",
      (field.transform || []).join(", "),
    ]) {
      const td = document.createElement("td");
      td.textContent = value || "";
      td.title = td.textContent;
      tr.append(td);
    }
    body.append(tr);
  }
}

$("recipes-refresh").addEventListener("click", loadRecipes);
$("recipe-close").addEventListener("click", () => {
  recipesState.open = null;
  $("recipe-detail").hidden = true;
});


/* ------------------------------------------------------- 확인하고 활성화 */

/** Run the recipe against its sample page, or against that and then promote. */
async function proveRecipe(promote) {
  const name = recipesState.open;
  if (!name || recipesState.busy) return;

  recipesState.busy = true;
  $("recipe-detail-error").hidden = true;
  $("recipe-test").disabled = true;
  $("recipe-activate").disabled = true;
  busy(true, promote ? "다시 재보고 활성화하는 중…" : "지금도 되는지 재보는 중…");

  try {
    const verb = promote ? "activate" : "test";
    const report = await rest.post(`/api/recipes/${encodeURIComponent(name)}/${verb}`, {});
    renderReport(report, promote);
    if (promote) {
      toast(`${name}을(를) 활성화했습니다.`);
      loadRecipes();
    }
  } catch (err) {
    // A refusal is the interesting answer here, not an accident: it means the
    // recipe did not earn the promotion, and the report above already says
    // which number fell short.
    $("recipe-detail-error").hidden = false;
    $("recipe-detail-error").textContent = String(err.message || err);
  } finally {
    recipesState.busy = false;
    $("recipe-test").disabled = false;
    $("recipe-activate").disabled = false;
    busy(false);
  }
}

function renderReport(report, promoted) {
  const box = $("recipe-report");
  box.hidden = false;
  box.className = `report ${report.passes ? "ok" : "bad"}`;
  box.replaceChildren();

  const headline = document.createElement("strong");
  headline.textContent = report.passes
    ? promoted
      ? "활성화했습니다."
      : "지금도 잘 됩니다."
    : "기준에 못 미칩니다.";
  box.append(headline);

  const list = document.createElement("dl");
  const rows = [
    ["뽑은 건수", String(report.record_count)],
    ["채움", `${Math.round((report.mean_fill || 0) * 100)}%`],
    ["일관성", `${Math.round((report.consistency || 0) * 100)}%`],
    ["점수", String(report.score)],
    ["기준 페이지", report.sample_url || ""],
  ];
  if (!report.container_matched) {
    rows.unshift(["반복 단위", "찾지 못했습니다 — 사이트가 바뀌었을 수 있습니다"]);
  }
  if (report.filtered_out) {
    rows.push(["필터로 걸러짐", `${report.filtered_out}건`]);
  }
  // Per-field fill is the number that says *which* selector went stale, which
  // is the whole reason to press the button after a site is restyled.
  for (const [field, rate] of Object.entries(report.fill_rates || {})) {
    rows.push([`　${field}`, `${Math.round(rate * 100)}%`]);
  }

  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  box.append(list);
}

$("recipe-test").addEventListener("click", () => proveRecipe(false));
$("recipe-activate").addEventListener("click", () => proveRecipe(true));
