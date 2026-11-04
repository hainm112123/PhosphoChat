"""Claims from a vault's sources: grounded recording, status vs the databases, projection (needs Neo4j)."""

from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.graph import network, signor
from app.knowledge import claims
from app.vault.store import VaultError

pytestmark = [pytest.mark.neo4j, pytest.mark.usefixtures("neo4j_ready")]

NOTE = ("ATM phosphorylates TDP1 at Ser81 after DNA damage, which stabilises TDP1. "
        "We also found that ATM phosphorylates TDP1 at S999 in our assay.")


@pytest.fixture
def src(vault):
    yield vault.save_source(title="ATM and TDP1", body=NOTE, source_url="pasted", content_type="note")
    signor._get_driver().execute_query("MATCH ()-[r:CLAIMED {vault: $v}]->() DELETE r", v=vault.name)


def _rec(vault, src, **kw):
    args = dict(enzyme="ATM", target="TDP1", site="S81", mechanism="phosphorylation", effect="up-regulates",
                source=src, quote="ATM phosphorylates TDP1 at Ser81 after DNA damage")
    args.update(kw)
    return claims.record(vault, **args)


def test_record_validates_and_normalises(vault, src):
    claim, added = _rec(vault, src)
    assert added and claim["site"] == "Ser81" and claim["enzyme_id"] == "Q13315" and claim["review"] == "unreviewed"
    assert _rec(vault, src)[1] is False                                  # duplicate ignored
    for bad, match in [({"enzyme": "NOTAPROTEIN123"}, "enzyme"), ({"site": "X12"}, "Site"),
                       ({"source": "missing.md"}, "Unknown source"),
                       ({"quote": "ATM phosphorylates TP53 at Ser15"}, "doesn't appear")]:
        with pytest.raises(VaultError, match=match):
            _rec(vault, src, **bad)
    assert len(vault.read_relations()) == 1


def test_statuses_against_databases(vault, src):
    _rec(vault, src)                                                        # ATM -> TDP1 Ser81: in SIGNOR
    _rec(vault, src, site="S999", quote="ATM phosphorylates TDP1 at S999 in our assay")
    _rec(vault, src, site=None, effect="down-regulates", quote="ATM phosphorylates TDP1 at Ser81")
    by_site = {(c["site"], c["effect"]): c for c in claims.list_claims(vault)}
    assert by_site[("Ser81", "up-regulates")]["status"] == "confirmed"
    assert "SIGNOR" in by_site[("Ser81", "up-regulates")]["databases"]
    assert by_site[("Ser999", "up-regulates")]["status"] == "new"
    assert by_site[(None, "down-regulates")]["status"] == "conflicting"


def test_sync_projects_claims_without_touching_database_edges(vault, src):
    before = signor._run("MATCH ()-[m:MODIFIES]->() RETURN count(m) AS n")[0]["n"]
    _rec(vault, src)
    _rec(vault, src, site="S999", quote="ATM phosphorylates TDP1 at S999 in our assay")
    assert claims.sync_claims(vault) == 2
    assert claims.sync_claims(vault) == 2                                   # replaced, not duplicated
    rows = signor._run("MATCH ()-[r:CLAIMED {vault: $v}]->() RETURN count(r) AS n", v=vault.name)
    assert rows[0]["n"] == 2
    assert signor._run("MATCH ()-[m:MODIFIES]->() RETURN count(m) AS n")[0]["n"] == before


def test_network_draws_claims_and_hides_rejected(vault, src):
    claim, _ = _rec(vault, src, site="S999", quote="ATM phosphorylates TDP1 at S999 in our assay")
    net = network.network(["TDP1"], claims=claims.list_claims(vault))
    edge = next(e for e in net["edges"] if e.get("claim"))
    assert edge["claim_status"] == "new" and edge["sources"] == ["your source"] and "Ser999" in edge["sites"]
    vault.update_relation(claim["id"], review="rejected")
    assert claims.list_claims(vault) == []
    assert not any(e.get("claim") for e in network.network(["TDP1"], claims=claims.list_claims(vault))["edges"])


async def test_failed_capture_records_no_claims(vault):
    from datetime import date

    from app.agents.jobs import job_agent, run_job
    from app.vault.store import StagedSource
    staged = StagedSource(title="ATM note", body=NOTE, source_url="pasted", content_type="note")
    src = f"{date.today().isoformat()}-atm-note.md"
    call = {"enzyme": "ATM", "target": "TDP1", "site": "S81", "source": src,
            "quote": "ATM phosphorylates TDP1 at Ser81 after DNA damage"}

    def fn(messages, info: AgentInfo):
        if sum(isinstance(m, ModelResponse) for m in messages):
            raise RuntimeError("model exploded")
        return ModelResponse(parts=[ToolCallPart("record_relation", call)])

    async def stream(messages, info: AgentInfo):
        if sum(isinstance(m, ModelResponse) for m in messages):
            raise RuntimeError("model exploded")
        yield {0: DeltaToolCall(name="record_relation", json_args=json.dumps(call))}

    async def emit(ev):
        pass
    with job_agent().override(model=FunctionModel(fn, stream_function=stream)), \
            pytest.raises(RuntimeError, match="model exploded"):
        await run_job(vault, "capture", {}, emit, staged=staged)
    assert vault.read_relations() == []                 # the claim recorded mid-run was rolled back
    assert not vault.relations_path.exists()


async def test_capture_records_claims_and_logs_them(vault):
    from datetime import date

    from app.agents.jobs import job_agent, run_job
    from app.vault.store import StagedSource
    staged = StagedSource(title="ATM note", body=NOTE, source_url="pasted", content_type="note")
    src = f"{date.today().isoformat()}-atm-note.md"
    page = ("---\ntitle: ATM\nsummary: ATM and TDP1.\nsources:\n  - " + src + "\n---\n# ATM\n\n"
            "**Summary:** ATM phosphorylates TDP1 at Ser81 [[source: " + src + "]].\n")
    steps = [
        [("record_relation", {"enzyme": "ATM", "target": "TDP1", "site": "S81", "source": src,
                              "quote": "ATM phosphorylates TDP1 at Ser81 after DNA damage"}),
         ("record_relation", {"enzyme": "ATM", "target": "TDP1", "site": "S999", "source": src,
                              "quote": "ATM phosphorylates TDP1 at S999 in our assay"})],
        [("write_page", {"name": "atm", "content": page})],
        None,
    ]

    def calls(messages, info):
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        return steps[turn] or [(info.output_tools[0].name, {"report": "ok"})]

    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(n, a) for n, a in calls(messages, info)])

    async def stream(messages, info: AgentInfo):
        yield {i: DeltaToolCall(name=n, json_args=json.dumps(a)) for i, (n, a) in enumerate(calls(messages, info))}

    async def emit(ev):
        pass
    try:
        with job_agent().override(model=FunctionModel(fn, stream_function=stream)):
            result = await run_job(vault, "capture", {}, emit, staged=staged)
        assert {c["status"] for c in result["claims"]} == {"confirmed", "new"}
        assert "Relations recorded: 2 (new: 1, conflicting: 0)" in vault.log_entries()[0].body
        assert len(vault.read_relations()) == 2
    finally:
        signor._get_driver().execute_query("MATCH ()-[r:CLAIMED {vault: $v}]->() DELETE r", v=vault.name)


def test_claims_api_and_admin_review(make_client):
    from app.api.deps import registry
    admin, user = make_client("admin"), make_client("user")
    v = registry().get("default")
    try:
        s = v.save_source(title="ATM and TDP1", body=NOTE, source_url="pasted", content_type="note")
        claim, _ = _rec(v, s, site="S999", quote="ATM phosphorylates TDP1 at S999 in our assay")
        listed = user.get("/api/vaults/default/claims?protein=TDP1").json()
        assert listed[0]["status"] == "new" and listed[0]["review"] == "unreviewed"
        assert user.patch(f"/api/vaults/default/claims/{claim['id']}", json={"review": "rejected"}).status_code == 403
        r = admin.patch(f"/api/vaults/default/claims/{claim['id']}", json={"review": "rejected"})
        assert r.status_code == 200 and r.json()["review"] == "rejected"
        assert v.history()[0]["message"].startswith("claims: rejected ATM -> TDP1")
        net = user.get("/api/graph/network?seeds=TDP1&vault=default").json()
        assert not any(e.get("claim") for e in net["edges"])
        assert admin.patch("/api/vaults/default/claims/nope", json={"review": "reviewed"}).status_code == 404
    finally:
        signor._get_driver().execute_query("MATCH ()-[r:CLAIMED {vault: 'default'}]->() DELETE r")
