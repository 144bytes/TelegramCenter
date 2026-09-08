import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import {
  Field, FormError, IssueDot, NumberRange, PageHead, Switch,
} from "../components/ui";
import type { AutoReplyKind, AutoReplyRule } from "../types";

const GLOBAL = "global";

interface Draft {
  enabled: boolean;
  delayMin: number;
  delayMax: number;
  first: string;
  periodic: string;
  faq: { id: string; match: string; response: string }[];
}

let seq = 0;
const nextId = () => `new_${++seq}`;

export function AutoReplyPage() {
  const { snapshot, t, act, notify, refresh } = useStore();
  const [owner, setOwner] = useState<string>(GLOBAL);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  // rows that have been emptied and are collapsing out of the list
  const [leaving, setLeaving] = useState<Set<string>>(new Set());

  const config = useMemo(
    () => snapshot?.auto_reply.find((c) => c.owner_id === owner) ?? null,
    [snapshot, owner]);

  const account = owner === GLOBAL
    ? null
    : snapshot?.accounts.find((a) => a.id === owner) ?? null;

  const operator = account?.operator_id
    ? snapshot?.operators.find((o) => o.id === account.operator_id) ?? null
    : null;

  const inherited = owner !== GLOBAL && !config;
  const faqLimit = snapshot?.settings.autoreply.faq_limit ?? 5;

  // Seed the draft only when the selected owner changes. Re-seeding on every
  // snapshot would wipe what the user is typing the moment any unrelated event
  // (a campaign sending, a probe finishing) triggers a refetch.
  const seededFor = useRef<string | null>(null);

  useEffect(() => {
    if (!snapshot) return;
    if (seededFor.current === owner) return;
    seededFor.current = owner;

    const source = config
      ?? (owner === GLOBAL
        ? null
        : snapshot.auto_reply.find((c) => c.owner_id === GLOBAL) ?? null);

    const rules: AutoReplyRule[] = source?.rules ?? [];
    const pick = (kind: AutoReplyKind) =>
      rules.find((r) => r.kind === kind)?.response ?? "";

    setDraft({
      enabled: source?.enabled ?? false,
      delayMin: source?.delay_min_sec
        ?? snapshot.settings.autoreply.default_delay_min_sec,
      delayMax: source?.delay_max_sec
        ?? snapshot.settings.autoreply.default_delay_max_sec,
      first: pick("FIRST_MESSAGE"),
      periodic: pick("PERIODIC"),
      faq: rules.filter((r) => r.kind === "FAQ")
        .map((r) => ({ id: r.id, match: r.match, response: r.response })),
    });
    setError(null);
  }, [owner, config, snapshot]);

  if (!snapshot || !draft) return null;

  // one blank row is always offered, until the limit is reached
  const faqRows = [...draft.faq];
  const blanks = faqRows.filter((r) => !r.match.trim() && !r.response.trim()).length;
  if (blanks === 0 && faqRows.length < faqLimit) {
    faqRows.push({ id: nextId(), match: "", response: "" });
  }

  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  function setFaq(id: string, patch: Partial<{ match: string; response: string }>) {
    const existing = draft!.faq.find((r) => r.id === id);
    const merged = existing
      ? draft!.faq.map((r) => (r.id === id ? { ...r, ...patch } : r))
      : [...draft!.faq, { id, match: "", response: "", ...patch }];
    set({ faq: merged });
  }

  function removeFaq(id: string) {
    // let the row collapse before it leaves, so the ones below slide up
    setLeaving((prev) => new Set(prev).add(id));
    window.setTimeout(() => {
      setDraft((d) => (d ? { ...d, faq: d.faq.filter((r) => r.id !== id) } : d));
      setLeaving((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }, 200);
  }

  function dropIfEmpty(id: string) {
    const row = draft!.faq.find((r) => r.id === id);
    // an emptied row is a deleted row: no point keeping a blank line around
    if (row && !row.match.trim() && !row.response.trim()) removeFaq(id);
  }

  async function save() {
    setError(null);
    const rules: Record<string, unknown>[] = [];
    if (draft!.first.trim()) {
      rules.push({ kind: "FIRST_MESSAGE", enabled: true, response: draft!.first });
    }
    if (draft!.periodic.trim()) {
      rules.push({ kind: "PERIODIC", enabled: true, response: draft!.periodic });
    }
    for (const row of draft!.faq) {
      if (!row.match.trim() && !row.response.trim()) continue;
      rules.push({ kind: "FAQ", enabled: true, match: row.match,
                   response: row.response });
    }
    try {
      await api.autoreply.save({
        owner_id: owner,
        enabled: draft!.enabled,
        delay_min_sec: draft!.delayMin,
        delay_max_sec: draft!.delayMax,
        rules,
      });
      notify(t("settings.saved"), "ok");
      await refresh();
    } catch (err) {
      // shown inline, next to the fields that caused it — never as a popup
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const canUseOperator = owner !== GLOBAL && !!operator;

  return (
    <>
      <PageHead
        title={t("autoreply.title")}
        sub={t("autoreply.sub")}
        actions={
          <>
            {owner !== GLOBAL && config && (
              <button className="btn" onClick={async () => {
                await act(() => api.autoreply.reset(owner));
                // the account now inherits again: rebuild the draft from global
                seededFor.current = null;
                setError(null);
              }}>
                {t("autoreply.use_global")}
              </button>
            )}
            <button className="btn accent" onClick={save}>{t("common.save")}</button>
          </>
        }
      />

      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <div>
            <span className="field-label">{t("nav.accounts")}</span>
            <select className="select" value={owner}
                    onChange={(e) => setOwner(e.target.value)}>
              <option value={GLOBAL}>{t("autoreply.global")}</option>
              {snapshot.accounts.map((a) => (
                <option key={a.id} value={a.id}>{a.handle}</option>
              ))}
            </select>
          </div>
          <div className="shrink" style={{ paddingBottom: 6 }}>
            <Switch checked={draft.enabled} onChange={(v) => set({ enabled: v })}
                    label={draft.enabled ? t("common.enabled") : t("common.disabled")} />
          </div>
          {config && (
            <div className="shrink" style={{ paddingBottom: 6 }}>
              <IssueDot issues={config.issues} state={config.effective} />
            </div>
          )}
        </div>

        <p className="field-hint" style={{ marginTop: 8 }}>
          {owner === GLOBAL
            ? t("autoreply.global_hint")
            : inherited
              ? t("autoreply.inherited_notice")
              : t("autoreply.for_account", { name: account?.handle ?? "" })}
        </p>
      </div>

      <div className="card">
        <FormError message={error} />

        <Field label={`${t("autoreply.delay")}, ${t("common.seconds")}`}
               hint={t("autoreply.delay_hint")}>
          <NumberRange min={draft.delayMin} max={draft.delayMax}
                       onChange={(lo, hi) => set({ delayMin: lo, delayMax: hi })} />
        </Field>

        <p className="field-hint" style={{ marginTop: -4, marginBottom: 16 }}>
          {canUseOperator
            ? t("autoreply.operator_note")
            : owner === GLOBAL ? t("autoreply.no_operator")
              : operator ? t("autoreply.operator_note") : t("autoreply.no_operator")}
        </p>

        <Field label={t("autoreply.first")} hint={t("autoreply.first_hint")}>
          <textarea className="textarea" style={{ minHeight: 66 }} value={draft.first}
                    onChange={(e) => set({ first: e.target.value })} />
        </Field>

        <Field label={t("autoreply.periodic")} hint={t("autoreply.periodic_hint")}>
          <textarea className="textarea" style={{ minHeight: 66 }} value={draft.periodic}
                    onChange={(e) => set({ periodic: e.target.value })} />
        </Field>

        <span className="field-label">
          {t("autoreply.faq")} · {t("autoreply.faq_hint", { limit: faqLimit })}
        </span>

        {faqLimit === 0 && (
          <p className="faint" style={{ fontSize: 12.5 }}>
            {t("autoreply.faq_limit_reached", { limit: 0 })}
          </p>
        )}

        {faqRows.map((row) => (
          <div
            className={`row faq-row${leaving.has(row.id) ? " leaving" : ""}`}
            key={row.id}
            onBlur={(e) => {
              // ignore moving between the two fields of the same row
              if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
              dropIfEmpty(row.id);
            }}
          >
            <input className="input" style={{ maxWidth: 220 }} value={row.match}
                   placeholder={t("autoreply.match")}
                   onChange={(e) => setFaq(row.id, { match: e.target.value })} />
            <span className="shrink faint" style={{ paddingBottom: 8 }}>→</span>
            <input className="input" value={row.response}
                   placeholder={t("autoreply.response")}
                   onChange={(e) => setFaq(row.id, { response: e.target.value })} />
            <button className="icon-btn shrink" style={{ marginBottom: 4 }}
                    onClick={() => removeFaq(row.id)}
                    disabled={!row.match && !row.response}>✕</button>
          </div>
        ))}

        {draft.faq.filter((r) => r.match.trim() || r.response.trim()).length
          >= faqLimit && faqLimit > 0 && (
          <p className="field-hint">
            {t("autoreply.faq_limit_reached", { limit: faqLimit })}
          </p>
        )}
      </div>
    </>
  );
}
