import type { UseQueryResult } from "@tanstack/react-query";
import { Database } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { ApiError, isDbDown, NetworkError } from "../api/client";
import { cx } from "../lib/cx";
import { Button } from "./forms";
import { Callout, ErrorBlock, useDelayed } from "./ui";
import styles from "./Loaded.module.css";

/** The full-page state for "Postgres isn't running". Tries again by itself every 10 seconds. */
export function DbDownPage({ message, onRetry }: { message: string; onRetry: () => void }) {
  useEffect(() => {
    const t = window.setInterval(onRetry, 10_000);
    return () => window.clearInterval(t);
  }, [onRetry]);
  return (
    <div className={styles.down} data-testid="db-down">
      <h1>Rectifi can't reach its database</h1>
      <Callout tone="over" lead="Your data is safe. Postgres just isn't running.">{message}</Callout>
      <p>
        Start it with <code>python scripts/dev.py db</code> (Docker), or check <code>DATABASE_URL</code> in <code>.env</code>.{" "}
        <code>python scripts/dev.py doctor</code> shows what is wrong.
      </p>
      <Button variant="primary" onClick={onRetry} icon={<Database size={16} strokeWidth={1.5} aria-hidden="true" />}>Try again</Button>
      <p className={styles.retryNote}>Rectifi tries again every 10 seconds on its own.</p>
    </div>
  );
}

/** Shows a skeleton (only if loading is slow), the page's data, or the right kind of failure. Stale data stays visible while a
 *  new month loads, dimmed, so the page never jumps. */
export function Loaded<T>({
  query, skeleton, children,
}: {
  query: UseQueryResult<T>;
  skeleton: ReactNode;
  children: (data: T) => ReactNode;
}) {
  const slow = useDelayed(query.isPending);
  if (query.data !== undefined) {
    return (
      <div className={cx(query.isPlaceholderData ? styles.stale : styles.fresh)} aria-busy={query.isPlaceholderData || undefined}>
        {children(query.data)}
      </div>
    );
  }
  if (query.isError) {
    const error = query.error;
    if (isDbDown(error)) return <DbDownPage message={(error as ApiError).message} onRetry={() => void query.refetch()} />;
    const message = error instanceof NetworkError || error instanceof ApiError ? error.message : "Something went wrong. Try again.";
    return <ErrorBlock message={message} onRetry={() => void query.refetch()} />;
  }
  return slow ? <>{skeleton}</> : null;
}
