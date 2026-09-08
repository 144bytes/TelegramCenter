import { useEffect, useState } from "react";
import { useStore } from "./state";
import { Status } from "./components/ui";
import { Accounts } from "./pages/Accounts";
import { AutoReplyPage } from "./pages/AutoReply";
import { Campaigns } from "./pages/Campaigns";
import { Dashboard } from "./pages/Dashboard";
import { Logs } from "./pages/Logs";
import { Operators } from "./pages/Operators";
import { SettingsPage } from "./pages/Settings";
import { Targets } from "./pages/Targets";

type Route =
  | "dashboard" | "accounts" | "campaigns" | "targets"
  | "autoreply" | "operators" | "settings" | "logs";

const ROUTES: { key: Route; icon: string }[] = [
  { key: "dashboard", icon: "◎" },
  { key: "accounts", icon: "◍" },
  { key: "campaigns", icon: "➤" },
  { key: "targets", icon: "▤" },
  { key: "autoreply", icon: "↩" },
  { key: "operators", icon: "☏" },
  { key: "settings", icon: "⚙" },
  { key: "logs", icon: "≡" },
];

function currentRoute(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "").split("/")[0];
  return (ROUTES.some((r) => r.key === raw) ? raw : "dashboard") as Route;
}

export function App() {
  const { snapshot, ready, fatal, connected, t, toasts } = useStore();
  const [route, setRoute] = useState<Route>(currentRoute);

  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (fatal === "no-token" || fatal === "forbidden") {
    return (
      <div className="splash">
        <h1 className="page-title">{t("noaccess.title")}</h1>
        <p className="muted">{t("noaccess.text")}</p>
      </div>
    );
  }

  if (!ready || !snapshot) {
    return (
      <div className="splash muted icon-line">
        <span className="spinner" />
        {t("common.loading")}
      </div>
    );
  }

  const counts: Partial<Record<Route, number>> = {
    accounts: snapshot.accounts.length,
    campaigns: snapshot.campaigns.length,
    targets: snapshot.targets.length,
    operators: snapshot.operators.length,
  };

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          <span className="brand-mark">TC</span>
          <span className="brand-name">{t("app.name")}</span>
        </div>
        {ROUTES.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`nav-item${route === item.key ? " active" : ""}`}
            onClick={() => { window.location.hash = `#/${item.key}`; }}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{t.dyn(`nav.${item.key}`)}</span>
            {counts[item.key] !== undefined && (
              <span className="nav-count">{counts[item.key]}</span>
            )}
          </button>
        ))}
        <div className="nav-spacer" />
        <div className="nav-foot">
          <Status tone={connected ? "ok" : "warn"}
                  label={connected ? t("conn.ok") : t("conn.retry")} />
        </div>
      </nav>

      <main className="main">
        {route === "dashboard" && <Dashboard />}
        {route === "accounts" && <Accounts />}
        {route === "campaigns" && <Campaigns />}
        {route === "targets" && <Targets />}
        {route === "autoreply" && <AutoReplyPage />}
        {route === "operators" && <Operators />}
        {route === "settings" && <SettingsPage />}
        {route === "logs" && <Logs />}
      </main>

      {!connected && (
        <div className="banner">
          <Status tone="warn" label={`${t("conn.lost")} — ${t("conn.retry")}`} />
        </div>
      )}

      <div className="toasts">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.kind}`}>{toast.text}</div>
        ))}
      </div>
    </div>
  );
}
