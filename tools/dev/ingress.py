"""Plays the Supervisor's ingress: strips its path, says who the user is, and passes the rest on.

INGRESS_USER is "id:username:display name", such as "ha-owner:owner:Owner".
"""

import os

import httpx
from fastapi import FastAPI, Request, Response

PREFIX = "/api/hassio_ingress/abc123"
USER = os.environ["INGRESS_USER"].split(":")
APP_URL = os.environ.get("MADPLAN_URL", "http://127.0.0.1:8099")
app = FastAPI()
client = httpx.AsyncClient(base_url=APP_URL, timeout=60)


@app.api_route(PREFIX + "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    headers.update({"X-Ingress-Path": PREFIX, "X-Remote-User-Id": USER[0], "X-Remote-User-Name": USER[1], "X-Remote-User-Display-Name": USER[2]})
    response = await client.request(request.method, "/" + path, params=request.query_params, content=await request.body(), headers=headers)
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    return Response(response.content, status_code=response.status_code, headers={k: v for k, v in response.headers.items() if k.lower() not in excluded})
