"""Cost is the reported usage times the checked-in price table, and nothing else."""

import pytest

from oncall.agent.pricing import (
    PRICE_RETRIEVED,
    PRICE_SOURCE,
    PRICES,
    UnknownPriceError,
    Usage,
    price_of,
    request_cost,
)


def test_the_price_table_records_where_it_came_from() -> None:
    assert PRICE_SOURCE.startswith("https://")
    assert len(PRICE_RETRIEVED) == len("2026-11-10")


def test_every_model_the_project_names_has_a_price() -> None:
    assert set(PRICES) == {
        "claude-fable-5-2",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    }


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        # One million of each kind of token: the cost is the sum of the rates.
        (
            "claude-sonnet-5-5",
            {
                "input_tokens": 1_000_000,
                "cache_creation_input_tokens": 1_000_000,
                "cache_read_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
            2 + 2.50 + 0.10 + 10,
        ),
        ("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 500}, 0.004 + 0.010),
        ("claude-fable-5-2", {"input_tokens": 1000, "output_tokens": 500}, 0.008 + 0.020),
        # A typical cached turn on Haiku: mostly cache reads.
        (
            "claude-haiku-5-5",
            {
                "input_tokens": 200,
                "cache_creation_input_tokens": 1500,
                "cache_read_input_tokens": 20_000,
                "output_tokens": 400,
            },
            (200 * 0.10 + 1500 * 0.125 + 20_000 * 0.01 + 400 * 0.50) / 1e6,
        ),
    ],
)
def test_cost_is_tokens_times_rates(model: str, usage: dict[str, int], expected: float) -> None:
    assert request_cost(model, Usage.model_validate(usage)) == pytest.approx(expected)


def test_haiku_long_prompt_rates_start_above_the_threshold() -> None:
    """The prompt length counts cached tokens too, and the higher rate covers the whole request."""
    at_threshold = Usage(input_tokens=10_000, cache_read_input_tokens=90_000, output_tokens=100)
    over = Usage(input_tokens=10_001, cache_read_input_tokens=90_000, output_tokens=100)
    assert at_threshold.prompt_tokens == 100_000
    assert request_cost("claude-haiku-5-5", at_threshold) == pytest.approx(
        (10_000 * 0.10 + 90_000 * 0.01 + 100 * 0.50) / 1e6
    )
    assert request_cost("claude-haiku-5-5", over) == pytest.approx(
        (10_001 * 0.50 + 90_000 * 0.05 + 100 * 2.50) / 1e6
    )


def test_cache_writes_are_priced_by_lifetime_when_the_api_says_which() -> None:
    usage = Usage.model_validate(
        {
            "cache_creation_input_tokens": 3000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1000,
                "ephemeral_1h_input_tokens": 2000,
            },
        }
    )
    assert request_cost("claude-opus-5-5", usage) == pytest.approx((1000 * 5 + 2000 * 8) / 1e6)


def test_null_cache_counts_are_zero() -> None:
    usage = Usage.model_validate(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }
    )
    assert usage.prompt_tokens == 10


def test_a_model_without_a_price_is_an_error_not_a_guess() -> None:
    with pytest.raises(UnknownPriceError, match="claude-haiku-9"):
        price_of("claude-haiku-9")
    with pytest.raises(UnknownPriceError):
        request_cost("claude-haiku-9", Usage(input_tokens=1))


@pytest.mark.parametrize("tier", ["priority", "batch"])
def test_a_tier_without_a_price_is_an_error(tier: str) -> None:
    with pytest.raises(UnknownPriceError, match=tier):
        request_cost("claude-haiku-5-5", Usage(input_tokens=1, service_tier=tier))


def test_the_standard_tier_is_priced() -> None:
    usage = Usage(input_tokens=1000, service_tier="standard")
    assert request_cost("claude-haiku-5-5", usage) == pytest.approx(1000 * 0.10 / 1e6)
