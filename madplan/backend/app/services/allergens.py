"""Allergy matching. This is a hard filter, so it errs on the side of excluding too much.

Matching works on names only: it cannot see traces, "may contain" labels or an unusual
product name, so the UI tells people to check the packaging. It is a safety net and not
a guarantee.
"""

from __future__ import annotations

from app.services.text import normalize, words

# Allergen word -> foods that contain it, including hidden sources that do not name it.
_ALLERGEN_TERMS: dict[str, tuple[str, ...]] = {
    "nødder": (
        "nød", "nødder", "nødde", "hasselnød", "valnød", "paranød", "cashew", "pekan", "pecan", "pistacie",
        "macadamia", "mandel", "mandler", "marcipan", "nougat", "nutella", "praline", "pesto",
        "kransekage", "jordnød", "peanut", "pinjekerne", "muesli", "granola",
    ),
    "jordnødder": ("jordnød", "peanut", "satay", "saté"),
    "gluten": (
        "hvede", "hvedemel", "rug", "rugbrød", "byg", "spelt", "pasta", "spaghetti", "lasagne",
        "penne", "fusilli", "nudler", "brød", "burgerbolle", "pita", "tortilla", "couscous", "bulgur",
        "paneret", "rasp", "mel", "sojasauce", "fladbrød", "rundstykke", "havregryn", "pizzadej",
    ),
    "mælk": (
        "mælk", "fløde", "smør", "ost", "yoghurt", "skyr", "creme fraiche", "cremefraiche", "kvark",
        "parmesan", "mozzarella", "feta", "cheddar", "hytteost", "flødeost", "mascarpone", "ricotta",
        "valle", "kefir", "ymer",
    ),
    "laktose": (
        "mælk", "fløde", "smør", "yoghurt", "skyr", "creme fraiche", "cremefraiche", "ost", "flødeost",
        "hytteost", "ricotta", "mascarpone",
    ),
    "æg": ("æg", "æggeblomme", "æggehvide", "mayonnaise", "remoulade", "aioli", "bearnaise", "pasta frisk"),
    "fisk": (
        "fisk", "fiskefilet", "laks", "torsk", "sej", "rødspætte", "tun", "makrel", "sild", "ansjos",
        "fiskesauce", "ørred", "kuller",
    ),
    "skaldyr": ("rejer", "reje", "krabbe", "hummer", "muslinger", "musling", "østers", "blæksprutte", "skaldyr"),
    "soja": ("soja", "sojasauce", "tofu", "edamame", "miso", "tempeh"),
    "sesam": ("sesam", "tahin", "hummus"),
    "selleri": ("selleri", "bladselleri", "knoldselleri"),
    "sennep": ("sennep", "remoulade"),
    "svinekød": ("svin", "gris", "flæsk", "bacon", "skinke", "pølse", "medister", "kotelet", "mørbrad"),
}

_ALIASES = {
    "nuts": "nødder", "nut": "nødder", "nød": "nødder", "peanuts": "jordnødder", "milk": "mælk",
    "dairy": "mælk", "lactose": "laktose", "egg": "æg", "eggs": "æg", "fish": "fisk",
    "shellfish": "skaldyr", "soy": "soja", "sesame": "sesam", "celery": "selleri",
    "mustard": "sennep", "pork": "svinekød", "grisekød": "svinekød", "gris": "svinekød",
}

# Shorter words match too much inside Danish compounds ("æg" is in "pålæg", "rug" in "krug"),
# so they must match a whole word.
_SHORT_WORD = 3


def allergen_terms(allergy: str) -> tuple[str, ...]:
    key = normalize(allergy)
    key = _ALIASES.get(key, key)
    return _ALLERGEN_TERMS.get(key, (key,)) if key else ()


def _term_hits(term: str, text_words: list[str], text: str) -> bool:
    if " " in term:
        return term in text
    if len(term) <= _SHORT_WORD:
        return term in text_words
    return any(term in word for word in text_words)


def conflicting_allergy(text: str | None, allergies: list[str]) -> str | None:
    """Returns the allergy that the text conflicts with, or None."""
    normalized = normalize(text)
    if not normalized:
        return None
    text_words = words(normalized)
    for allergy in allergies:
        for term in allergen_terms(allergy):
            if _term_hits(normalize(term), text_words, normalized):
                return allergy
    return None


def recipe_conflict(ingredient_names: list[str], title: str, allergies: list[str]) -> str | None:
    for text in [title, *ingredient_names]:
        hit = conflicting_allergy(text, allergies)
        if hit:
            return hit
    return None
