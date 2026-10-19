import { useEffect, useState } from "react";
import { Navigate, NavLink, Outlet, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api } from "./api";
import { VaultProvider } from "./vault";
import { ChatPage } from "./pages/ChatPage";
import { CapturePage } from "./pages/CapturePage";
import { WikiPage } from "./pages/WikiPage";
import { SourcesPage } from "./pages/SourcesPage";
import { LogPage } from "./pages/LogPage";

function Shell() {
  const { vault = "default" } = useParams();
  const navigate = useNavigate();
  const [vaults, setVaults] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [graph, setGraph] = useState<{ ok: boolean; error?: string } | null>(null);

  useEffect(() => {
    api.vaults().then((vs) => setVaults(vs.map((v) => v.name))).catch(() => {});
    api.config().then((c) => setModel(c.model)).catch(() => {});
    api.health().then((h) => setGraph(h.graph)).catch((e) => setGraph({ ok: false, error: String(e) }));
  }, []);

  async function newVault() {
    const name = prompt("New vault name (lowercase letters, digits, - and _):")?.trim();
    if (!name) return;
    try {
      await api.createVault(name);
      setVaults((vs) => [...vs, name].sort());
      navigate(`/${name}/capture`);
    } catch (e) {
      alert(String(e instanceof Error ? e.message : e));
    }
  }

  const nav = [
    ["chat", "Chat"],
    ["capture", "Capture"],
    ["wiki", "Wiki"],
    ["sources", "Sources"],
    ["log", "Log"],
  ];

  return (
    <VaultProvider vault={vault}>
      <div className="app">
        <nav className="sidebar">
          <div className="brand">
            <span className="logo" aria-hidden>℗</span> PhosphoChat
          </div>
          <label className="vault-select">
            <span className="muted small">Vault</span>
            <select value={vault} onChange={(e) => (e.target.value === "__new" ? newVault() : navigate(`/${e.target.value}/chat`))}>
              {(vaults.includes(vault) ? vaults : [vault, ...vaults]).map((v) => <option key={v} value={v}>{v}</option>)}
              <option value="__new">+ New vault...</option>
            </select>
          </label>
          {nav.map(([path, label]) => (
            <NavLink key={path} to={`/${vault}/${path}`} className="nav-link">{label}</NavLink>
          ))}
          <div className="status">
            <div title={model}><span className="dot dot-ok" /> {model || "model..."}</div>
            <div title={graph?.error}>
              <span className={`dot ${graph == null ? "" : graph.ok ? "dot-ok" : "dot-bad"}`} />
              {graph == null ? "graph..." : graph.ok ? "PhosphoSIGNOR graph" : "graph offline"}
            </div>
          </div>
        </nav>
        <main className="main">
          <Outlet />
        </main>
      </div>
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
        <Route path="capture" element={<CapturePage />} />
        <Route path="wiki" element={<WikiPage />} />
        <Route path="wiki/:page" element={<WikiPage />} />
        <Route path="sources" element={<SourcesPage />} />
        <Route path="sources/:name" element={<SourcesPage />} />
        <Route path="log" element={<LogPage />} />
      </Route>
    </Routes>
  );
}
