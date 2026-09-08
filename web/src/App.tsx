import { useEffect, useState } from "react";
import { useStore } from "./state";
import { Accounts } from "./pages/Accounts";
import { AutoReplyPage } from "./pages/AutoReply";
import { Campaigns } from "./pages/Campaigns";
import { Dashboard } from "./pages/Dashboard";
import { Logs } from "./pages/Logs";
import { Operators } from "./pages/Operators";
import { SettingsPage } from "./pages/Settings";
import { Targets } from "./pages/Targets";
import { Templates } from "./pages/Templates";

type Route =
  | "dashboard" | "accounts" | "campaigns" | "targets" | "templates"
  | "autoreply" | "operators" | "settings" | "logs";

const ROUTES: { key: Route; icon: string }[] = [
  { key: "dashboard", icon: "◎" },
  { key: "accounts", icon: "◍" },
  { key: "campaigns", icon: "➤" },
  { key: "targets", icon: "▤" },
  { key: "templates", icon: "❏" },
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
      <div style={{ padding: 48, maxWidth: 560 }}>
        <h1 className="page-title">Нет доступа</h1>
        <p className="muted">
          Эта вкладка открыта без ключа доступа. Откройте интерфейс кнопкой
          «Открыть» в окне приложения — она добавляет ключ в адрес.
        </p>
      </div>
    );
  }

  if (!ready || !snapshot) {
    return (
      <div style={{ padding: 48 }} className="muted icon-line">
        <span className="spinner" />
        {t("common.loading")}
      </div>
    );
  }

  const counts: Partial<Record<Route, number>> = {
    accounts: snapshot.accounts.length,
    campaigns: snapshot.campaigns.length,
    targets: snapshot.targets.length,
    templates: snapshot.templates.length,
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
            className={`nav-item${route === item.key ? " active" : ""}`}
            onClick={() => { window.location.hash = `#/${item.key}`; }}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{t(`nav.${item.key}`)}</span>
            {counts[item.key] !== undefined && (
              <span className="nav-count">{counts[item.key]}</span>
            )}
          </button>
        ))}
        <div className="nav-spacer" />
        <div className="icon-line faint" style={{ padding: "0 10px", fontSize: 11 }}>
          <span className={`dot ${connected ? "ok" : "warn"}`} />
          {connected ? t("conn.ok") : t("conn.retry")}
        </div>
      </nav>

      <main className="main">
        {route === "dashboard" && <Dashboard />}
        {route === "accounts" && <Accounts />}
        {route === "campaigns" && <Campaigns />}
        {route === "targets" && <Targets />}
        {route === "templates" && <Templates />}
        {route === "autoreply" && <AutoReplyPage />}
        {route === "operators" && <Operators />}
        {route === "settings" && <SettingsPage />}
        {route === "logs" && <Logs />}
      </main>

      {!connected && (
        <div className="banner">
          <span className="dot warn" />
          {t("conn.lost")} — {t("conn.retry")}
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
