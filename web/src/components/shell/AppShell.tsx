import { BellRing, BookText, Ellipsis, House, LogOut, MessageSquare, Plus, WifiOff } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "wouter";
import { useSignOut, useAuthStatus, useIsOwner, isOwnerView } from "../../api/auth";
import { useChatStatus } from "../../api/chat";
import { useOffline } from "../../api/client";
import { cx } from "../../lib/cx";
import { setTheme, useTheme, type ThemeChoice } from "../../lib/theme";
import { Dialog } from "../Dialog";
import { Button, LinkButton, SegmentedControl } from "../forms";
import { Eyebrow } from "../ui";
import { isActive, MORE_ITEMS, PAGE_NAMES, visibleGroups, visibleItems, type NavItem } from "./nav";
import { UnreadChip } from "./UnreadChip";
import styles from "./shell.module.css";

// The page's own controls (month stepper, "New conversation") live in the top bar on a phone. Pages hand them over through this slot.
const SlotContext = createContext<HTMLElement | null>(null);
export const useTopbarSlot = () => useContext(SlotContext);

const THEME_OPTIONS: { value: ThemeChoice; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export function ThemeSwitch() {
  const theme = useTheme();
  return <SegmentedControl legend="Colour theme" name="theme" options={THEME_OPTIONS} value={theme} onChange={setTheme} full />;
}

/** Only when a password is set and you are signed in. On a laptop with no password nothing is drawn. */
function SignOut({ onDone }: { onDone?: () => void }) {
  const auth = useAuthStatus();
  const out = useSignOut();
  if (!auth.data?.required || !auth.data.signed_in) return null;
  return (
    <Button variant="tertiary" block loading={out.isPending} loadingLabel="Signing out…" icon={<LogOut size={16} strokeWidth={1.5} aria-hidden="true" />}
      onClick={() => { onDone?.(); out.mutate(); }}>
      Sign out
    </Button>
  );
}

function NavLink({ item, path }: { item: NavItem; path: string }) {
  const Icon = item.icon;
  return (
    <Link href={item.to} className={styles.link} aria-current={isActive(path, item) ? "page" : undefined}>
      <Icon size={20} strokeWidth={1.5} aria-hidden="true" />
      <span>{item.label}</span>
      {item.to === "/alerts" && <UnreadChip />}
    </Link>
  );
}

function Sidebar({ path }: { path: string }) {
  const model = useChatStatus(); // for a friend this is switched off: the model is the owner's only
  const auth = useAuthStatus();
  const modelLine = model.data ? (model.data.ok ? `Model: ${model.data.model}` : "Assistant is off") : "";
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <Link href="/" className={styles.wordmark}>Rectifi</Link>
        <p className={styles.tagline}>every rupee, accounted for</p>
      </div>
      <LinkButton to="/add" variant="primary" block className={styles.addBtn} icon={<Plus size={18} strokeWidth={2} aria-hidden="true" />}>
        Add entry
      </LinkButton>
      <nav className={styles.nav} aria-label="Main">
        {visibleGroups(auth.data).map((g) => (
          <div key={g.title} className={styles.group}>
            <Eyebrow as="h2">{g.title}</Eyebrow>
            <ul>
              {g.items.map((item) => (
                <li key={item.to}><NavLink item={item} path={path} /></li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <div className={styles.sideFoot}>
        <div>
          <Eyebrow as="span" className={styles.themeLabel}>Theme</Eyebrow>
          <ThemeSwitch />
        </div>
        {modelLine && <p className={styles.modelLine}>{modelLine}</p>}
        <SignOut />
      </div>
    </aside>
  );
}

function TopBar({ path, setSlot }: { path: string; setSlot: (el: HTMLElement | null) => void }) {
  return (
    <header className={styles.topbar}>
      <div className={styles.topTitle} aria-hidden="true">{PAGE_NAMES[path] ?? "Rectifi"}</div>
      <div className={styles.topSlot} ref={setSlot} />
    </header>
  );
}

/** What the More sheet lists. A friend has Alerts in the bottom bar (where the owner has Ask), so it is not listed twice. */
function useMoreItems(): NavItem[] {
  const status = useAuthStatus().data;
  const owner = isOwnerView(status);
  return visibleItems(MORE_ITEMS, status).filter((i) => owner || i.to !== "/alerts");
}

function BottomNav({ path, onMore }: { path: string; onMore: () => void }) {
  const owner = useIsOwner();
  const more = useMoreItems();
  const inMore = more.some((i) => isActive(path, i));
  const cur = (on: boolean) => (on ? ("page" as const) : undefined);
  return (
    <nav className={styles.bottomnav} aria-label="Main">
      <ul>
        <li>
          <Link href="/" className={styles.tab} aria-current={cur(path === "/" || path === "/overview")}>
            <House size={22} strokeWidth={1.5} aria-hidden="true" />
            <span>Home</span>
          </Link>
        </li>
        <li>
          <Link href="/ledger" className={styles.tab} aria-current={cur(path === "/ledger")}>
            <BookText size={22} strokeWidth={1.5} aria-hidden="true" />
            <span>Ledger</span>
          </Link>
        </li>
        <li>
          <Link href="/add" className={cx(styles.tab, styles.addTab)} aria-current={cur(path === "/add")}>
            <span><Plus size={22} strokeWidth={2} aria-hidden="true" /><span>Add</span></span>
          </Link>
        </li>
        <li>
          {owner ? (
            <Link href="/ask" className={styles.tab} aria-current={cur(path === "/ask")}>
              <MessageSquare size={22} strokeWidth={1.5} aria-hidden="true" />
              <span>Ask</span>
            </Link>
          ) : (
            <Link href="/alerts" className={styles.tab} aria-current={cur(path === "/alerts")}>
              <BellRing size={22} strokeWidth={1.5} aria-hidden="true" />
              <span>Alerts</span>
            </Link>
          )}
        </li>
        <li>
          <button type="button" className={styles.tab} onClick={onMore} aria-haspopup="dialog" aria-current={cur(inMore)}>
            <Ellipsis size={22} strokeWidth={1.5} aria-hidden="true" />
            <span>More</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}

function MoreSheet({ open, onClose, path }: { open: boolean; onClose: () => void; path: string }) {
  const items = useMoreItems();
  return (
    <Dialog open={open} onClose={onClose} title="More" sheet>
      <nav aria-label="More pages">
        <ul className={styles.moreList}>
          {items.map((item) => (
            <li key={item.to}>
              <Link href={item.to} className={styles.link} aria-current={isActive(path, item) ? "page" : undefined} onClick={onClose}>
                <item.icon size={20} strokeWidth={1.5} aria-hidden="true" />
                <span>{item.label}</span>
                {item.to === "/alerts" && <UnreadChip />}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className={styles.moreTheme}>
        <Eyebrow as="span">Theme</Eyebrow>
        <ThemeSwitch />
      </div>
      <div className={styles.moreTheme}><SignOut onDone={onClose} /></div>
    </Dialog>
  );
}

function OfflineBanner() {
  const offline = useOffline();
  const client = useQueryClient();
  if (!offline) return null;
  return (
    <div className={styles.offline} role="alert">
      <WifiOff size={18} strokeWidth={1.5} aria-hidden="true" />
      <p>Rectifi on your laptop isn't answering. Is the laptop on and <code>dev.py up</code> running?</p>
      <Button variant="secondary" onClick={() => void client.refetchQueries({ type: "active" })}>Retry</Button>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [path] = useLocation();
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const [more, setMore] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);

  // After moving to another page, start at the top and put the keyboard cursor in the content (not on the sidebar link).
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    window.scrollTo(0, 0);
    mainRef.current?.focus({ preventScroll: true });
  }, [path]);

  return (
    <SlotContext.Provider value={slot}>
      <a className={styles.skip} href="#main" onClick={(e) => { e.preventDefault(); mainRef.current?.focus(); }}>Skip to content</a>
      <div className={styles.shell}>
        <Sidebar path={path} />
        <div className={styles.column}>
          <TopBar path={path} setSlot={setSlot} />
          <OfflineBanner />
          <main id="main" ref={mainRef} tabIndex={-1} className={styles.main}>{children}</main>
        </div>
      </div>
      <BottomNav path={path} onMore={() => setMore(true)} />
      <MoreSheet open={more} onClose={() => setMore(false)} path={path} />
    </SlotContext.Provider>
  );
}
