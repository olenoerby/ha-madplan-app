"""The one gateway to AI. Every AI call in MadPlan goes through ``run_agent``.

The work is done by the Home Assistant AI Task entity chosen in the app's configuration, with
``ai_task.generate_data``. Which model it is, what it may cost and how it is limited are set in
that integration in Home Assistant. When AI is not set up or a call fails, callers get
``AiUnavailable`` and fall back to the deterministic code path, so the app keeps working.

Answers are untrusted. When a structured answer is wanted, it is asked for as JSON and checked
against the expected schema, and an answer that does not fit is refused like a failed call.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core import ha
from app.core.config import settings
from app.core.db import session_scope
from app.models import AiUsage
from app.services.app_settings import get_ai_settings
from app.settings_schema import AiSettings

log = logging.getLogger(__name__)

WEB_SEARCH = "web_search"


class AiUnavailable(Exception):
    """``reason`` is not_configured, feature_disabled or call_failed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class AgentSpec:
    name: str
    instructions: str
    input: Any
    # WEB_SEARCH when the task needs the web. Whether the agent can search is set in Home Assistant.
    tools: list[Any] = field(default_factory=list)
    output_type: Any = None
    context: Any = None
    model: str | None = None
    max_tokens: int | None = None


def ensure_allowed(db: Session, feature: str, household_id: int | None) -> tuple[AiSettings, str]:
    """Raises AiUnavailable unless AI is set up. Checked before every call."""
    ai = get_ai_settings(db)
    if not ai.enabled or not ai.entity_id:
        raise AiUnavailable("not_configured")
    if not getattr(ai.features, feature, False):
        raise AiUnavailable("feature_disabled", feature)
    return ai, ai.entity_id


_DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", re.S)
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _text_and_images(value: Any) -> tuple[str, list[tuple[str, bytes]]]:
    """The spec's input as text, and the images in it, from the forms the features use."""
    if isinstance(value, str):
        return value, []
    texts: list[str] = []
    images: list[tuple[str, bytes]] = []
    for item in value or []:
        content = item.get("content") if isinstance(item, dict) else None
        role = item.get("role") if isinstance(item, dict) else None
        if isinstance(content, str):
            texts.append(f"{role}: {content}" if role else content)
            continue
        for part in content or []:
            if part.get("type") == "input_text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "input_image":
                match = _DATA_URL.match(part.get("image_url", ""))
                if match:
                    images.append((match.group(1), base64.b64decode(match.group(2))))
    return "\n\n".join(texts), images


def _store_images(images: list[tuple[str, bytes]]) -> tuple[list[dict], list[Path]]:
    """Puts photos where Home Assistant's media source can hand them to the AI Task."""
    folder = Path(settings.media_dir) / "madplan"
    folder.mkdir(parents=True, exist_ok=True)
    attachments, paths = [], []
    for mime, data in images:
        name = f"{uuid.uuid4().hex}.{_EXTENSIONS[mime]}"
        path = folder / name
        path.write_bytes(data)
        paths.append(path)
        attachments.append({"media_content_id": f"media-source://media_source/local/madplan/{name}", "media_content_type": mime})
    return attachments, paths


def _schema_note(output_type: type[BaseModel]) -> str:
    schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
    return (
        "\n\nAnswer with one JSON object and nothing else: no prose, no code fences. "
        f"It must match this JSON schema: {schema}"
    )


def _parse(text: str, output_type: type[BaseModel]) -> BaseModel:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    return output_type.model_validate_json(cleaned[start : end + 1])


def run_agent(spec: AgentSpec, feature: str, household_id: int | None, user_id: int | None = None) -> Any:
    """Runs one task on Home Assistant's AI Task entity and returns its answer."""
    with session_scope() as db:
        _, entity_id = ensure_allowed(db, feature, household_id)
    text, images = _text_and_images(spec.input)
    instructions = spec.instructions + "\n\n" + text
    if WEB_SEARCH in spec.tools:
        instructions += "\n\nSearch the web for this, and only give what your search results show."
    if spec.output_type is not None:
        instructions += _schema_note(spec.output_type)
    paths: list[Path] = []
    ok = True
    try:
        attachments, paths = _store_images(images) if images else ([], [])
        answer = ha.generate_data(entity_id, spec.name, instructions, attachments)
        return _parse(answer, spec.output_type) if spec.output_type is not None else answer
    except (ha.HomeAssistantUnavailable, OSError) as exc:
        ok = False
        log.warning("AI call for %s failed: %s", feature, exc)
        raise AiUnavailable("call_failed", type(exc).__name__) from exc
    except (ValueError, ValidationError) as exc:
        ok = False
        log.warning("AI answer for %s did not fit: %s", feature, exc)
        raise AiUnavailable("call_failed", "bad_answer") from exc
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        with session_scope() as db:
            # What was used is counted by the AI integration in Home Assistant. This is what it was for.
            db.add(AiUsage(feature=feature, model=entity_id, user_id=user_id, requests=1, ok=ok))
