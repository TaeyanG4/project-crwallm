"""Which backend answers which task.

The point of routing is that the tasks are not equally hard
(docs/08_LLM_ARCHITECTURE.md). Naming a column from three samples is
something a 4B model does; turning a DOM into exact selectors is not. Routing
lets a machine with a modest GPU do the easy work locally and send only the
hard work to a paid API - which is the practical answer to "what if my GPU is
too small", and it is a configuration line rather than a code path.

**Fallback is for unavailability, not for disappointment.** A backend that is
down or missing a model is retried elsewhere. A backend that answered badly is
not: that is what the scoring loop is for, and silently escalating to a paid
API because a local answer scored low is a way to spend money by accident.

**Keys are references, never values.** ``api_key_ref = "env:OPENAI_API_KEY"``
resolves at call time. A key in a config file gets committed
(docs/11_SECURITY_MODEL.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from crwallm.llm.gateway import (
    GatewayHealth,
    GenerationOptions,
    ModelGateway,
    ModelUnavailableError,
    StructuredResult,
    TaskKind,
)
from crwallm.llm.openai_compat import OllamaGateway, OpenAICompatGateway

__all__ = ["BackendConfig", "RoutedGateway", "RoutingConfig", "resolve_secret"]


class SecretResolutionError(RuntimeError):
    """A configured key reference points at nothing."""


def resolve_secret(ref: str | None) -> str | None:
    """``env:NAME`` to its value.

    Only ``env:`` for now. The point is the indirection: config stores where
    to find the key, and the key itself never enters a file that gets
    committed or a prompt that gets sent.
    """
    if not ref:
        return None
    scheme, _, name = ref.partition(":")
    if scheme != "env":
        raise SecretResolutionError(f"unsupported secret reference {ref!r}; expected 'env:NAME'")
    value = os.environ.get(name)
    if not value:
        raise SecretResolutionError(
            f"{ref} is configured but ${name} is not set in the environment"
        )
    return value


@dataclass(frozen=True, slots=True)
class BackendConfig:
    name: str
    kind: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    embed_model: str | None = None
    api_key_ref: str | None = None
    num_ctx: int = 16384
    num_predict: int = 2048
    temperature: float = 0.2
    think: bool = False

    def build(self) -> ModelGateway:
        options = GenerationOptions(
            num_ctx=self.num_ctx,
            num_predict=self.num_predict,
            temperature=self.temperature,
            think=self.think,
        )
        common: dict[str, Any] = {
            "model": self.model,
            "name": self.name,
            "default_options": options,
            "embed_model": self.embed_model,
            "api_key": resolve_secret(self.api_key_ref),
        }
        if self.kind == "ollama":
            return OllamaGateway(base_url=self.base_url, **common)
        return OpenAICompatGateway(base_url=self.base_url, **common)


@dataclass(slots=True)
class RoutingConfig:
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    tasks: dict[TaskKind, str] = field(default_factory=dict)
    fallback: str | None = None

    @classmethod
    def local_default(
        cls,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3.5:9b",
        embed_model: str = "bge-m3",
    ) -> RoutingConfig:
        """Everything on one local backend.

        What a machine with a usable GPU and no API key should do, and what
        the tool assumes until told otherwise.
        """
        local = BackendConfig(
            name="local", kind="ollama", base_url=base_url, model=model, embed_model=embed_model
        )
        return cls(
            backends={"local": local},
            tasks=dict.fromkeys(TaskKind, "local"),
            fallback=None,
        )

    def backend_for(self, task: TaskKind) -> BackendConfig:
        name = self.tasks.get(task) or self.fallback
        if name is None or name not in self.backends:
            raise KeyError(f"no backend routed for {task.value}")
        return self.backends[name]


class RoutedGateway:
    """A gateway that picks its backend per task.

    Implements ``ModelGateway``, so nothing above it knows routing exists.
    """

    name = "routed"

    def __init__(self, config: RoutingConfig) -> None:
        self._config = config
        self._built: dict[str, ModelGateway] = {}

    def _gateway(self, backend: BackendConfig) -> ModelGateway:
        if backend.name not in self._built:
            self._built[backend.name] = backend.build()
        return self._built[backend.name]

    def for_task(self, task: TaskKind) -> ModelGateway:
        return self._gateway(self._config.backend_for(task))

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
        primary = self._config.backend_for(task)
        try:
            return await self._gateway(primary).generate_structured(
                task=task, prompt=prompt, schema=schema, system=system, options=options, n=n
            )
        except ModelUnavailableError:
            # Unavailable, not unsatisfying. A backend that answered poorly is
            # the scoring loop's problem; escalating for that would spend money
            # on a judgement the caller never made.
            fallback = self._config.fallback
            if (
                fallback is None
                or fallback == primary.name
                or fallback not in self._config.backends
            ):
                raise
            return await self._gateway(self._config.backends[fallback]).generate_structured(
                task=task, prompt=prompt, schema=schema, system=system, options=options, n=n
            )

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        return await self.for_task(TaskKind.EMBED).embed(texts, model=model)

    async def health(self) -> GatewayHealth:
        for name in dict.fromkeys(
            list(self._config.tasks.values())
            + ([self._config.fallback] if self._config.fallback else [])
        ):
            if name is None or name not in self._config.backends:
                continue
            health = await self._gateway(self._config.backends[name]).health()
            if health.reachable:
                return health
        return GatewayHealth(reachable=False, backend="routed", detail="no backend reachable")

    async def health_all(self) -> dict[str, GatewayHealth]:
        return {
            name: await self._gateway(config).health()
            for name, config in self._config.backends.items()
        }

    async def aclose(self) -> None:
        for gateway in self._built.values():
            await gateway.aclose()
        self._built.clear()
