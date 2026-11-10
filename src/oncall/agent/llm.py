"""The model client: the only module that talks to the Anthropic API.

The loop sees a model as one function: a request body in, a response body
out, both plain JSON-shaped dicts. Keeping the SDK's objects on this side of
that line means the live client and a replayed trace are interchangeable, and
the loop is tested without a key or a network.
"""

import os
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ConfigDict

from oncall.agent.pricing import Usage

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
# One request of this loop produces a few thousand tokens at most; the SDK's
# default of ten minutes would let a stuck request eat the run's time budget.
REQUEST_TIMEOUT_SECONDS = 180


class ModelError(RuntimeError):
    """The model could not be reached or refused the request as malformed."""


class ConfigurationError(RuntimeError):
    """The run cannot start: no key, or an unknown model."""


@dataclass(frozen=True)
class Reply:
    body: dict[str, Any]  # the response exactly as the API returned it
    request_id: str | None


class ModelClient(Protocol):
    def create(self, request: dict[str, Any]) -> Reply: ...


class ContentBlock(BaseModel):
    """One block of a response. Only tool calls are read; the rest is passed back as is."""

    model_config = ConfigDict(extra="ignore")

    type: str
    id: str = ""
    name: str = ""
    input: dict[str, Any] = {}


class StopDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: str | None = None
    explanation: str | None = None


class Response(BaseModel):
    """The parts of a Messages API response that the loop acts on."""

    model_config = ConfigDict(extra="ignore")

    stop_reason: str | None
    content: list[ContentBlock]
    usage: Usage
    stop_details: StopDetails | None = None


def api_key_from_environment() -> str:
    """The API key, from the environment variable and from nowhere else."""
    key = os.environ.get(API_KEY_VARIABLE, "")
    if not key:
        raise ConfigurationError(
            f"{API_KEY_VARIABLE} is not set. Put it in .env (see .env.example) and run through "
            "make, or export it in your shell."
        )
    return key


class AnthropicClient:
    def __init__(self, api_key: str) -> None:
        # The key is passed explicitly so the SDK does not go looking for
        # credentials anywhere else (it can also read profiles and config files).
        # The SDK retries connection errors, 408, 409, 429 and 5xx itself
        # (twice, with backoff); what reaches this code has already been retried.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)

    def check_model(self, model: str) -> None:
        """Fail before the first paid request if the model ID does not exist."""
        try:
            self._client.models.retrieve(model)
        except anthropic.NotFoundError as err:
            raise ConfigurationError(f"the API does not know a model called {model!r}") from err
        except anthropic.AuthenticationError as err:
            raise ConfigurationError(f"the API rejected the key in {API_KEY_VARIABLE}") from err
        except anthropic.APIError as err:
            raise ConfigurationError(f"could not check the model with the API: {err}") from err

    def create(self, request: dict[str, Any]) -> Reply:
        # Not streamed: responses are short, and one complete body is simpler
        # to record and replay than a stream of events.
        try:
            message = self._client.messages.create(**request)
        except anthropic.APIError as err:
            raise ModelError(f"{type(err).__name__}: {err}") from err
        # mode="json" gives plain JSON types; unset fields are left out, so
        # this is the body as the API sent it.
        return Reply(body=dict(message.to_dict(mode="json")), request_id=message._request_id)
