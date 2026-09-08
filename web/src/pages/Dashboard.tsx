import { useStore } from "../state";
import {
  ActionLink, IssueDot, PageHead, SectionHead, Status, issueText,
} from "../components/ui";

/** The overview.
 *
 *  The list is not built here: `StateManager.problems()` decides what counts
 *  as one problem, because "these four complaints are the same dead session"
 *  is a judgement about status and every judgement about status is made in
 *  one place on the backend. This page draws the rows and links to them.
 */
export function Dashboard() {
  const { snapshot, t } = useStore();
  if (!snapshot) return null;

  const ready = snapshot.accounts.filter((a) => a.effective === "READY").length;
  const active = snapshot.campaigns.filter(
    (c) => c.effective === "RUNNING" || c.effective === "SCHEDULED").length;
  const problems = snapshot.problems;

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

      <div className="stats">
        {stats.map((s) => (
          <div className="card" key={s.label}>
            <div className="stat-value">{s.value}</div>
            <div className="stat-label">{s.label}</div>
            <div className="stat-note">{s.note}</div>
          </div>
        ))}
      </div>

      <div className="card">
        <SectionHead title={t("dash.problems")} />
        {!problems.length ? (
          <Status tone="ok" label={t("dash.all_good")} />
        ) : (
          <div>
            {problems.map((p) => (
              <div key={p.key} className="problem-row">
                <IssueDot issues={p.issues} />
                <span className="problem-where" title={p.title}>
                  {p.title}
                  {/* One record in two roles is one thing to fix, so it is one
                      row - but the user still needs to know both are meant. */}
                  {p.roles.length > 1 && <span className="faint"> · {t("dash.roles.both")}</span>}
                </span>
                <span className="problem-what">
                  {issueText(t, p.issues[0])}
                  {p.blocked_campaigns > 0 && (
                    <span className="faint">
                      {" · "}{t("dash.blocks_campaigns", { n: p.blocked_campaigns })}
                    </span>
                  )}
                </span>
                {/* The address carries the record's id, so the page it opens
                    scrolls to it and marks it instead of leaving the user to
                    look for it. */}
                <ActionLink size="sm" variant="ghost" href={`#/${p.route}/${p.id}`}>
                  {t("dash.open")}
                </ActionLink>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
