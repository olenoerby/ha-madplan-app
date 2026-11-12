"""The only way a crawler reaches the network, and the rules it must pass first.

A request has to clear four independent checks, in this order:

1. The host is not on the legal blocklist. robots.txt alone is not enough: a site can have
   no robots.txt at all and still forbid automated use in its terms, so a robots-only
   crawler would wrongly conclude it may read everything.
2. The host and path are on the adapter's allowlist, which keeps crawlers away from
   private APIs such as Tjek's squid-api even if a page links to one.
3. robots.txt allows it, per RFC 9309. A site whose robots.txt cannot be read because of a
   server or network error counts as fully disallowed.
4. The per-host rate limit, which honours Crawl-delay when it is stricter than ours.

Redirects are not followed automatically, because each hop has to pass the same checks.
See docs/crawling-policy.md for the reasoning behind each source.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

# Hosts whose terms forbid automated access regardless of robots.txt. Entries also cover
# subdomains. Removing one requires a written agreement with the site's owner. MadPlan has
# no source for these sites: this only makes sure nothing ever fetches from them, such as
# a link on another page.
LEGALLY_BLOCKED_HOSTS: dict[str, str] = {
    "minetilbud.dk": (
        "Dayli A/S forbids automated, systematic use of MineTilbud.dk including its APIs "
        "without a separate written agreement (persondatapolitik, section 2; contact "
        "kontakt@dayli.dk)."
    ),
    "publisher.dk": "Covered by the same Dayli A/S prohibition as minetilbud.dk.",
    "dayli.dk": "Covered by the same Dayli A/S prohibition as minetilbud.dk.",
    "tjek.com": (
        "Tjek's API content needs prior written approval (terms clause 8.3). MadPlan only "
        "reads the public etilbudsavis.dk pages, never the API behind them."
    ),
}

ROBOTS_CACHE_SECONDS = 24 * 3600
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 8 * 1024 * 1024


class CrawlDenied(Exception):
    """Raised instead of fetching. ``reason`` is one of the DENY_* codes below."""

    def __init__(self, url: str, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {url} {detail}".strip())
        self.url = url
        self.reason = reason
        self.detail = detail


DENY_LEGAL = "legal_block"
DENY_ALLOWLIST = "not_on_allowlist"
DENY_ROBOTS = "robots_disallow"
DENY_ROBOTS_UNREACHABLE = "robots_unreachable"
DENY_NOT_PUBLIC = "not_public"


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def legal_block_reason(url: str) -> str | None:
    host = _host_of(url)
    for blocked, reason in LEGALLY_BLOCKED_HOSTS.items():
        if host == blocked or host.endswith(f".{blocked}"):
            return reason
    return None


@dataclass
class _RobotsEntry:
    fetched_at: float
    parser: RobotFileParser | None  # None means "treat the whole host as disallowed"
    allow_all: bool = False


@dataclass
class HostRule:
    """What an adapter may read from one host."""

    host: str
    path_prefixes: tuple[str, ...]


class PoliteClient:
    def __init__(
        self,
        rules: list[HostRule],
        delay_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        timeout: float = 25,
        url_guard: Callable[[str], bool] | None = None,
    ) -> None:
        self.rules = rules
        self.delay_seconds = max(1.0, delay_seconds)
        self.user_agent = settings.crawler_user_agent
        self._sleep = sleep
        self._clock = clock
        # For addresses from outside: every one of them, redirects too, has to pass this.
        self._url_guard = url_guard
        self._robots: dict[str, _RobotsEntry] = {}
        self._last_request: dict[str, float] = {}
        self.requests_made = 0
        self.denied = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": self.user_agent, "Accept-Language": "da-DK,da;q=0.9"},
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _on_allowlist(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return False
        return any(
            parts.hostname == rule.host and parts.path.startswith(rule.path_prefixes)
            for rule in self.rules
        )

    def _wait_turn(self, host: str, extra_delay: float | None = None) -> None:
        delay = max(self.delay_seconds, extra_delay or 0)
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def _get_robots(self, url: str) -> httpx.Response:
        """A robots.txt may redirect, often to the site's www address, so each hop is checked."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self._url_guard and not self._url_guard(current):
                raise httpx.RequestError(f"robots.txt led to {current}, which is not allowed")
            response = self._client.get(current)
            if not (response.is_redirect and response.headers.get("location")):
                return response
            current = urljoin(current, response.headers["location"])
        raise httpx.TooManyRedirects(f"too many redirects from {url}")

    def _load_robots(self, url: str) -> _RobotsEntry:
        parts = urlsplit(url)
        host = parts.hostname or ""
        cached = self._robots.get(host)
        if cached and self._clock() - cached.fetched_at < ROBOTS_CACHE_SECONDS:
            return cached

        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        entry = _RobotsEntry(fetched_at=self._clock(), parser=None)
        try:
            self._wait_turn(host)
            response = self._get_robots(robots_url)
            self.requests_made += 1
            if response.status_code == 200 and "html" not in response.headers.get("content-type", ""):
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                entry.parser = parser
            elif response.status_code in (401, 403, 429):
                # RFC 9309 would allow access here, but a site refusing to show its rules
                # is not a site saying yes, so these stay disallowed.
                log.warning("robots.txt for %s answered %s", host, response.status_code)
            elif 400 <= response.status_code < 500:
                # RFC 9309 2.3.1.3: a missing robots.txt places no restriction. The legal
                # blocklist above is what catches sites that have none but forbid it.
                entry.allow_all = True
            elif response.status_code == 200:
                # An HTML page served at /robots.txt is a soft 404, not a rule set.
                entry.allow_all = True
        except httpx.HTTPError as exc:
            log.warning("robots.txt for %s could not be read: %s", host, exc)
        self._robots[host] = entry
        return entry

    def check(self, url: str) -> float | None:
        """Raises CrawlDenied unless the URL may be fetched. Returns any Crawl-delay."""
        reason = legal_block_reason(url)
        if reason:
            raise CrawlDenied(url, DENY_LEGAL, reason)
        if not self._on_allowlist(url):
            raise CrawlDenied(url, DENY_ALLOWLIST)
        if self._url_guard and not self._url_guard(url):
            raise CrawlDenied(url, DENY_NOT_PUBLIC)
        entry = self._load_robots(url)
        if entry.allow_all:
            return None
        if entry.parser is None:
            raise CrawlDenied(url, DENY_ROBOTS_UNREACHABLE)
        if not entry.parser.can_fetch(self.user_agent, url):
            raise CrawlDenied(url, DENY_ROBOTS)
        delay = entry.parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    def get(self, url: str) -> httpx.Response:
        """Fetches a page, re-checking policy on every redirect hop."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                crawl_delay = self.check(current)
            except CrawlDenied:
                self.denied += 1
                raise
            self._wait_turn(_host_of(current), crawl_delay)
            response = self._client.get(current)
            self.requests_made += 1
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            if len(response.content) > MAX_BODY_BYTES:
                raise httpx.HTTPError(f"response from {current} is too large")
            response.raise_for_status()
            return response
        raise httpx.TooManyRedirects(f"too many redirects from {url}")


@dataclass
class SourceInfo:
    """Static description of a source, shown to admins."""

    slug: str
    name: str
    homepage: str
    status: str  # "allowed" or "blocked"
    terms_checked_on: str
    note: str
    rules: list[HostRule] = field(default_factory=list)
