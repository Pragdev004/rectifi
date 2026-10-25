import { createPortal } from "react-dom";
import type { ReactNode } from "react";
import { cx } from "../lib/cx";
import { useIsDesktop } from "../lib/media";
import { useTopbarSlot } from "./shell/AppShell";
import { Eyebrow } from "./ui";
import styles from "./ui.module.css";

/** `name` is the page's name (the eyebrow on a laptop, the title in the phone's top bar); `title` is the h1.
 *  `controls` sit at the right on a laptop and move into the top bar on a phone. */
export function PageHeader({ name, title, sub, controls }: { name: string; title: string; sub?: ReactNode; controls?: ReactNode }) {
  const desktop = useIsDesktop();
  const slot = useTopbarSlot();
  return (
    <>
      <header className={cx(styles.pageHeader, !desktop && styles.compact)}>
        <div>
          <Eyebrow>{name}</Eyebrow>
          <h1>{title}</h1>
          {sub && <p className={styles.sub}>{sub}</p>}
        </div>
        {desktop && controls && <div className={styles.pageControls}>{controls}</div>}
      </header>
      {!desktop && controls && slot && createPortal(controls, slot)}
    </>
  );
}
