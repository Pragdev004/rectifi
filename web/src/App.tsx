import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { Route, Switch } from "wouter";
import { useAuthStatus, isOwnerView } from "./api/auth";
import { NetworkError } from "./api/client";
import { AppShell } from "./components/shell/AppShell";
import { ToastProvider } from "./components/Toast";
import { EmptyState, Skeleton } from "./components/ui";
import { LinkButton } from "./components/forms";
import Overview from "./pages/Overview";
import Accounts from "./pages/Accounts";
import Add from "./pages/Add";
import Budgets from "./pages/Budgets";
import Ledger from "./pages/Ledger";
import AskPage from "./pages/Ask";
import InsightsPage from "./pages/Insights";
import PhonePage from "./pages/Phone";
import ReviewPage from "./pages/Review";
import ChartsPage from "./pages/Charts";
import LibraryPage from "./pages/Library";
import LoginPage from "./pages/Login";
import MemoryPage from "./pages/Memory";
import PrivacyPage from "./pages/Privacy";
import RecoverPage from "./pages/Recover";
import SettingsPage from "./pages/Settings";
import SignupPage from "./pages/Signup";
import TracesPage from "./pages/Traces";

/** Alerts and Leaks load on first visit. If the chunk is gone (a rebuild), show a plain message, never a blank page. */
function LoadFailed() {
  return (
    <EmptyState title="This page could not load." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
      Rectifi was updated while this tab was open. Reload the page.
    </EmptyState>
  );
}
const lazyPage = (load: () => Promise<{ default: React.ComponentType }>) =>
  lazy(() => load().catch(() => ({ default: LoadFailed })));
const AlertsPage = lazyPage(() => import("./pages/Alerts"));
const LeaksPage = lazyPage(() => import("./pages/Leaks"));

const client = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => error instanceof NetworkError && count < 1,
    },
  },
});

/** The owner-only pages (Ask, Charts, Library, Memory, Traces). A friend is told so in words instead of getting a page full of 403s.
 *  The server refuses them too: this is only what the screen shows. */
function ownerOnly(Page: React.ComponentType): React.ComponentType {
  return function OwnerOnly() {
    const auth = useAuthStatus();
    if (auth.data && !isOwnerView(auth.data)) {
      return (
        <EmptyState title="This page is for the owner." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
          Ask, Charts, Library, Memory and Traces are only for the person who runs this Rectifi.
        </EmptyState>
      );
    }
    return <Page />;
  };
}
const OwnerAsk = ownerOnly(AskPage);
const OwnerCharts = ownerOnly(ChartsPage);
const OwnerLibrary = ownerOnly(LibraryPage);
const OwnerMemory = ownerOnly(MemoryPage);
const OwnerTraces = ownerOnly(TracesPage);

function NotFound() {
  return (
    <EmptyState title="There is nothing at this address." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
      The page you opened does not exist.
    </EmptyState>
  );
}

function Pages() {
  return (
    <AppShell>
      <Suspense fallback={<Skeleton height={120} />}>
      <Switch>
        <Route path="/" component={Overview} />
        <Route path="/overview" component={Overview} />
        <Route path="/review" component={ReviewPage} />
        <Route path="/insights" component={InsightsPage} />
        <Route path="/alerts" component={AlertsPage} />
        <Route path="/leaks" component={LeaksPage} />
        <Route path="/add" component={Add} />
        <Route path="/ledger" component={Ledger} />
        <Route path="/budgets" component={Budgets} />
        <Route path="/accounts" component={Accounts} />
        <Route path="/phone" component={PhonePage} />
        <Route path="/ask" component={OwnerAsk} />
        <Route path="/charts" component={OwnerCharts} />
        <Route path="/library" component={OwnerLibrary} />
        <Route path="/memory" component={OwnerMemory} />
        <Route path="/traces" component={OwnerTraces} />
        <Route path="/settings" component={SettingsPage} />
        <Route><NotFound /></Route>
      </Switch>
      </Suspense>
    </AppShell>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <Switch>
          {/* outside the shell: no sidebar and no data requests before you are signed in. Privacy is public. */}
          <Route path="/login" component={LoginPage} />
          <Route path="/signup" component={SignupPage} />
          <Route path="/recover" component={RecoverPage} />
          <Route path="/privacy" component={PrivacyPage} />
          <Route component={Pages} />
        </Switch>
      </ToastProvider>
    </QueryClientProvider>
  );
}
