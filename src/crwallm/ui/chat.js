/*
 * 대화: say what you want in a sentence.
 *
 * The model plans, looks at the page, builds a recipe, scores it and queues a
 * crawl. Each of those shows up as it happens rather than at the end, because
 * a turn takes a minute and a minute of nothing on screen is
 * indistinguishable from a hang - and what it decided, and why, is exactly
 * what waiting for the end throws away.
 *
 * The transcript lives here, in the page. A local single-user tool has no
 * session to hang a conversation off, and persisting one would mean deciding
 * when it expires.
 *
 * Needs Ollama. When it is not there the answer says so in a sentence rather
 * than a stack trace, because "the model is not running" is a thing a person
 * can fix and a ConnectError is not.
 */

const chatState = {
  history: [], // [{role, content}] - what the server is told about the past
  busy: false,
};

/* The four the agent can actually take (llm/agent.py's Action literal). The
 * first version listed adapt/test/activate/submit, which are CLI commands and
 * not tool names - so a real turn showed "make_recipe" in English among the
 * Korean labels. */
const ACTION_LABEL = {
  inspect: "페이지 살펴보기",
  make_recipe: "레시피 만들기",
  crawl: "크롤 큐에 넣기",
  answer: "답하기",
};

function chatLine(className) {
  const el = document.createElement("div");
  el.className = className;
  $("chat-log").append(el);
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  return el;
}

function saySelf(text) {
  const bubble = chatLine("bubble self");
  bubble.textContent = text;
}

function stepCard(action, detail) {
  const card = chatLine("step");
  const title = document.createElement("div");
  title.className = "step-title";
  title.textContent = ACTION_LABEL[action] || action;
  const body = document.createElement("div");
  body.className = "step-detail";
  body.textContent = detail || "";
  card.append(title, body);
  return card;
}

async function sendChat() {
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message || chatState.busy) return;

  chatState.busy = true;
  input.value = "";
  $("chat-error").hidden = true;
  $("chat-send").disabled = true;
  saySelf(message);

  // Taken off screen on the first send so it does not sit above the
  // conversation forever - removed, not destroyed, because 비우기 puts it back.
  if (CHAT_PLACEHOLDER && CHAT_PLACEHOLDER.isConnected) CHAT_PLACEHOLDER.remove();

  const open = new Map(); // action -> its card, so finishing updates in place
  let answered = false;

  try {
    for await (const frame of rest.stream("/api/chat", {
      message,
      history: chatState.history.slice(-20),
    })) {
      const event = frame.data || {};
      if (event.type === "thinking") {
        const think = chatLine("thinking");
        think.textContent = event.text;
      } else if (event.type === "action.started") {
        open.set(event.action, stepCard(event.action, event.detail));
      } else if (event.type === "action.finished") {
        const card = open.get(event.action) || stepCard(event.action, "");
        card.classList.add(event.ok ? "ok" : "bad");
        card.querySelector(".step-detail").textContent = event.summary || "";
        open.delete(event.action);
      } else if (event.type === "answer") {
        const bubble = chatLine("bubble other");
        bubble.textContent = event.text;
        chatState.history.push({ role: "user", content: message });
        chatState.history.push({ role: "assistant", content: event.text });
        answered = true;
      }
    }
    if (!answered) {
      $("chat-error").hidden = false;
      $("chat-error").textContent = "답이 끊겼습니다. 다시 시도해주세요.";
    }
  } catch (err) {
    $("chat-error").hidden = false;
    $("chat-error").textContent = modelError(String(err.message || err));
  } finally {
    chatState.busy = false;
    $("chat-send").disabled = false;
    input.focus();
    // A turn usually ends with a job queued; the list should already show it.
    if (HOST.full) loadJobs();
  }
}

/** The one failure worth translating: nothing is listening on Ollama's port. */
function modelError(text) {
  if (/connect|refused|11434|ollama/i.test(text)) {
    return (
      "모델 서버(Ollama)가 응답하지 않습니다. " +
      "터미널에서 crwallm setup --no-browser 로 준비하거나, 이미 설치했다면 실행 중인지 확인해주세요."
    );
  }
  return text;
}

$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});


/* Kept rather than rebuilt. Writing the sentence here as well would put the
 * same text in two files, and the copy in the script is the one that goes
 * stale - so the original node is parked and put back. */
const CHAT_PLACEHOLDER = $("chat-log").querySelector(".hint");

$("chat-clear").addEventListener("click", () => {
  // The transcript is the only state this view has, and it is also what the
  // server is told about the past - clearing the screen has to clear that too,
  // or the next turn carries a conversation nobody can see.
  chatState.history = [];
  const log = $("chat-log");
  log.replaceChildren();
  if (CHAT_PLACEHOLDER) log.append(CHAT_PLACEHOLDER);
  $("chat-error").hidden = true;
});
