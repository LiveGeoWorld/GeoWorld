"""Explicit retryable outcomes; unexpected contract failures must stop generation."""


class ConstructionRejected(ValueError):
    """A valid construction attempt could not produce a feasible distinction."""
