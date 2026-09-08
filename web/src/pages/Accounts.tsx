import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { CampaignModal } from "../components/CampaignModal";
import { LoginModal } from "../components/LoginModal";
import {
  Confirm, EntityBadge, Field, IssueDot, Modal, PageHead, Status, Switch,
  fmtTime, issueText,
} from "../components/ui";
import type { T } from "../i18n";
import type { Account, Campaign } from "../types";

interface CheckResult { tone: "ok" | "warn" | "err"; text: string }

/** What the check concluded, in the terms the user asked for: green when the
 *  account passed, red when it did not, yellow when it passed but the answer
 *  left something unclear. */
function verdictOf(account: Account, t: T): CheckResult {
  const error = account.issues.find((i) => i.level === "error");
  if (error) return { tone: "err", text: issueText(t, error) };

  // A found limit is a failed check, even though the dot in the list stays
  // yellow: the list shows how bad things are, this line answers one question.
  if (account.spam_state === "LIMITED" || account.spam_state === "FAILED") {
    const spam = account.issues.find((i) => i.code.startsWith("account.spam"));
    return { tone: "err", text: spam ? issueText(t, spam) : t("accounts.check_bad") };
  }

  const warning = account.issues.find((i) => i.level === "warning");
  if (warning) return { tone: "warn", text: issueText(t, warning) };
  return { tone: "ok", text: t("accounts.check_ok") };
}

export function Accounts() {
  const { snapshot, t, act } = useStore();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [login, setLogin] = useState(false);
  const [editing, setEditing] = useState<Account | null>(null);
  const [removing, setRemoving] = useState<Account | null>(null);
  const [campaignFor, setCampaignFor] = useState<string | null>(null);
  const [editCampaign, setEditCampaign] = useState<Campaign | null>(null);

  if (!snapshot) return null;
  const checkup = snapshot.checkup;

  function toggle(id: string) {
    setOpen((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <>
      <PageHead
        title={t("accounts.title")}
        sub={t("accounts.sub")}
        actions={
          <>
            <button className="btn" title={t("accounts.rescan_hint")}
                    onClick={() => act(
                      () => api.accounts.rescan(), t("common.refresh"))}>
              {t("accounts.rescan")}
            </button>
            <button className="btn" disabled={checkup.running}
                    title={t("accounts.probe_all_hint")}
                    onClick={() => act(() => api.accounts.probeAll())}>
              {checkup.running
                ? t("accounts.checking_progress",
                    { done: checkup.done, total: checkup.total })
                : t("accounts.probe_all")}
            </button>
            <button className="btn accent" onClick={() => setLogin(true)}>
              {t("accounts.login")}
            </button>
          </>
        }
      />

      {!snapshot.accounts.length && <div className="empty">{t("accounts.empty")}</div>}

      {snapshot.accounts.map((account) => {
        const campaigns = snapshot.campaigns.filter(
          (c) => c.account_id === account.id);
        const isOpen = open.has(account.id);
        return (
          <div className="acc-card account" key={account.id}>
            <div className="acc-head" onClick={() => toggle(account.id)}>
              <span className={`chevron${isOpen ? " open" : ""}`}>›</span>
              <EntityBadge kind="account" />
              <div className="acc-id">
                <div className="acc-name truncate">{account.handle}</div>
                <div className="acc-meta truncate">
                  {account.display_name}
                  {account.phone ? ` · ${account.phone}` : ""}
                </div>
              </div>
              <span className="pill col">
                {t.count(campaigns.length, "count.campaigns")}
              </span>
              <span className="pill col wide">
                {t("accounts.autoreply")}:{" "}
                {account.has_own_autoreply
                  ? t("accounts.autoreply_own")
                  : t("accounts.autoreply_inherited")}
              </span>
              <div className="acc-status">
                <Status state={account.effective} issues={account.issues}
                        kind="account" />
                <button className="btn sm" onClick={(e) => {
                  e.stopPropagation(); setEditing(account);
                }}>{t("common.edit")}</button>
              </div>
            </div>

            {isOpen && (
              <div className="acc-body">
                <div style={{ display: "flex", gap: 24, flexWrap: "wrap",
                              marginBottom: 14 }}>
                  <Meta label={t("accounts.api")} value={
                    snapshot.api_profiles.find(
                      (p) => p.id === account.api_profile_id)?.name
                    ?? t("common.none")} />
                  <Meta label={t("accounts.proxy")} value={
                    snapshot.network_profiles.find(
                      (p) => p.id === account.network_profile_id)?.name
                    ?? t("common.none")} />
                  <Meta label={t("accounts.operator")} value={
                    snapshot.operators.find(
                      (o) => o.id === account.operator_id)?.handle
                    ?? t("common.none")} />
                  <Meta label={t("accounts.last_check")}
                        value={fmtTime(account.last_check_at, t("common.never"))} />
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: 8,
                              marginBottom: 8 }}>
                  <strong style={{ fontSize: 13 }}>{t("accounts.campaigns")}</strong>
                  <button className="btn sm accent" style={{ marginLeft: "auto" }}
                          onClick={() => setCampaignFor(account.id)}>
                    {t("accounts.new_campaign")}
                  </button>
                </div>

                {!campaigns.length ? (
                  <p className="faint" style={{ fontSize: 12.5, margin: 0 }}>
                    {t("accounts.no_campaigns")}
                  </p>
                ) : (
                  <table className="table">
                    <tbody>
                      {campaigns.map((c) => (
                        <tr key={c.id}>
                          <td style={{ width: 26 }}>
                            <IssueDot issues={c.issues} state={c.effective} />
                          </td>
                          <td>{c.name}</td>
                          <td className="muted" style={{ width: 130 }}>
                            {t(`state.${c.effective}`)}
                          </td>
                          <td className="muted num" style={{ width: 110 }}>
                            {c.is_recurring
                              ? c.sent_total
                              : `${c.sent_count}/${c.total_count}`}
                            {c.failed_count > 0 && (
                              <span className="err-text"> · {c.failed_count}</span>
                            )}
                          </td>
                          <td className="actions">
                            <CampaignActions campaign={c} />
                            <button className="btn sm ghost"
                                    onClick={() => setEditCampaign(c)}>
                              {t("common.edit")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        );
      })}

      {login && <LoginModal asOperator={false} onClose={() => setLogin(false)} />}
      {editing && (
        <AccountModal account={editing} onClose={() => setEditing(null)}
                      onDelete={() => { setRemoving(editing); setEditing(null); }} />
      )}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", { name: removing.handle })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const id = removing.id;
            setRemoving(null);
            void act(() => api.accounts.remove(id));
          }}
        />
      )}
      {campaignFor && (
        <CampaignModal campaign={null} accountId={campaignFor}
                       onClose={() => setCampaignFor(null)} />
      )}
      {editCampaign && (
        <CampaignModal campaign={editCampaign}
                       onClose={() => setEditCampaign(null)} />
      )}
    </>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      <div style={{ fontSize: 13 }}>{value}</div>
    </div>
  );
}

export function CampaignActions({ campaign }: { campaign: Campaign }) {
  const { t, act } = useStore();
  const running = campaign.raw_state === "SCHEDULED" || campaign.raw_state === "RUNNING";
  return running ? (
    <button className="btn sm ghost"
            onClick={() => act(() => api.campaigns.pause(campaign.id))}>
      {t("campaigns.pause")}
    </button>
  ) : (
    <button className="btn sm ghost"
            onClick={() => act(() => api.campaigns.start(campaign.id))}>
      {t("campaigns.start")}
    </button>
  );
}

function AccountModal({ account, onClose, onDelete }: {
  account: Account; onClose: () => void; onDelete: () => void;
}) {
  const { snapshot, t, act } = useStore();
  const ownReply = snapshot?.auto_reply.find((c) => c.owner_id === account.id);
  const [apiId, setApiId] = useState(account.api_profile_id ?? "");
  const [proxyId, setProxyId] = useState(account.network_profile_id ?? "");
  const [operatorId, setOperatorId] = useState(account.operator_id ?? "");
  const [disabled, setDisabled] = useState(account.disabled);
  // The check runs in the background, so the answer arrives through the
  // snapshot rather than from the call: wait for this account's check stamp to
  // move while nothing is running any more.
  const [waiting, setWaiting] = useState(false);
  const [result, setResult] = useState<CheckResult | null>(null);
  const before = useRef<string | null>(null);
  const fresh = snapshot?.accounts.find((a) => a.id === account.id) ?? account;
  const running = snapshot?.checkup.running ?? false;

  useEffect(() => {
    if (!waiting || running) return;
    if (fresh.last_check_at === before.current) return;
    setWaiting(false);
    setResult(verdictOf(fresh, t));
  }, [waiting, running, fresh.last_check_at]);   // eslint-disable-line react-hooks/exhaustive-deps

  // The check can switch the account off while this window is open. Without
  // following that, the switch would still read "on" and saving would quietly
  // turn the account back on - undoing the stop and clearing its mark.
  useEffect(() => {
    setDisabled(fresh.disabled);
  }, [fresh.disabled]);

  async function save() {
    await act(() => api.accounts.update({
      id: account.id,
      api_profile_id: apiId || null,
      network_profile_id: proxyId || null,
      operator_id: operatorId || null,
    }));
    if (disabled !== fresh.disabled) {
      await act(() => api.accounts.setDisabled(account.id, disabled));
    }
    onClose();
  }

  return (
    <Modal
      title={account.handle}
      onClose={onClose}
      footer={
        <>
          <button className="btn danger left" onClick={onDelete}>
            {t("common.delete")}
          </button>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn accent" onClick={save}>{t("common.save")}</button>
        </>
      }
    >
      <Field label={t("accounts.api")}>
        <select className="select" value={apiId}
                onChange={(e) => setApiId(e.target.value)}>
          <option value="">{t("common.none")}</option>
          {snapshot?.api_profiles.map((p) => (
            <option key={p.id} value={p.id}>{p.name || p.id}</option>
          ))}
        </select>
      </Field>

      <Field label={t("accounts.proxy")}>
        <select className="select" value={proxyId}
                onChange={(e) => setProxyId(e.target.value)}>
          <option value="">{t("common.none")}</option>
          {snapshot?.network_profiles.map((p) => (
            <option key={p.id} value={p.id}>{p.name || p.host}</option>
          ))}
        </select>
      </Field>

      <Field label={t("accounts.operator")}>
        <select className="select" value={operatorId}
                onChange={(e) => setOperatorId(e.target.value)}>
          <option value="">{t("common.none")}</option>
          {snapshot?.operators.map((o) => (
            <option key={o.id} value={o.id}>{o.handle}</option>
          ))}
        </select>
      </Field>

      <div style={{ margin: "16px 0" }}>
        <Switch checked={!disabled} onChange={(v) => setDisabled(!v)}
                label={disabled ? t("common.disabled") : t("common.enabled")} />
      </div>

      <div style={{ borderTop: "1px solid var(--border-soft)", paddingTop: 14,
                    marginBottom: 16 }}>
        <Switch
          checked={!!ownReply?.enabled}
          disabled={!ownReply}
          label={t("accounts.autoreply_switch")}
          onChange={(v) => act(() => api.autoreply.enable(account.id, v))}
        />
        <p className="field-hint">
          {ownReply
            ? t("accounts.autoreply_own_hint")
            : t("accounts.autoreply_inherited_hint")}
        </p>
      </div>

      <div className="modal-section">
        <button className="btn" disabled={!account.key}
                onClick={() => act(() => api.accounts.promote(account.id),
                                   t("accounts.promote"))}>
          {t("accounts.promote")}
        </button>
        <p className="field-hint">{t("accounts.promote_hint")}</p>

        <button className="btn" style={{ marginTop: 12 }} disabled={waiting}
                onClick={() => {
                  before.current = fresh.last_check_at;
                  setResult(null);
                  setWaiting(true);
                  void act(() => api.accounts.check(account.id));
                }}>
          {waiting ? t("accounts.checking_one") : t("accounts.check_one")}
        </button>
        <p className="field-hint">{t("accounts.check_one_hint")}</p>
        {result && (
          <p className={`check-result ${result.tone}`}>
            <span className={`dot ${result.tone}`} /> {result.text}
          </p>
        )}
      </div>
    </Modal>
  );
}
