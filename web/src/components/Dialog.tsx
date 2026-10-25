import { X } from "lucide-react";
import { useCallback, useEffect, useRef, type ReactNode } from "react";
import { cx } from "../lib/cx";
import { Button, IconButton } from "./forms";
import styles from "./Dialog.module.css";

/** A native <dialog>: the browser traps focus and Esc closes it. On a phone the `sheet` variant rises from the bottom. */
export function Dialog({
  open, onClose, title, children, footer, sheet, labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  sheet?: boolean;
  labelledBy?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const closing = useRef(false);
  const quiet = useRef(false); // set while we close it ourselves, so the native close event is not reported twice

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      closing.current = false;
      d.removeAttribute("data-closing");
      d.showModal();
    } else if (!open && d.open) {
      quiet.current = true;
      d.close();
    }
  }, [open]);

  const requestClose = useCallback(() => {
    const d = ref.current;
    if (!d || closing.current) return;
    closing.current = true;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    d.setAttribute("data-closing", "true");
    window.setTimeout(() => {
      closing.current = false;
      d.removeAttribute("data-closing");
      if (d.open) {
        quiet.current = true;
        d.close();
      }
      onClose();
    }, reduced ? 0 : 140);
  }, [onClose]);

  const titleId = labelledBy ?? `${title.replace(/\W+/g, "-")}-title`;
  return (
    <dialog
      ref={ref}
      className={cx(styles.dialog, sheet && styles.sheet)}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        requestClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) requestClose();
      }}
      onClose={() => {
        if (quiet.current) {
          quiet.current = false;
          return;
        }
        if (open) onClose();
      }}
    >
      {open && (
        <div className={styles.inner}>
          <div className={styles.head}>
            <h2 id={titleId}>{title}</h2>
            <IconButton label="Close" onClick={requestClose}><X size={20} strokeWidth={1.5} aria-hidden="true" /></IconButton>
          </div>
          <div className={styles.body}>{children}</div>
          {footer && <div className={styles.foot}>{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

export function ConfirmDialog({
  open, title, children, confirmLabel, busyLabel, busy, danger = true, onConfirm, onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  busy?: boolean;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} loading={busy} loadingLabel={busyLabel} onClick={onConfirm}>{confirmLabel}</Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}
