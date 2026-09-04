/*
 * Two hosts, one client.
 *
 * The same page runs in the desktop window and in a browser. In the window
 * pywebview injects `window.pywebview.api`; in a browser there is a server on
 * the same origin. Everything above this file is written once and does not
 * know which one it got.
 *
 * The two are not equally capable and pretending otherwise would be a lie the
 * user finds out about by clicking. The window has no server, so it has no
 * job queue, no recipe files it can read and no model to talk to - `HOST.full`
 * is how the navigation knows which sections to offer.
 */

/** Presence, not protocol. `location.protocol === "file:"` describes how the
 * page was loaded, which is not the question being asked. */
const HOST = {
  get desktop() {
    return !!(window.pywebview && window.pywebview.api);
  },
  get served() {
    return window.CRWALLM_TOKEN !== undefined;
  },
  /** Whether anything beyond 모으기 can work here. */
  get full() {
    return !this.desktop && this.served;
  },
};

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

function headers(extra) {
  return { "X-CRWALLM-Token": window.CRWALLM_TOKEN || "", ...extra };
}

/** FastAPI puts the reason in `detail`, as a string or a list of validation
 * errors. Both say more than the status code. */
async function reason(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((e) => [e.loc?.slice(1).join("."), e.msg].filter(Boolean).join(": "))
        .join("; ");
    }
  } catch {
    /* not JSON; the status line stands */
  }
  return `서버가 응답하지 않습니다 (${response.status}).`;
}

async function send(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: headers(body === undefined ? {} : { "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await reason(response));
  return response.status === 204 ? null : response.json();
}

/** The general API. Browser only - there is no server behind the window. */
const rest = {
  get: (path) => send("GET", path),
  post: (path, body) => send("POST", path, body),
  del: (path) => send("DELETE", path),

  /** Server-sent events, as an async iterator of {event, data}.
   *
   * Hand-parsed rather than using EventSource, which can only issue GETs
   * without headers - and every interesting stream here is a POST that needs
   * the token. */
  async *stream(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: headers(body === undefined ? {} : { "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw new Error(await reason(response));

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line. A partial frame stays in the
      // buffer: a chunk boundary lands mid-frame often enough that parsing
      // whatever arrived would drop events at random.
      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        let name = "message";
        const lines = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          else if (line.startsWith("data:")) lines.push(line.slice(5).trim());
        }
        if (!lines.length) continue;
        try {
          yield { event: name, data: JSON.parse(lines.join("\n")) };
        } catch {
          yield { event: name, data: lines.join("\n") };
        }
      }
    }
  },
};

/* ------------------------------------------------------- the four verbs */

/* 모으기 works in both hosts, so it goes through whichever one is here. */

const httpQuick = {
  look: (url) => rest.post("/api/ui/look", { sid: SID, url }),
  collect: (url, picks, options) =>
    rest.post("/api/ui/collect", { sid: SID, url, picks, ...(options || {}) }),
  stop: () => rest.post("/api/ui/stop", { sid: SID }),
  /* A browser has no save dialog, so the file comes back as a download and
   * the browser asks where. Same verb, same result, different machinery. */
  async save() {
    const response = await fetch("/api/ui/save", {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ sid: SID }),
    });
    if (!response.ok) return { ok: false, error: await reason(response) };

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
    return { ok: true, rows: null };
  },
};

function api() {
  if (HOST.desktop) return window.pywebview.api;
  if (HOST.served) return httpQuick;
  throw new Error("아직 준비 중입니다. 잠시 후 다시 시도해주세요.");
}


/* ------------------------------------------------------------- 기본값 */

/*
 * What the forms start with.
 *
 * Per browser, not per server: these are one person's habits - how hard they
 * are willing to hit a site, how deep they usually go - and writing them into
 * .env would make one person's preference everybody's configuration. The
 * server's own defaults are the fallback, fetched once, so the two sets cannot
 * silently disagree.
 */

const DEFAULT_KEYS = ["max_pages", "max_depth", "fetch_mode", "concurrency", "per_host", "interval_ms"];

const FALLBACK = {
  max_pages: 50,
  max_depth: 3,
  fetch_mode: "http",
  concurrency: 32,
  per_host: 8,
  interval_ms: 0,
};

function loadDefaults() {
  try {
    const kept = JSON.parse(localStorage.getItem("crwallm-defaults") || "{}");
    const out = { ...FALLBACK };
    for (const key of DEFAULT_KEYS) {
      if (kept[key] !== undefined && kept[key] !== null) out[key] = kept[key];
    }
    return out;
  } catch {
    return { ...FALLBACK };
  }
}

function saveDefaults(values) {
  try {
    localStorage.setItem("crwallm-defaults", JSON.stringify(values));
  } catch {
    /* private window; the forms just start from the fallback each time */
  }
  Object.assign(defaults, values);
}

const defaults = loadDefaults();

/** Numbers arrive from inputs as strings, and an empty box is not a zero. */
function num(el, fallback) {
  const value = Number(el.value);
  return Number.isFinite(value) && el.value !== "" ? value : fallback;
}

/** One regex per line is how a person writes several. */
function lines(el) {
  return el.value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}
