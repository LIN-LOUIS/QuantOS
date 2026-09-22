import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> { data: T | null; loading: boolean; error: unknown; reload: () => void }

export function useAsync<T>(load: () => Promise<T>, dependencies: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    let active = true;
    setLoading(true); setError(null);
    load().then((value) => { if (active) setData(value); })
      .catch((value: unknown) => { if (active) setError(value); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
    // dependencies are supplied by callers as the semantic reload boundary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, revision]);
  return { data, loading, error, reload };
}
