import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Same rule as VAULT_NAME_RE in backend/app/vault/store.py.
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

function problem(name: string, existing: string[]): string | null {
  if (!name) return null;
  if (name.length > 64) return "Use at most 64 characters.";
  if (!NAME_RE.test(name)) return "Start with a letter or digit; use only lowercase letters, digits, - and _.";
  if (existing.includes(name)) return `A vault named "${name}" already exists.`;
  return null;
}

export function NewVaultModal({ existing, onCreated, onClose }: {
  existing: string[];
  onCreated: (name: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const hint = problem(name, existing);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name || hint || saving) return;
    setSaving(true);
    setServerError(null);
    try {
      await api.createVault(name);
      onCreated(name);
    } catch (err) {
      setServerError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="new-vault-title" onSubmit={submit}>
        <h2 id="new-vault-title">New vault</h2>
        <p className="muted small">
          A vault is an empty wiki with its own sources, pages, log and git history.
        </p>
        <label htmlFor="new-vault-name" className="field-label">Name</label>
        <input
          id="new-vault-name"
          ref={inputRef}
          value={name}
          autoComplete="off"
          spellCheck={false}
          placeholder="e.g. kinase-notes"
          aria-invalid={Boolean(hint || serverError)}
          aria-describedby="new-vault-hint"
          onChange={(e) => { setName(e.target.value.trim()); setServerError(null); }}
        />
        <div id="new-vault-hint" className={`small ${hint || serverError ? "field-error" : "muted"}`}>
          {serverError ?? hint ?? "Lowercase letters, digits, - and _ (max 64)."}
        </div>
        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn" disabled={!name || Boolean(hint) || saving}>
            {saving ? "Creating..." : "Create vault"}
          </button>
        </div>
      </form>
    </div>
  );
}
