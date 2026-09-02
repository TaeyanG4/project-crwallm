"""One client for Ollama, vLLM, LM Studio, OpenAI and the rest.

They all copied the same chat-completions shape, so ``base_url`` is the only
thing that changes between "the model on this machine" and "the model someone
else runs" (docs/08_LLM_ARCHITECTURE.md).

**Two dialects of structured output.** OpenAI takes
``response_format: {"type": "json_schema", ...}``; Ollama takes ``format``
with a bare JSON schema on its native endpoint. Both *constrain decoding*,
which is the point - invalid JSON becomes impossible rather than unlikely,
and a whole class of parse-failure handling never has to be written. The
native Ollama path is preferred when talking to Ollama because it also carries
``options`` (``num_ctx`` above all) that the OpenAI shape has nowhere to put.

**Thinking is off unless asked for.** Reasoning models spend their token
budget deliberating before they answer, which for "name this column" is 35x
slower and frequently produces no JSON at all within the budget. Measured, not
assumed: 69s versus 2.0s on qwen3:14b.

**num_ctx is not optional.** Ollama honours a model's Modelfile default,
frequently 2048, and exceeding it does not raise: the prompt is silently
truncated and the model answers about a fragment. A reduced DOM is 2-4k
tokens, so the default would cut it in half every time. Every request sets it,
and oversized prompts are refused rather than sent.
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from crwallm.llm.gateway import (
    GatewayError,
    GatewayHealth,
    GenerationOptions,
    ModelUnavailableError,
    StructuredResult,
    TaskKind,
    Usage,
)

__all__ = ["OllamaGateway", "OpenAICompatGateway"]

T = TypeVar("T", bound=BaseModel)

_JSON_SYSTEM = (
    "You answer only with a single JSON object matching the schema. "
    "No prose, no markdown fence, no explanation."
)


def _strip_fence(text: str) -> str:
    """Remove a markdown fence if the model added one anyway.

    Constrained decoding should make this unnecessary. It is here because
    "should" is doing a lot of work across a dozen backends, and the recovery
    is three lines.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[-1]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


class OpenAICompatGateway:
    """Chat completions over an OpenAI-shaped API."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        name: str = "openai_compat",
        api_key: str | None = None,
        timeout_s: float = 180.0,
        default_options: GenerationOptions | None = None,
        embed_model: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.embed_model = embed_model
        self._base_url = base_url.rstrip("/")
        self._defaults = default_options or GenerationOptions()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            # Generous: a 14B model on a busy GPU takes tens of seconds for a
            # long structured answer, and a timeout here reads as a broken
            # model rather than a slow one.
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ requests

    def _payload(
        self,
        *,
        prompt: str,
        schema: type[BaseModel],
        system: str | None,
        options: GenerationOptions,
        n: int,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or _JSON_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
            "temperature": options.temperature,
            "top_p": options.top_p,
            "max_tokens": options.num_predict,
            "n": n,
            **({"seed": options.seed} if options.seed is not None else {}),
            **({"stop": list(options.stop)} if options.stop else {}),
            **options.extra,
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.ConnectError as exc:
            raise ModelUnavailableError(
                f"{self.name}: cannot reach {self._base_url} - is the server running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise GatewayError(f"{self.name}: timed out") from exc

        if response.status_code == 404:
            raise ModelUnavailableError(
                f"{self.name}: {self.model!r} is not available at {self._base_url} "
                f"- try `ollama pull {self.model}`"
            )
        if response.status_code >= 400:
            raise GatewayError(f"{self.name}: HTTP {response.status_code}: {response.text[:300]}")

        return dict(response.json())

    # ----------------------------------------------------------- generate

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
        opts = options or self._defaults
        started = time.perf_counter()

        data = await self._post(
            "/chat/completions",
            self._payload(prompt=prompt, schema=schema, system=system, options=opts, n=n),
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage_raw = data.get("usage") or {}

        results: list[StructuredResult[M]] = []
        failures: list[str] = []

        for choice in data.get("choices", []):
            text = (choice.get("message") or {}).get("content") or ""
            parsed = self._validate(text, schema, failures)
            if parsed is None:
                continue
            results.append(
                StructuredResult(
                    value=parsed,
                    usage=Usage(
                        prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                        completion_tokens=int(usage_raw.get("completion_tokens", 0)),
                        elapsed_ms=elapsed_ms,
                        model=self.model,
                        backend=self.name,
                    ),
                    raw=text,
                )
            )

        if not results:
            # The raw answers are the evidence for why nothing survived, and
            # the retry loop feeds them back to the model.
            raise GatewayError(
                f"{self.name}: no candidate validated against {schema.__name__} "
                f"({task.value}); {'; '.join(failures[:2]) or 'empty response'}"
            )
        return results

    @staticmethod
    def _validate[M: BaseModel](text: str, schema: type[M], failures: list[str]) -> M | None:
        """Drop bad candidates rather than raising on them.

        Three usable proposals out of five is a good outcome; failing the
        whole call because the fourth was malformed would throw away the three.
        """
        cleaned = _strip_fence(text)
        if not cleaned:
            failures.append("empty")
            return None
        try:
            return schema.model_validate_json(cleaned)
        except ValidationError as exc:
            failures.append(f"schema: {exc.errors()[0].get('msg', 'invalid')}")
        except ValueError as exc:
            failures.append(f"json: {exc}")
        return None

    # ------------------------------------------------------------- embed

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        target = model or self.embed_model or self.model
        data = await self._post("/embeddings", {"model": target, "input": texts})
        rows = sorted(data.get("data", []), key=lambda d: int(d.get("index", 0)))
        return [list(map(float, row["embedding"])) for row in rows]

    # ------------------------------------------------------------ health

    async def health(self) -> GatewayHealth:
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            return GatewayHealth(
                reachable=False, backend=self.name, detail=f"{type(exc).__name__}: {exc}"
            )
        if response.status_code >= 400:
            return GatewayHealth(
                reachable=False, backend=self.name, detail=f"HTTP {response.status_code}"
            )
        models = tuple(m.get("id", "") for m in response.json().get("data", []))
        return GatewayHealth(
            reachable=True,
            backend=self.name,
            models=models,
            detail="ok" if self.model in models else f"{self.model!r} not in the list",
        )


class OllamaGateway(OpenAICompatGateway):
    """Ollama, through its native endpoint.

    The OpenAI-compatible surface works, but it has nowhere to put
    ``options`` - and ``num_ctx`` lives there. Without it Ollama uses the
    model's Modelfile default, silently truncates anything longer, and returns
    a confident answer about half a document. That single field is the reason
    this subclass exists.

    ``format`` also takes the JSON schema directly, so decoding is constrained
    the same way, and ``keep_alive`` stops the model being unloaded between
    calls - which matters most in the retry loop, where three attempts is
    normal and each reload would cost ten to thirty seconds.
    """

    def __init__(self, *, base_url: str = "http://127.0.0.1:11434", **kwargs: Any) -> None:
        # Callers configure the OpenAI-compatible URL; the native API is one
        # level up from it.
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        self._root = root
        super().__init__(base_url=f"{root}/v1", name=kwargs.pop("name", "ollama"), **kwargs)
        self._native = httpx.AsyncClient(base_url=root, timeout=httpx.Timeout(180.0, connect=10.0))

    async def aclose(self) -> None:
        await super().aclose()
        await self._native.aclose()

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
        opts = options or self._defaults

        # Ollama has no `n`, so several candidates means several calls. They
        # are sequential rather than concurrent: one GPU serving one model
        # gains nothing from parallel requests and loses predictability.
        results: list[StructuredResult[M]] = []
        failures: list[str] = []

        for attempt in range(n):
            # Identical prompts with temperature > 0 still diverge, but a
            # varying seed makes it deliberate rather than incidental.
            call_opts = opts.with_(seed=None if opts.seed is None else opts.seed + attempt)
            text, usage = await self._native_call(prompt, schema, system, call_opts)
            parsed = self._validate(text, schema, failures)
            if parsed is not None:
                results.append(StructuredResult(value=parsed, usage=usage, raw=text))

        if not results:
            raise GatewayError(
                f"{self.name}: no candidate validated against {schema.__name__} "
                f"({task.value}); {'; '.join(failures[:2]) or 'empty response'}"
            )
        return results

    async def _native_call(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str | None,
        options: GenerationOptions,
    ) -> tuple[str, Usage]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or _JSON_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": schema.model_json_schema(),
            "keep_alive": options.keep_alive,
            # Measured on qwen3:14b: 69s with thinking, 2.0s without, for the
            # same structured question. See GenerationOptions.think.
            "think": options.think,
            "options": {
                "num_ctx": options.num_ctx,
                "num_predict": options.num_predict,
                "temperature": options.temperature,
                "top_p": options.top_p,
                **({"seed": options.seed} if options.seed is not None else {}),
                **({"stop": list(options.stop)} if options.stop else {}),
                **options.extra,
            },
        }

        started = time.perf_counter()
        try:
            response = await self._native.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ModelUnavailableError(
                f"{self.name}: cannot reach {self._root} - is `ollama serve` running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise GatewayError(
                f"{self.name}: timed out after {options.num_predict} tokens"
            ) from exc

        if response.status_code == 404:
            raise ModelUnavailableError(
                f"{self.name}: model {self.model!r} is not installed - "
                f"run `ollama pull {self.model}`"
            )
        if response.status_code >= 400:
            raise GatewayError(f"{self.name}: HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        return (
            (data.get("message") or {}).get("content") or "",
            Usage(
                prompt_tokens=int(data.get("prompt_eval_count", 0)),
                completion_tokens=int(data.get("eval_count", 0)),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                model=self.model,
                backend=self.name,
            ),
        )

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        target = model or self.embed_model or self.model
        response = await self._native.post(
            "/api/embed", json={"model": target, "input": texts, "keep_alive": -1}
        )
        if response.status_code == 404:
            raise ModelUnavailableError(
                f"{self.name}: embedding model {target!r} is not installed - "
                f"run `ollama pull {target}`"
            )
        response.raise_for_status()
        return [list(map(float, row)) for row in response.json().get("embeddings", [])]

    async def health(self) -> GatewayHealth:
        try:
            response = await self._native.get("/api/tags")
        except httpx.HTTPError as exc:
            return GatewayHealth(
                reachable=False,
                backend=self.name,
                detail=f"cannot reach {self._root}: {type(exc).__name__}",
            )
        if response.status_code >= 400:
            return GatewayHealth(
                reachable=False, backend=self.name, detail=f"HTTP {response.status_code}"
            )

        models = tuple(m.get("name", "") for m in response.json().get("models", []))
        installed = self.model in models
        return GatewayHealth(
            reachable=True,
            backend=self.name,
            models=models,
            detail="ok" if installed else f"{self.model!r} not installed",
        )

    async def show(self, model: str | None = None) -> dict[str, Any]:
        """Model metadata, including the context length it was built with.

        Worth having: asking for 16k on a model trained for 8k is a silently
        worse answer, not an error.
        """
        response = await self._native.post("/api/show", json={"model": model or self.model})
        response.raise_for_status()
        return dict(response.json())


def _unused_json_marker() -> str:  # pragma: no cover
    return json.dumps({})
