"""Danish-aware text matching and quantity parsing.

Danish builds compounds with the head noun last, so "letmælk" is milk and "oksekød" is
meat, while "løgismose" is not an onion. A query word therefore matches a word in the
text when they are equal, inflected, or when the text word ends in the query word and
that ending is long enough to be meaningful.
"""

from __future__ import annotations

import re
import unicodedata

_WORD = re.compile(r"[0-9a-zæøåäöü]+")
_INFLECTIONS = ("e", "er", "en", "et", "r", "n", "s", "erne", "ene", "ne", "t")
_COMPOUND_MIN = 4

UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "g": ("g", 1), "gr": ("g", 1), "gram": ("g", 1),
    "kg": ("g", 1000), "kilo": ("g", 1000), "kilogram": ("g", 1000),
    "ml": ("ml", 1), "cl": ("ml", 10), "dl": ("ml", 100),
    "l": ("ml", 1000), "ltr": ("ml", 1000), "liter": ("ml", 1000),
    "tsk": ("ml", 5), "spsk": ("ml", 15),
    "stk": ("stk", 1), "fed": ("stk", 1), "pk": ("stk", 1), "ps": ("stk", 1),
    "bdt": ("stk", 1), "ds": ("stk", 1), "dåse": ("stk", 1), "dåser": ("stk", 1),
    "fl": ("stk", 1), "gl": ("stk", 1), "bakke": ("stk", 1), "skive": ("stk", 1),
    "skiver": ("stk", 1),
}

# Things like "400 g", "1,5 l", "2 x 125 g" and "3-pak".
_QUANTITY = re.compile(
    r"(?:(?P<count>\d+)\s*[x×]\s*)?(?:(?P<numerator>\d+)/)?(?P<amount>\d+(?:[.,]\d+)?)(?:\s*-\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?P<unit>kilogram|kilo|kg|gram|gr|g|liter|ltr|ml|cl|dl|l)\b",
    re.IGNORECASE,
)
# What follows "med" is what comes with the product: "rullepølse med løg" is not an onion.
_WITH = re.compile(r"\s(?:med|m)\s")
_ALTERNATIVES = re.compile(r"\b(?:eller|el)\b")
# The end of a sentence, and not of an abbreviation: "150 gr. Arla yoghurt" carries on.
_SENTENCE_END = re.compile(r"(?<=\w{4})\.\s")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    # NFKC folds compatibility forms but leaves æ, ø and å alone.
    return " ".join(_WORD.findall(unicodedata.normalize("NFKC", text).lower()))


def words(text: str | None) -> list[str]:
    return normalize(text).split()


def word_matches(query_word: str, text_word: str) -> bool:
    if query_word == text_word:
        return True
    if text_word.startswith(query_word) and text_word[len(query_word):] in _INFLECTIONS:
        return True
    if query_word.startswith(text_word) and query_word[len(text_word):] in _INFLECTIONS:
        return True
    return len(query_word) >= _COMPOUND_MIN and text_word.endswith(query_word)


def text_matches(query: str | None, text: str | None) -> bool:
    """True when every word of the query is found in the text."""
    query_words = words(query)
    if not query_words:
        return False
    text_words = words(text)
    return all(any(word_matches(q, t) for t in text_words) for q in query_words)


class WordIndex:
    """Answers "which of these texts match this query" without comparing against every text.

    The vocabulary is small compared with the number of offers, so each query word only has
    to be compared with it once, and the result is reused for the rest of the plan.
    """

    def __init__(self, texts: list[str]) -> None:
        self._postings: dict[str, set[int]] = {}
        for position, text in enumerate(texts):
            for word in words(text):
                self._postings.setdefault(word, set()).add(position)
        self._word_cache: dict[str, set[int]] = {}

    def _positions_for_word(self, query_word: str) -> set[int]:
        cached = self._word_cache.get(query_word)
        if cached is not None:
            return cached
        found: set[int] = set()
        for word, positions in self._postings.items():
            if word_matches(query_word, word):
                found |= positions
        self._word_cache[query_word] = found
        return found

    def find(self, query: str) -> set[int]:
        """Positions of the texts containing every word of the query."""
        query_words = words(query)
        if not query_words:
            return set()
        result = self._positions_for_word(query_words[0])
        for word in query_words[1:]:
            result = result & self._positions_for_word(word)
        return result


def product_head(title: str | None, description: str | None = None) -> str:
    """The words that say what a product is: the name, and the start of the description.

    Later text and categories mention all sorts of other things. The description counts because
    a leaflet can name just a brand as the title, with "formalet kaffe" underneath.
    """
    parts = [_WITH.split(f" {normalize(part)} ", maxsplit=1)[0] for part in _name_parts(title, description)]
    return " ".join(part.strip() for part in parts if part.strip())


def _name_parts(title: str | None, description: str | None) -> tuple[str | None, str]:
    return title, _SENTENCE_END.split(description or "", maxsplit=1)[0]


def product_name(title: str | None, description: str | None = None) -> str:
    """The whole name, including what comes after "med". "Nudler m/kyllingsmag" are still instant."""
    return " ".join(normalize(part) for part in _name_parts(title, description) if part).strip()


def lists_alternatives(text: str) -> bool:
    """True for "koteletter eller flæsk": several products, rather than one with a flavour."""
    return bool(_ALTERNATIVES.search(text))


def to_base_unit(quantity: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """Converts to g, ml or stk. Unknown units are returned as they are."""
    if unit is None:
        return quantity, None
    key = normalize(unit).replace(" ", "")
    if key in UNIT_ALIASES:
        base, factor = UNIT_ALIASES[key]
        return (None if quantity is None else quantity * factor), base
    return quantity, key or None


def parse_pack_quantity(text: str | None) -> tuple[float | None, str | None]:
    """Finds a pack size in free text. A range such as "150-400 g" returns the low end."""
    if not text:
        return None, None
    match = _QUANTITY.search(text)
    if not match:
        return None, None
    amount = float(match.group("amount").replace(",", "."))
    if match.group("numerator"):
        # "1/2 kg" is half a kilo, and not two.
        amount = int(match.group("numerator")) / amount
    count = int(match.group("count")) if match.group("count") else 1
    quantity, unit = to_base_unit(amount * count, match.group("unit"))
    return quantity, unit


_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
_INGREDIENT = re.compile(
    r"^\s*(?P<amount>\d+\s*/\s*\d+|\d+(?:[.,]\d+)?(?:\s*-\s*\d+(?:[.,]\d+)?)?|[½¼¾⅓⅔])?\s*"
    r"(?:(?P<unit>kilogram|kilo|kg|gram|gr|g|liter|ltr|dl|cl|ml|l|spiseskefulde|spiseskefuld|teskefulde|teskefuld|spsk|tsk|stk|fed|bdt|bundt|dåser?|pakker?|pk|ps|knsp)\b\.?\s*)?"
    r"(?P<name>.+)$",
    re.IGNORECASE,
)
_NOTE_IN_BRACKETS = re.compile(r"\([^)]*\)")
_ASIDE = re.compile(r"\s[-–]\s.*$")
_PACKS = re.compile(r"^\s*(\d+)\s*[x×]\s*(?=\d)", re.IGNORECASE)
# Not the one in "1,0 % fedt", which is part of a number.
_COMMA = re.compile(r",(?!\d)")
_LONG_UNITS = {"spiseskefulde": "spsk", "spiseskefuld": "spsk", "teskefulde": "tsk", "teskefuld": "tsk", "bundt": "bdt", "dåser": "dåse", "pakke": "pk", "pakker": "pk"}


def split_ingredient(text: str) -> tuple[str, float | None, str | None]:
    """Reads "400 g hakket oksekød" and "2 løg, finthakket" as a name, an amount and a unit.

    Mealie stores recipes it has scraped this way, as plain text. A number without a unit means
    pieces. How something is to be prepared, after the comma, is not part of what to buy.
    """
    # "(i tynde skiver)" and "- gerne Alouette" say how or which, and not what to buy.
    cleaned = _ASIDE.sub(" ", _NOTE_IN_BRACKETS.sub(" ", text)).strip() or text.strip()
    # "1 x 500 g laksefilet" is 500 g, and not one piece of something called "x 500 g laksefilet".
    packs = _PACKS.match(cleaned)
    count = int(packs.group(1)) if packs else 1
    if packs:
        cleaned = cleaned[packs.end():]
    match = _INGREDIENT.match(cleaned)
    if not match or not match.group("name"):
        return cleaned, None, None
    amount_text = match.group("amount")
    quantity: float | None = None
    if amount_text:
        if amount_text in _FRACTIONS:
            quantity = _FRACTIONS[amount_text]
        elif "/" in amount_text:
            top, bottom = amount_text.split("/")
            quantity = int(top) / int(bottom) if int(bottom) else None
        else:
            # "2-3 løg" buys for the higher amount, as running short is what spoils dinner.
            quantity = float(amount_text.split("-")[-1].strip().replace(",", "."))
    unit = match.group("unit")
    # A unit word with no number first is most likely just the start of a name.
    if unit and quantity is None:
        return _COMMA.split(cleaned)[0].strip(), None, None
    unit_key = unit.lower() if unit else None
    unit_key = _LONG_UNITS.get(unit_key, unit_key)
    name = _COMMA.split(match.group("name"))[0].strip()
    if quantity is not None and unit_key is None:
        unit_key = "stk"
    return name, quantity * count if quantity is not None else None, unit_key


def round_quantity(quantity: float, unit: str | None) -> float:
    if unit == "stk":
        return float(max(1, round(quantity + 0.49)))
    return float(round(quantity))
