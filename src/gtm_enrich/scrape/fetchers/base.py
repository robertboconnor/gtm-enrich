"""The fetch backend contract.

A fetcher answers exactly one question: given a URL, what is on that page? It
does not decide *whether* to fetch (robots), *which* URL to try (apex vs www),
or what to do with the result -- `scrape/fetch.py` owns all of that, so those
policies stay identical no matter which backend is selected.

Backends differ in one interesting way: some render markdown themselves and some
only return HTML. `FetchedContent` carries whichever it has, and the caller
converts when needed.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


class FetcherError(RuntimeError):
    """A backend could not retrieve the page."""


class FetcherNotConfigured(FetcherError):
    """The backend is missing an API key or endpoint."""


@dataclass
class FetchedContent:
    """What a backend hands back, normalized across four very different APIs."""

    final_url: str
    status_code: int
    html: str | None = None
    markdown: str | None = None
    title: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.html and not self.markdown:
            raise FetcherError("Backend returned neither HTML nor markdown.")


class Fetcher(ABC):
    """Retrieves one page. Implementations own their own HTTP client."""

    name: str = "base"
    # True when the backend executes JavaScript before capturing the page.
    renders_javascript: bool = False

    @abstractmethod
    async def fetch(self, url: str) -> FetchedContent:
        """Retrieve `url`. Raise `FetcherError` on any failure."""

    async def aclose(self) -> None:  # noqa: B027 - optional; not every backend owns a client
        """Release any held connections."""

    @staticmethod
    def require_env(name: str, backend: str) -> str:
        value = os.getenv(name)
        if not value:
            raise FetcherNotConfigured(
                f"{name} is not set, which the '{backend}' scraper backend requires. "
                f"Set it in .env, or use --scraper direct (no key needed)."
            )
        return value
