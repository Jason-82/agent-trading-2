"""Thin wrapper around the Anthropic SDK for schema-constrained calls."""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Models that reject the `effort` parameter.
_NO_EFFORT_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5")


class LLMError(RuntimeError):
    """The model did not return a usable structured answer."""


class LLM:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic()

    async def parse(self, *, model: str, system: str, user: str, schema: type[T],
                    max_tokens: int = 16000, effort: str | None = None) -> T:
        kwargs: dict[str, Any] = {}
        if effort and not model.startswith(_NO_EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": effort}
        try:
            resp = await self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                **kwargs,
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"{model}: API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{model}: connection error: {e}") from e
        except anthropic.APIError as e:
            raise LLMError(f"{model}: {e}") from e
        except ValueError as e:  # incl. pydantic.ValidationError: truncated or off-schema output
            raise LLMError(f"{model}: unusable structured output: {e}") from e

        if resp.stop_reason == "refusal":
            category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMError(f"{model}: refused (category={category})")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{model}: hit max_tokens={max_tokens} before finishing")
        if resp.parsed_output is None:
            raise LLMError(f"{model}: no parsed output (stop_reason={resp.stop_reason})")
        return resp.parsed_output


def dumps(obj: Any) -> str:
    """Deterministic, compact JSON for prompts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
