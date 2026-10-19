import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type PageSummary } from "./api";

type VaultState = {
  vault: string;
  pageList: PageSummary[];
  pages: Set<string>;
  refresh: () => void;
  version: number; // bumps whenever the vault may have changed
};

const Ctx = createContext<VaultState | null>(null);

export function VaultProvider({ vault, children }: { vault: string; children: ReactNode }) {
  const [pageList, setPageList] = useState<PageSummary[]>([]);
  const [version, setVersion] = useState(0);

  const refresh = useCallback(() => setVersion((n) => n + 1), []);

  useEffect(() => {
    api.pages(vault).then(setPageList).catch(() => setPageList([]));
  }, [vault, version]);

  return (
    <Ctx.Provider value={{ vault, pageList, pages: new Set(pageList.map((p) => p.name)), refresh, version }}>
      {children}
    </Ctx.Provider>
  );
}

export function useVault(): VaultState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useVault outside VaultProvider");
  return v;
}
