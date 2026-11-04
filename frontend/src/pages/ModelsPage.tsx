import { useCallback, useEffect, useMemo, useState } from "react";
import {
  MODELS_CHANGED_EVENT, modelsApi,
  type CatalogProvider, type ModelRole, type RoleSetting,
} from "../api";

const ROLES: { role: ModelRole; title: string }[] = [
  { role: "chat", title: "Chat" },
  { role: "job", title: "Wiki jobs" },
  { role: "verify", title: "Fact-check" },
];

const CUSTOM = "__custom";

export function ModelsPage() {
  const [providers, setProviders] = useState<CatalogProvider[] | null>(null);
  const [roles, setRoles] = useState<Record<ModelRole, RoleSetting> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadCatalog = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      setProviders((await modelsApi.catalog(refresh)).providers);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCatalog();
    modelsApi.settings().then((s) => setRoles(s.roles)).catch((e) => setError(e.message));
  }, [loadCatalog]);

  const available = useMemo(() => (providers ?? []).filter((p) => p.status === "ok" && p.models.length), [providers]);

  function saved(next: Record<ModelRole, RoleSetting>) {
    setRoles(next);
    window.dispatchEvent(new Event(MODELS_CHANGED_EVENT));
  }

  return (
    <div className="page models-page">
      <header className="page-head">
        <h1>Models</h1>
        {/* <span className="muted">Choose the model for each kind of work. Choices here apply to everyone and override .env.</span> */}
      </header>
      {error && <div className="error">{error}</div>}

      <div className="role-cards">
        {ROLES.map(({ role, title }) => (
          <RoleCard key={role} role={role} title={title} setting={roles?.[role]}
            available={available} onSaved={saved} />
        ))}
      </div>

      <section className="card providers">
        <div className="providers-head">
          <h2>Providers</h2>
          {/* <span className="muted small">Detected from the keys in .env and from local servers on this machine.</span> */}
          <button className="btn btn-small btn-ghost" onClick={() => loadCatalog(true)} disabled={loading}>
            {loading ? "Checking..." : "Refresh"}
          </button>
        </div>
        {providers == null ? <div className="muted small pulse">Asking providers for their models...</div> : (
          <ul className="provider-list">
            {providers.map((p) => (
              <li key={p.prefix}>
                <span className={`dot ${p.status === "ok" ? "dot-ok" : p.status === "error" ? "dot-bad" : ""}`} />
                <strong>{p.label}</strong>
                <span className="muted small">
                  {p.status === "ok" ? `${p.models.length} model${p.models.length === 1 ? "" : "s"}`
                    : p.status === "error" ? p.error
                    : p.needs?.startsWith("not running") ? p.needs : `not configured: set ${p.needs}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function RoleCard({ role, title, setting, available, onSaved }: {
  role: ModelRole;
  title: string;
  setting?: RoleSetting;
  available: CatalogProvider[];
  onSaved: (roles: Record<ModelRole, RoleSetting>) => void;
}) {
  const [choice, setChoice] = useState("");
  const [custom, setCustom] = useState("");
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | "reset" | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const listed = useMemo(() => new Set(available.flatMap((p) => p.models.map((m) => m.id))), [available]);
  useEffect(() => {
    if (!setting) return;
    setChoice(listed.has(setting.model) ? setting.model : CUSTOM);
    setCustom(listed.has(setting.model) ? "" : setting.model);
  }, [setting, listed]);

  const model = choice === CUSTOM ? custom.trim() : choice;
  const q = filter.trim().toLowerCase();
  const groups = available
    .map((p) => ({ ...p, models: p.models.filter((m) => !q || m.id.toLowerCase().includes(q)) }))
    .filter((p) => p.models.length);
  const fromApp = setting?.source === "app";
  const statusBad = setting && ["missing", "invalid"].includes(setting.status.status);

  async function run(kind: "save" | "test" | "reset") {
    setBusy(kind);
    setNote(null);
    try {
      if (kind === "test") {
        const r = await modelsApi.test(model);
        setNote(r.ok ? { ok: true, text: `Works: replied "${r.reply}" in ${r.seconds}s.` }
          : { ok: false, text: r.error ?? "Failed." });
      } else {
        const r = await modelsApi.save({ [role]: kind === "reset" ? "" : model });
        onSaved(r.roles);
        setNote({ ok: true, text: kind === "reset" ? "Back to the default setting." : "Saved." });
      }
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="card role-card">
      <div className="role-head">
        <h2>{title}</h2>
        {/* {setting && <span className={`badge ${fromApp ? "badge-kind-capture" : ""}`}>{fromApp ? "set here" : `from ${setting.source}`}</span>} */}
      </div>
      <p className="muted small">{setting?.help}</p>
      <div className="current">
        <span className={`dot ${statusBad ? "dot-bad" : "dot-ok"}`} />
        <code>{setting?.model ?? "..."}</code>
        {setting && <span className="muted small">{setting.status.provider} · {setting.status.detail}</span>}
      </div>

      <label className="field-label" htmlFor={`filter-${role}`}>Choose a model</label>
      <input id={`filter-${role}`} placeholder="Filter models..." value={filter} onChange={(e) => setFilter(e.target.value)} />
      <select aria-label={`${title} model`} value={choice} onChange={(e) => setChoice(e.target.value)}>
        {groups.map((p) => (
          <optgroup key={p.prefix} label={p.label}>
            {p.models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </optgroup>
        ))}
        <option value={CUSTOM}>Custom model id...</option>
      </select>
      {choice === CUSTOM && (
        <input aria-label={`${title} custom model id`} placeholder="provider:model-id, e.g. google-cloud:gemini-2.5-pro"
          value={custom} onChange={(e) => setCustom(e.target.value)} />
      )}

      <div className="role-actions">
        <button className="btn btn-small btn-ghost" disabled={!model || busy !== null} onClick={() => run("test")}>
          {busy === "test" ? "Testing..." : "Test"}
        </button>
        <button className="btn btn-small" disabled={!model || model === setting?.model || busy !== null} onClick={() => run("save")}>
          {busy === "save" ? "Saving..." : "Save"}
        </button>
        {fromApp && (
          <button className="btn btn-small btn-ghost" disabled={busy !== null} onClick={() => run("reset")}>Reset to default</button>
        )}
      </div>
      {note && <div className={`small ${note.ok ? "ok-note" : "field-error"}`} role="status">{note.text}</div>}
    </section>
  );
}
