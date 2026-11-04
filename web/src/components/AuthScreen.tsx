import { useId, type ReactNode } from "react";
import { cx } from "../lib/cx";
import { Eyebrow } from "./ui";
import styles from "./AuthScreen.module.css";

/** The full-screen slip the sign-in, sign-up, recover and Privacy pages sit on. No sidebar, no data requests. */
export function AuthScreen({ eyebrow = "Private", title, wide, children }: { eyebrow?: string; title: string; wide?: boolean; children: ReactNode }) {
  const id = useId();
  return (
    <main className={styles.screen}>
      <div className={cx(styles.inner, wide && styles.wide)}>
        <div className={styles.brand}>
          <p className={styles.wordmark}>Rectifi</p>
          <p className={styles.tagline}>every rupee, accounted for</p>
        </div>
        <section className={styles.slip} aria-labelledby={`${id}-h`}>
          <div>
            <Eyebrow>{eyebrow}</Eyebrow>
            <h1 id={`${id}-h`}>{title}</h1>
          </div>
          {children}
        </section>
      </div>
    </main>
  );
}

export const authStyles = styles;
