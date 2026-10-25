import { useCallback } from "react";
import { useSearchParams } from "wouter";

/** Page state that lives in the address (?month=2026-09), so a refresh, a back button and a shared link all work. */
export function useUrlState<K extends string>(defaults: Record<K, string>) {
  const [params, setParams] = useSearchParams();
  const values = {} as Record<K, string>;
  for (const key of Object.keys(defaults) as K[]) values[key] = params.get(key) ?? defaults[key];

  const update = useCallback(
    (next: Partial<Record<K, string>>, options: { replace?: boolean } = {}) => {
      setParams(
        (prev) => {
          const copy = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(next) as [K, string | undefined][]) {
            if (v === undefined || v === "" || v === defaults[k]) copy.delete(k);
            else copy.set(k, v);
          }
          return copy;
        },
        { replace: options.replace ?? false },
      );
    },
    [setParams],
  );
  return [values, update] as const;
}
