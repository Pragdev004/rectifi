"""The read-only toolkit and build_graph(tools=...): a write tool must be impossible in the limited agent."""

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseToolkit

from rectifi import tools
from rectifi.agent import graph as agent_graph
from rectifi.agent.contracts import build_rag_subgraph
from rectifi.agent.graph import READ_ONLY_NAMES, READ_ONLY_TOOLS, RectifiReadOnlyToolkit, chat
from rectifi.agent.tools import AGENT_TOOLS
from rag_support import RuleModel
from support.fakes import BowEmbeddings

WRITE_TOOLS = {"log_transaction", "log_sms", "set_budget", "recategorize", "save_commitment"}


def test_toolkit_holds_exactly_the_read_only_tools():
    kit = RectifiReadOnlyToolkit()
    assert isinstance(kit, BaseToolkit)
    names = [t.name for t in kit.get_tools()]
    assert names == list(READ_ONLY_NAMES) and not WRITE_TOOLS & set(names)
    assert {t.name for t in AGENT_TOOLS} == set(names) | WRITE_TOOLS              # every agent tool is either read-only or a known write tool


def test_limited_agent_refuses_a_write_call_and_writes_nothing():
    call = AIMessage("", tool_calls=[{"name": "log_transaction", "args": {"amount": "500", "direction": "debit"}, "id": "c1"}])
    model = RuleModel(messages_script=[call, AIMessage("Sorry, I can only read.")])
    g = agent_graph.build_graph(model, tools=READ_ONLY_TOOLS)
    new = chat(g, "log 500 for lunch", "t-ro")
    assert model.bound == list(READ_ONLY_NAMES)
    assert "Unknown tool log_transaction" in new[2].content and new[2].status == "error"
    assert tools.list_transactions(limit=5) == []


def test_default_agent_still_has_every_tool():
    call = AIMessage("", tool_calls=[{"name": "log_transaction", "args": {"amount": "500", "direction": "debit", "payee": "lunch"}, "id": "c1"}])
    model = RuleModel(messages_script=[call, AIMessage("Saved.")])
    g = agent_graph.build_graph(model)
    chat(g, "spent 500 on lunch", "t-all")
    assert {t.name for t in AGENT_TOOLS} <= set(model.bound)
    assert len(tools.list_transactions(limit=5)) == 1


def test_the_contract_function_builds_the_graph():
    llm = RuleModel()
    g = build_rag_subgraph(llms={"grade": llm, "chat": llm}, embeddings=BowEmbeddings())
    assert {"decide_retrieval", "retrieve", "grade_one", "refine", "check_support", "finalize"} <= set(g.get_graph().nodes)
