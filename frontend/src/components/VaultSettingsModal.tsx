import { useEffect, useState } from "react";
import { api, type VaultDeleted, type VaultUsage } from "../api";
import { Modal } from "./Modal";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Admin settings for one vault. For now: deleting it (type its name to confirm). */
export function VaultSettingsModal({ vault, onDeleted, onClose }: {
  vault: string;
  onDeleted: (result: VaultDeleted) => void;
  onClose: () => void;
}) {
  const [usage, setUsage] = useState<VaultUsage | null>(null);
  const [typed, setTyped] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.vaultUsage(vault).then(setUsage, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [vault]);

  async function remove(e: React.FormEvent) {
    e.preventDefault();
    if (typed !== vault || deleting) return;
    setDeleting(true);
    setError(null);
    try {
      onDeleted(await api.deleteVault(vault));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setDeleting(false);
    }
  }

  const busy = Boolean(usage?.active_jobs);
  return (
    <Modal title={`Vault settings: ${vault}`} onClose={onClose}>
      <form className="danger-zone" onSubmit={remove}>
        <h3>Delete this vault</h3>
        {!usage ? <p className="muted small pulse">{error ? "" : "Checking what's in it..."}</p> : (
          <ul className="small">
            <li>
              {plural(usage.pages, "wiki page")}, {plural(usage.sources, "source")} and {plural(usage.claims, "recorded claim")} move
              to the trash (<code>vaults/.trash</code>), where an admin can restore the folder.
            </li>
            {usage.claims > 0 && <li>Its claims are removed from the graph.</li>}
            <li><strong>{plural(usage.conversations, "chat conversation")}</strong> (all users) are deleted for good.</li>
            {usage.is_default && <li>This is the default vault: it's recreated empty right away.</li>}
          </ul>
        )}
        {busy && <p className="field-error small">Wait for {plural(usage!.active_jobs, "running job")} to finish first.</p>}
        <label htmlFor="delete-vault-confirm" className="field-label small">
          Type <strong>{vault}</strong> to confirm
        </label>
        <input
          id="delete-vault-confirm" value={typed} autoComplete="off" spellCheck={false} autoFocus
          disabled={!usage || busy} onChange={(e) => setTyped(e.target.value.trim())}
        />
        {error && <div className="field-error small">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-danger-solid" disabled={typed !== vault || !usage || busy || deleting}>
            {deleting ? "Deleting..." : "Delete vault"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
