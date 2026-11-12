"""A photo of the till receipt instead of MadPlan's estimate of what the trip cost.

The model's reading is untrusted: numbers outside what a supermarket receipt can hold are dropped,
and nothing is saved until a person has looked at the reading and confirmed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ShoppingList
from app.services.catalog import find_ingredient
from app.services.text import normalize, text_matches

MAX_LINES = 150
MAX_LINE_PRICE = 5000.0
MAX_TOTAL = 20000.0


@dataclass
class ReadLine:
    text: str
    name: str
    quantity: float | None
    unit: str | None
    price: float


def _other(receipt) -> float:
    other = getattr(receipt, "other", None)
    return round(other, 2) if other is not None and 0 < other <= 500 else 0.0


def clean(receipt) -> tuple[float | None, str | None, list[ReadLine]]:
    """Keeps what could be true of a supermarket receipt."""
    lines: list[ReadLine] = []
    for line in receipt.lines[:MAX_LINES]:
        price = line.price
        if price is None or not (0 < price <= MAX_LINE_PRICE):
            continue
        name = (line.name or line.text or "").strip()[:160]
        if not name:
            continue
        quantity = line.quantity if line.quantity is not None and 0 < line.quantity < 100000 else None
        unit = line.unit if line.unit in ("g", "kg", "ml", "l", "stk") else None
        lines.append(ReadLine((line.text or "")[:200], name, quantity, unit, round(price, 2)))
    total = receipt.total if receipt.total is not None and 0 < receipt.total <= MAX_TOTAL else None
    store = (receipt.store or "").strip()[:80] or None
    return (round(total, 2) if total is not None else None), store, lines


def _same(item_name: str, line: ReadLine) -> bool:
    ingredient = find_ingredient(item_name)
    found = find_ingredient(line.name)
    if ingredient is not None and found is not None:
        return ingredient.key == found.key
    a, b = normalize(item_name), normalize(line.name)
    return bool(a and b) and (a == b or text_matches(a, b) or text_matches(b, a))


def proposal(shopping: ShoppingList, receipt) -> dict:
    """The receipt matched against the list, for a person to check before anything is saved."""
    total, store, lines = clean(receipt)
    free = [i for i in shopping.items if not i.covered_by_pantry]
    used: set[int] = set()
    rows = []
    for line in lines:
        match = next((i for i in free if i.id not in used and _same(i.name, line)), None)
        if match is not None:
            used.add(match.id)
        rows.append(
            {
                "text": line.text,
                "name": line.name,
                "quantity": line.quantity,
                "unit": line.unit,
                "price": line.price,
                "item_id": match.id if match else None,
                "item_name": match.name if match else None,
            }
        )
    lines_sum = round(sum(line.price for line in lines), 2)
    # Deposits and bags are paid, but are not goods for the list or the pantry.
    other = _other(receipt)
    return {
        "store": store,
        "total": total,
        "lines_sum": lines_sum,
        "other": other,
        # When the lines do not add up to the total, something was not read, and people should know.
        "adds_up": total is not None and abs(total - lines_sum - other) <= 1,
        "lines": rows,
        "not_on_receipt": [{"id": i.id, "name": i.name} for i in free if i.id not in used and not i.manual],
    }
