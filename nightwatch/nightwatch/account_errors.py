"""Shared failure types for optional account integration and locks."""

from __future__ import annotations


class AccountBrokerError(RuntimeError):
    """Base error for account discovery, activation, and synchronization."""


class AccountUnavailable(AccountBrokerError):
    """The optional account capability is missing or temporarily unavailable."""


class AccountSchemaError(AccountBrokerError):
    """A machine-readable document or external state record cannot be trusted."""


class AccountBusy(AccountBrokerError):
    """Another Nightwatch owner currently holds the relevant lock."""
