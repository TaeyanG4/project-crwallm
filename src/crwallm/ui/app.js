/*
 * Three steps, one at a time.
 *
 * Everything the person does maps to one call on the Python side: look,
 * collect, save, stop. There is no client-side model of a crawl, no recipe
 * object, no job. What is on screen is what exists.
 *
 * Errors are shown where the mistake was made - a bad address under the
 * address box, not in a corner - because that is where someone is looking
 * when it goes wrong.
 */

const $ = (id) => document.getElementById(id);

const state = {
  url: "",
  columns: [],
  rows: [],
  total: 0,
};

/* Python calls this to report progress. */
window.crwallm = {
  on(event, payload) {
    if (event !== "progress") return;
    const bits = [`${payload.pages}쪽`];
    if (payload.rows) bits.push(`${payload.rows}건`);
    if (payload.failed) bits.push(`실패 ${payload.failed}`);
    $("busy-detail").textContent = bits.join(" · ");
  },
};

/* --------------------------------------------------------------- helpers */

/* One step is on screen at a time. The address stays visible while picking,
 * because that is still the same question; the result replaces both, because
 * by then it is a different one. */
const STEPS = {
  url: ["step-url"],
  pick: ["step-url", "step-pick"],
  result: ["step-result"],
};

function show(step) {
  const visible = STEPS[step];
  for (const id of ["step-url", "step-pick", "step-result"]) {
    $(id).hidden = !visible.includes(id);
  }
}

function busy(on, text) {
  $("busy").hidden = !on;
  if (text) $("busy-text").textContent = text;
  if (on) $("busy-detail").textContent = "";
}

let toastTimer = null;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4000);
}

/* ------------------------------------------------------------- 두 개의 집 */

/*
 * The same page runs in the desktop window and in a browser. In the window
 * pywebview injects `window.pywebview.api`; in a browser there is a server on
 * the same origin. Both answer the same four verbs, so everything below this
 * point is written once and does not know which one it got.
 *
 * The desktop is checked first and by presence, not by protocol. Guessing
 * from `location.protocol === "file:"` describes how the page was loaded,
 * which is not the question being asked.
 */

/** One browser tab's worth of work, so two tabs do not overwrite each other. */
const SID = (() => {
  try {
    const kept = sessionStorage.getItem("crwallm-sid");
    if (kept) return kept;
    const made = Math.random().toString(36).slice(2) + Date.now().toString(36);
    sessionStorage.setItem("crwallm-sid", made);
    return made;
  } catch {
    return Math.random().toString(36).slice(2);
  }
})();

async function post(path, body) {
  const response = await fetch(`/api/ui/${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CRWALLM-Token": window.CRWALLM_TOKEN || "",
    },
    body: JSON.stringify({ sid: SID, ...body }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error((detail && detail.detail) || `서버가 응답하지 않습니다 (${response.status}).`);
  }
  return response.json();
}

const http = {
  look: (url) => post("look", { url }),
  collect: (url, picks, options) =>
    post("collect", { url, picks, max_pages: (options && options.max_pages) || 1 }),
  stop: () => post("stop", {}),
  /* A browser has no save dialog, so the file comes back as a download and
   * the browser asks where. Same verb, same result, different machinery. */
  async save() {
    const response = await fetch("/api/ui/save", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CRWALLM-Token": window.CRWALLM_TOKEN || "",
      },
      body: JSON.stringify({ sid: SID }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      return { ok: false, error: (detail && detail.detail) || "저장하지 못했습니다." };
    }
    const blob = await response.blob();
    const name =
      /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(
        response.headers.get("content-disposition") || "",
      )?.[1] || "crwallm.csv";
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = decodeURIComponent(name);
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
    return { ok: true, rows: state.total };
  },
};

function api() {
  if (window.pywebview && window.pywebview.api) return window.pywebview.api;
  if (window.CRWALLM_TOKEN !== undefined) return http;
  throw new Error("아직 준비 중입니다. 잠시 후 다시 시도해주세요.");
}

/* ------------------------------------------------------------- ① 살펴보기 */

async function look() {
  const url = $("url").value.trim();
  $("url-error").hidden = true;
  if (!url) {
    fail("주소를 입력해주세요.");
    return;
  }

  busy(true, "페이지를 살펴보는 중…");
  try {
    const found = await api().look(url);
    if (!found.ok) {
      fail(found.error);
      return;
    }
    state.url = found.url;
    state.columns = found.columns;
    renderPicker(found);
    show("pick");
  } catch (err) {
    fail(String(err.message || err));
  } finally {
    busy(false);
  }
}

function fail(message) {
  const box = $("url-error");
  box.textContent = message;
  box.hidden = false;
  show("url");
}

const KIND_LABEL = { text: "글", href: "링크", src: "이미지" };

function renderPicker(found) {
  $("found").textContent = found.count
    ? `이 페이지에 ${found.count}개가 반복되고 있어요.`
    : "반복되는 목록을 찾지 못했어요.";

  if (found.hint) {
    $("found").textContent = found.hint;
  }

  const body = $("columns").querySelector("tbody");
  body.replaceChildren();

  for (const column of found.columns) {
    const row = document.createElement("tr");

    const left = document.createElement("td");
    const kind = document.createElement("span");
    kind.className = "sample-kind";
    kind.textContent = KIND_LABEL[column.kind] || column.kind;
    left.append(kind);

    for (const sample of column.samples.slice(0, 2)) {
      const line = document.createElement("span");
      line.className = "sample";
      line.textContent = sample;
      line.title = sample;
      left.append(line);
    }

    const right = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "예: 제목";
    input.value = column.suggested || "";
    input.dataset.index = String(column.index);
    // Enter on the last box is the same as pressing the button.
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") collect();
    });
    right.append(input);

    row.append(left, right);
    body.append(row);
  }

  $("collect").disabled = found.columns.length === 0;
}

/* ---------------------------------------------------------------- ② 모으기 */

async function collect() {
  const picks = [...$("columns").querySelectorAll("input[type=text]")]
    .map((input) => ({ index: Number(input.dataset.index), name: input.value.trim() }))
    .filter((pick) => pick.name);

  if (picks.length === 0) {
    // Reachable only by clearing every box, since they all arrive filled.
    toast("이름을 전부 지우셨어요. 모을 것을 하나는 남겨주세요.", true);
    return;
  }

  const many = $("more-pages").checked;
  busy(true, many ? "여러 쪽을 모으는 중…" : "모으는 중…");

  try {
    const out = await api().collect(state.url, picks, { max_pages: many ? 50 : 1 });
    if (!out.ok) {
      toast(out.error, true);
      return;
    }
    state.rows = out.rows;
    state.total = out.total;
    renderResults(out);
    show("result");
  } catch (err) {
    toast(String(err.message || err), true);
  } finally {
    busy(false);
  }
}

function renderResults(out) {
  const summary = out.cancelled
    ? `중지했습니다 — ${out.total}건까지 모았어요.`
    : `${out.total}건을 모았어요.`;
  $("result-summary").textContent = summary;

  $("result-hint").hidden = !out.hint;
  if (out.hint) $("result-hint").textContent = out.hint;

  $("truncated").hidden = out.total <= out.shown;
  if (out.total > out.shown) {
    $("truncated").textContent =
      `화면에는 ${out.shown}건만 보여드립니다. 저장하면 ${out.total}건 전부 들어갑니다.`;
  }

  const columns = [];
  for (const row of out.rows) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) columns.push(key);
    }
  }

  const head = $("results").querySelector("thead");
  const body = $("results").querySelector("tbody");
  head.replaceChildren();
  body.replaceChildren();

  const headRow = document.createElement("tr");
  for (const name of columns) {
    const th = document.createElement("th");
    th.textContent = name;
    headRow.append(th);
  }
  head.append(headRow);

  for (const row of out.rows) {
    const tr = document.createElement("tr");
    for (const name of columns) {
      const td = document.createElement("td");
      const value = row[name];
      td.textContent = value === null || value === undefined ? "" : String(value);
      td.title = td.textContent;
      tr.append(td);
    }
    body.append(tr);
  }

  $("save-csv").disabled = out.total === 0;
}

/* ----------------------------------------------------------------- ③ 저장 */

async function save() {
  try {
    const result = await api().save("csv");
    if (result.cancelled) return;
    if (!result.ok) {
      toast(result.error, true);
      return;
    }
    toast(`${result.rows}건을 저장했습니다.`);
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

/* ------------------------------------------------------------------ wiring */

$("look").addEventListener("click", look);
$("url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") look();
});
$("collect").addEventListener("click", collect);
$("save-csv").addEventListener("click", save);
$("again").addEventListener("click", () => {
  show("url");
  $("url").focus();
  $("url").select();
});
$("stop").addEventListener("click", async () => {
  $("busy-text").textContent = "멈추는 중…";
  try {
    await api().stop();
  } catch {
    /* the window is closing; nothing to report */
  }
});

window.addEventListener("pywebviewready", () => $("url").focus());
