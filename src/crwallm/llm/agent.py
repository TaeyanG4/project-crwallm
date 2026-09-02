"""The conversational loop: a model that drives the crawler.

This is the point of the project - "LLM만으로 크롤링이 쉽고 간편하게" - and
it is built on ``generate_structured`` rather than on a vendor's tool-calling
API, for the reason the gateway exists at all (docs/08_LLM_ARCHITECTURE.md).

**Constrained decoding, not hoping.** A tool-calling API asks the model to
emit well-formed JSON and trusts it. A 9B model asked to do that will
eventually produce ``{"url": "https://..."`` and the loop has to decide what
that meant. Here the schema is the grammar: the model picks one of a closed
set of actions and fills in fields that are typed, so a malformed call is not
a failure mode that exists. It also means any model works, including ones with
no tool-calling support at all.

**One action at a time.** The model does not plan a sequence. It sees what has
happened so far and chooses the next step, which is what lets it react to a
page that turned out to be empty or a recipe that scored badly - the thing a
pre-planned sequence cannot do.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from crwallm.llm.gateway import GatewayError, GenerationOptions, ModelGateway, TaskKind

__all__ = [
    "ActionFinished",
    "ActionStarted",
    "AgentAction",
    "AgentDeps",
    "AgentEvent",
    "Answer",
    "Thinking",
    "Turn",
    "run_agent",
]

MAX_STEPS = 8
"""How many actions before the loop gives up and answers with what it has.

Not a safety valve so much as an honesty one: a model that has inspected the
same page four times is stuck, and saying so beats looping until a timeout."""


# ------------------------------------------------------------------ actions


class AgentAction(BaseModel):
    """One step, chosen from a closed set.

    A flat schema with optional fields rather than a discriminated union:
    small models handle "fill in the fields that apply" markedly better than
    they handle nested variants, and the validation below recovers the same
    guarantees.
    """

    reasoning: str = Field(
        max_length=300,
        description="One short sentence on why this step, shown to the user.",
    )

    action: Literal["inspect", "make_recipe", "crawl", "answer"]

    url: str | None = Field(
        default=None, description="For inspect and make_recipe: the page to look at."
    )
    recipe_name: str | None = Field(
        default=None,
        description="For make_recipe: a short lowercase name. For crawl: which recipe to use.",
    )
    seed_urls: list[str] | None = Field(default=None, description="For crawl: where to start.")
    max_pages: int | None = Field(default=None, ge=1, le=5000)
    max_depth: int | None = Field(default=None, ge=0, le=10)
    spider: bool | None = Field(
        default=None, description="For crawl: walk the whole site rather than the given pages."
    )
    message: str | None = Field(default=None, description="For answer: what to tell the user.")

    def requirements_met(self) -> str | None:
        """Whatever the schema cannot express. Returns a complaint, or None."""
        match self.action:
            case "inspect" | "make_recipe":
                if not self.url:
                    return f"{self.action} needs a url"
                if self.action == "make_recipe" and not self.recipe_name:
                    return "make_recipe needs a recipe_name"
            case "crawl":
                if not self.seed_urls:
                    return "crawl needs seed_urls"
            case "answer":
                if not self.message:
                    return "answer needs a message"
        return None


# ------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class Thinking:
    """The model's one-line rationale, before the work starts."""

    text: str
    type: str = "thinking"


@dataclass(frozen=True, slots=True)
class ActionStarted:
    action: str
    detail: str
    type: str = "action.started"


@dataclass(frozen=True, slots=True)
class ActionFinished:
    action: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    type: str = "action.finished"


@dataclass(frozen=True, slots=True)
class Answer:
    """The end of a turn. Always the last event."""

    text: str
    type: str = "answer"


AgentEvent = Thinking | ActionStarted | ActionFinished | Answer


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange, as the transcript remembers it."""

    role: Literal["user", "assistant"]
    content: str


# --------------------------------------------------------------------- deps


@dataclass(slots=True)
class AgentDeps:
    """What the agent is allowed to do.

    Injected rather than imported so the tools can be swapped for a test's -
    and so the set of things a conversation can trigger is one visible list
    rather than whatever the module happened to import.
    """

    inspect: Callable[[str], Awaitable[dict[str, Any]]]
    make_recipe: Callable[[str, str], Awaitable[dict[str, Any]]]
    crawl: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    list_recipes: Callable[[], Awaitable[list[dict[str, Any]]]]


# ------------------------------------------------------------------ prompts

_SYSTEM = """You drive a web crawler. The user describes what data they want; \
you get it for them.

How the crawler works:
- A RECIPE says how to extract from one kind of page: a container selector and \
named fields. Recipes are made by looking at a real page.
- A CRAWL fetches pages and applies a recipe. Without a recipe it fetches \
pages and extracts nothing.

So the order is almost always: inspect a sample page, make a recipe from it, \
then crawl with that recipe.

Pick ONE action per step. You will see the result and pick again.
- inspect: fetch one page and see what repeats on it. Do this first when you \
do not know the site.
- make_recipe: build and score an extraction recipe from a page. Needs a short \
lowercase name like "products" or "news".
- crawl: run it. Give seed_urls; give recipe_name if a recipe exists.
- answer: reply to the user. Use this when the work is done, when you need \
something from them, or when you cannot proceed.

Be brief. The user sees your reasoning line for each step."""


def _describe_state(history: list[Turn], transcript: list[str]) -> str:
    lines: list[str] = []

    if history:
        lines.append("Conversation so far:")
        for turn in history[-6:]:
            lines.append(f"  {turn.role}: {turn.content[:400]}")
        lines.append("")

    if transcript:
        lines.append("What you have done this turn:")
        lines.extend(f"  {entry}" for entry in transcript)
        lines.append("")
        lines.append("Choose the next action, or answer if you are done.")
    else:
        lines.append("Choose the first action.")

    return "\n".join(lines)


def _first_url(text: str) -> str | None:
    """The URL in the user's message, if there is one.

    Seeds ``last_url`` so the very first action has something to fall back on
    when the model omits it."""
    match = re.search(r"https?://[^\s,\)\]}>\"']+", text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------- run


async def run_agent(
    gateway: ModelGateway,
    deps: AgentDeps,
    *,
    message: str,
    history: list[Turn] | None = None,
    options: GenerationOptions | None = None,
    max_steps: int = MAX_STEPS,
) -> AsyncIterator[AgentEvent]:
    """One turn of the conversation, as a stream of events.

    An async generator for the same reason the crawl engine is one: the caller
    decides what to do with each event as it happens, and a turn that takes
    forty seconds should not be forty seconds of nothing.
    """
    turns = list(history or [])
    turns.append(Turn(role="user", content=message))
    transcript: list[str] = []
    last_recipe: str | None = None
    last_url: str | None = _first_url(message)

    for _step in range(max_steps):
        prompt = _describe_state(turns, transcript)

        try:
            proposals = await gateway.generate_structured(
                task=TaskKind.PLAN,
                prompt=prompt,
                schema=AgentAction,
                system=_SYSTEM,
                options=options,
                n=1,
            )
        except GatewayError as exc:
            yield Answer(text=f"모델을 부르지 못했습니다: {exc}")
            return

        action = proposals[0].value

        # Two omissions the model makes constantly, filled in from what it
        # has already done rather than left to the prompt. Both were found by
        # running it: `make_recipe` without a url was proposed five times in a
        # row, silently burning the step budget, and a crawl without the
        # recipe just built is the worst kind of failure - it succeeds and
        # produces nothing. Each fill-in is recorded in the transcript, so the
        # model sees what it actually ran.
        if action.action in {"inspect", "make_recipe"} and not action.url and last_url:
            action = action.model_copy(update={"url": last_url})
            transcript.append(f"(no url given; used {last_url})")

        if action.action == "crawl" and not action.recipe_name and last_recipe is not None:
            action = action.model_copy(update={"recipe_name": last_recipe})
            transcript.append(f"(used the recipe just built: {last_recipe})")

        if action.action == "crawl" and not action.seed_urls and last_url:
            action = action.model_copy(update={"seed_urls": [last_url]})
            transcript.append(f"(no seeds given; used {last_url})")

        complaint = action.requirements_met()
        if complaint is not None:
            # Tell the model what it got wrong and let it try again. Visible,
            # because a step spent on a malformed action is a step the user
            # waited through, and five of them in a row is the difference
            # between a slow turn and a broken one.
            yield Thinking(text=f"({complaint} — 다시 시도)")
            transcript.append(f"(rejected: {complaint})")
            continue

        yield Thinking(text=action.reasoning)

        if action.action == "answer":
            yield Answer(text=action.message or "")
            return

        started = time.perf_counter()
        yield ActionStarted(action=action.action, detail=_detail_of(action))

        try:
            result = await _execute(deps, action)
            ok = True
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False

        elapsed = time.perf_counter() - started
        if action.action == "make_recipe" and ok and result.get("ok"):
            last_recipe = str(result.get("name") or "") or None
        if action.url:
            last_url = action.url

        summary = _summarise(action.action, result, ok)
        yield ActionFinished(action=action.action, ok=ok, summary=summary, data=result)
        transcript.append(f"{action.action}({_detail_of(action)}) -> {summary} [{elapsed:.1f}s]")

    # Out of steps. What was actually accomplished still has to be reported -
    # a turn that queued a crawl and then said "I could not finish" sends the
    # user looking for a problem that is not there.
    done = [line for line in transcript if not line.startswith("(")]
    if done:
        yield Answer(text="여기까지 했습니다:\n" + "\n".join(f"- {line}" for line in done))
    else:
        yield Answer(
            text=(
                f"{max_steps}단계를 썼는데 아무것도 하지 못했습니다. "
                "무엇을 원하시는지 더 구체적으로 알려주시면 다시 해보겠습니다."
            )
        )


def _detail_of(action: AgentAction) -> str:
    if action.action in {"inspect", "make_recipe"}:
        return action.url or ""
    if action.action == "crawl":
        return ", ".join(action.seed_urls or [])
    return ""


async def _execute(deps: AgentDeps, action: AgentAction) -> dict[str, Any]:
    match action.action:
        case "inspect":
            return await deps.inspect(action.url or "")
        case "make_recipe":
            return await deps.make_recipe(action.url or "", action.recipe_name or "")
        case "crawl":
            return await deps.crawl(
                {
                    "seed_urls": action.seed_urls or [],
                    "recipe": action.recipe_name,
                    "max_pages": action.max_pages or 50,
                    "max_depth": action.max_depth if action.max_depth is not None else 2,
                    "spider": bool(action.spider),
                }
            )
        case _:
            raise ValueError(f"not an executable action: {action.action}")


def _summarise(action: str, result: dict[str, Any], ok: bool) -> str:
    """One line, for the model's next prompt and for the user's screen.

    The model reads this to decide what to do next, so it has to carry the
    facts that change the decision - how many columns were found, whether the
    recipe scored well - and nothing else.
    """
    if not ok:
        return str(result.get("error", "failed"))

    match action:
        case "inspect":
            declared = result.get("declared") or {}
            # Declared data first: it outranks anything read off the layout,
            # and a recipe written against it survives a restyle.
            parts = []
            if declared.get("jsonld_types"):
                parts.append(
                    f"declares JSON-LD {declared['jsonld_types']} "
                    f"with paths {declared.get('jsonld_paths', [])}"
                )
            if declared.get("embedded_scripts"):
                parts.append(f"has embedded JSON in {declared['embedded_scripts']}")
            if declared.get("is_video_page"):
                parts.append("is a video page")

            columns = result.get("columns") or []
            if not result.get("container"):
                parts.append("no repeated structure - this looks like a detail page")
            else:
                names = [
                    f"{c.get('selector')} ({c.get('kind')})"
                    for c in columns[:8]
                    if isinstance(c, dict)
                ]
                parts.append(
                    f"{result.get('records', 0)} repeating items, "
                    f"container {result.get('container')}, "
                    f"columns: {', '.join(names) or 'none'}"
                )
            return "; ".join(parts)
        case "make_recipe":
            if not result.get("ok"):
                return str(result.get("reason", "could not build a recipe"))
            return (
                f"recipe '{result.get('name')}' scored {result.get('score')} "
                f"({result.get('records')} records, "
                f"{result.get('fill')} fill, fields: {', '.join(result.get('fields', []))}). "
                f"Pass recipe_name='{result.get('name')}' to crawl."
            )
        case "crawl":
            recipe = result.get("recipe")
            return (
                f"job {str(result.get('job_id', ''))[:8]} queued"
                + (
                    f" with recipe '{recipe}'"
                    if recipe
                    else " WITHOUT a recipe - it will extract nothing"
                )
                + ". The work is done; answer the user."
            )
        case _:
            return "done"
