"""Chat completion provider for Azure OpenAI.

Every LLM-backed component in this codebase (query condensation, reranking,
answer generation, groundedness verification, the eval judge) checks
``provider.available`` and has a deterministic non-LLM fallback.  That is why
there is no "stub chat model" here: faking completions would make the system
look like it worked when it did not.  Instead the pipeline degrades to
heuristics and reports ``llm: unavailable`` in the response trace, so a demo
run without Azure credentials is honest about what produced the answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from rag.config import Settings
from rag.observability.tracing import get_logger, record_usage
from rag.providers.http import aclose as http_aclose
from rag.providers.http import make_client, post_with_retry

log = get_logger(__name__)

# Approximate USD per 1K tokens, used for the cost column in eval reports.
# Override with your negotiated rates; these are list prices for gpt-4o class
# models and exist to make relative cost visible, not to be an invoice.
PRICING_PER_1K = {
    "prompt": 0.0025,
    "completion": 0.01,
}

# Sent with every temperature-0 call so that repeated runs are comparable.
DETERMINISM_SEED = 20260101


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.prompt_tokens / 1000 * PRICING_PER_1K["prompt"]
            + self.completion_tokens / 1000 * PRICING_PER_1K["completion"]
        )

    def json(self) -> Any:
        """Parse the completion as JSON, tolerating markdown fences."""
        return parse_json_response(self.text)


def parse_json_response(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Models occasionally wrap JSON in ```json fences or prepend a sentence even
    when asked not to; callers of this function treat a parse failure as "use
    the heuristic fallback", so being lenient here avoids spurious degradation.
    """
    if not text:
        return None
    cleaned = text.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost JSON object or array in the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


class ChatProvider:
    """Azure OpenAI chat completions over REST."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.available = settings.has_azure_openai
        self._client = make_client(settings, timeout_s=120.0)
        self._verified = False

    @property
    def name(self) -> str:
        if not self.available:
            return "unavailable"
        return f"azure-openai:{self._settings.aoai_chat_deployment}"

    def _url(self, deployment: str) -> str:
        return (
            f"{self._settings.aoai_endpoint}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={self._settings.aoai_api_version}"
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        json_mode: bool = False,
        utility: bool = False,
    ) -> ChatResult | None:
        """Run a completion.

        Returns ``None`` when no Azure OpenAI is configured or the call fails
        after retries, which is the signal for callers to use their fallback.
        Set ``utility=True`` for the cheap high-volume calls (rerank, condense,
        judge) so they route to the smaller deployment.
        """
        if not self.available:
            return None

        deployment = (
            self._settings.utility_deployment
            if utility
            else self._settings.aoai_chat_deployment
        )
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if temperature == 0.0:
            # Best-effort determinism. temperature=0 alone is not reproducible on
            # Azure OpenAI -- an observed case had the reranker score every
            # candidate 0 on one call and score the right one 10 on the next,
            # with identical input. `seed` makes runs comparable, which matters
            # most for the evaluation harness. It is a hint, not a guarantee:
            # `system_fingerprint` changes when the backend does.
            payload["seed"] = DETERMINISM_SEED
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "api-key": self._settings.aoai_api_key,
            "Content-Type": "application/json",
        }

        try:
            response = await post_with_retry(
                self._client, self._url(deployment), payload, headers,
                what="Azure OpenAI chat completion",
            )
        except RuntimeError as exc:
            log.warning(
                "chat completion unavailable, caller will use its fallback",
                deployment=deployment,
                error=str(exc)[:200],
            )
            return None

        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage", {})
        record_usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            llm_calls=1,
        )
        self._verified = True
        return ChatResult(
            text=(choice.get("message") or {}).get("content") or "",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", deployment),
            finish_reason=choice.get("finish_reason", ""),
            raw=data,
        )

    async def aclose(self) -> None:
        await http_aclose(self._client)

    async def probe(self) -> bool:
        """One cheap call to confirm the deployment really answers."""
        if not self.available:
            return False
        result = await self.complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
            utility=True,
        )
        if result is None:
            self.available = False
            log.warning("Azure OpenAI chat deployment did not respond; "
                        "falling back to heuristics for every LLM stage")
            return False
        log.info("chat provider active", provider=self.name)
        return True


def get_chat_provider(settings: Settings) -> ChatProvider:
    provider = ChatProvider(settings)
    if provider.available:
        return provider

    if settings.azure_openai_credentials_present:
        # The most confusing state to be in, so it gets its own message:
        # everything needed is present, it is simply not switched on.
        log.warning(
            "Azure OpenAI credentials are present but AZURE_OPENAI_ENABLED is "
            "not set, so they will NOT be used. Answers will be extractive and "
            "reranking lexical. This is deliberate: credentials are often "
            "inherited from the machine rather than chosen for this run.",
            deployment=settings.aoai_chat_deployment,
            hint="set AZURE_OPENAI_ENABLED=true to use them",
        )
    else:
        # Enumerate what is absent rather than naming one setting: partial
        # configuration is the case that costs debugging time, and a fixed
        # message can end up pointing at the setting that is already correct.
        missing = [name for name, value in (
            ("AZURE_OPENAI_ENDPOINT", settings.aoai_endpoint),
            ("AZURE_OPENAI_API_KEY", settings.aoai_api_key),
            ("AZURE_OPENAI_CHAT_DEPLOYMENT", settings.aoai_chat_deployment),
        ) if not value]
        log.info(
            "no Azure OpenAI chat provider; "
            "answers will be extractive and reranking will be lexical",
            missing=",".join(missing),
            hint=f"set AZURE_OPENAI_ENABLED=true plus {' and '.join(missing)}",
        )
    return provider
