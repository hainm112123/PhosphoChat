import { useCallback, useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api } from "./api";
import { AdminOnly, useAuth } from "./auth";
import { ChangePasswordModal } from "./components/ChangePasswordModal";
import { NewVaultModal } from "./components/NewVaultModal";
import { VaultProvider } from "./vault";
import { ChatPage } from "./pages/ChatPage";
import { CapturePage } from "./pages/CapturePage";
import { WikiPage } from "./pages/WikiPage";
import { SourcesPage } from "./pages/SourcesPage";
import { LogPage } from "./pages/LogPage";
import { UsersPage } from "./pages/UsersPage";

function Shell() {
  const { vault = "default" } = useParams();
  const navigate = useNavigate();
  const { user, isAdmin, logout } = useAuth();
  const [changingPassword, setChangingPassword] = useState(false);
  const closePassword = useCallback(() => setChangingPassword(false), []);
  const [vaults, setVaults] = useState<string[]>([]);
  const [models, setModels] = useState<{ chat: string; job: string; verify: string } | null>(null);
  const [graph, setGraph] = useState<{ ok: boolean; error?: string } | null>(null);

  useEffect(() => {
    api.vaults().then((vs) => setVaults(vs.map((v) => v.name))).catch(() => {});
    api.config().then((c) => setModels(c.models)).catch(() => {});
    api.health().then((h) => setGraph(h.graph)).catch((e) => setGraph({ ok: false, error: String(e) }));
  }, []);

  const [creating, setCreating] = useState(false);
  const closeModal = useCallback(() => setCreating(false), []);

  function vaultCreated(name: string) {
    setCreating(false);
    setVaults((vs) => [...vs, name].sort());
    navigate(`/${name}/capture`);
  }

  // Capture and Users are management: shown to admins only (the server enforces it too).
  const nav = [
    ["chat", "Chat", false],
    ["capture", "Capture", true],
    ["wiki", "Wiki", false],
    ["sources", "Sources", false],
    ["log", "Log", false],
    ["users", "Users", true],
  ].filter(([, , adminOnly]) => isAdmin || !adminOnly) as [string, string, boolean][];

  return (
    <VaultProvider vault={vault}>
      <div className="app">
        <nav className="sidebar">
          <div className="brand">
            <span className="logo" aria-hidden>℗</span> PhosphoChat
          </div>
          <label className="vault-select">
            <span className="muted small">Vault</span>
            <select value={vault} onChange={(e) => (e.target.value === "__new" ? setCreating(true) : navigate(`/${e.target.value}/chat`))}>
              {(vaults.includes(vault) ? vaults : [vault, ...vaults]).map((v) => <option key={v} value={v}>{v}</option>)}
              {isAdmin && <option value="__new">+ New vault...</option>}
            </select>
          </label>
          {nav.map(([path, label]) => (
            <NavLink key={path} to={`/${vault}/${path}`} className="nav-link">{label}</NavLink>
          ))}
          <div className="status">
            {models == null ? <div><span className="dot" /> model...</div>
              : (() => {
                const tip = `Chat: ${models.chat}\nWiki jobs: ${models.job}\nFact-check: ${models.verify}`;
                return models.chat === models.job
                  ? <div title={tip}><span className="dot dot-ok" /> {models.chat}</div>
                  : <>
                    <div title={tip}><span className="dot dot-ok" /> chat · {models.chat}</div>
                    <div title={tip}><span className="dot dot-ok" /> jobs · {models.job}</div>
                  </>;
              })()}
            <div title={graph?.error}>
              <span className={`dot ${graph == null ? "" : graph.ok ? "dot-ok" : "dot-bad"}`} />
              {graph == null ? "graph..." : graph.ok ? "PhosphoSIGNOR graph" : "graph offline"}
            </div>
          </div>
          <div className="account">
            <div className="account-name">
              <strong>{user.username}</strong>
              <span className={`badge ${isAdmin ? "badge-kind-capture" : ""}`}>{user.role}</span>
            </div>
            <div className="account-actions">
              <button className="link-btn" onClick={() => setChangingPassword(true)}>Change password</button>
              <button className="link-btn" onClick={logout}>Log out</button>
            </div>
          </div>
        </nav>
        <main className="main">
          <Outlet />
        </main>
      </div>
      {creating && isAdmin && <NewVaultModal existing={vaults} onCreated={vaultCreated} onClose={closeModal} />}
      {changingPassword && <ChangePasswordModal onClose={closePassword} />}
    </VaultProvider>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/default/chat" replace />} />
      <Route path="/:vault" element={<Shell />}>
        <Route index element={<Navigate to="chat" replace />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="capture" element={<AdminOnly what="capture sources"><CapturePage /></AdminOnly>} />
        <Route path="wiki" element={<WikiPage />} />
        <Route path="wiki/:page" element={<WikiPage />} />
        <Route path="sources" element={<SourcesPage />} />
        <Route path="sources/:name" element={<SourcesPage />} />
        <Route path="log" element={<LogPage />} />
        <Route path="users" element={<AdminOnly what="manage users"><UsersPage /></AdminOnly>} />
      </Route>
    </Routes>
  );
}
