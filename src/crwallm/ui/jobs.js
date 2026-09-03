/*
 * 작업: what the tool has done, and what it is doing right now.
 *
 * The honest front door. 모으기 holds its result in memory and forgets it when
 * the window closes; a job is the same crawl written down, so it survives, can
 * be stopped and retried, and can be looked at while it runs.
 *
 * Browser only. A job lives in Postgres and is executed by a worker, and the
 * desktop window has neither - which is why the tab is not offered there.
 *
 * The event feed is a real stream, not a poll. A crawl that takes a minute
 * with nothing on screen is indistinguishable from one that has hung, and the
 * interesting part is exactly what a "done" message throws away.
 */

/* db/models.py's JobStatus, not a guess at it. "succeeded" was the guess, and
 * a finished job showed the raw word "completed" beside Korean labels while
 * 중지/다시 실행 stayed enabled on a job that had already ended. */
const TERMINAL = ["completed", "failed", "cancelled"];

const STATUS_LABEL = {
  queued: "대기",
  running: "진행 중",
  completed: "완료",
  failed: "실패",
  cancelled: "중지됨",
};

const jobsState = {
  open: null, // the job id whose detail is showing
  stream: null, // AbortController for the live feed
  poll: null, // interval id for the list
};

/* ------------------------------------------------------------------ 목록 */

function jobStatusChip(status) {
  const chip = document.createElement("span");
  chip.className = `chip chip-${status}`;
  chip.textContent = STATUS_LABEL[status] || status;
  return chip;
}

function when(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const seconds = Math.round((Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return "방금";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간 전`;
  return then.toLocaleDateString("ko-KR");
}

async function loadJobs() {
  const empty = $("jobs-empty");
  try {
    const jobs = await rest.get("/api/jobs?limit=30");
    renderJobs(jobs);
    empty.hidden = jobs.length > 0;
    if (!jobs.length) empty.textContent = "아직 아무것도 없습니다. 위에서 주소를 넣어 시작해보세요.";

    // Poll only while something is moving. A finished list does not change on
    // its own, and a timer that never stops is a timer nobody notices.
    const active = jobs.some((j) => !TERMINAL.includes(j.status));
    clearInterval(jobsState.poll);
    jobsState.poll = active ? setInterval(loadJobs, 2000) : null;
  } catch (err) {
    clearInterval(jobsState.poll);
    jobsState.poll = null;
    empty.hidden = false;
    empty.textContent = `작업 목록을 읽지 못했습니다: ${err.message || err}`;
  }
}

function renderJobs(jobs) {
  const head = $("jobs-table").querySelector("thead");
  const body = $("jobs-table").querySelector("tbody");
  head.replaceChildren();
  body.replaceChildren();

  const headRow = document.createElement("tr");
  for (const name of ["상태", "쪽", "건", "실패", "시작", ""]) {
    const th = document.createElement("th");
    th.textContent = name;
    headRow.append(th);
  }
  head.append(headRow);

  for (const job of jobs) {
    const tr = document.createElement("tr");

    const status = document.createElement("td");
    status.append(jobStatusChip(job.status));
    tr.append(status);

    for (const value of [job.pages_crawled, job.records_extracted, job.pages_failed]) {
      const td = document.createElement("td");
      td.textContent = String(value ?? 0);
      tr.append(td);
    }

    const started = document.createElement("td");
    started.textContent = when(job.started_at || job.created_at);
    tr.append(started);

    const actions = document.createElement("td");
    const open = document.createElement("button");
    open.textContent = "열기";
    open.addEventListener("click", () => openJob(job.id));
    actions.append(open);
    tr.append(actions);

    body.append(tr);
  }
}

/* ------------------------------------------------------------------ 상세 */

async function openJob(id) {
  jobsState.open = id;
  $("job-detail").hidden = false;
  $("job-detail-title").textContent = `작업 ${id.slice(0, 8)}`;
  $("job-feed").replaceChildren();
  $("job-detail-error").hidden = true;

  await refreshJobDetail();
  followJob(id);
  $("job-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function refreshJobDetail() {
  const id = jobsState.open;
  if (!id) return;
  try {
    const job = await rest.get(`/api/jobs/${id}`);
    $("job-detail-title").replaceChildren(
      document.createTextNode(`작업 ${id.slice(0, 8)}  `),
      jobStatusChip(job.status),
    );
    $("job-detail-stats").textContent =
      `${job.pages_crawled}쪽 · ${job.records_extracted}건` +
      (job.pages_failed ? ` · 실패 ${job.pages_failed}` : "") +
      (job.attempts > 1 ? ` · 시도 ${job.attempts}회` : "");

    const failed = job.error_message || "";
    $("job-detail-error").hidden = !failed;
    $("job-detail-error").textContent = failed;

    const done = TERMINAL.includes(job.status);
    $("job-cancel").disabled = done;
    $("job-retry").disabled = !done || job.status === "completed";
    $("job-export").disabled = job.records_extracted === 0;
  } catch (err) {
    $("job-detail-error").hidden = false;
    $("job-detail-error").textContent = String(err.message || err);
  }
}

/* Every type the crawl emits, from schemas/events.py. Anything missing shows
 * its raw name - which is how "links.discovered" and "url.rejected" turned up
 * in a feed among Korean labels. */
const EVENT_LABEL = {
  "job.started": "시작",
  "job.completed": "완료",
  "job.failed": "실패",
  "job.cancelled": "중지",
  "page.fetched": "가져옴",
  "page.failed": "실패",
  "links.discovered": "링크 발견",
  "url.rejected": "범위 밖",
  "pattern.budget_exhausted": "예산 소진",
  "duplicate.detected": "중복",
  "records.extracted": "추출",
  "records.filtered": "걸러냄",
  progress: "진행",
};

function feedLine(type, payload) {
  const line = document.createElement("div");
  line.className = "feed-line";

  const tag = document.createElement("span");
  tag.className = "feed-tag";
  tag.textContent = EVENT_LABEL[type] || type;
  line.append(tag);

  const text = document.createElement("span");
  if (type === "records.extracted") {
    text.textContent = `${payload.count ?? (payload.records || []).length}건  ${payload.url || ""}`;
  } else if (type === "job.completed") {
    text.textContent = `${payload.pages_fetched}쪽 · ${payload.records_extracted}건 · ${payload.elapsed_s}초`;
  } else if (type === "progress") {
    text.textContent = `${payload.pages_done}쪽 · 대기 ${payload.pages_queued} · ${payload.records_total}건`;
  } else if (type === "links.discovered") {
    // found and enqueued, not count. The two differ by everything the gate
    // turned away, which is the number worth seeing.
    text.textContent = `${payload.enqueued}/${payload.found}개  ${payload.url || ""}`.trim();
  } else if (payload.url) {
    text.textContent =
      `${payload.status ?? payload.error_kind ?? payload.reason ?? ""} ${payload.url}`.trim();
  } else {
    text.textContent = payload.message || payload.reason || "";
  }
  line.append(text);
  return line;
}

/** Frames that end the stream rather than describing the crawl. */
const STREAM_OVER = new Set(["end", "timeout"]);

/** Follow one job's events until it ends or the view moves on. */
async function followJob(id) {
  if (jobsState.stream) jobsState.stream.abort();
  const controller = new AbortController();
  jobsState.stream = controller;

  const feed = $("job-feed");
  try {
    // Backfill first: a job opened after it finished has no stream left to
    // give, and an empty feed reads as "nothing happened".
    const past = await rest.get(`/api/jobs/${id}/events?limit=200`);
    let cursor = 0;
    for (const event of past) {
      feed.append(feedLine(event.event_type, event.payload));
      cursor = Math.max(cursor, event.id);
    }
    feed.scrollTop = feed.scrollHeight;

    // Resume from where the backfill stopped. Without `after` the stream
    // starts at row zero and re-sends everything just drawn - a finished job
    // opened cold showed its first two hundred events twice.
    for await (const frame of rest.stream(`/api/jobs/${id}/stream?after=${cursor}`)) {
      if (controller.signal.aborted || jobsState.open !== id) return;
      if (STREAM_OVER.has(frame.event)) break;

      // The frame is an envelope - {id, event_type, payload, created_at} - and
      // the crawl's own fields are one level down. Passing the envelope drew
      // every line with its label and nothing else.
      const event = frame.data || {};
      const type = event.event_type || frame.event;
      feed.append(feedLine(type, event.payload || {}));
      feed.scrollTop = feed.scrollHeight;

      if (type.startsWith("job.")) {
        await refreshJobDetail();
        loadJobs();
      }
    }
  } catch (err) {
    if (!controller.signal.aborted) {
      const line = document.createElement("div");
      line.className = "feed-line hint";
      line.textContent = `진행 상황을 더 받지 못했습니다: ${err.message || err}`;
      feed.append(line);
    }
  }
}

function closeJob() {
  jobsState.open = null;
  if (jobsState.stream) jobsState.stream.abort();
  jobsState.stream = null;
  $("job-detail").hidden = true;
}

/* ------------------------------------------------------------- 새 작업 */

async function submitJob() {
  const url = $("job-url").value.trim();
  const recipe = $("job-recipe").value.trim();
  const box = $("job-error");
  box.hidden = true;

  if (!url) {
    box.hidden = false;
    box.textContent = "주소를 입력해주세요.";
    return;
  }

  // The host, not the registrable domain. The CLI uses tldextract to widen
  // quotes.toscrape.com to toscrape.com; a browser has no public-suffix list
  // and guessing one gets .co.kr wrong, so this stays narrower than the CLI
  // rather than wrong. Leaving it out is not an option - CrawlSpec requires
  // it, and the form used to fail with "spec.allowed_domains: Field required".
  let host;
  try {
    host = new URL(/^https?:\/\//i.test(url) ? url : `https://${url}`).hostname;
  } catch {
    box.hidden = false;
    box.textContent = "주소 형식이 올바르지 않습니다.";
    return;
  }

  const spec = {
    seed_urls: [url],
    allowed_domains: [host],
    follow_links: $("job-follow").checked,
    limits: { max_pages: Number($("job-pages").value) || 50 },
  };
  if (recipe) spec.recipe = recipe;

  busy(true, "큐에 넣는 중…");
  try {
    const submitted = await rest.post("/api/jobs", { spec });
    $("job-url").value = "";
    await loadJobs();
    await openJob(submitted.id);
  } catch (err) {
    box.hidden = false;
    box.textContent = String(err.message || err);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------------------ wiring */

$("jobs-refresh").addEventListener("click", loadJobs);
$("job-close").addEventListener("click", closeJob);
$("job-new-toggle").addEventListener("click", () => {
  const panel = $("job-new");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) $("job-url").focus();
});
$("job-submit").addEventListener("click", submitJob);
$("job-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitJob();
});

$("job-cancel").addEventListener("click", async () => {
  try {
    await rest.post(`/api/jobs/${jobsState.open}/cancel`);
    toast("중지를 요청했습니다. 지금 보고 있는 쪽까지는 끝냅니다.");
    await refreshJobDetail();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});

$("job-retry").addEventListener("click", async () => {
  try {
    await rest.post(`/api/jobs/${jobsState.open}/retry`);
    toast("다시 큐에 넣었습니다.");
    await refreshJobDetail();
    loadJobs();
  } catch (err) {
    toast(String(err.message || err), true);
  }
});

$("job-export").addEventListener("click", () => {
  /* A plain navigation: the export is a GET, and letting the browser handle it
   * means the file streams straight to disk instead of through memory. */
  window.location.href = `/api/jobs/${jobsState.open}/export?format=csv`;
});
