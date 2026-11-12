"""A stand-in for Home Assistant behind the Supervisor, for trying the app in a browser.

It answers only what MadPlan asks: the configuration, states, services (AI Task answers with a
fixed reply) and the websocket command listing users. It is not Home Assistant: a change to how
MadPlan talks to Home Assistant still needs trying on a real one.
"""

import json

from fastapi import FastAPI, Request, WebSocket

app = FastAPI()
USERS = [
    {"id": "ha-owner", "name": "Owner", "is_owner": True, "group_ids": ["system-admin"]},
    {"id": "ha-family", "name": "Family", "is_owner": False, "group_ids": ["system-users"]},
]
STATE = {"language": "da"}


@app.get("/core/api/config")
def config():
    return {"language": STATE["language"], "location_name": "Home", "version": "2026.11.1"}


@app.get("/core/api/states")
def states():
    return [{"entity_id": "ai_task.openai_ai_task", "attributes": {"friendly_name": "OpenAI AI Task"}}]


@app.post("/core/api/services/{domain}/{service}")
async def service(domain: str, service: str, request: Request):
    body = await request.json()
    print("SERVICE", domain, service, json.dumps(body)[:300], flush=True)
    if domain == "ai_task":
        return {"changed_states": [], "service_response": {"data": json.dumps({"answer": "Try a lasagne tonight.", "offers": []})}}
    return []


@app.post("/set_language/{language}")
def set_language(language: str):
    """Switch Home Assistant's language, to see the app follow it."""
    STATE["language"] = language
    return STATE


@app.websocket("/core/websocket")
async def socket(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"type": "auth_required"})
    await ws.receive_json()
    await ws.send_json({"type": "auth_ok"})
    message = await ws.receive_json()
    await ws.send_json({"id": message["id"], "type": "result", "success": True, "result": USERS})
    await ws.close()
