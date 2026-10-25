"""/api routes for chat. F1 created this file empty; its workstream fills it in.

A route validates input, calls one function in tools (or imports rectifi.agent / rectifi.rag lazily inside the handler)
and shapes the answer with api/wire.py. Importing this module must not load langchain.
"""

from __future__ import annotations

from starlette.routing import Route

routes: list[Route] = []
