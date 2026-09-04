/*
 * 설정: the model, the form defaults, and what this install is actually set to.
 *
 * All three existed only as CLI commands - `crwallm model`, and `crwallm
 * config` for the rest - which meant the screen could not tell you which model
 * it was about to use, let alone change it.
 *
 * Three things, three different lifetimes, and the screen says which is which:
 *
 *   모델      written to .env, so the worker and the CLI see it too
 *   기본값    this browser only; one person's habits, not configuration
 *   서버 설정  read-only, because an endpoint that could rewrite the API's own
 *            host and token is an endpoint that can lock you out of the window
 *            you are using to call it
 */

const settingsState = { pulling: null };

function gb(value) {
  return value ? `${Number(value).toFixed(1)} GB` : "";
}

/* --------------------------------------------------------------- 모델 */

async function loadModels() {
  const error = $("models-error");
  error.hidden = true;
  try {
    const info = await rest.get("/api/models");

    $("models-hardware").textContent = info.reachable
      ? `${info.hardware}  ·  쓰는 모델 ${info.chosen}  ·  임베딩 ${info.embed}`
      : "";
    if (!info.reachable) {
      error.hidden = false;
      error.textContent =
        `모델 서버(Ollama)가 ${info.ollama} 에서 응답하지 않습니다. ` +
        "터미널에서 crwallm setup --no-browser 로 준비하거나, 이미 설치했다면 실행 중인지 확인해주세요.";
    }
    $("chat-model").textContent = info.reachable ? `지금 쓰는 모델: ${info.chosen}` : "";

    renderInstalled(info);
    renderAvailable(info);
  } catch (err) {
    error.hidden = false;
    error.textContent = `모델 목록을 읽지 못했습니다: ${err.message || err}`;
  }
}

function headRow(table, names) {
  const head = table.querySelector("thead");
  head.replaceChildren();
  const tr = document.createElement("tr");
  for (const name of names) {
    const th = document.createElement("th");
    th.textContent = name;
    tr.append(th);
  }
  head.append(tr);
  return table.querySelector("tbody");
}

function renderInstalled(info) {
  const body = headRow($("models-table"), ["이름", "크기", "", ""]);
  body.replaceChildren();

  if (!info.installed.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "hint";
    td.textContent = info.reachable
      ? "설치된 모델이 없습니다. 아래에서 하나 받으세요."
      : "모델 서버가 꺼져 있어 확인할 수 없습니다.";
    tr.append(td);
    body.append(tr);
    return;
  }

  for (const model of info.installed) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.textContent = model.name;
    tr.append(name);

    const size = document.createElement("td");
    size.textContent = [gb(model.size_gb), model.quantization].filter(Boolean).join("  ");
    tr.append(size);

    const chosen = document.createElement("td");
    if (model.chosen) {
      const chip = document.createElement("span");
      chip.className = "chip chip-active";
      chip.textContent = "사용 중";
      chosen.append(chip);
    } else {
      const use = document.createElement("button");
      use.textContent = "이걸로";
      use.addEventListener("click", () => chooseModel(model.name));
      chosen.append(use);
    }
    tr.append(chosen);

    const actions = document.createElement("td");
    if (!model.chosen) {
      const drop = document.createElement("button");
      drop.textContent = "지우기";
      drop.addEventListener("click", () => removeModel(model.name, gb(model.size_gb)));
      actions.append(drop);
    }
    tr.append(actions);

    body.append(tr);
  }
}

function renderAvailable(info) {
  const body = headRow($("models-available"), ["이름", "크기", "설명", ""]);
  body.replaceChildren();

  // An empty table under a heading reads as broken. It usually means every
  // catalogued model is already here, which is a fine answer worth saying.
  if (!info.available.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "hint";
    td.textContent = info.reachable
      ? "목록에 있는 모델은 모두 설치되어 있습니다."
      : "모델 서버가 꺼져 있어 확인할 수 없습니다.";
    tr.append(td);
    body.append(tr);
    return;
  }

  for (const model of info.available) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.textContent = model.name;
    tr.append(name);

    const size = document.createElement("td");
    size.textContent = gb(model.size_gb);
    tr.append(size);

    const note = document.createElement("td");
    // A model that will not load on this machine is worth saying so about
    // before somebody spends twenty minutes downloading it.
    const parts = [model.note.split("\n")[0]];
    if (!model.fits) parts.push("(이 기기에는 큽니다)");
    if (model.name === info.recommended) parts.push("← 이 기기에 권장");
    note.textContent = parts.filter(Boolean).join("  ");
    note.className = model.fits ? "" : "hint";
    tr.append(note);

    const actions = document.createElement("td");
    const get = document.createElement("button");
    get.textContent = "받기";
    get.disabled = !info.reachable;
    get.addEventListener("click", () => pullModel(model.name));
    actions.append(get);
    tr.append(actions);

    body.append(tr);
  }
}

async function chooseModel(name) {
  try {
    const result = await rest.post("/api/models/use", { name });
    toast(result.note || `${name}을(를) 쓰도록 했습니다.`);
    loadModels();
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

async function removeModel(name, size) {
  // Deleting gigabytes is not undoable by clicking again, and the size is the
  // number that makes the question answerable.
  if (!window.confirm(`${name} (${size}) 을(를) 지웁니다. 다시 받으려면 그만큼 내려받아야 합니다.`)) {
    return;
  }
  try {
    await rest.del(`/api/models/${encodeURIComponent(name)}`);
    toast(`${name}을(를) 지웠습니다.`);
    loadModels();
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

async function pullModel(name) {
  if (settingsState.pulling) {
    toast("이미 받는 중입니다.", true);
    return;
  }
  settingsState.pulling = name;
  $("pull-progress").hidden = false;
  $("pull-label").textContent = `${name} 받는 중…`;
  $("pull-bar").style.width = "0%";

  try {
    for await (const frame of rest.stream("/api/models/pull", { name })) {
      if (frame.event === "progress") {
        const percent = frame.data.percent || 0;
        $("pull-bar").style.width = `${percent}%`;
        $("pull-label").textContent = `${name}  ${frame.data.status || ""}  ${percent}%`;
      } else if (frame.event === "failed") {
        throw new Error(frame.data.error || "받지 못했습니다.");
      }
    }
    $("pull-bar").style.width = "100%";
    $("pull-label").textContent = `${name} 완료`;
    toast(`${name}을(를) 받았습니다.`);
    loadModels();
  } catch (err) {
    $("pull-label").textContent = String(err.message || err);
    toast(String(err.message || err), true);
  } finally {
    settingsState.pulling = null;
  }
}

/* ------------------------------------------------------------- 기본값 */

const DEFAULT_FIELDS = {
  max_pages: "d-pages",
  max_depth: "d-depth",
  fetch_mode: "d-fetch",
  concurrency: "d-concurrency",
  per_host: "d-perhost",
  interval_ms: "d-interval",
};

function showDefaults(values) {
  for (const [key, id] of Object.entries(DEFAULT_FIELDS)) {
    $(id).value = values[key];
  }
}

function readDefaults() {
  return {
    max_pages: num($("d-pages"), FALLBACK.max_pages),
    max_depth: num($("d-depth"), FALLBACK.max_depth),
    fetch_mode: $("d-fetch").value,
    concurrency: num($("d-concurrency"), FALLBACK.concurrency),
    per_host: num($("d-perhost"), FALLBACK.per_host),
    interval_ms: num($("d-interval"), FALLBACK.interval_ms),
  };
}

/* --------------------------------------------------------- 서버 설정 */

const SETTING_LABEL = {
  env: "환경",
  api: "API 주소",
  api_token_set: "API 토큰",
  allowed_hosts: "허용 호스트",
  database: "데이터베이스",
  archive_dir: "원본 보관 폴더",
  recipes_dir: "레시피 폴더",
  ollama: "모델 서버",
  llm_model: "쓰는 모델",
  embed_model: "임베딩 모델",
};

async function loadSettings() {
  const body = $("settings-table").querySelector("tbody");
  body.replaceChildren();
  try {
    const info = await rest.get("/api/settings");

    // The engine's own defaults become the screen's, unless this browser has
    // been told otherwise. Two sets of numbers that can disagree is one set
    // too many.
    if (!localStorage.getItem("crwallm-defaults")) {
      const limits = info.limits || {};
      Object.assign(defaults, {
        max_pages: limits.max_pages ?? defaults.max_pages,
        max_depth: limits.max_depth ?? defaults.max_depth,
        concurrency: limits.global_concurrency ?? defaults.concurrency,
        per_host: limits.per_host_concurrency ?? defaults.per_host,
        interval_ms: limits.min_interval_ms ?? defaults.interval_ms,
      });
    }
    showDefaults(defaults);

    for (const [key, label] of Object.entries(SETTING_LABEL)) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = label;
      const value = document.createElement("td");
      const raw = info[key];
      value.textContent = Array.isArray(raw)
        ? raw.join(", ")
        : typeof raw === "boolean"
          ? raw
            ? "설정됨"
            : "없음"
          : String(raw ?? "");
      tr.append(name, value);
      body.append(tr);
    }
  } catch (err) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.className = "error";
    td.textContent = `설정을 읽지 못했습니다: ${err.message || err}`;
    tr.append(td);
    body.append(tr);
  }
}

async function loadSettingsView() {
  showDefaults(defaults);
  await Promise.all([loadSettings(), loadModels()]);
}

/* ---------------------------------------------------------- 준비 상태 */

/** A line in the header saying what is actually up.
 *
 * The tool degrades rather than fails - no database means no 작업 tab, no
 * Ollama means no 대화 - and "why is this empty" should be answerable without
 * opening a terminal.
 */
async function loadHealth() {
  const box = $("health");
  box.replaceChildren();
  box.hidden = false;

  const chips = [];
  try {
    const ready = await rest.get("/ready");
    chips.push(["데이터베이스", ready.database]);
  } catch {
    chips.push(["데이터베이스", false]);
  }
  try {
    const models = await rest.get("/api/models");
    chips.push(["모델", models.reachable]);
  } catch {
    chips.push(["모델", false]);
  }

  for (const [label, ok] of chips) {
    const chip = document.createElement("span");
    chip.className = `chip chip-${ok ? "completed" : "cancelled"}`;
    chip.textContent = `${label} ${ok ? "켜짐" : "꺼짐"}`;
    chip.title = ok ? "" : "이 기능이 필요한 탭은 비어 보입니다.";
    box.append(chip);
  }
}

/* ------------------------------------------------------------------ wiring */

$("models-refresh").addEventListener("click", loadModels);
$("defaults-save").addEventListener("click", () => {
  saveDefaults(readDefaults());
  $("defaults-saved").textContent = "저장했습니다. 다음에 화면을 열 때부터 적용됩니다.";
  setTimeout(() => ($("defaults-saved").textContent = ""), 4000);
});
$("defaults-reset").addEventListener("click", async () => {
  try {
    localStorage.removeItem("crwallm-defaults");
  } catch {
    /* nothing was stored */
  }
  Object.assign(defaults, FALLBACK);
  await loadSettings();
  $("defaults-saved").textContent = "서버 기본값으로 되돌렸습니다.";
  setTimeout(() => ($("defaults-saved").textContent = ""), 4000);
});
