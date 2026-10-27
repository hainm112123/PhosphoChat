import { useCallback, useEffect, useState } from "react";
import { authApi, type Role, type User } from "../api";
import { useAuth } from "../auth";
import { Modal } from "../components/Modal";

const NAME_RE = /^[a-z0-9._-]{3,32}$/;

type Dialog =
  | { kind: "add" }
  | { kind: "reset"; user: User }
  | { kind: "delete"; user: User };

export function UsersPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const close = useCallback(() => setDialog(null), []);

  const load = useCallback(() => {
    authApi.users().then(setUsers).catch((e) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  async function patch(u: User, change: { role?: Role; disabled?: boolean }) {
    setError(null);
    try {
      await authApi.updateUser(u.id, change);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <h1>Users</h1>
        <span className="muted">Users can chat and read every vault. Admins can also capture, run jobs and manage users.</span>
        <button className="btn" onClick={() => setDialog({ kind: "add" })}>Add user</button>
      </header>
      {error && <div className="error">{error}</div>}

      <div className="table-wrap">
        <table className="users">
          <thead>
            <tr><th>Username</th><th>Role</th><th>Status</th><th>Created</th><th aria-label="Actions" /></tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.disabled ? "is-disabled" : ""}>
                <td><strong>{u.username}</strong>{u.id === me.id && <span className="muted small"> (you)</span>}</td>
                <td>
                  <select aria-label={`Role of ${u.username}`} value={u.role}
                    onChange={(e) => patch(u, { role: e.target.value as Role })}>
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>
                  <span className={`badge ${u.disabled ? "badge-failed" : "badge-succeeded"}`}>
                    {u.disabled ? "disabled" : "active"}
                  </span>
                </td>
                <td className="muted small">{u.created_at.slice(0, 10)}</td>
                <td className="row-actions">
                  <button className="btn btn-small btn-ghost" onClick={() => setDialog({ kind: "reset", user: u })}>
                    Reset password
                  </button>
                  {u.id !== me.id && (
                    <>
                      <button className="btn btn-small btn-ghost" onClick={() => patch(u, { disabled: !u.disabled })}>
                        {u.disabled ? "Enable" : "Disable"}
                      </button>
                      <button className="btn btn-small btn-ghost btn-danger" onClick={() => setDialog({ kind: "delete", user: u })}>
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {dialog?.kind === "add" && <AddUser existing={users.map((u) => u.username)} onDone={() => { close(); load(); }} onClose={close} />}
      {dialog?.kind === "reset" && <ResetPassword user={dialog.user} onClose={close} />}
      {dialog?.kind === "delete" && <DeleteUser user={dialog.user} onDone={() => { close(); load(); }} onClose={close} />}
    </div>
  );
}

function AddUser({ existing, onDone, onClose }: { existing: string[]; onDone: () => void; onClose: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const name = username.trim().toLowerCase();
  const hint = name && !NAME_RE.test(name) ? "3-32 characters: lowercase letters, digits, '.', '_' or '-'."
    : existing.includes(name) ? `"${name}" already exists.`
    : password && password.length < 8 ? "The password needs at least 8 characters." : null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (hint || !name || !password) return;
    setBusy(true);
    try {
      await authApi.createUser(name, password, role);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <Modal title="Add user" onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <label className="field-label" htmlFor="nu-name">Username</label>
        <input id="nu-name" autoFocus autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />
        <label className="field-label" htmlFor="nu-pass">Temporary password</label>
        <input id="nu-pass" type="text" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
        <span className="muted small">Share it with the person; they can change it after signing in.</span>
        <label className="field-label" htmlFor="nu-role">Role</label>
        <select id="nu-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="user">user: chat and read vaults</option>
          <option value="admin">admin: also capture, run jobs, manage users</option>
        </select>
        {(hint || error) && <div className="field-error small" role="alert">{error ?? hint}</div>}
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn" disabled={busy || Boolean(hint) || !name || !password}>{busy ? "Adding..." : "Add user"}</button>
        </div>
      </form>
    </Modal>
  );
}

function ResetPassword({ user, onClose }: { user: User; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    try {
      await authApi.updateUser(user.id, { password });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={`Reset password for ${user.username}`} onClose={onClose}>
      {done ? (
        <>
          <p className="muted">Password changed. {user.username} was signed out everywhere.</p>
          <div className="modal-actions"><button className="btn" onClick={onClose}>Done</button></div>
        </>
      ) : (
        <form className="modal-form" onSubmit={submit}>
          <label className="field-label" htmlFor="rp-pass">New password</label>
          <input id="rp-pass" type="text" autoFocus autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
          {(error || (password && password.length < 8)) && (
            <div className="field-error small" role="alert">{error ?? "Use at least 8 characters."}</div>
          )}
          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button className="btn" disabled={password.length < 8}>Set password</button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function DeleteUser({ user, onDone, onClose }: { user: User; onDone: () => void; onClose: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function confirmDelete() {
    try {
      await authApi.deleteUser(user.id);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }
  return (
    <Modal title={`Delete ${user.username}?`} onClose={onClose}>
      <p className="muted">They lose access immediately. Vault content they read or created is not affected.</p>
      {error && <div className="field-error small" role="alert">{error}</div>}
      <div className="modal-actions">
        <button className="btn btn-ghost" onClick={onClose} autoFocus>Cancel</button>
        <button className="btn btn-danger-solid" onClick={confirmDelete}>Delete user</button>
      </div>
    </Modal>
  );
}
