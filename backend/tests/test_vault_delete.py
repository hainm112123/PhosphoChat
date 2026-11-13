"""Deleting a vault: the folder goes to the trash; its chats, jobs and graph claims go too."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta

import pytest

from app.api.deps import registry
from app.chat_store import chat_store
from app.jobs.runner import Job, jobs
from app.knowledge import claims
from app.vault.store import TRASH_STAMP, VaultRegistry

REAL_GRAPH = {"remove_claims": claims.remove_claims, "sweep_deleted_claims": claims.sweep_deleted_claims}


@pytest.fixture(autouse=True)
def graph_offline(monkeypatch):
    """Offline by default; the Neo4j test at the end puts the real functions back."""
    def down(*a, **k):
        raise RuntimeError("Cannot reach the Neo4j database.")
    monkeypatch.setattr(claims, "remove_claims", down)
    monkeypatch.setattr(claims, "sweep_deleted_claims", down)
    monkeypatch.setattr(claims, "safe_sync", lambda v: None)


def _ids(client, vault):
    return {c["id"] for c in client.get(f"/api/vaults/{vault}/conversations").json()}


def test_trash_moves_the_folder_and_frees_the_name(tmp_path):
    reg = VaultRegistry(tmp_path)
    v = reg.create("lab")
    v.save_source(title="Note", body="ATM phosphorylates TDP1.", source_url="pasted", content_type="note")
    # git leaves read-only files behind, which Windows won't delete without help
    obj = next(p for p in (v.root / ".git").rglob("*") if p.is_file())
    os.chmod(obj, stat.S_IREAD)
    target = reg.trash("lab", deleted_by="boss")
    assert not (tmp_path / "lab").exists() and target.parent == tmp_path / ".trash"
    assert (target / "sources").is_dir() and "by boss" in (target / "DELETED.md").read_text(encoding="utf-8")
    assert reg.names() == [] and reg.deleted_names() == ["lab"]
    with pytest.raises(KeyError):
        reg.get("lab")
    reg.create("lab")  # the name is free again
    assert reg.names() == ["lab"] and reg.deleted_names() == []
    with pytest.raises(KeyError):
        reg.trash("nope")


def test_purge_trash_removes_only_old_folders(tmp_path):
    reg = VaultRegistry(tmp_path)
    old = reg.trash_dir / f"lab-{(datetime.now() - timedelta(days=40)).strftime(TRASH_STAMP)}"
    new = reg.trash_dir / f"lab-{datetime.now().strftime(TRASH_STAMP)}"
    for p in (old, new):
        (p / "wiki").mkdir(parents=True)
    os.chmod(old / "wiki", stat.S_IREAD | stat.S_IEXEC)
    assert reg.purge_trash(0) == 0 and reg.purge_trash(30) == 1
    assert not old.exists() and new.exists()


def test_delete_vault_api(make_client):
    admin = make_client("admin", username="boss")
    alice, bob = make_client("user", username="alice"), make_client("user", username="bobby")
    assert admin.post("/api/vaults", json={"name": "lab"}).status_code == 201
    store = chat_store()
    from app.auth.deps import auth_store
    ids = {u.username: u.id for u in auth_store().list_users()}
    lab_a, lab_b = store.create(ids["alice"], "lab", "a"), store.create(ids["bobby"], "lab", "b")
    keep = store.create(ids["alice"], "default", "kept")

    assert alice.delete("/api/vaults/lab?confirm=lab").status_code == 403
    assert alice.get("/api/vaults/lab/usage").status_code == 403
    usage = admin.get("/api/vaults/lab/usage").json()
    assert usage == {"name": "lab", "pages": 0, "sources": 0, "claims": 0, "conversations": 2,
                     "active_jobs": 0, "is_default": False}
    assert admin.delete("/api/vaults/lab").status_code == 400            # no confirmation
    assert admin.delete("/api/vaults/lab?confirm=default").status_code == 400
    assert admin.delete("/api/vaults/nope?confirm=nope").status_code == 404

    jobs.jobs["busy"] = Job(id="busy", vault="lab", kind="lint", params={}, status="running")
    try:
        assert admin.delete("/api/vaults/lab?confirm=lab").status_code == 409
    finally:
        del jobs.jobs["busy"]
    jobs.jobs["old"] = Job(id="old", vault="lab", kind="lint", params={}, status="succeeded")

    r = admin.delete("/api/vaults/lab?confirm=lab")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claims_removed"] is None                               # graph offline: swept later
    assert body["conversations_removed"] == 2 and body["recreated"] is False
    assert ".trash" in body["trashed_to"]
    assert "lab" not in [v["name"] for v in admin.get("/api/vaults").json()]
    assert admin.get("/api/vaults/lab/pages").status_code == 404
    assert store.get(lab_a.id, ids["alice"], "lab") is None and store.get(lab_b.id, ids["bobby"], "lab") is None
    assert store.get(keep.id, ids["alice"], "default") is not None      # other vaults untouched
    assert "old" not in jobs.jobs
    assert _ids(bob, "default") == set()


def test_deleting_the_default_vault_recreates_it_empty(make_client):
    admin = make_client("admin")
    v = registry().get("default")
    v.save_source(title="Note", body="Some text.", source_url="pasted", content_type="note")
    r = admin.delete("/api/vaults/default?confirm=default").json()
    assert r["recreated"] is True
    assert admin.get("/api/vaults/default/sources").json() == []
    assert "default" in [x["name"] for x in admin.get("/api/vaults").json()]


@pytest.mark.neo4j
def test_delete_removes_only_this_vaults_claim_edges(make_client, neo4j_ready, monkeypatch):
    from app.graph import signor
    for name, fn in REAL_GRAPH.items():
        monkeypatch.setattr(claims, name, fn)
    admin = make_client("admin")
    reg = registry()
    note = "ATM phosphorylates TDP1 at Ser81 after DNA damage."
    count = "MATCH ()-[r:CLAIMED {vault: $v}]->() RETURN count(r) AS n"
    try:
        for v in (reg.create("dellab"), reg.create("delother")):
            src = v.save_source(title="Note", body=note, source_url="pasted", content_type="note")
            claims.record(v, enzyme="ATM", target="TDP1", site="S81", mechanism="phosphorylation",
                          effect="up-regulates", source=src, quote=note.rstrip("."))
            claims.sync_claims(v)
        modifies = signor._run("MATCH ()-[m:MODIFIES]->() RETURN count(m) AS n")[0]["n"]
        assert admin.delete("/api/vaults/dellab?confirm=dellab").json()["claims_removed"] == 1
        assert signor._run(count, v="dellab")[0]["n"] == 0
        assert signor._run(count, v="delother")[0]["n"] == 1
        assert signor._run("MATCH ()-[m:MODIFIES]->() RETURN count(m) AS n")[0]["n"] == modifies

        # An edge left behind (the vault was deleted while the graph was down) goes at the next
        # sweep, which touches only vaults found in this app's trash.
        signor._get_driver().execute_query("""
            MATCH (a:Entity {id: 'Q13315'}), (b:Entity {id: 'Q9NUW8'})
            CREATE (a)-[:CLAIMED {vault: 'dellab', claim_id: 'left-behind'}]->(b)""")
        assert reg.deleted_names() == ["dellab"]
        assert claims.sweep_deleted_claims(reg.deleted_names()) == 1
        assert signor._run(count, v="dellab")[0]["n"] == 0
        assert signor._run(count, v="delother")[0]["n"] == 1
    finally:
        signor._get_driver().execute_query(
            "MATCH ()-[r:CLAIMED]->() WHERE r.vault IN ['dellab', 'delother'] DELETE r")
