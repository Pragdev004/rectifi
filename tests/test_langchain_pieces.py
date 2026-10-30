"""Small, direct checks of LangChain pieces that docs/CONCEPTS.md names: prompt templates (few-shot, placeholders), the output-parser
fallback, LCEL chains with `|`, RunnableLambda and RunnableParallel. Scripted models only."""

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda, RunnableParallel
from support.fakes import ScriptedModel

from rectifi.agent import classify
from rectifi.privacy import mask_sensitive


def test_the_classifier_prompt_is_system_then_few_shot_then_rows():
    prompt = classify._prompt("debit", ["Food & Dining", "Groceries", "Miscellaneous"])
    messages = prompt.invoke({"rows": "1. debit | amount 10.00 | payee: Zomato | narration: -"}).to_messages()
    assert messages[0].type == "system" and "Miscellaneous" in messages[0].content        # categories and fallback are filled in
    assert [m.type for m in messages[1:-1]] and {m.type for m in messages[1:-1]} <= {"human", "ai"}
    assert messages[-1].type == "human" and "Zomato" in messages[-1].content            # the rows come last
    assert all("Food & Dining" in m.content or "Groceries" in m.content or "Miscellaneous" in m.content
               for m in messages[2:-1] if m.type == "ai")                              # an example only uses categories that exist


def test_a_model_without_structured_output_is_asked_for_json_and_parsed_with_a_pydantic_parser():
    names = ["Food & Dining", "Miscellaneous"]
    schema = classify.label_schema(names)
    model = ScriptedModel(script=[AIMessage('{"labels": [{"i": 1, "category": "Food & Dining", "confidence": 0.9, "why": "cafe"}]}')])
    labels = classify._plain_json_fallback(model, "debit", names, "1. debit | amount 10.00 | payee: Cafe | narration: -", schema, None)
    assert labels[0].category == "Food & Dining"
    assert "Reply with JSON only" in "\n".join(str(m.content) for m in model.seen)                         # the format hint went into the prompt


def test_lcel_pipes_compose_with_a_masking_lambda_first():
    model = ScriptedModel(script=[AIMessage("ok")])
    chain = RunnableLambda(lambda text: mask_sensitive(text)) | model
    chain.invoke("my account 0012345678901 paid")
    seen = str(model.seen[-1].content)
    assert "0012345678901" not in seen and "8901" in seen


def test_runnable_parallel_runs_branches_on_the_same_input():
    both = RunnableParallel(upper=RunnableLambda(str.upper), size=RunnableLambda(len))
    assert both.invoke("abc") == {"upper": "ABC", "size": 3}
    assert HumanMessage("x").type == "human"
