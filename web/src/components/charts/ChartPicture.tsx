import { cx } from "../../lib/cx";
import styles from "./charts.module.css";

/** The chart as two PNGs, one for each theme. CSS shows the one that matches the page's theme, including a manual choice
 *  (an HTML <picture> can't see the app's own light/dark switch). The alt text comes from the server's checks. */
export function ChartPicture({ light, dark, alt, caption }: { light: string | null; dark: string | null; alt: string; caption?: string }) {
  if (!light) return null;
  return (
    <figure className={styles.picture}>
      <img className={cx(styles.img, styles.imgLight)} src={`data:image/png;base64,${light}`} alt={alt} />
      {dark && <img className={cx(styles.img, styles.imgDark)} src={`data:image/png;base64,${dark}`} alt="" aria-hidden="true" />}
      {caption && <figcaption className={styles.caption}>{caption}</figcaption>}
    </figure>
  );
}
