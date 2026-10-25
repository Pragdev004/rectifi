import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Route, Switch } from "wouter";
import { NetworkError } from "./api/client";
import { AppShell } from "./components/shell/AppShell";
import { ToastProvider } from "./components/Toast";
import { EmptyState } from "./components/ui";
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
import TracesPage from "./pages/Traces";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => error instanceof NetworkError && count < 1,
    },
  },
});

function NotFound() {
  return (
    <EmptyState title="There is nothing at this address." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
      The page you opened does not exist.
    </EmptyState>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <AppShell>
          <Switch>
            <Route path="/" component={Overview} />
            <Route path="/overview" component={Overview} />
            <Route path="/review" component={ReviewPage} />
            <Route path="/insights" component={InsightsPage} />
            <Route path="/add" component={Add} />
            <Route path="/ledger" component={Ledger} />
            <Route path="/budgets" component={Budgets} />
            <Route path="/accounts" component={Accounts} />
            <Route path="/phone" component={PhonePage} />
            <Route path="/ask" component={AskPage} />
            <Route path="/charts" component={ChartsPage} />
            <Route path="/library" component={LibraryPage} />
            <Route path="/memory" component={MemoryPage} />
            <Route path="/traces" component={TracesPage} />
            <Route path="/login" component={LoginPage} />
            <Route><NotFound /></Route>
          </Switch>
        </AppShell>
      </ToastProvider>
    </QueryClientProvider>
  );
}
