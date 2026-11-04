"""Graph explorer API and the networks shown under chat answers (needs Neo4j)."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.chat import chat_agent
from app.graph import network

pytestmark = [pytest.mark.neo4j, pytest.mark.usefixtures("neo4j_ready")]


def test_network_shape_and_filters():
    r = network.network(["TDP1"])
    names = {n["name"]: n for n in r["nodes"]}
    assert {"ATM", "PRKDC", "TDP1"} <= set(names)
    assert names["ATM"]["kinase_group"] == "Atypical" and names["TDP1"]["is_seed"]
    atm = next(e for e in r["edges"] if e["source"] == names["ATM"]["id"])
    assert "SIGNOR" in atm["sources"] and atm["n_sources"] >= 2 and "Ser81" in atm["sites"]
    small = network.network(["TP53"], depth=2, limit=40)
    assert len(small["edges"]) <= 40 and small["truncated"] is True


def test_graph_api_requires_login_and_works(make_client):
    anon = make_client(None)
    assert anon.get("/api/graph/network?seeds=TDP1").status_code == 401
    user = make_client("user")
    r = user.get("/api/graph/network?seeds=TDP1&sources=SIGNOR").json()
    assert r["edges"] and all("SIGNOR" in e["sources"] for e in r["edges"])
    assert user.get("/api/graph/search?q=mapk").json()[0]["name"].startswith("MAPK")
    detail = user.get("/api/graph/interaction?enzyme=ATM&target=TDP1").json()
    assert {rec["source"] for rec in detail["interactions"][0]["records"]} >= {"SIGNOR"}
    meta = user.get("/api/graph/meta").json()
    assert "TK" in meta["kinase_groups"] and "PhosphoSitePlus" in meta["sources"]
    assert user.get("/api/graph/network?seeds=NOTAPROTEIN123").status_code == 400


def _tool_then_answer():
    """A chat model that calls regulators_of('TDP1') once, then answers."""
    def calls(messages):
        return not any(isinstance(m, ModelResponse) for m in messages)

    def fn(messages, info: AgentInfo):
        if calls(messages):
            return ModelResponse(parts=[ToolCallPart("regulators_of", {"target": "TDP1"})])
        return ModelResponse(parts=[TextPart("ATM and PRKDC (SIGNOR).")])

    async def stream(messages, info: AgentInfo):
        if calls(messages):
            yield {0: DeltaToolCall(name="regulators_of", json_args=json.dumps({"target": "TDP1"}))}
        else:
            yield "ATM and PRKDC (SIGNOR)."
    return FunctionModel(fn, stream_function=stream)


def test_chat_stream_includes_a_graph(make_client):
    c = make_client("user")
    with chat_agent().override(model=_tool_then_answer()):
        with c.stream("POST", "/api/vaults/default/chat", json={"message": "Who phosphorylates TDP1?"}) as r:
            raw = r.read().decode().replace("\r\n", "\n")
    graphs = [json.loads(b.split("data: ", 1)[1]) for b in raw.split("\n\n") if b.startswith("event: graph")]
    # one network from the pre-fetched context, one from the model's own tool call
    assert [g["tool"] for g in graphs] == ["context", "regulators_of"]
    for g in graphs:
        assert {"ATM", "PRKDC", "TDP1"} <= {n["name"] for n in g["nodes"]}
