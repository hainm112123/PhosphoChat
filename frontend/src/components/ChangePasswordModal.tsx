import { useState } from "react";
import { authApi } from "../api";
import { Modal } from "./Modal";

export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const hint = next && next.length < 8 ? "Use at least 8 characters."
    : repeat && repeat !== next ? "The new passwords don't match." : null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (hint || !current || !next) return;
    setBusy(true);
    setError(null);
    try {
      await authApi.changePassword(current, next);
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Change password" onClose={onClose}>
      {done ? (
        <>
          <p className="muted">Your password was changed.</p>
          <div className="modal-actions"><button className="btn" onClick={onClose}>Done</button></div>
        </>
      ) : (
        <form className="modal-form" onSubmit={submit}>
          <label className="field-label" htmlFor="pw-current">Current password</label>
          <input id="pw-current" type="password" autoComplete="current-password" autoFocus
            value={current} onChange={(e) => setCurrent(e.target.value)} />
          <label className="field-label" htmlFor="pw-new">New password</label>
          <input id="pw-new" type="password" autoComplete="new-password"
            value={next} onChange={(e) => setNext(e.target.value)} />
          <label className="field-label" htmlFor="pw-repeat">Repeat new password</label>
          <input id="pw-repeat" type="password" autoComplete="new-password"
            value={repeat} onChange={(e) => setRepeat(e.target.value)} />
          {(hint || error) && <div className="field-error small" role="alert">{error ?? hint}</div>}
          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button className="btn" disabled={busy || Boolean(hint) || !current || !next || repeat !== next}>
              {busy ? "Saving..." : "Change password"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
