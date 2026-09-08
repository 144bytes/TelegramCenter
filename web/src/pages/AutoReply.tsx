import { useEffect, useMemo, useRef, useState } from "react";
import { api, errorMsg } from "../api";
import { useStore } from "../state";
import {
  ActionButton, Field, FormError, PageHead, RangeInput, Select, Status, Switch,
  TextArea, TextInput, issueText,
} from "../components/ui";
import type { AutoReplyConfig, AutoReplyKind, AutoReplyRule, Snapshot } from "../types";

const GLOBAL = "global";

/** How long typing has to pause before the page saves. */
const SAVE_AFTER_MS = 1000;

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

/** What the page shows for an owner: its own rules, or the shared ones it
 *  answers with until it has its own. */
function draftOf(snapshot: Snapshot, config: AutoReplyConfig | null): Draft {
  const source = config ?? snapshot.auto_reply.find((c) => c.owner_id === GLOBAL) ?? null;
  const rules: AutoReplyRule[] = source?.rules ?? [];
  const pick = (kind: AutoReplyKind) => rules.find((r) => r.kind === kind)?.response ?? "";
  return {
    enabled: source?.enabled ?? false,
    delayMin: source?.delay_min_sec ?? snapshot.settings.autoreply.default_delay_min_sec,
    delayMax: source?.delay_max_sec ?? snapshot.settings.autoreply.default_delay_max_sec,
    first: pick("FIRST_MESSAGE"),
    periodic: pick("PERIODIC"),
    faq: rules.filter((r) => r.kind === "FAQ")
      .map((r) => ({ id: r.id, match: r.match, response: r.response })),
  };
}

/** The body the backend stores. Empty rows are no rules. */
function payloadOf(owner: string, draft: Draft) {
  const rules: Record<string, unknown>[] = [];
  if (draft.first.trim()) rules.push({ kind: "FIRST_MESSAGE", enabled: true, response: draft.first });
  if (draft.periodic.trim()) rules.push({ kind: "PERIODIC", enabled: true, response: draft.periodic });
  for (const row of draft.faq) {
    if (!row.match.trim() && !row.response.trim()) continue;
    rules.push({ kind: "FAQ", enabled: true, match: row.match, response: row.response });
  }
  return { owner_id: owner, enabled: draft.enabled, delay_min_sec: draft.delayMin,
           delay_max_sec: draft.delayMax, rules };
}

export function AutoReplyPage() {
  const { snapshot, t, act, notify, refresh } = useStore();
  const [owner, setOwner] = useState<string>(GLOBAL);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // rows that have been emptied and are collapsing out of the list
  const [leaving, setLeaving] = useState<Set<string>>(new Set());

  // What was last loaded or stored, to tell an edit from a re-render.
  const stored = useRef("");
  const timer = useRef<number | undefined>(undefined);
  const latest = useRef<{ owner: string; draft: Draft | null }>({ owner, draft });
  latest.current = { owner, draft };

  const config = useMemo(
    () => snapshot?.auto_reply.find((c) => c.owner_id === owner) ?? null, [snapshot, owner]);
  const account = owner === GLOBAL ? null
    : snapshot?.accounts.find((a) => a.id === owner) ?? null;
  const inherited = owner !== GLOBAL && !config;
  const faqLimit = snapshot?.settings.autoreply.faq_limit ?? 5;

  // Load the draft only when the owner changes. Loading on every snapshot
  // would wipe what is being typed whenever anything else happens.
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!snapshot || loadedFor.current === owner) return;
    loadedFor.current = owner;
    const fresh = draftOf(snapshot, config);
    stored.current = JSON.stringify(payloadOf(owner, fresh));
    setDraft(fresh);
    setError(null);
    setSaved(false);
  }, [owner, config, snapshot]);

  /** Store the draft if it changed. False when the backend refused it. */
  async function flush(): Promise<boolean> {
    window.clearTimeout(timer.current);
    const { owner: who, draft: now } = latest.current;
    if (!now) return true;
    const body = payloadOf(who, now);
    const text = JSON.stringify(body);
    if (text === stored.current) return true;
    try {
      await api.autoreply.save(body);
      stored.current = text;
      setError(null);
      setSaved(true);
      await refresh();
      return true;
    } catch (err) {
      // shown inline, next to the fields that caused it - never as a popup
      setError(t.msg(errorMsg(err)));
      setSaved(false);
      return false;
    }
  }
  const flushRef = useRef(flush);
  flushRef.current = flush;

  // Leaving the page keeps what was typed. The page is gone by then, so a
  // refusal can only be said as a notice.
  useEffect(() => () => {
    window.clearTimeout(timer.current);
    void flushRef.current().then((ok) => {
      if (!ok) notify(t("autoreply.not_saved"), "err");
    });
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!snapshot || !draft) return null;

  /** Apply an edit, and save it once typing pauses - or at once, for a
   *  switch or a finished number. */
  function change(patch: Partial<Draft>, now = false) {
    const cur = latest.current.draft;
    if (!cur) return;
    const next = { ...cur, ...patch };
    // the save below reads this, before React has rendered the new state
    latest.current = { owner, draft: next };
    setDraft(next);
    setSaved(false);
    window.clearTimeout(timer.current);
    if (now) void flush();
    else timer.current = window.setTimeout(() => void flush(), SAVE_AFTER_MS);
  }

  /** Another owner: only once this one's edits are stored. A refusal keeps
   *  the page where it is, with the error beside the fields. */
  async function switchTo(next: string) {
    if (next === owner) return;
    if (!(await flush())) return;
    setOwner(next);
  }

  const operatorGap = owner === GLOBAL
    ? snapshot.auto_reply.find((c) => c.owner_id === GLOBAL)?.operator_gap ?? null
    : account?.operator_gap ?? null;

  // one blank row is always offered, until the limit is reached
  const faqRows = [...draft.faq];
  const blanks = faqRows.filter((r) => !r.match.trim() && !r.response.trim()).length;
  if (blanks === 0 && faqRows.length < faqLimit) {
    faqRows.push({ id: nextId(), match: "", response: "" });
  }

  function setFaq(id: string, patch: Partial<{ match: string; response: string }>) {
    const existing = draft!.faq.find((r) => r.id === id);
    change({ faq: existing
      ? draft!.faq.map((r) => (r.id === id ? { ...r, ...patch } : r))
      : [...draft!.faq, { id, match: "", response: "", ...patch }] });
  }

  function removeFaq(id: string) {
    // let the row collapse before it leaves, so the ones below slide up
    setLeaving((prev) => new Set(prev).add(id));
    window.setTimeout(() => {
      change({ faq: (latest.current.draft?.faq ?? []).filter((r) => r.id !== id) }, true);
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

  const owners = [
    { value: GLOBAL, label: t("autoreply.global") },
    ...snapshot.accounts.map((a) => ({ value: a.id, label: a.handle })),
  ];
  const shown = config ?? snapshot.auto_reply.find((c) => c.owner_id === GLOBAL) ?? null;

  return (
    <>
      <PageHead title={t("autoreply.title")} sub={t("autoreply.sub")} />

      <div className="stack">
      <div className="card stack">
        <div className="toolbar">
          <span className="field-label">{t("campaigns.account")}</span>
          <Select width="pick" value={owner} options={owners} onChange={(v) => void switchTo(v)} />
          <Switch checked={draft.enabled} onChange={(v) => change({ enabled: v }, true)}
                  label={draft.enabled ? t("common.enabled") : t("common.disabled")} />
          {/* off is what the switch already says */}
          {shown && shown.effective !== "DISABLED" && (
            <Status state={shown.effective} issues={shown.issues} />
          )}
          <span className="spacer" />
          <span className="saved-note">{saved ? t("settings.saved") : ""}</span>
          {owner !== GLOBAL && config && (
            <ActionButton onClick={async () => {
              window.clearTimeout(timer.current);
              await act(() => api.autoreply.reset(owner));
              // the account inherits again: load the shared rules
              loadedFor.current = null;
              setError(null);
            }}>
              {t("autoreply.use_global")}
            </ActionButton>
          )}
        </div>
        <p className="note">
          {owner === GLOBAL ? t("autoreply.global_hint")
            : inherited ? t("autoreply.inherited_notice")
            : t("autoreply.for_account", { name: account?.handle ?? "" })}
        </p>
      </div>

      <div className="card">
        <FormError message={error} />

        <Field label={t("autoreply.delay")} hint={t("autoreply.delay_hint")}>
          <RangeInput min={draft.delayMin} max={draft.delayMax} unit={t("common.seconds")}
                      onChange={(lo, hi) => change({ delayMin: lo, delayMax: hi }, true)} />
        </Field>

        {/* Whether @operator works here is decided by the backend, for the
            owner this page is about. A rule that says it is never switched
            off: the accounts without an operator just do not send it. */}
        <p className="field field-hint">
          {operatorGap
            ? <Status tone={operatorGap.level === "error" ? "err" : "warn"}
                      label={issueText(t, operatorGap)} />
            : t("autoreply.operator_note")}
        </p>

        <Field label={t("autoreply.first")} hint={t("autoreply.first_hint")}>
          <TextArea size="short" value={draft.first} onChange={(first) => change({ first })} />
        </Field>

        <Field label={t("autoreply.periodic")} hint={t("autoreply.periodic_hint")}>
          <TextArea size="short" value={draft.periodic}
                    onChange={(periodic) => change({ periodic })} />
        </Field>

        <span className="field-label">
          {t("autoreply.faq")} · {t("autoreply.faq_hint", { limit: faqLimit })}
        </span>

        {faqRows.map((row) => (
          <div className={`faq-row${leaving.has(row.id) ? " leaving" : ""}`} key={row.id}
               onBlur={(e) => {
                 // ignore moving between the two fields of the same row
                 if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
                 dropIfEmpty(row.id);
               }}>
            <TextInput className="match" value={row.match} placeholder={t("autoreply.match")}
                       onChange={(match) => setFaq(row.id, { match })} />
            <span className="faint">→</span>
            <TextInput className="reply" value={row.response}
                       placeholder={t("autoreply.response")}
                       onChange={(response) => setFaq(row.id, { response })} />
            <ActionButton variant="icon" size="sm" quiet title={t("common.delete")}
                          disabled={!row.match && !row.response}
                          onClick={() => removeFaq(row.id)}>
              ✕
            </ActionButton>
          </div>
        ))}

        {draft.faq.filter((r) => r.match.trim() || r.response.trim()).length >= faqLimit && (
          <p className="field-hint">{t("autoreply.faq_limit_reached", { limit: faqLimit })}</p>
        )}
      </div>
      </div>
    </>
  );
}
