import { useEffect, useState } from "react";
import { authApi, type User } from "../api";

export function LoginPage({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [needsAdmin, setNeedsAdmin] = useState(false);

  useEffect(() => {
    authApi.status().then((s) => setNeedsAdmin(s.needs_admin)).catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await authApi.login(username, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <div className="brand"><span className="logo" aria-hidden>℗</span> PhosphoChat</div>
        <p className="muted small">Sign in to chat with the wiki and the PhosphoSIGNOR graph.</p>
        {needsAdmin && (
          <div className="warning small">
            No admin account exists yet. Set <code>ADMIN_USERNAME</code> and <code>ADMIN_PASSWORD</code> in
            <code> .env</code>, then restart the backend.
          </div>
        )}
        <label className="field-label" htmlFor="login-username">Username</label>
        <input id="login-username" autoComplete="username" autoFocus value={username}
          onChange={(e) => setUsername(e.target.value)} />
        <label className="field-label" htmlFor="login-password">Password</label>
        <input id="login-password" type="password" autoComplete="current-password" value={password}
          onChange={(e) => setPassword(e.target.value)} />
        {error && <div className="field-error small" role="alert">{error}</div>}
        <button className="btn" disabled={busy || !username || !password}>{busy ? "Signing in..." : "Sign in"}</button>
      </form>
    </div>
  );
}
