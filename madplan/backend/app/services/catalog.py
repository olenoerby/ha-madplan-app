"""Known ingredients: how to recognise them in an offer, and a rough normal price.

``price`` is kroner per kg, litre or piece. These are MadPlan's own estimates, used only when
no real price is known, and the UI labels them as estimates. They were calibrated in November
2026 against the shelf prices of Danish supermarkets, and real prices always win over them.

``excludes`` come from auditing matches against real offers. To repeat that, see
docs/development.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.services.text import normalize, text_matches, words


@dataclass(frozen=True)
class Ingredient:
    key: str
    terms: tuple[str, ...]
    unit: str  # base unit used in recipes: g, ml or stk
    price: float  # estimated normal price per kg, litre or piece
    pack: float  # typical pack size in the base unit
    excludes: tuple[str, ...] = ()
    staple: bool = False  # assumed to be in the cupboard unless the household says otherwise
    protein: str | None = None
    carb_heavy: bool = False
    crawl: bool = True  # worth searching offers for
    piece_g: int | None = None  # what one weighs, for comparing "3 onions" with "a 750 g net"
    section: str = "other"  # where it usually is in a Danish supermarket, for sorting the list


def _i(key, terms, unit, price, pack, **kw) -> Ingredient:
    return Ingredient(key, tuple(terms) or (key,), unit, price, pack, **kw)


# Words that mean a product is not the plain food a recipe asks for, whatever else its name says.
# An ingredient with the word in its own name is exempt, so pizza dough can still be "pizzadej".
NOT_THE_PLAIN_FOOD = (
    # Not for eating
    "katte", "hunde", "foder", "godbid", "plante", "stikløg", "blomsterløg", "frø", "spiring", "perler", "velour", "forfang",
    # Baby food
    "babymad", "babymos", "mums",
    # Ready-made
    "indbagt", "færdigret", "tærte", "smøreost", "friskost", "granulat", "iste", "fromage", "mousse", "chips", "snack", "pizza",
    "lasagne", "suppe", "dressing", "salat", "sauce", "gratin", "marineret", "fries", "pommes", "bites", "pålæg", "skiveskåret",
)

_PROCESSED = ("pålæg", "salat", "postej", "paté", "chips", "snack", "kiks", "hundemad", "kattemad", "foder")

INGREDIENTS: tuple[Ingredient, ...] = (
    # Meat
    _i("hakket oksekød", ["hakket oksekød", "oksekød hakket", "hakket okse", "oksefars"], "g", 110, 500, protein="beef", excludes=_PROCESSED + ("burger", "bøf"), section="meat"),
    _i("hakket svinekød", ["hakket svinekød", "hakket gris", "svinefars"], "g", 80, 500, protein="pork", excludes=_PROCESSED, section="meat"),
    _i("hakket kylling", ["hakket kylling", "kyllingefars", "hakket kyllingekød"], "g", 85, 400, protein="chicken", excludes=_PROCESSED, section="meat"),
    _i("kyllingebryst", ["kyllingebryst", "kyllingefilet", "kyllingebrystfilet"], "g", 110, 600, protein="chicken", excludes=_PROCESSED + ("toast",), section="meat"),
    _i("kyllingelår", ["kyllingelår", "kyllingeoverlår", "kyllingeunderlår"], "g", 45, 800, protein="chicken", excludes=_PROCESSED, section="meat"),
    _i("hel kylling", ["hel kylling", "helstegt kylling"], "g", 40, 1300, protein="chicken", excludes=_PROCESSED, section="meat"),
    _i("flæskesteg", ["flæskesteg"], "g", 65, 1500, protein="pork", excludes=_PROCESSED, section="meat"),
    _i("svinemørbrad", ["svinemørbrad", "mørbrad"], "g", 110, 500, protein="pork", excludes=_PROCESSED + ("okse",), section="meat"),
    _i("koteletter", ["kotelet", "koteletter", "nakkekotelet"], "g", 95, 800, protein="pork", excludes=_PROCESSED, section="meat"),
    _i("medister", ["medister", "medisterpølse"], "g", 85, 500, protein="pork", excludes=_PROCESSED, section="meat"),
    _i("bacon", ["bacon"], "g", 90, 150, protein="pork", excludes=("chips", "snack", "pålæg"), section="meat"),
    _i("pølser", ["pølser", "wienerpølser", "grillpølser", "frankfurter"], "g", 70, 400, protein="pork", excludes=("hund", "kat"), section="meat"),
    _i("oksekød i tern", ["oksekød i tern", "oksetern", "gullasch"], "g", 170, 500, protein="beef", excludes=_PROCESSED, section="meat"),
    _i("lammekølle", ["lammekølle"], "g", 125, 800, protein="lamb", excludes=_PROCESSED, section="meat"),
    _i("lammefars", ["lammefars", "hakket lam", "hakket lammekød"], "g", 110, 400, protein="lamb", excludes=_PROCESSED, section="meat"),
    _i("stegeflæsk", ["stegeflæsk", "flæsk i skiver"], "g", 95, 500, protein="pork", excludes=_PROCESSED, section="meat"),
    _i("skinke", ["skinke", "skinketern", "kogt skinke", "røget skinke"], "g", 150, 150, protein="pork", excludes=("ost", "salat"), section="meat"),
    # Fish
    _i("laks", ["laks", "laksefilet", "laksesider"], "g", 175, 400, protein="fish", excludes=("pulled", "i skiver", "koldrøget", "varmrøget", "paté", "røget", "gravad", "pålæg", "salat", "postej", "kat"), section="fish"),
    _i("torsk", ["torsk", "torskefilet"], "g", 220, 400, protein="fish", excludes=("rogn", "kat"), section="fish"),
    _i("fiskefilet", ["fiskefilet", "rødspættefilet", "panerede fiskefileter", "sejfilet"], "g", 150, 400, protein="fish", excludes=("kat", "hund"), section="fish"),
    _i("rejer", ["rejer", "tigerrejer"], "g", 170, 250, protein="fish", excludes=("ost", "skinkeost", "salat", "chips"), section="fish"),
    _i("tun på dåse", ["tun"], "g", 105, 185, protein="fish", excludes=("kat", "salat", "tunge"), section="fish"),
    _i("makrel på dåse", ["makrel"], "g", 130, 125, protein="fish", excludes=("kat",), section="fish"),
    # Dairy and eggs
    _i("mælk", ["mælk", "letmælk", "minimælk", "sødmælk", "skummetmælk"], "ml", 12, 1000, excludes=("kokos", "mandel", "havre", "soja", "chokolade", "kakao", "kondens", "pulver"), section="dairy"),
    _i("fløde", ["fløde", "piskefløde", "madlavningsfløde", "kaffefløde"], "ml", 50, 250, excludes=("is", "ost", "kage"), section="dairy"),
    _i("creme fraiche", ["creme fraiche", "cremefraiche"], "ml", 50, 250, section="dairy"),
    _i("smør", ["smør"], "g", 115, 250, excludes=("kage", "kiks", "bar"), section="dairy"),
    _i("yoghurt", ["yoghurt", "skyr"], "ml", 15, 1000, excludes=("is", "drik"), section="dairy"),
    _i("æg", ["æg"], "stk", 3.5, 10, excludes=("påske", "chokolade", "kinder", "nudler", "salat"), section="dairy"),
    _i("revet ost", ["revet ost", "pizzatopping", "revet mozzarella"], "g", 105, 200, section="dairy"),
    _i("skæreost", ["skæreost", "ost", "gouda", "havarti", "danbo"], "g", 85, 500, excludes=("revet", "salatost", "buko", "madlavning", "pizzatopping", "kiks", "snack", "chips", "kage", "flødeost", "hytteost", "kat", "pops"), section="dairy"),
    _i("mozzarella", ["mozzarella"], "g", 100, 125, excludes=("revet",), section="dairy"),
    _i("parmesan", ["parmesan", "grana padano"], "g", 200, 150, section="dairy"),
    _i("feta", ["feta", "fetaost", "salatost"], "g", 110, 200, section="dairy"),
    _i("hytteost", ["hytteost"], "g", 48, 250, section="dairy"),
    _i("flødeost", ["flødeost", "philadelphia"], "g", 80, 150, section="dairy"),
    # Vegetables
    _i("løg", ["løg", "gule løg"], "stk", 2, 1, excludes=("rødløg", "forårsløg", "løgismose", "ristede", "syltede", "løgsuppe", "tærte"), piece_g=120, section="produce"),
    _i("rødløg", ["rødløg", "røde løg"], "stk", 2.5, 1, excludes=("syltede",), piece_g=120, section="produce"),
    _i("forårsløg", ["forårsløg"], "stk", 9, 1, section="produce"),
    _i("hvidløg", ["hvidløg"], "stk", 5, 1, excludes=("crouton", "paté", "philadelphia", "mayonnaise", "brød", "dressing", "olie", "smør", "flûte"), piece_g=50, section="produce"),
    _i("kartofler", ["kartofler", "bagekartofler", "nye kartofler"], "g", 14, 2000, carb_heavy=True, excludes=("glas", "svesker", "glaskartofler", "chips", "pulver", "mos", "bådkartofler"), section="produce"),
    _i("søde kartofler", ["søde kartofler", "sødekartofler"], "g", 28, 750, carb_heavy=True, excludes=("chips",), section="produce"),
    _i("gulerødder", ["gulerødder", "gulerod"], "g", 14, 1000, excludes=("kage", "juice"), section="produce"),
    _i("broccoli", ["broccoli"], "stk", 16, 1, excludes=("potato", "cheese", "blomkålsris", "rice", "ris", "frost",), piece_g=450, section="produce"),
    _i("blomkål", ["blomkål"], "stk", 18, 1, excludes=("blomkålsris", "broccoli", "gratin", "ris"), piece_g=900, section="produce"),
    _i("spidskål", ["spidskål"], "stk", 16, 1, piece_g=800, section="produce"),
    _i("hvidkål", ["hvidkål"], "stk", 14, 1, piece_g=1500, section="produce"),
    _i("rødkål", ["rødkål"], "stk", 16, 1, excludes=("syltet", "glas"), piece_g=1200, section="produce"),
    _i("porrer", ["porrer", "porre"], "stk", 6, 1, piece_g=200, excludes=("farserede", "farseret",), section="produce"),
    _i("peberfrugt", ["peberfrugt", "peberfrugter"], "stk", 8, 1, excludes=("fyldte", "veggie", "pulver",), piece_g=180, section="produce"),
    _i("squash", ["squash", "zucchini"], "stk", 8, 1, piece_g=300, excludes=("sodavand", "tuborg", "harboe", "orange", "appelsin", "sport", "fersken", "cider", "sukkerfri", "light", "energi", "ex pant", "spaghetti",), section="produce"),
    _i("aubergine", ["aubergine"], "stk", 10, 1, piece_g=350, section="produce"),
    _i("tomater", ["tomater", "cherrytomater", "tomat"], "g", 55, 500, excludes=("tun", "makrel", "flåede", "hakkede", "soltørrede", "ketchup", "suppe", "sauce", "puré", "juice", "dåse"), section="produce"),
    _i("agurk", ["agurk"], "stk", 10, 1, excludes=("syltede", "salat"), piece_g=350, section="produce"),
    _i("salat", ["iceberg", "hjertesalat", "romainesalat", "salathoved", "blandet salat"], "stk", 12, 1, piece_g=400, section="produce"),
    _i("spinat", ["spinat", "babyspinat"], "g", 55, 200, excludes=("tærte", "lasagne"), section="produce"),
    _i("champignon", ["champignon", "champignoner"], "g", 64, 250, excludes=("sauce", "suppe"), section="produce"),
    _i("majs", ["majs", "majskolber"], "g", 32, 340, excludes=("chips", "stivelse", "mel", "cornflakes", "pop"), section="produce"),
    _i("grønne bønner", ["grønne bønner", "haricots verts"], "g", 25, 400, excludes=("kidney", "lima", "edamame",), section="produce"),
    _i("ærter", ["ærter", "frosne ærter"], "g", 35, 450, excludes=("kikærter", "sukkerærter", "suppe",), section="produce"),
    _i("rødbeder", ["rødbeder"], "g", 30, 750, excludes=("syltede",), section="produce"),
    _i("citron", ["citron", "citroner"], "stk", 5, 1, excludes=("urtete", "ingefær", "instant", "yoghurt", "bites", "marmelade", "is", "kage", "vand", "te", "juice", "saft", "sodavand"), piece_g=120, section="produce"),
    _i("avocado", ["avocado", "avocadoer"], "stk", 10, 1, excludes=("olie",), piece_g=200, section="produce"),
    # Fruit
    _i("æbler", ["æbler"], "g", 15, 1000, excludes=("rynkeby", "sunjoy", "saftige", "16 æbler", "most", "juice", "kage", "mos", "chips"), section="produce"),
    _i("bananer", ["bananer"], "g", 25, 1000, excludes=("pære", "yoghurt", "chips", "kage", "split"), section="produce"),
    _i("bær", ["blåbær", "hindbær", "jordbær", "frosne bær", "bærblanding"], "g", 70, 300, excludes=("jordbær yoghurt", "danonino", "yoghurt", "danonino", "yoghurt", "marmelade", "saft", "is", "syltetøj", "kage", "te"), section="produce"),
    # Canned, dry and starches
    _i("hakkede tomater", ["hakkede tomater", "flåede tomater"], "g", 17, 400, crawl=True, section="dry"),
    _i("tomatpuré", ["tomatpuré"], "g", 60, 140, section="dry"),
    _i("kokosmælk", ["kokosmælk"], "ml", 25, 400, section="dry"),
    _i("kikærter", ["kikærter"], "g", 33, 400, protein="legumes", section="dry"),
    _i("linser", ["linser", "røde linser"], "g", 42, 500, protein="legumes", excludes=("kontakt", "hummus"), section="dry"),
    _i("kidneybønner", ["kidneybønner", "bønner i chili"], "g", 31, 400, protein="legumes", section="dry"),
    _i("sorte bønner", ["sorte bønner"], "g", 30, 400, protein="legumes", section="dry"),
    _i("tofu", ["tofu"], "g", 70, 280, protein="legumes", section="dry"),
    _i("ris", ["ris", "jasminris", "basmatiris", "langkornet ris"], "g", 17, 1000, carb_heavy=True, excludes=("kage", "mælk", "chips", "kiks", "pops", "blomkål", "bar"), section="dry"),
    _i("spaghetti", ["spaghetti"], "g", 16, 500, carb_heavy=True, excludes=("squash", "græskar", "napoli", "miracoli", "bolognese",), section="dry"),
    _i("pasta", ["pasta", "penne", "fusilli", "farfalle", "makaroni"], "g", 24, 500, carb_heavy=True, excludes=("sauce", "salat", "pesto"), section="dry"),
    _i("lasagneplader", ["lasagneplader", "lasagne plader"], "g", 20, 500, carb_heavy=True, section="dry"),
    _i("nudler", ["nudler", "ægnudler"], "g", 40, 250, carb_heavy=True, excludes=("smag", "kopnudler", "cupnudler", "kylling", "oksekød", "chili", "suppe",), section="dry"),
    _i("couscous", ["couscous"], "g", 38, 500, carb_heavy=True, section="dry"),
    _i("bulgur", ["bulgur"], "g", 35, 500, carb_heavy=True, section="dry"),
    _i("havregryn", ["havregryn"], "g", 14, 1000, carb_heavy=True, excludes=("bar", "kiks", "drik"), section="dry"),
    _i("mel", ["hvedemel", "mel"], "g", 8, 1000, carb_heavy=True, staple=True, crawl=False, excludes=("melon", "karamel", "mælk"), section="dry"),
    _i("tortilla", ["tortilla", "wraps", "tortillapandekager"], "stk", 4, 8, carb_heavy=True, excludes=("chips",), section="dry"),
    _i("pitabrød", ["pita", "pitabrød"], "stk", 3, 6, carb_heavy=True, section="dry"),
    _i("burgerboller", ["burgerboller", "burgerbrød"], "stk", 4, 4, carb_heavy=True, section="dry"),
    _i("rugbrød", ["rugbrød"], "g", 30, 1000, carb_heavy=True, excludes=("kiks", "chips"), section="dry"),
    _i("franskbrød", ["franskbrød", "toastbrød", "toast"], "g", 30, 500, carb_heavy=True, section="dry"),
    _i("pizzadej", ["pizzadej"], "stk", 18, 1, carb_heavy=True, section="dry"),
    _i("tortillachips", ["tortillachips", "nachos"], "g", 60, 200, section="dry"),
    _i("taco krydderi", ["tacokrydderi", "taco krydderi"], "stk", 10, 1, section="dry"),
    _i("salsa", ["salsa", "tacosauce"], "g", 60, 230, section="dry"),
    _i("pesto", ["pesto"], "g", 120, 190, section="dry"),
    _i("mayonnaise", ["mayonnaise"], "g", 60, 375, section="dry"),
    _i("sojasauce", ["sojasauce"], "ml", 80, 150, staple=True, crawl=False, section="dry"),
    # Pantry staples, assumed to be at home and not priced
    _i("olie", ["olivenolie", "rapsolie", "olie"], "ml", 80, 500, staple=True, crawl=False, excludes=("sardin", "tun"), section="dry"),
    _i("salt", ["salt"], "g", 10, 1000, staple=True, crawl=False, excludes=("saltlakrids", "chips"), section="dry"),
    _i("peber", ["peber", "sort peber"], "g", 200, 50, staple=True, crawl=False, excludes=("peberfrugt", "peberrod", "peberkorn sauce", "pebernødder"), section="dry"),
    _i("sukker", ["sukker"], "g", 12, 1000, staple=True, crawl=False, excludes=("sukkerfri", "sukkerærter", "uden"), section="dry"),
    _i("krydderier", ["paprika", "karry", "spidskommen", "oregano", "timian", "basilikum", "kanel", "chiliflager", "garam masala", "laurbærblad", "persille", "karse"], "g", 300, 30, staple=True, crawl=False, section="dry"),
    _i("bouillon", ["bouillon", "hønsefond", "oksefond"], "stk", 3, 10, staple=True, crawl=False, section="dry"),
    _i("eddike", ["eddike", "balsamico"], "ml", 30, 500, staple=True, crawl=False, section="dry"),
    _i("sennep", ["sennep", "dijonsennep"], "g", 50, 200, staple=True, crawl=False, section="dry"),
    # Everyday extras
    _i("kaffe", ["kaffe", "formalet kaffe", "kaffebønner"], "g", 120, 400, excludes=("iskaffe", "cocio", "kakao", "maskine", "fløde", "kapsel", "filter", "kage"), section="dry"),
    _i("te", ["te", "teposer"], "stk", 1.5, 20, crawl=False, excludes=("citron",), section="dry"),
    _i("juice", ["juice", "appelsinjuice", "æblejuice"], "ml", 20, 1000, section="drinks"),
    _i("cola", ["coca cola", "cola", "pepsi"], "ml", 14, 1500, section="drinks"),
    _i("danskvand", ["danskvand"], "ml", 7, 1500, section="drinks"),
    _i("øl", ["øl", "pilsner"], "ml", 16, 330, excludes=("mørk", "fri"), section="drinks"),
    _i("rødvin", ["rødvin"], "ml", 75, 750, section="drinks"),
    _i("nødder", ["nødder", "mandler", "cashewnødder", "peanuts"], "g", 100, 200, section="snacks"),
    _i("mørk chokolade", ["mørk chokolade", "chokolade"], "g", 120, 100, excludes=("mælk", "kage", "is"), section="snacks"),
    _i("popcorn", ["popcorn"], "g", 40, 200, section="snacks"),
    _i("kiks", ["kiks"], "g", 50, 200, section="snacks"),
    _i("hummus", ["hummus"], "g", 90, 200, section="snacks"),
    _i("honning", ["honning"], "g", 100, 350, section="snacks"),
    _i("marmelade", ["marmelade", "syltetøj"], "g", 45, 400, section="snacks"),
    _i("pålæg", ["pålæg", "skinkepålæg", "rullepølse", "leverpostej"], "g", 120, 150, protein="pork", section="snacks"),
)

_BY_KEY = {ingredient.key: ingredient for ingredient in INGREDIENTS}
_INDEX: dict[str, Ingredient] = {}
for _ingredient in INGREDIENTS:
    for _term in (_ingredient.key, *_ingredient.terms):
        _INDEX.setdefault(normalize(_term), _ingredient)

CRAWL_TERMS: tuple[str, ...] = tuple(i.key for i in INGREDIENTS if i.crawl)

# Roughly how many days fresh food keeps from the day it is bought, on the careful side. It gives
# the waste agent something to work with, as nobody types in dates, and the pantry shows these as
# estimates. Anything not listed keeps for months, or there is no telling.
_KEEPS_DAYS: dict[str, int] = {
    "hakket oksekød": 3, "hakket svinekød": 3, "hakket kylling": 2, "kyllingebryst": 3, "kyllingelår": 3,
    "hel kylling": 3, "flæskesteg": 4, "svinemørbrad": 4, "koteletter": 4, "medister": 4, "bacon": 14,
    "pølser": 14, "oksekød i tern": 4, "lammekølle": 5, "lammefars": 3, "stegeflæsk": 5, "skinke": 7,
    "laks": 2, "torsk": 2, "fiskefilet": 2, "rejer": 3,
    "mælk": 7, "fløde": 10, "creme fraiche": 14, "yoghurt": 14, "æg": 28, "revet ost": 21, "skæreost": 30,
    "mozzarella": 10, "feta": 30, "hytteost": 10, "flødeost": 21,
    "forårsløg": 7, "broccoli": 7, "blomkål": 10, "spidskål": 14, "porrer": 14, "peberfrugt": 10, "squash": 10,
    "aubergine": 7, "tomater": 7, "agurk": 7, "salat": 5, "spinat": 4, "champignon": 5, "grønne bønner": 5,
    "citron": 21, "avocado": 4, "æbler": 30, "bananer": 5, "bær": 3, "tofu": 14,
    "rugbrød": 7, "franskbrød": 4, "pitabrød": 5, "burgerboller": 4, "hummus": 5, "pålæg": 7,
}
_FROZEN = ("frost", "frosne", "frossen", "frosset")


def _contains(word: str, text: str, text_words: list[str]) -> bool:
    """Short words must match whole words: "is" should catch ice cream, not "pris"."""
    return word in text_words if len(word) <= 4 else word in text


def _excluded(ingredient: Ingredient, text: str) -> bool:
    text_words = text.split()
    if any(_contains(normalize(word), text, text_words) for word in ingredient.excludes):
        return True
    own_name = normalize(" ".join((ingredient.key, *ingredient.terms)))
    return any(word not in own_name and _contains(word, text, text_words) for word in NOT_THE_PLAIN_FOOD)


@lru_cache(maxsize=4096)
def find_ingredient(name: str | None) -> Ingredient | None:
    """Maps a free-text ingredient to the catalog, for recipes from Mealie and the AI too."""
    normalized = normalize(name)
    if not normalized:
        return None
    if normalized in _INDEX:
        return _INDEX[normalized]
    best: tuple[int, Ingredient] | None = None
    for term, ingredient in _INDEX.items():
        if text_matches(term, normalized) and not _excluded(ingredient, normalized):
            specificity = len(term)
            if best is None or specificity > best[0]:
                best = (specificity, ingredient)
    return best[1] if best else None


def keeps_days(name: str | None) -> int | None:
    """How long something keeps from the day it is bought, or None when there is no estimate."""
    ingredient = find_ingredient(name)
    if ingredient is None or any(word in normalize(name) for word in _FROZEN):
        return None
    return _KEEPS_DAYS.get(ingredient.key)


def is_excluded(ingredient: Ingredient, text: str) -> bool:
    return _excluded(ingredient, normalize(text))


def offer_matches(ingredient: Ingredient, search_text: str) -> bool:
    if _excluded(ingredient, search_text):
        return False
    return any(text_matches(term, search_text) for term in ingredient.terms)


def significant_words(name: str) -> str:
    """The words of an unknown ingredient that are worth searching offers for."""
    return " ".join(w for w in words(name) if len(w) > 2)



# Store sections in the order most Danish supermarkets are walked: fruit and vegetables by the door,
# then bread, meat, fish, the dairy cooler, the dry goods, the freezers, drinks and household goods.
SECTION_ORDER = ("produce", "bakery", "meat", "fish", "dairy", "dry", "frozen", "drinks", "snacks", "household", "other")

# For what is not in the catalog, such as toilet paper typed on a list by hand.
_SECTION_WORDS = (
    ("household", ("toiletpapir", "køkkenrulle", "opvask", "vaskemiddel", "skyllemiddel", "sæbe", "tandpasta", "shampoo", "bleer", "affaldsposer", "fryseposer", "husholdningsfilm", "stanniol", "batterier", "pærer")),
    ("frozen", ("frost", "frossen", "frosne", "ispinde", "is ")),
    ("bakery", ("brød", "boller", "rugbrød", "toast", "bagel", "pitabrød", "tortilla", "wienerbrød", "croissant")),
    ("drinks", ("vand", "sodavand", "saft", "vin", "øl", "juice", "mælkedrik")),
    ("snacks", ("chips", "slik", "kiks", "chokolade", "lakrids", "vingummi")),
)


def section_of(name: str | None) -> str:
    """Where in the store something is likely to be. "other" when there is no telling."""
    ingredient = find_ingredient(name)
    text = f"{normalize(name)} "
    for section, needles in _SECTION_WORDS:
        if section in ("bakery", "frozen") and any(n in text for n in needles):
            return section
    if ingredient is not None:
        return ingredient.section
    for section, needles in _SECTION_WORDS:
        if any(n in text for n in needles):
            return section
    return "other"
