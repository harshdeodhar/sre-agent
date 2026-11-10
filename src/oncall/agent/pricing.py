"""What a model request costs.

Cost is never estimated. It is the token counts the API reports for a request,
multiplied by the prices below, which were copied from Anthropic's pricing page
on the date recorded here. A model without an entry cannot be run: the caller
gets an error, not a guessed price.

These are list prices for the standard tier. This project never asks for the
Batch API, priority tier, a one-hour cache or US-only inference, each of which
is priced differently. `request_cost` refuses usage that reports a tier it has
no price for.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, field_validator

PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing.md"
PRICE_RETRIEVED = "2026-11-10"

_PER_TOKEN = 1 / 1_000_000  # prices are quoted per million tokens


class UnknownPriceError(LookupError):
    """There is no checked-in price for this model or tier."""


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class ModelPrice:
    standard: Rates
    # Claude Haiku 5.5 is priced by prompt length: a request whose prompt is
    # over the threshold pays `long_prompt` rates for all of its tokens. The
    # prompt length counts every input token, cached or not.
    long_prompt_threshold: int | None = None
    long_prompt: Rates | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if (
            self.long_prompt is not None
            and self.long_prompt_threshold is not None
            and prompt_tokens > self.long_prompt_threshold
        ):
            return self.long_prompt
        return self.standard


PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-2": ModelPrice(Rates(8, 10, 16, 0.20, 40)),
    "claude-opus-5-5": ModelPrice(Rates(4, 5, 8, 0.20, 20)),
    "claude-sonnet-5-5": ModelPrice(Rates(2, 2.50, 4, 0.10, 10)),
    "claude-haiku-5-5": ModelPrice(
        standard=Rates(0.10, 0.125, 0.20, 0.01, 0.50),
        long_prompt_threshold=100_000,
        long_prompt=Rates(0.50, 0.625, 1, 0.05, 2.50),
    ),
}


class CacheCreation(BaseModel):
    """How many of the tokens written to the cache went to which lifetime."""

    model_config = ConfigDict(extra="ignore")

    ephemeral_5m_input_tokens: int = 0
    ephemeral_1h_input_tokens: int = 0


class Usage(BaseModel):
    """The `usage` object of one Messages API response.

    `input_tokens` is only the part of the prompt that was neither read from
    nor written to the cache. The three input counts are disjoint and add up
    to the prompt.
    """

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation: CacheCreation | None = None
    service_tier: str | None = None

    @field_validator(
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        mode="before",
    )
    @classmethod
    def _none_is_zero(cls, value: object) -> object:
        # The API sends null for the cache counts when caching played no part.
        return 0 if value is None else value

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def price_of(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownPriceError(
            f"no price is checked in for model {model!r}; add it to {__name__} from {PRICE_SOURCE}"
        ) from None


def request_cost(model: str, usage: Usage) -> float:
    """The cost in US dollars of one request, from the usage the API reported."""
    if usage.service_tier not in (None, "standard"):
        raise UnknownPriceError(
            f"usage reports service tier {usage.service_tier!r}; only standard prices are checked in"
        )
    rates = price_of(model).rates_for(usage.prompt_tokens)
    if usage.cache_creation is not None:
        written_5m = usage.cache_creation.ephemeral_5m_input_tokens
        written_1h = usage.cache_creation.ephemeral_1h_input_tokens
    else:
        # No breakdown: this code only ever asks for the default 5-minute cache.
        written_5m, written_1h = usage.cache_creation_input_tokens, 0
    return _PER_TOKEN * (
        usage.input_tokens * rates.input
        + written_5m * rates.cache_write_5m
        + written_1h * rates.cache_write_1h
        + usage.cache_read_input_tokens * rates.cache_read
        + usage.output_tokens * rates.output
    )
