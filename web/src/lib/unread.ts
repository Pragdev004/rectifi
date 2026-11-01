/** The words on the little chip beside Alerts in the menu. A number with the word "new", never a bare dot. Nothing unread means no chip. */
export function unreadWords(count: number | null | undefined): string | null {
  if (!count || count < 1) return null;
  return `${count > 99 ? "99+" : count} new`;
}

/** The same count in a full sentence for a screen reader. */
export function unreadSpoken(count: number | null | undefined): string | null {
  if (!count || count < 1) return null;
  return `${count} new ${count === 1 ? "alert" : "alerts"}`;
}
