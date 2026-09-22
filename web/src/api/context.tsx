import { createContext, useContext, useMemo, type ReactNode } from "react";
import { HttpResearchApi, type ResearchApi } from "./client";

const ApiContext = createContext<ResearchApi | null>(null);

export function ApiProvider({ children, api }: { children: ReactNode; api?: ResearchApi }) {
  const value = useMemo(() => api ?? new HttpResearchApi(), [api]);
  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
}

export function useApi(): ResearchApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("Research API provider is missing");
  return api;
}
