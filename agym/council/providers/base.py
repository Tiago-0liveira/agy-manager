"""Provider adapter base interface for AGYM Council.

Defines the transport-neutral asynchronous interface for Antigravity,
Fake, and external LLM provider integrations.
"""

from __future__ import annotations

import abc
from typing import AsyncIterator

from agym.council.models import (
    AccountStatus,
    AttemptReconciliation,
    AttemptSnapshot,
    ModelDescriptor,
    ProviderCapabilities,
    TurnEvent,
    TurnRequest,
    TurnResult,
)


class ProviderAdapter(abc.ABC):
    """Abstract Base Class for Council model provider adapters.

    All council providers (Antigravity headless CLI, Fake development adapter,
    and remote API adapters) implement these 6 async lifecycle methods.
    """

    @abc.abstractmethod
    async def capabilities(self, account_ref: str | None = None) -> ProviderCapabilities:
        """Return provider capabilities for an account or generic capabilities."""
        ...

    @abc.abstractmethod
    async def check_account(self, account_ref: str) -> AccountStatus:
        """Probe account readiness without leaking secret credentials or auth codes."""
        ...

    @abc.abstractmethod
    async def discover_models(self, account_ref: str) -> list[ModelDescriptor]:
        """Query and return the catalog of available models for the account."""
        ...

    @abc.abstractmethod
    async def run_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent | TurnResult]:
        """Execute a worker turn as an asynchronous stream of progressive events and result."""
        ...

    @abc.abstractmethod
    async def cancel(self, attempt_id: str) -> None:
        """Actively terminate an in-flight attempt. Must be idempotent."""
        ...

    @abc.abstractmethod
    async def reconcile(
        self, in_flight_attempts: list[AttemptSnapshot]
    ) -> list[AttemptReconciliation]:
        """Inspect interrupted attempts after restart and return reconciled statuses."""
        ...
