import { useStore } from "../state";
import { IssueDot, PageHead, issueText } from "../components/ui";
import type { Issue } from "../types";

interface Problem {
  where: string;
  issues: Issue[];
  route: string;
}

export function Dashboard() {
  const { snapshot, t } = useStore();
  if (!snapshot) return null;

  const ready = snapshot.accounts.filter((a) => a.effective === "READY").length;
  const active = snapshot.campaigns.filter(
    (c) => c.effective === "RUNNING" || c.effective === "SCHEDULED").length;

  const problems: Problem[] = [];
  for (const a of snapshot.accounts) {
    if (a.issues.length) problems.push({ where: a.handle, issues: a.issues, route: "accounts" });
  }
  for (const c of snapshot.campaigns) {
    if (c.issues.length) problems.push({ where: c.name, issues: c.issues, route: "campaigns" });
  }
  for (const o of snapshot.operators) {
    if (o.issues.some((i) => i.level === "error")) {
      problems.push({ where: o.handle, issues: o.issues, route: "operators" });
    }
  }
  for (const p of snapshot.api_profiles) {
    if (p.issues.length) problems.push({ where: p.name, issues: p.issues, route: "settings" });
  }
  for (const p of snapshot.network_profiles) {
    if (p.issues.length) problems.push({ where: p.name, issues: p.issues, route: "settings" });
  }

  const stats = [
    { label: t("dash.accounts"), value: snapshot.accounts.length,
      note: t("dash.ready", { n: ready }) },
    { label: t("dash.campaigns"), value: snapshot.campaigns.length,
      note: t("dash.active", { n: active }) },
    { label: t("dash.targets"), value: snapshot.targets.length, note: "" },
    { label: t("dash.operators"), value: snapshot.operators.length, note: "" },
  ];

  return (
    <>
      <PageHead title={t("nav.dashboard")} />

      <div className="grid cols-3">
        {stats.map((s) => (
          <div className="card" key={s.label}>
            <div className="stat-value">{s.value}</div>
            <div className="stat-label">{s.label}</div>
            {s.note && <div className="faint" style={{ fontSize: 12 }}>{s.note}</div>}
          </div>
        ))}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <p className="card-title">{t("dash.problems")}</p>
        {!problems.length ? (
          <p className="card-sub icon-line" style={{ marginTop: 8 }}>
            <span className="dot ok" />
            {t("dash.all_good")}
          </p>
        ) : (
          <div style={{ marginTop: 10 }}>
            {problems.map((p, i) => (
              <div key={i} style={{
                display: "flex", alignItems: "center", gap: 10, padding: "7px 0",
                borderTop: i ? "1px solid var(--border-soft)" : undefined,
              }}>
                <IssueDot issues={p.issues} />
                <span style={{ minWidth: 160 }}>{p.where}</span>
                <span className="muted truncate" style={{ flex: 1 }}>
                  {issueText(t, p.issues[0])}
                </span>
                <a className="btn sm ghost" href={`#/${p.route}`}>{t("common.edit")}</a>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
