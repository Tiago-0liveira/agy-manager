"""Execution strategy mapping for the Antigravity orchestration subsystem.

Owns the mapping from ExecutionStrategy to Antigravity CLI execution settings.
No strategy-specific flags should appear inside engine.py, coordinator.py,
or scheduler.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from agym.orchestration.contracts import ExecutionStrategy

__all__ = [
    "ExecutionSettings",
    "UnsupportedStrategyError",
    "get_execution_settings",
    "map_strategy",
    "is_strategy_supported",
    "get_strategy_args",
]


class UnsupportedStrategyError(ValueError):
    """Raised when an execution strategy is not supported by the runner/CLI."""

    def __init__(self, strategy: ExecutionStrategy | str, reason: str) -> None:
        self.strategy = strategy
        self.reason = reason
        strat_val = strategy.value if isinstance(strategy, ExecutionStrategy) else str(strategy)
        super().__init__(f"Execution strategy '{strat_val}' is not supported: {reason}")


@dataclass(frozen=True)
class ExecutionSettings:
    """Settings to be applied when executing a model with a given strategy."""

    strategy: ExecutionStrategy
    is_supported: bool
    args: tuple[str, ...] = ()
    effort: str | None = None
    unsupported_reason: str | None = None


# Static mappings for execution strategies
_STRATEGY_SETTINGS: dict[ExecutionStrategy, ExecutionSettings] = {
    ExecutionStrategy.STANDARD: ExecutionSettings(
        strategy=ExecutionStrategy.STANDARD,
        is_supported=True,
        effort="medium",
        args=("--effort", "medium"),
    ),
    ExecutionStrategy.HIGH_EFFORT: ExecutionSettings(
        strategy=ExecutionStrategy.HIGH_EFFORT,
        is_supported=True,
        effort="high",
        args=("--effort", "high"),
    ),
    ExecutionStrategy.BOOST: ExecutionSettings(
        strategy=ExecutionStrategy.BOOST,
        is_supported=True,
        effort="high",
        args=("--effort", "high"),
    ),
}


def get_execution_settings(strategy: ExecutionStrategy | str) -> ExecutionSettings:
    """Retrieve execution settings for a given strategy.

    Args:
        strategy: The ExecutionStrategy enum member or string representation.

    Returns:
        ExecutionSettings describing CLI flags and whether the strategy is supported.
    """
    if isinstance(strategy, str) and not isinstance(strategy, ExecutionStrategy):
        try:
            strategy = ExecutionStrategy(strategy)
        except ValueError:
            return ExecutionSettings(
                strategy=ExecutionStrategy.STANDARD,
                is_supported=False,
                args=(),
                unsupported_reason=f"Unknown execution strategy: '{strategy}'",
            )

    settings = _STRATEGY_SETTINGS.get(strategy)
    if settings is not None:
        return settings

    return ExecutionSettings(
        strategy=strategy,
        is_supported=False,
        args=(),
        unsupported_reason=f"Strategy '{strategy.value}' has no registered execution settings",
    )


# Alias for get_execution_settings
map_strategy = get_execution_settings


def is_strategy_supported(strategy: ExecutionStrategy | str) -> bool:
    """Check whether a given strategy is supported by the Antigravity runner."""
    return get_execution_settings(strategy).is_supported


def get_strategy_args(strategy: ExecutionStrategy | str) -> list[str]:
    """Get the CLI arguments for a strategy, raising UnsupportedStrategyError if unsupported."""
    settings = get_execution_settings(strategy)
    if not settings.is_supported:
        raise UnsupportedStrategyError(
            strategy=settings.strategy,
            reason=settings.unsupported_reason or "Strategy is not supported",
        )
    return list(settings.args)
