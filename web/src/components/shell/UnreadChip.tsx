import { useUnread } from "../../api/alerts";
import { unreadSpoken, unreadWords } from "../../lib/unread";
import styles from "./shell.module.css";

/** "3 new" beside Alerts in the menu. The count comes from the server; this only writes it out. Nothing is drawn when it is 0 or the request failed. */
export function UnreadChip() {
  const { data } = useUnread();
  const words = unreadWords(data);
  if (!words) return null;
  return (
    <span className={styles.unread} data-testid="unread-chip">
      <span aria-hidden="true">{words}</span>
      <span className="sr-only">{unreadSpoken(data)}</span>
    </span>
  );
}
