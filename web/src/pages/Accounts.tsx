import { useEffect, useState } from "react";
import { api } from "../api";
import { buttonCheckGoing } from "../checkup";
import { useStore } from "../state";
import {
  CampaignDuplicate, CampaignModal, CampaignToggle,
} from "../components/CampaignModal";
import { LoginModal } from "../components/LoginModal";
import {
  ActionButton, BulkBar, Confirm, DataTable, EntityCard, Field, FormError, FormFooter,
  Meta, MetaRow, Modal, PageHead, SectionHead, Select, Status, Switch, byId, fmtTime,
  issueText, useBulk, useDraft, useHashFocus, useReveal, useSelection, useSubmit,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { T } from "../i18n";
import type { Account, Campaign } from "../types";

interface CheckResult { tone: "ok" | "warn" | "err"; text: string }

/** What a check of one account concluded, in the terms the user asked for. */
function verdictOf(account: Account, t: T): CheckResult {
  const error = account.issues.find((i) => i.level === "error");
  if (error) return { tone: "err", text: issueText(t, error) };
  // a found limit is a failed check, even though the list dot stays yellow
  if (account.spam_state === "LIMITED" || account.spam_state === "FAILED") {
    const spam = account.issues.find((i) => i.code.startsWith("account.spam"));
    return { tone: "err", text: spam ? issueText(t, spam) : t("accounts.check_bad") };
  }
  const warning = account.issues.find((i) => i.level === "warning");
  if (warning) return { tone: "warn", text: issueText(t, warning) };
  return { tone: "ok", text: t("accounts.check_ok") };
}

/** The API, proxy and operator pickers of one account - or of several. */
export function useProfileChoices(withUnchanged: boolean) {
  const { snapshot, t } = useStore();
  const keep = withUnchanged ? [{ value: "", label: t("bulk.unchanged") }] : [];
  const none = { value: withUnchanged ? "none" : "", label: t("common.none") };
  return {
    apis: [...keep, ...(withUnchanged ? [] : [none]),
           ...(snapshot?.api_profiles ?? []).map((p) => ({ value: p.id, label: p.name || p.id }))],
    proxies: [...keep, none,
              ...(snapshot?.network_profiles ?? []).map((p) => ({ value: p.id, label: p.name || p.host }))],
    operators: [...keep, none,
                ...(snapshot?.operators ?? []).map((o) => ({ value: o.id, label: o.handle }))],
  };
}

export function Accounts() {
  const { snapshot, t, act, lang } = useStore();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [login, setLogin] = useState(false);
  // Windows keep ids and read the live records: see byId.
  const [editing, setEditing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [campaignFor, setCampaignFor] = useState<string | null>(null);
  const [editCampaign, setEditCampaign] = useState<string | null>(null);
  const [relogin, setRelogin] = useState(false);
  const [bulkEdit, setBulkEdit] = useState(false);
  const [bulkDelete, setBulkDelete] = useState(false);
  const selection = useSelection((snapshot?.accounts ?? []).map((a) => a.id));
  const bulk = useBulk("accounts", selection);
  useHashFocus((id) => setOpen((cur) => new Set(cur).add(id)));

  if (!snapshot) return null;
  const checkup = snapshot.checkup;
  const mine = checkup.running && checkup.kind === "accounts";
  const checking = buttonCheckGoing(checkup);
  const edited = byId(snapshot.accounts, editing);
  const removed = byId(snapshot.accounts, removing);
  const editedCampaign = byId(snapshot.campaigns, editCampaign);

  const toggle = (id: string) => setOpen((cur) => {
    const next = new Set(cur);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const profileName = (id: string | null, list: { id: string; name: string }[]) =>
    list.find((p) => p.id === id)?.name || t("common.none");

  const campaignColumns: Column<Campaign>[] = [
    { key: "status", label: t("common.status"), width: 140,
      render: (c) => <Status state={c.effective} issues={c.issues} /> },
    { key: "name", label: t("common.name"),
      render: (c) => <span title={c.name}>{c.name}</span> },
    { key: "sent", label: t("campaigns.progress"), width: 96, className: "num",
      render: (c) => <SentCount campaign={c} /> },
    { key: "actions", label: "", width: "var(--w-actions-3)", className: "actions",
      render: (c) => (
        <span className="ab-row">
          <CampaignToggle campaign={c} />
          <CampaignDuplicate campaign={c} />
          <ActionButton size="sm" variant="ghost" onClick={() => setEditCampaign(c.id)}>
            {t("common.edit")}
          </ActionButton>
        </span>
      ) },
  ];

  return (
    <>
      <PageHead
        title={t("accounts.title")}
        sub={t("accounts.sub")}
        actions={
          <>
            <ActionButton title={t("accounts.rescan_hint")}
                          onClick={() => act(() => api.accounts.rescan(), t("common.refresh"))}>
              {t("accounts.rescan")}
            </ActionButton>
            <ActionButton disabled={checking} title={t("accounts.probe_all_hint")}
                          onClick={() => act(() => api.accounts.probeAll())}>
              {mine ? t("accounts.checking_progress", { done: checkup.done, total: checkup.total })
                    : t("accounts.probe_all")}
            </ActionButton>
            <ActionButton variant="primary" onClick={() => setLogin(true)}>
              {t("accounts.login")}
            </ActionButton>
          </>
        }
      />

      <BulkBar selection={selection}>
        <ActionButton size="sm" disabled={checking}
                      onClick={() => act(() => api.bulk.check("accounts", selection.ids))}>
          {t("common.check")}
        </ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("enable")}>{t("common.enable")}</ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("disable")}>{t("common.disable")}</ActionButton>
        <ActionButton size="sm" onClick={() => setBulkEdit(true)}>{t("common.edit")}</ActionButton>
        <ActionButton size="sm" variant="danger" onClick={() => setBulkDelete(true)}>
          {t("common.delete")}
        </ActionButton>
      </BulkBar>

      <div className="stack">
        {!snapshot.accounts.length && <div className="empty">{t("accounts.empty")}</div>}
        {snapshot.accounts.map((account) => {
          const campaigns = snapshot.campaigns.filter((c) => c.account_id === account.id);
          return (
            <EntityCard
              key={account.id} id={account.id} kind="account"
              open={open.has(account.id)} onToggle={() => toggle(account.id)}
              picked={selection.has(account.id)} onPick={() => selection.toggle(account.id)}
              name={account.handle}
              sub={[account.display_name, account.phone]
                .filter((x) => x && x !== account.handle).join(" · ") || "—"}
              pills={[
                t.count(campaigns.length, "count.campaigns"),
                `${t("accounts.autoreply")}: ${account.has_own_autoreply
                  ? t("accounts.autoreply_own") : t("accounts.autoreply_inherited")}`,
              ]}
              status={<Status state={account.effective} issues={account.issues}
                              until={account.flood_until} />}
              onEdit={() => setEditing(account.id)}
            >
              <MetaRow>
                <Meta label={t("accounts.api")}
                      value={profileName(account.api_profile_id, snapshot.api_profiles)} />
                <Meta label={t("accounts.proxy")}
                      value={profileName(account.network_profile_id, snapshot.network_profiles)} />
                <Meta label={t("accounts.operator")} value={
                  snapshot.operators.find((o) => o.id === account.operator_id)?.handle
                  ?? t("common.none")} />
                <Meta label={t("accounts.last_check")}
                      value={fmtTime(account.last_check_at, lang, t("common.never"))} />
              </MetaRow>

              {/* A revoked key cannot be checked back to life: the card offers
                  the one thing that helps. */}
              {account.effective === "AUTH_DEAD" && (
                <ReloginBlock onRelogin={() => setRelogin(true)} />
              )}

              <div>
                <SectionHead title={t("accounts.campaigns")} action={
                  <ActionButton onClick={() => setCampaignFor(account.id)}>
                    {t("accounts.new_campaign")}
                  </ActionButton>
                } />
                <DataTable columns={campaignColumns} rows={campaigns} rowKey={(c) => c.id}
                           empty={t("accounts.no_campaigns")} />
              </div>
            </EntityCard>
          );
        })}
      </div>

      {bulkDelete && (
        <DeleteAccounts
          text={t("bulk.confirm_delete", { n: selection.count })}
          linked={snapshot.accounts.some((a) => selection.has(a.id) && a.linked_operator_id)}
          keepLabel={t("accounts.keep_operators")}
          onCancel={() => setBulkDelete(false)}
          onConfirm={(keepOperator) => {
            setBulkDelete(false);
            void bulk.run("delete", { keep_operator: keepOperator });
          }} />
      )}
      {bulkEdit && (
        <BulkAccountModal count={selection.count} onClose={() => setBulkEdit(false)}
                          apply={bulk.apply} />
      )}
      {login && <LoginModal asOperator={false} onClose={() => setLogin(false)} />}
      {relogin && <LoginModal asOperator={false} onClose={() => setRelogin(false)} />}
      {edited && (
        <AccountModal account={edited} onClose={() => setEditing(null)}
                      onDelete={() => { setRemoving(edited.id); setEditing(null); }} />
      )}
      {removed && (
        <DeleteAccounts
          text={t("common.confirm_delete", { name: removed.handle })}
          linked={!!removed.linked_operator_id}
          keepLabel={t("accounts.keep_operator")}
          onCancel={() => setRemoving(null)}
          onConfirm={(keepOperator) => {
            const id = removed.id;
            setRemoving(null);
            void act(() => api.accounts.remove(id, keepOperator));
          }} />
      )}
      {campaignFor && (
        <CampaignModal campaign={null} accountId={campaignFor}
                       onClose={() => setCampaignFor(null)} />
      )}
      {editedCampaign && (
        <CampaignModal campaign={editedCampaign} onClose={() => setEditCampaign(null)} />
      )}
    </>
  );
}

/** «Удалить?» for one account or several. An operator made from one of them
 *  raises the one question, and the answer belongs to this dialog alone:
 *  the next one starts unanswered again. */
function DeleteAccounts({ text, linked, keepLabel, onCancel, onConfirm }: {
  text: string;
  linked: boolean;
  keepLabel: string;
  onCancel: () => void;
  onConfirm: (keepOperator: boolean) => void;
}) {
  const [keepOperator, setKeepOperator] = useState(false);
  return (
    <Confirm text={text} onCancel={onCancel} onConfirm={() => onConfirm(keepOperator)}>
      {linked && <Switch checked={keepOperator} onChange={setKeepOperator} label={keepLabel} />}
    </Confirm>
  );
}

/** What a campaign has sent: in total for one that repeats, of its channels
 *  for one pass. Failures after a dot. */
export function SentCount({ campaign: c }: { campaign: Campaign }) {
  return (
    <>
      {c.is_recurring ? c.sent_total : `${c.sent_count}/${c.total_count}`}
      {c.failed_count > 0 && <span className="err-text"> · {c.failed_count}</span>}
    </>
  );
}

/** Sign in again: the one thing that helps a revoked session. The same
 *  block on an account and on an operator. */
export function ReloginBlock({ onRelogin }: { onRelogin: () => void }) {
  const { t } = useStore();
  return (
    <div>
      <SectionHead title={t("accounts.relogin_hint")} action={
        <ActionButton variant="primary" onClick={onRelogin}>{t("accounts.relogin")}</ActionButton>
      } />
    </div>
  );
}

/** Change API, proxy or operator on several accounts at once: one request,
 *  the same save the account's own window makes, so each account reconnects
 *  once, through its new profiles. Blank means «leave it alone». */
function BulkAccountModal({ count, onClose, apply }: {
  /** How many were picked: the window's title. Read once - a finished
   *  save clears the selection while the window is still closing. */
  count: number;
  onClose: () => void;
  apply: (action: string, values: Record<string, unknown>) => Promise<unknown>;
}) {
  const { t } = useStore();
  const choices = useProfileChoices(true);
  const [picked] = useState(count);
  const [apiId, setApiId] = useState("");
  const [proxyId, setProxyId] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const submit = useSubmit();

  async function save() {
    const values: Record<string, unknown> = {};
    if (apiId) values.api_profile_id = apiId;
    if (proxyId) values.network_profile_id = proxyId === "none" ? "" : proxyId;
    if (operatorId) values.operator_id = operatorId === "none" ? "" : operatorId;
    if (!Object.keys(values).length) { onClose(); return; }
    if (await submit.run(() => apply("update", values))) onClose();
  }

  return (
    <Modal title={t("bulk.edit_title", { n: picked })} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} />}>
      <FormError message={submit.error} />
      <Field label={t("accounts.api")}>
        <Select value={apiId} options={choices.apis} onChange={setApiId} />
      </Field>
      <Field label={t("accounts.proxy")}>
        <Select value={proxyId} options={choices.proxies} onChange={setProxyId} />
      </Field>
      <Field label={t("accounts.operator")}>
        <Select value={operatorId} options={choices.operators} onChange={setOperatorId} />
      </Field>
    </Modal>
  );
}

/** One account's window. Everything in it is a draft over the live record
 *  (useDraft): Save sends what changed in one request, Cancel drops it all.
 *  The actions in it act on what the window shows, so they save the
 *  changes first - «Проверить» is available the moment the switch says
 *  «Включено», and pressing it switches the account on and checks it. */
function AccountModal({ account, onClose, onDelete }: {
  /** The live record: the page reads it from the snapshot on every render. */
  account: Account;
  onClose: () => void;
  onDelete: () => void;
}) {
  const { snapshot, t } = useStore();
  const choices = useProfileChoices(false);
  const ownReply = snapshot?.auto_reply.find((c) => c.owner_id === account.id);
  const form = useDraft({
    api_profile_id: account.api_profile_id ?? "",
    network_profile_id: account.network_profile_id ?? "",
    operator_id: account.operator_id ?? "",
    disabled: account.disabled,
    autoreply: ownReply?.enabled ?? false,
  });
  const submit = useSubmit();
  // The check runs in the background, so its answer arrives with the
  // snapshot, once the run this window started is over.
  const [waiting, setWaiting] = useState(false);
  const [result, setResult] = useState<CheckResult | null>(null);
  const resultRef = useReveal<HTMLParagraphElement>(result);
  const checking = snapshot ? buttonCheckGoing(snapshot.checkup) : false;

  useEffect(() => {
    if (!waiting || checking) return;
    setWaiting(false);
    setResult(verdictOf(account, t));
  }, [waiting, checking]);   // eslint-disable-line react-hooks/exhaustive-deps

  /** Save what changed, if anything, then do `then` - one request for the
   *  changes, and the window stays open. */
  const saveThen = (then?: () => Promise<unknown>) => submit.run(async () => {
    if (form.dirty) await api.accounts.update({ id: account.id, ...form.changes });
    if (then) await then();
  });

  async function save() {
    if (await saveThen()) onClose();
  }

  async function check() {
    setResult(null);
    // Refused - another button's run, nothing to check - is said in the
    // window rather than waited for.
    if (await saveThen(() => api.accounts.check(account.id))) setWaiting(true);
  }

  const savesFirst = form.dirty ? ` ${t("accounts.saves_first")}` : "";
  const checkHint = form.values.disabled ? t("accounts.check_off_hint")
    : checking && !waiting ? t("err.check.busy")
    : t("accounts.check_one_hint") + savesFirst;

  return (
    <Modal title={account.handle} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} left={
             <ActionButton variant="danger" onClick={onDelete}>{t("common.delete")}</ActionButton>
           } />}>
      <FormError message={submit.error} />
      <Field label={t("accounts.api")}>
        <Select value={form.values.api_profile_id} options={choices.apis}
                onChange={(v) => form.set("api_profile_id", v)} />
      </Field>
      <Field label={t("accounts.proxy")}>
        <Select value={form.values.network_profile_id} options={choices.proxies}
                onChange={(v) => form.set("network_profile_id", v)} />
      </Field>
      <Field label={t("accounts.operator")}>
        <Select value={form.values.operator_id} options={choices.operators}
                onChange={(v) => form.set("operator_id", v)} />
      </Field>
      <Field>
        <Switch checked={!form.values.disabled} onChange={(v) => form.set("disabled", !v)}
                label={form.values.disabled ? t("common.disabled") : t("common.enabled")} />
      </Field>

      <div className="modal-section">
        <Switch checked={form.values.autoreply} disabled={!ownReply}
                label={t("accounts.autoreply_switch")}
                onChange={(v) => form.set("autoreply", v)} />
        <p className="field-hint">
          {ownReply ? t("accounts.autoreply_own_hint") : t("accounts.autoreply_inherited_hint")}
        </p>
      </div>

      <div className="modal-section">
        {account.linked_operator_id ? (
          <p className="field-hint">{t("accounts.is_operator")}</p>
        ) : (
          <>
            <ActionButton disabled={!account.key || submit.busy}
                          onClick={() => void saveThen(() => api.accounts.promote(account.id))}>
              {t("accounts.promote")}
            </ActionButton>
            <p className="field-hint">{t("accounts.promote_hint") + savesFirst}</p>
          </>
        )}
      </div>

      <div className="modal-section">
        <ActionButton disabled={submit.busy || waiting || checking || form.values.disabled}
                      onClick={() => void check()}>
          {waiting ? t("accounts.checking_one") : t("accounts.check_one")}
        </ActionButton>
        <p className="field-hint">{checkHint}</p>
        {result && (
          <p className="check-result" ref={resultRef}>
            <Status tone={result.tone} label={result.text} />
          </p>
        )}
      </div>
    </Modal>
  );
}
