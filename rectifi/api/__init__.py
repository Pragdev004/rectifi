"""The web API: a thin Starlette layer over rectifi.tools so the browser app (web/) can use Rectifi.

It is a third front door next to the agent and the MCP server (CLAUDE.md rule 13): it does no money maths, every
amount crosses the wire as a string, and every /api request passes the guards in security.py.
Importing this package must not import rectifi.agent or langchain (a test checks that); the Ask routes load them lazily.
"""
