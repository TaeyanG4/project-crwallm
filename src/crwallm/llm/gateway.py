"""ModelGateway - the contract, and why it is shaped this way.

The application never asks for "a completion". It asks for a *validated
object*: a CrawlSpec, a set of field rules, a classification. That framing is
the whole design (docs/08_LLM_ARCHITECTURE.md).

**The gateway is a candidate producer, not an authority.** Whatever comes back
goes through the same Pydantic and Policy gates as something typed by hand.
A model that emits ``max_pages: 10_000_000`` or ``seed_urls:
["file:///etc/passwd"]`` is refused by the schema, not by prompt discipline.

**One client covers local and cloud.** Ollama speaks the OpenAI chat API at
``/v1``, so the same implementation serves Ollama, vLLM, LM Studio, OpenAI and
anything else that copied the shape. Only Anthropic needs its own path, and
that is because it does structured output through tool use.

**Structured output is constrained, not requested.** Ollama's ``format`` takes
a JSON schema and constrains decoding, which makes invalid JSON impossible
rather than unlikely. Asking politely in a prompt and parsing the result is
how a whole class of failures gets invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

__all__ = [
    "GatewayError",
    "GatewayHealth",
    "ModelGateway",
    "ModelUnavailableError",
    "StructuredResult",
    "TaskKind",
    "Usage",
]

T = TypeVar("T", bound=BaseModel)


class TaskKind(StrEnum):
    """What the model is being asked to do.

    Separated because the difficulty is not remotely uniform, and the routing
    table is what lets a small local model do the easy work while the hard
    work goes somewhere else (docs/08_LLM_ARCHITECTURE.md).
    """

    PLAN = "plan"
    """Choose the next action in a conversation.

    A closed set of verbs and a handful of typed fields, so the schema is
    small - but the model has to follow a procedure (look at the page before
    writing a recipe, write the recipe before crawling with it) and notice
    when a step did not work. Harder than COMPILE_SPEC, easier than
    ADAPT_SELECTORS, and the one the user is waiting on, so latency counts
    for more here than anywhere else."""

    COMPILE_SPEC = "compile_spec"
    """Natural language to a CrawlSpec. A small, constrained JSON schema -
    a 4B model handles it."""

    ADAPT_SELECTORS = "adapt_selectors"
    """DOM to field selectors. Long context, structural reasoning, and an
    exact string where one wrong character means zero records. The hard one."""

    REPAIR_RECIPE = "repair_recipe"
    """A drifted recipe plus evidence, to a corrected one. As hard as
    adaptation, with more context."""

    CLASSIFY = "classify"
    """Does this record match a description. Title plus a sentence - small
    models are good at it."""

    EMBED = "embed"
    """Vectors for semantic filtering and, later, RAG."""


class GatewayError(RuntimeError):
    """The model could not be asked, or its answer could not be used."""


class ModelUnavailableError(GatewayError):
    """The backend is not reachable, or the model is not installed.

    Distinct from a bad answer: this one is fixed by starting a server or
    pulling a model, and the message should say which.
    """


@dataclass(frozen=True, slots=True)
class Usage:
    """What one call cost.

    Tokens matter even locally, where they are free: they are the honest unit
    for "is the context big enough" and "is this prompt getting out of hand".
    Time is what a person actually waits for.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    model: str = ""
    backend: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class StructuredResult[M: BaseModel]:
    """A validated object plus what it cost to get."""

    value: M
    usage: Usage
    raw: str = ""
    """The model's literal output. Kept for the failure path: when validation
    rejects an answer, the answer is the evidence."""


@dataclass(frozen=True, slots=True)
class GatewayHealth:
    reachable: bool
    backend: str
    models: tuple[str, ...] = ()
    detail: str = ""
    context_length: int | None = None


@dataclass(slots=True)
class GenerationOptions:
    """Per-call knobs.

    ``num_ctx`` is here rather than left to the server for a specific reason.
    Ollama defaults a model's context to whatever its Modelfile says, often
    2048 - and exceeding it does not raise. The prompt is silently truncated,
    the model reasons about a fragment, and the selectors come back
    confidently wrong. Every call sets it explicitly, and the caller checks
    the prompt fits (docs/08_LLM_ARCHITECTURE.md).
    """

    num_ctx: int = 16384
    num_predict: int = 2048
    temperature: float = 0.2
    top_p: float = 0.9
    seed: int | None = None
    keep_alive: str | int = -1
    """Keep the model resident. Without it Ollama unloads between calls and
    each one pays ten to thirty seconds of load time - which is ruinous for
    the retry loop, where three attempts is normal."""

    think: bool = False
    """Reasoning models emit a thinking block before the answer.

    Off by default, and the measurement is not subtle: the same structured
    question against qwen3:14b took 69 seconds with thinking on - burning the
    entire token budget before producing any JSON - and 2.0 seconds with it
    off. Naming a column from three samples is not a task that benefits from
    deliberation, and the tasks here are all of that shape.

    Turn it on for repair, where the model is reasoning about why a recipe
    stopped working rather than reading values off a page.
    """

    stop: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def with_(self, **overrides: Any) -> GenerationOptions:
        """A copy with some fields changed.

        ``replace`` rather than a hand-built dict: splatting a dict loses every
        field's type, so a typo in a caller would be caught by nothing.
        """
        updated = replace(self, **overrides)
        if "extra" not in overrides:
            # replace() copies the reference, and a shared mutable default is
            # a bug that only shows up once two calls are in flight.
            updated.extra = dict(self.extra)
        return updated


class ModelGateway(Protocol):
    """What the application sees.

    Deliberately small. Everything above this line works in terms of validated
    objects; everything below is a vendor's idea of an API.
    """

    name: str

    async def generate_structured[M: BaseModel](
        self,
        *,
        task: TaskKind,
        prompt: str,
        schema: type[M],
        system: str | None = None,
        options: GenerationOptions | None = None,
        n: int = 1,
    ) -> list[StructuredResult[M]]:
        """Produce ``n`` validated candidates.

        ``n > 1`` is not a convenience. Generating several proposals and
        scoring them deterministically beats generating one and hoping - and
        it is what makes a mediocre local model usable, because the scorer
        does the choosing (docs/08_LLM_ARCHITECTURE.md).

        Candidates that fail validation are dropped rather than raised on: out
        of five, three usable ones is a good outcome. An empty list means none
        survived, and that is a ``GatewayError``.
        """
        ...

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]: ...

    async def health(self) -> GatewayHealth: ...

    async def aclose(self) -> None: ...
