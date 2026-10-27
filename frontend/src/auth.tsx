import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { authApi, UNAUTHORIZED_EVENT, type User } from "./api";
import { LoginPage } from "./pages/LoginPage";

type AuthState = {
  user: User;
  isAdmin: boolean;
  logout: () => Promise<void>;
};

const Ctx = createContext<AuthState | null>(null);

/** Renders the login page until there is a session, then the app. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    authApi.me().then(setUser).catch(() => setUser(null)).finally(() => setChecked(true));
    const onUnauthorized = () => setUser(null);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout().catch(() => {});
    // Reload so nothing from this session (chat history in memory) is left for the next person.
    window.location.assign("/");
  }, []);

  const lastUserId = useRef<number | null>(null);
  const onLogin = useCallback((u: User) => {
    if (lastUserId.current !== null && lastUserId.current !== u.id) {
      window.location.reload(); // a different person signed in: drop the previous user's state
      return;
    }
    setUser(u);
  }, []);
  useEffect(() => {
    if (user) lastUserId.current = user.id;
  }, [user]);

  if (!checked) return null;
  if (!user) return <LoginPage onLogin={onLogin} />;
  return <Ctx.Provider value={{ user, isAdmin: user.role === "admin", logout }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

/** Shown when a user opens an admin-only page directly. */
export function AdminOnly({ what, children }: { what: string; children: ReactNode }) {
  const { isAdmin } = useAuth();
  if (isAdmin) return <>{children}</>;
  return (
    <div className="page">
      <div className="empty">
        <h2>Admins only</h2>
        <p>Only admins can {what}. Ask an admin if you need this.</p>
      </div>
    </div>
  );
}
