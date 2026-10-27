"""Authentication and the user/admin role split."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.chat import chat_agent
from app.auth.store import AuthError, Conflict, hash_password, verify_password


# --------------------------------------------------------------------------- store

def test_password_hashing():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and verify_password("correct horse", h)
    assert not verify_password("wrong", h) and not verify_password("x", "garbage")


def test_user_rules(auth_db):
    with pytest.raises(AuthError):
        auth_db.create_user("ab", "password123")          # too short
    with pytest.raises(AuthError):
        auth_db.create_user("alice", "short")             # weak password
    auth_db.create_user("Alice", "password123")           # stored lowercase
    with pytest.raises(Conflict):
        auth_db.create_user("alice", "password123")


def test_last_admin_and_self_guards(auth_db):
    admin = auth_db.create_user("boss", "password123", "admin")
    with pytest.raises(Conflict, match="last active admin"):
        auth_db.update_user(admin.id, actor_id=999, role="user")
    with pytest.raises(Conflict, match="own account"):
        auth_db.update_user(admin.id, actor_id=admin.id, disabled=True)
    with pytest.raises(Conflict, match="own account"):
        auth_db.delete_user(admin.id, actor_id=admin.id)
    second = auth_db.create_user("boss2", "password123", "admin")
    assert auth_db.update_user(admin.id, actor_id=second.id, role="user").role == "user"


def test_bootstrap_admin(auth_db):
    assert auth_db.ensure_admin("", "") is None
    assert auth_db.ensure_admin("root", "password123").is_admin
    assert auth_db.ensure_admin("other", "password123") is None   # an admin exists already


def test_expired_and_disabled_sessions(auth_db):
    u = auth_db.create_user("alice", "password123")
    token = auth_db.create_session(u.id)
    assert auth_db.session_user(token).username == "alice"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    auth_db._q("UPDATE sessions SET expires_at = ?", past)
    assert auth_db.session_user(token) is None
    token = auth_db.create_session(u.id)
    admin = auth_db.create_user("boss", "password123", "admin")
    auth_db.update_user(u.id, actor_id=admin.id, disabled=True)
    assert auth_db.session_user(token) is None


# --------------------------------------------------------------------------- HTTP: login

def test_login_logout_and_me(make_client, auth_db):
    c = make_client(None)
    assert c.get("/api/auth/status").json() == {"needs_admin": True}
    assert c.get("/api/auth/me").status_code == 401
    auth_db.create_user("alice", "password123")
    bad_pw = c.post("/api/auth/login", json={"username": "alice", "password": "nope-nope"})
    no_user = c.post("/api/auth/login", json={"username": "bob", "password": "nope-nope"})
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json() == no_user.json()                 # no hint which one was wrong
    r = c.post("/api/auth/login", json={"username": "alice", "password": "password123"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/auth/me").json()["username"] == "alice"
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").status_code == 401


def test_login_throttle(make_client, auth_db):
    c = make_client(None)
    auth_db.create_user("alice", "password123")
    for _ in range(5):
        c.post("/api/auth/login", json={"username": "alice", "password": "wrong-pass"})
    r = c.post("/api/auth/login", json={"username": "alice", "password": "password123"})
    assert r.status_code == 429


def test_change_own_password(make_client):
    c = make_client("user", username="alice")
    assert c.post("/api/auth/password", json={"current": "nope", "new": "newpassword1"}).status_code == 400
    assert c.post("/api/auth/password", json={"current": "password123", "new": "newpassword1"}).status_code == 204
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": "alice", "password": "newpassword1"}).status_code == 200


def test_cross_site_post_is_refused(make_client):
    c = make_client("admin")
    r = c.post("/api/vaults", json={"name": "evil"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/vaults", json={"name": "fine"},
                  headers={"Origin": "http://testserver"}).status_code == 201


# --------------------------------------------------------------------------- HTTP: roles

READS = [
    ("GET", "/api/config"), ("GET", "/api/vaults"),
    ("GET", "/api/vaults/default/pages"), ("GET", "/api/vaults/default/sources"),
    ("GET", "/api/vaults/default/index"), ("GET", "/api/vaults/default/log"),
    ("GET", "/api/vaults/default/history"), ("GET", "/api/auth/me"),
]
MANAGEMENT = [
    ("POST", "/api/vaults", {"json": {"name": "newvault"}}),
    ("POST", "/api/vaults/default/capture", {"data": {"text": "a note"}}),
    ("POST", "/api/vaults/default/chat/save", {"json": {"question": "q", "answer": "a"}}),
    ("POST", "/api/vaults/default/jobs/sync", {}),
    ("POST", "/api/vaults/default/jobs/lint", {}),
    ("POST", "/api/vaults/default/jobs/digest", {}),
    ("POST", "/api/jobs/nope/retry", {}),
    ("GET", "/api/vaults/default/jobs", {}),
    ("GET", "/api/jobs/nope", {}),
    ("GET", "/api/users", {}),
    ("POST", "/api/users", {"json": {"username": "zed", "password": "password123"}}),
    ("PATCH", "/api/users/1", {"json": {"role": "user"}}),
    ("DELETE", "/api/users/1", {}),
]


def test_anonymous_gets_401_everywhere(make_client):
    c = make_client(None)
    for method, path in READS:
        assert c.request(method, path).status_code == 401, path
    for method, path, kw in MANAGEMENT:
        assert c.request(method, path, **kw).status_code == 401, path
    assert c.post("/api/vaults/default/chat", json={"message": "hi"}).status_code == 401


def test_user_can_read_but_not_manage(make_client):
    c = make_client("user")
    for method, path in READS:
        assert c.request(method, path).status_code == 200, path
    for method, path, kw in MANAGEMENT:
        assert c.request(method, path, **kw).status_code == 403, path


def test_admin_can_manage(make_client):
    c = make_client("admin")
    assert c.post("/api/vaults", json={"name": "newvault"}).status_code == 201
    assert c.post("/api/vaults/default/jobs/sync").status_code == 202
    assert c.get("/api/users").status_code == 200
    r = c.post("/api/users", json={"username": "zed", "password": "password123"})
    assert r.status_code == 201 and r.json()["role"] == "user"
    uid = r.json()["id"]
    assert c.patch(f"/api/users/{uid}", json={"role": "admin"}).json()["role"] == "admin"
    assert c.patch(f"/api/users/{uid}", json={"disabled": True}).json()["disabled"] is True
    assert c.delete(f"/api/users/{uid}").status_code == 204
    assert c.delete("/api/users/12345").status_code == 404


def test_disabled_user_is_logged_out(make_client, auth_db):
    admin = make_client("admin")
    alice = make_client("user", username="alice")
    assert alice.get("/api/auth/me").status_code == 200
    uid = next(u.id for u in auth_db.list_users() if u.username == "alice")
    admin.patch(f"/api/users/{uid}", json={"disabled": True})
    assert alice.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- chat isolation

def _echo_history_model():
    """Answers with the number of user prompts it has seen in this conversation."""
    def fn(messages, info: AgentInfo):
        n = sum(1 for m in messages if m.kind == "request"
                for p in m.parts if p.part_kind == "user-prompt")
        return ModelResponse(parts=[TextPart(f"turns={n}")])

    async def stream(messages, info: AgentInfo):
        yield fn(messages, info).parts[0].content
    return FunctionModel(fn, stream_function=stream)


def _chat(client, message, session=None):
    body = {"message": message, **({"session_id": session} if session else {})}
    with client.stream("POST", "/api/vaults/default/chat", json=body) as r:
        raw = r.read().decode()
    events = {}
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.setdefault(lines["event"], []).append(json.loads(lines["data"]))
    text = "".join(e["delta"] for e in events.get("text", []))
    return events["session"][0]["session_id"], text


def test_chat_history_is_per_user(make_client):
    alice, bob = make_client("user", username="alice"), make_client("user", username="bobby")
    with chat_agent().override(model=_echo_history_model()):
        sid, text = _chat(alice, "first")
        assert text == "turns=1"
        assert _chat(alice, "second", sid)[1] == "turns=2"
        # bob reuses alice's session id but starts from an empty history
        assert _chat(bob, "hijack", sid)[1] == "turns=1"
