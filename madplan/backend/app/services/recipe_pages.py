"""Reads a recipe from a real web page, and refuses a page that does not hold a whole recipe.

This is the only way a new recipe gets into MadPlan. A model may suggest where to look, but what a
recipe says comes from the page itself: the name, ingredients, steps and picture that the site
publishes as structured data (schema.org/Recipe). Nothing is filled in, so a page that lacks any
of them is refused rather than completed. That is what keeps made-up recipes out.

Mealie reads the same structured data when it imports the page later, so what is refused here is
mostly what it could not have imported well either.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.security import is_public_http_url
from app.crawlers.policy import CrawlDenied, HostRule, PoliteClient
from app.services.recipes import meal_types_from_labels, not_a_meal
from app.services.text import split_ingredient

log = logging.getLogger(__name__)

# Why a page was refused. The page says these to people, so they are part of the API.
NOT_ALLOWED = "not_allowed"  # robots.txt, a site's terms, or not an address on the public internet
UNREACHABLE = "unreachable"
NO_RECIPE = "no_recipe"
NO_TITLE = "no_title"
NO_IMAGE = "no_image"
NO_INGREDIENTS = "no_ingredients"
NO_STEPS = "no_steps"
NO_SERVINGS = "no_servings"
NOT_A_MEAL = "not_a_meal"

_MIN_INGREDIENTS = 2
_MIN_STEPS_LENGTH = 40
_MAX_URL_LENGTH = 500
_TIMEOUT_SECONDS = 12

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(?:li|p|div|h\d)>|<br\s*/?>", re.IGNORECASE)
_STEP_NUMBER = re.compile(r"^(?:trin|step)?\s*\d+\s*[.):-]\s*", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class PageRefused(Exception):
    """The page cannot be used. ``reason`` is one of the codes above."""

    def __init__(self, reason: str, url: str = "") -> None:
        super().__init__(f"{reason}: {url}".rstrip(": "))
        self.reason = reason
        self.url = url


@dataclass
class RecipePage:
    url: str
    title: str
    description: str | None
    image_url: str
    servings: float
    ingredients: list[dict[str, Any]]
    steps: list[str]
    meal_types: list[str]


class _JsonLdBlocks(HTMLParser):
    """Collects the page's structured data. A real parser, as pages are not tidy."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and "ld+json" in (dict(attrs).get("type") or "").lower():
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._current is not None:
            self.blocks.append("".join(self._current))
            self._current = None


def _clean(value: Any) -> str:
    """Plain text. Sites put entities and tags in these fields, some of them escaped twice."""
    text = html.unescape(html.unescape(str(value)))
    return " ".join(_TAG.sub(" ", text).split())


def _is_recipe(node: dict) -> bool:
    kinds = node.get("@type")
    for kind in kinds if isinstance(kinds, list) else [kinds]:
        # "Recipe", "schema:Recipe" or "https://schema.org/Recipe".
        if isinstance(kind, str) and re.split(r"[/:#]", kind)[-1].lower() == "recipe":
            return True
    return False


def _recipes_in(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [recipe for item in node for recipe in _recipes_in(item)]
    if not isinstance(node, dict):
        return []
    found = [node] if _is_recipe(node) else []
    for key in ("@graph", "mainEntity", "mainEntityOfPage"):
        if isinstance(node.get(key), (list, dict)):
            found += _recipes_in(node[key])
    return found


def _image(value: Any, page_url: str) -> str | None:
    if isinstance(value, list):
        return next((image for image in (_image(item, page_url) for item in value) if image), None)
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl"), page_url)
    if not isinstance(value, str):
        return None
    address = urljoin(page_url, html.unescape(value).strip())
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(address) > 1000:
        return None
    return address


def _servings(value: Any) -> float | None:
    if isinstance(value, list):
        return next((servings for servings in (_servings(item) for item in value) if servings), None)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        # "4", "4 personer", "4-6 portioner".
        match = _NUMBER.search(str(value))
        if not match:
            return None
        amount = float(match.group().replace(",", "."))
    return amount if 0 < amount <= 200 else None


def _steps(value: Any) -> list[str]:
    if isinstance(value, list):
        return [step for item in value for step in _steps(item)]
    if isinstance(value, dict):
        if value.get("itemListElement"):
            return _steps(value["itemListElement"])
        return _steps(value.get("text") or value.get("name") or "")
    if not isinstance(value, str):
        return []
    # One long text with its steps on separate lines, or as an HTML list.
    lines = _BLOCK_END.sub("\n", html.unescape(value)).splitlines()
    steps = (_STEP_NUMBER.sub("", _clean(line)) for line in lines)
    return [step for step in steps if len(step) > 1]


def _ingredient_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        value = _BLOCK_END.sub("\n", html.unescape(value)).splitlines()
    if not isinstance(value, list):
        return []
    lines = [_clean(line) for line in value if isinstance(line, (str, int, float))]
    # "Til sovsen:" is a heading in the list, and nothing to buy.
    return [line for line in lines if line and not line.endswith(":")]


def _labels(value: Any) -> list[str]:
    if isinstance(value, list):
        return [label for item in value for label in _labels(item)]
    if isinstance(value, dict):
        return _labels(value.get("name") or "")
    return [part.strip() for part in _clean(value).split(",") if part.strip()] if value else []


def _read(node: dict, url: str, fallback_meal_type: str) -> RecipePage:
    title = _clean(node.get("name") or node.get("headline") or "")
    if not title:
        raise PageRefused(NO_TITLE, url)
    image = _image(node.get("image"), url)
    if not image:
        raise PageRefused(NO_IMAGE, url)
    lines = _ingredient_lines(node.get("recipeIngredient") or node.get("ingredients"))
    if len(lines) < _MIN_INGREDIENTS:
        raise PageRefused(NO_INGREDIENTS, url)
    steps = _steps(node.get("recipeInstructions"))
    if sum(len(step) for step in steps) < _MIN_STEPS_LENGTH:
        raise PageRefused(NO_STEPS, url)
    servings = _servings(node.get("recipeYield"))
    if servings is None:
        # Amounts cannot be scaled to the household without it, and guessing would be making it up.
        raise PageRefused(NO_SERVINGS, url)
    categories = _labels(node.get("recipeCategory"))
    if not_a_meal(categories):
        raise PageRefused(NOT_A_MEAL, url)

    ingredients = []
    for line in lines:
        name, quantity, unit = split_ingredient(line)
        # The line as the page has it, as that is what a person should read and what is checked
        # for allergies. The parts are what prices and amounts are worked out from.
        ingredients.append({"name": name, "quantity": quantity, "unit": unit, "text": line})
    description = _clean(node.get("description") or "")
    return RecipePage(
        url=url,
        title=title[:200],
        description=description[:1000] or None,
        image_url=image,
        servings=servings,
        ingredients=ingredients,
        steps=steps,
        meal_types=meal_types_from_labels(categories) or meal_types_from_labels(_labels(node.get("keywords"))) or [fallback_meal_type],
    )


def parse_recipe_page(markup: str, url: str, fallback_meal_type: str = "dinner") -> RecipePage:
    """The page's recipe, or PageRefused saying what was missing."""
    parser = _JsonLdBlocks()
    parser.feed(markup)
    nodes: list[dict] = []
    for block in parser.blocks:
        try:
            nodes += _recipes_in(json.loads(block.strip(), strict=False))
        except ValueError:
            log.info("structured data on %s could not be read", url)
    if not nodes:
        raise PageRefused(NO_RECIPE, url)
    refused: PageRefused | None = None
    for node in nodes:
        try:
            return _read(node, url, fallback_meal_type)
        except PageRefused as exc:
            refused = refused or exc
    raise refused


def domain_of(url: str | None) -> str | None:
    """"madensverden.dk" for a link to any page there, as it reads on a button."""
    host = urlsplit(url or "").hostname
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("ascii").decode("idna")
    except UnicodeError:
        return host


class RecipeReader:
    """Reads pages one at a time under the crawl policy: terms, robots.txt and a rate limit.

    Addresses from a model or a web page are not trusted, so each one has to be on the public
    internet, and so does every address a page redirects to.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None, sleep=None) -> None:
        extra = {"sleep": sleep} if sleep else {}
        self._client = PoliteClient([], delay_seconds=1.0, transport=transport, timeout=_TIMEOUT_SECONDS, url_guard=is_public_http_url, **extra)

    def __enter__(self) -> RecipeReader:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _allow_site(self, host: str) -> None:
        bare = host[4:] if host.startswith("www.") else host
        # A site and its www address are the same site, and one often redirects to the other.
        for name in (bare, f"www.{bare}"):
            if not any(rule.host == name for rule in self._client.rules):
                self._client.rules.append(HostRule(name, ("/",)))

    def read(self, url: str, fallback_meal_type: str = "dinner") -> RecipePage:
        address = (url or "").strip().split("#")[0]
        if address.startswith("http://"):
            address = "https://" + address[len("http://"):]
        if len(address) > _MAX_URL_LENGTH or not is_public_http_url(address):
            raise PageRefused(NOT_ALLOWED, address)
        self._allow_site((urlsplit(address).hostname or "").lower())
        try:
            response = self._client.get(address)
        except CrawlDenied as exc:
            raise PageRefused(NOT_ALLOWED, address) from exc
        except httpx.HTTPError as exc:
            log.info("could not read %s: %s", address, exc)
            raise PageRefused(UNREACHABLE, address) from exc
        if "html" not in response.headers.get("content-type", "html").lower():
            raise PageRefused(NO_RECIPE, address)
        # The address it ended up at, which is the one worth keeping.
        return parse_recipe_page(response.text, str(response.url), fallback_meal_type)
