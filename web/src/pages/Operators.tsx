import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { LoginModal } from "../components/LoginModal";
import { OperatorChat } from "../components/OperatorChat";
import {
  Confirm, EntityBadge, Field, IssueDot, Modal, PageHead, Status, fmtTime,
} from "../components/ui";
import type { Account, Operator } from "../types";

export function Operators() {
  const { snapshot, t, act } = useStore();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [login, setLogin] = useState(false);
  const [editing, setEditing] = useState<Operator | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<Operator | null>(null);
  const [chatsFor, setChatsFor] = useState<Operator | null>(null);
  const [loginInto, setLoginInto] = useState<Operator | null>(null);

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
        title={t("operators.title")}
        sub={t("operators.sub")}
        actions={
          <>
            <button className="btn" title={t("accounts.rescan_hint")}
                    onClick={() => act(() => api.operators.rescan(),
                                       t("common.refresh"))}>
              {t("accounts.rescan")}
            </button>
            <button className="btn" disabled={checkup.running}
                    title={t("accounts.probe_all_hint")}
                    onClick={() => act(() => api.operators.probeAll())}>
              {checkup.running
                ? t("accounts.checking_progress",
                    { done: checkup.done, total: checkup.total })
                : t("accounts.probe_all")}
            </button>
            {/* Adding a real account is the accent one, the same way the
                accounts page is arranged. Saving a bare @username is a much
                smaller act and lives under the list instead. */}
            <button className="btn accent" onClick={() => setLogin(true)}>
              {t("operators.login")}
            </button>
          </>
        }
      />

      {!snapshot.operators.length && (
        <div className="empty">{t("operators.empty")}</div>
      )}

      {snapshot.operators.map((operator) => {
        const bound = snapshot.accounts.filter(
          (a) => a.operator_id === operator.id);
        const free = snapshot.accounts.filter((a) => !a.operator_id);
        const isOpen = open.has(operator.id);

        return (
          <div className="acc-card operator" key={operator.id}>
            <div className="acc-head" onClick={() => toggle(operator.id)}>
              <span className={`chevron${isOpen ? " open" : ""}`}>›</span>
              <EntityBadge kind="operator" />
              <div className="acc-id">
                <div className="acc-name truncate">{operator.handle}</div>
                <div className="acc-meta truncate">
                  {operator.display_name || "—"}
                </div>
              </div>
              <span className="pill col">
                {t.count(bound.length, "count.accounts")}
              </span>
              <span className="pill col wide">
                {operator.logged_in
                  ? t("operators.has_session")
                  : t("operators.handle_only")}
              </span>
              <div className="acc-status">
                <Status state={operator.effective} issues={operator.issues} />
                <button className="btn sm" onClick={(e) => {
                  e.stopPropagation(); setEditing(operator);
                }}>{t("common.edit")}</button>
              </div>
            </div>

            {isOpen && (
              <div className="acc-body">
                <div style={{ display: "flex", gap: 24, flexWrap: "wrap",
                              marginBottom: 14 }}>
                  <Meta label={t("accounts.api")} value={
                    snapshot.api_profiles.find(
                      (p) => p.id === operator.api_profile_id)?.name
                    ?? t("common.none")} />
                  <Meta label={t("accounts.proxy")} value={
                    snapshot.network_profiles.find(
                      (p) => p.id === operator.network_profile_id)?.name
                    ?? t("common.none")} />
                  <Meta label={t("accounts.last_check")}
                        value={fmtTime(operator.last_check_at,
                                       t("common.never"))} />
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: 8,
                              marginBottom: 8 }}>
                  <strong style={{ fontSize: 13 }}>{t("operators.bound")}</strong>
                  <button className="btn sm" style={{ marginLeft: "auto" }}
                          disabled={!operator.logged_in}
                          title={operator.logged_in ? ""
                            : t("operators.not_logged_in")}
                          onClick={() => setChatsFor(operator)}>
                    {t("operators.open_chats")}
                  </button>
                </div>

                <BoundAccounts operator={operator} bound={bound} free={free} />
              </div>
            )}
          </div>
        );
      })}

      {/* A handle is not an account, so it is not offered beside the page's
          real actions - it sits where the list it joins ends. */}
      <button className="add-row" onClick={() => setCreating(true)}
              title={t("operators.new_handle_hint")}>
        <span className="add-row-plus">+</span>
        {t("operators.new_handle")}
      </button>

      {login && <LoginModal asOperator onClose={() => setLogin(false)} />}
      {loginInto && (
        <LoginModal asOperator targetId={loginInto.id}
                    onClose={() => setLoginInto(null)} />
      )}
      {(creating || editing) && (
        <OperatorModal operator={editing}
                       onClose={() => { setCreating(false); setEditing(null); }}
                       onLogin={() => {
                         setLoginInto(editing); setEditing(null);
                       }}
                       onDelete={() => {
                         setRemoving(editing); setEditing(null);
                       }} />
      )}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", { name: removing.handle })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const id = removing.id;
            setRemoving(null);
            void act(() => api.operators.remove(id));
          }}
        />
      )}
      {chatsFor && <ChatsModal operator={chatsFor} onClose={() => setChatsFor(null)} />}
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

/** One operator can carry several accounts, so the binding is edited from
 *  here rather than one account at a time. */
function BoundAccounts({ operator, bound, free }: {
  operator: Operator; bound: Account[]; free: Account[];
}) {
  const { t, act } = useStore();
  const [picked, setPicked] = useState("");

  return (
    <>
      {!bound.length ? (
        <p className="faint" style={{ fontSize: 12.5, margin: "0 0 10px" }}>
          {t("operators.no_accounts")}
        </p>
      ) : (
        <table className="table" style={{ marginBottom: 10 }}>
          <tbody>
            {bound.map((account) => (
              <tr key={account.id}>
                <td style={{ width: 26 }}>
                  <IssueDot issues={account.issues} state={account.effective}
                            kind="account" />
                </td>
                <td>{account.handle}</td>
                <td className="muted truncate">{account.display_name}</td>
                <td className="actions">
                  <button className="btn sm danger" onClick={() => act(
                    () => api.accounts.update({
                      id: account.id, operator_id: null }))}>
                    {t("operators.unbind")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* A disabled picker next to a note saying there is nothing to pick
          was two ways of saying the same thing. */}
      {!free.length ? (
        <p className="field-hint">{t("operators.all_bound")}</p>
      ) : (
      <div className="row" style={{ maxWidth: 460 }}>
        <select className="select" value={picked}
                onChange={(e) => setPicked(e.target.value)}>
          <option value="">{t("operators.pick_account")}</option>
          {free.map((account) => (
            <option key={account.id} value={account.id}>{account.handle}</option>
          ))}
        </select>
        <button className="btn shrink" disabled={!picked}
                onClick={async () => {
                  await act(() => api.accounts.update({
                    id: picked, operator_id: operator.id }));
                  setPicked("");
                }}>
          {t("operators.bind")}
        </button>
      </div>
      )}
    </>
  );
}

function OperatorModal({ operator, onClose, onDelete, onLogin }: {
  operator: Operator | null; onClose: () => void; onDelete: () => void;
  onLogin: () => void;
}) {
  const { snapshot, t, act } = useStore();
  const [username, setUsername] = useState(operator?.username ?? "");
  const [displayName, setDisplayName] = useState(operator?.display_name ?? "");
  const [notes, setNotes] = useState(operator?.notes ?? "");
  const [apiId, setApiId] = useState(operator?.api_profile_id ?? "");
  const [proxyId, setProxyId] = useState(operator?.network_profile_id ?? "");

  async function save() {
    const body = {
      id: operator?.id, username, display_name: displayName, notes,
      api_profile_id: apiId || null, network_profile_id: proxyId || null,
    };
    await act(() => (operator
      ? api.operators.update(body)
      : api.operators.create(body)));
    onClose();
  }

  return (
    <Modal
      title={operator ? t("common.edit") : t("operators.new")}
      onClose={onClose}
      footer={
        <>
          {operator && (
            <button className="btn danger left" onClick={onDelete}>
              {t("common.delete")}
            </button>
          )}
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn accent" onClick={save}>{t("common.save")}</button>
        </>
      }
    >
      <Field label={t("operators.username")}>
        <input className="input" autoFocus value={username} placeholder="@name"
               onChange={(e) => setUsername(e.target.value)} />
      </Field>
      <Field label={`${t("operators.display_name")} (${t("common.optional")})`}>
        <input className="input" value={displayName}
               onChange={(e) => setDisplayName(e.target.value)} />
      </Field>

      {/* Until there is a session these decide nothing - the login form asks
          for them, and it is the only moment they matter. */}
      {operator?.logged_in && (
        <>
          <Field label={t("accounts.api")} hint={t("operators.connection_hint")}>
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
        </>
      )}

      <Field label={t("operators.notes")}>
        <textarea className="textarea" style={{ minHeight: 60 }} value={notes}
                  onChange={(e) => setNotes(e.target.value)} />
      </Field>
      {operator && !operator.logged_in && (
        <div className="modal-section">
          <p className="icon-line sm muted">
            <span className="dot warn" /> {t("operators.not_logged_in")}
          </p>
          {/* Placed like "make this an operator" is on an account: the one
              action that changes what this record is, set apart from the
              fields that merely describe it. */}
          <button className="btn" style={{ marginTop: 10 }} onClick={onLogin}>
            {t("operators.sign_in_here")}
          </button>
          <p className="field-hint">{t("operators.sign_in_here_hint")}</p>
        </div>
      )}
    </Modal>
  );
}

function ChatsModal({ operator, onClose }: {
  operator: Operator; onClose: () => void;
}) {
  const { t } = useStore();
  return (
    <Modal wide title={`${operator.handle} — ${t("operators.chats")}`}
           onClose={onClose}>
      <OperatorChat operator={operator} />
    </Modal>
  );
}
