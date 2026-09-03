/*
 * Which screens this host can actually offer, and switching between them.
 *
 * The window has no server behind it, so it has no job queue, no recipe files
 * it can read and no model to talk to. Showing those tabs there and failing on
 * click would be a worse answer than not showing them: the person cannot tell
 * a missing feature from a broken one.
 *
 * Loaded last, so every view's own script has already registered its handlers.
 */

const VIEWS = ["collect", "jobs", "recipes", "chat"];

/** What each view needs before it has anything to show. */
const ON_ENTER = {
  jobs: () => loadJobs(),
  recipes: () => loadRecipes(),
  chat: () => $("chat-input").focus(),
  collect: () => $("url").focus(),
};

const SUBTITLE = {
  collect: "주소를 붙여넣으면 그 페이지에 무엇이 있는지 보여드립니다.",
  jobs: "큐에 넣어 두고 다른 일을 하세요. 끝난 뒤에도 결과가 남습니다.",
  recipes: "한 번 이해한 사이트는 모델 없이 계속 모읍니다.",
  chat: "문장으로 시키면 살펴보고, 레시피를 만들고, 크롤을 걸어둡니다.",
};

function showView(name) {
  for (const view of VIEWS) {
    $(`view-${view}`).hidden = view !== name;
  }
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("on", tab.dataset.view === name);
  }
  $("subtitle").textContent = SUBTITLE[name] || "";

  try {
    sessionStorage.setItem("crwallm-view", name);
  } catch {
    /* private window; the tab just will not be remembered */
  }
  ON_ENTER[name]?.();
}

function setUpNav() {
  // 모으기 is the only view that works with nothing running, so in the window
  // it is the whole app and the navigation is noise.
  if (!HOST.full) {
    showView("collect");
    return;
  }

  $("nav").hidden = false;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showView(tab.dataset.view));
  }

  let start = "collect";
  try {
    const kept = sessionStorage.getItem("crwallm-view");
    if (kept && VIEWS.includes(kept)) start = kept;
  } catch {
    /* nothing remembered */
  }
  showView(start);
}

/* In a browser the token is in the document, so the host is known at parse
 * time. In the window pywebview injects its API after load, and asking before
 * that arrives would hide the tabs on a host that has them - hence the event
 * as well as the immediate call. */
setUpNav();
window.addEventListener("pywebviewready", setUpNav);
