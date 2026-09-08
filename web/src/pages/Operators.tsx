import { useState } from "react";
import { api } from "../api";
import { buttonCheckGoing } from "../checkup";
import { useStore } from "../state";
import { LoginModal } from "../components/LoginModal";
import { OperatorChat } from "../components/OperatorChat";
import { ReloginBlock, useProfileChoices } from "./Accounts";
import {
  ActionButton, BulkBar, Confirm, DataTable, EntityCard, Field, FormError, FormFooter,
  Meta, MetaRow, Modal, PageHead, SectionHead, Select, Status, TextArea, TextInput,
  byId, fmtTime, useBulk, useDraft, useHashFocus, useSelection, useSubmit,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { Account, Operator } from "../types";

export function Operators() {
  const { snapshot, t, act, lang } = useStore();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [login, setLogin] = useState(false);
  // Windows keep ids and read the live records: see byId.
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [chatsFor, setChatsFor] = useState<string | null>(null);
  const [loginInto, setLoginInto] = useState<string | null>(null);
  const [bulkEdit, setBulkEdit] = useState(false);
  const [bulkDelete, setBulkDelete] = useState(false);
  const selection = useSelection((snapshot?.operators ?? []).map((o) => o.id));
  const bulk = useBulk("operators", selection);
  // arriving from the overview: open the card that was linked to, then mark it
  useHashFocus((id) => setOpen((cur) => new Set(cur).add(id)));

  if (!snapshot) return null;
  const checkup = snapshot.checkup;
  const mine = checkup.running && checkup.kind === "operators";
  const checking = buttonCheckGoing(checkup);
  const edited = byId(snapshot.operators, editing);
  const removed = byId(snapshot.operators, removing);
  const chatting = byId(snapshot.operators, chatsFor);

  const toggle = (id: string) => setOpen((cur) => {
    const next = new Set(cur);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const profileName = (id: string | null, list: { id: string; name: string }[]) =>
    list.find((p) => p.id === id)?.name || t("common.none");

  return (
    <>
      <PageHead
        title={t("operators.title")}
        sub={t("operators.sub")}
        actions={
          <>
            <ActionButton title={t("accounts.rescan_hint")}
                          onClick={() => act(() => api.operators.rescan(), t("common.refresh"))}>
              {t("accounts.rescan")}
            </ActionButton>
            <ActionButton disabled={checking} title={t("accounts.probe_all_hint")}
                          onClick={() => act(() => api.operators.probeAll())}>
              {mine ? t("accounts.checking_progress", { done: checkup.done, total: checkup.total })
                    : t("accounts.probe_all")}
            </ActionButton>
            <ActionButton variant="primary" onClick={() => setLogin(true)}>
              {t("operators.login")}
            </ActionButton>
          </>
        }
      />

      <BulkBar selection={selection}>
        <ActionButton size="sm" disabled={checking}
                      onClick={() => act(() => api.bulk.check("operators", selection.ids))}>
          {t("common.check")}
        </ActionButton>
        <ActionButton size="sm" onClick={() => setBulkEdit(true)}>{t("common.edit")}</ActionButton>
        <ActionButton size="sm" variant="danger" onClick={() => setBulkDelete(true)}>
          {t("common.delete")}
        </ActionButton>
      </BulkBar>

      <div className="stack">
        {!snapshot.operators.length && <div className="empty">{t("operators.empty")}</div>}
        {snapshot.operators.map((operator) => {
          const bound = snapshot.accounts.filter((a) => a.operator_id === operator.id);
          const source = snapshot.accounts.find((a) => a.id === operator.account_id);
          return (
            <EntityCard
              key={operator.id} id={operator.id} kind="operator"
              open={open.has(operator.id)} onToggle={() => toggle(operator.id)}
              picked={selection.has(operator.id)} onPick={() => selection.toggle(operator.id)}
              name={operator.handle}
              sub={[operator.display_name, operator.notes]
                .find((x) => x && x !== operator.handle) || "—"}
              pills={[
                t.count(bound.length, "count.accounts"),
                source ? t("operators.linked_to", { name: source.handle })
                  : operator.logged_in ? t("operators.has_session")
                  : t("operators.handle_only"),
              ]}
              status={<Status state={operator.effective} issues={operator.issues}
                              until={operator.flood_until} />}
              onEdit={() => setEditing(operator.id)}
            >
              <MetaRow>
                <Meta label={t("accounts.api")}
                      value={profileName(operator.api_profile_id, snapshot.api_profiles)} />
                <Meta label={t("accounts.proxy")}
                      value={profileName(operator.network_profile_id, snapshot.network_profiles)} />
                <Meta label={t("operators.account")}
                      value={source?.handle ?? t("common.none")} />
                <Meta label={t("accounts.last_check")}
                      value={fmtTime(operator.last_check_at, lang, t("common.never"))} />
              </MetaRow>

              {/* The same way back an account has: a revoked session is only
                  fixed by signing in again. A linked operator's session is its
                  account's, and the account card offers it. */}
              {operator.effective === "AUTH_DEAD" && !operator.account_id && (
                <ReloginBlock onRelogin={() => setLoginInto(operator.id)} />
              )}

              <div>
                <SectionHead title={t("operators.bound")} action={
                  <ActionButton disabled={!operator.logged_in}
                                title={operator.logged_in ? undefined : t("operators.not_logged_in")}
                                onClick={() => setChatsFor(operator.id)}>
                    {t("operators.open_chats")}
                  </ActionButton>
                } />
                <BoundAccounts operator={operator} bound={bound} />
              </div>
            </EntityCard>
          );
        })}

        {/* A handle is not an account, so it is not offered beside the page's
            real actions - it sits where the list it joins ends. */}
        <button type="button" className="add-row" onClick={() => setCreating(true)}
                title={t("operators.new_handle_hint")}>
          <span className="add-row-plus">+</span>
          {t("operators.new_handle")}
        </button>
      </div>

      {bulkDelete && (
        <Confirm text={t("bulk.confirm_delete", { n: selection.count })}
                 onCancel={() => setBulkDelete(false)}
                 onConfirm={() => { setBulkDelete(false); void bulk.run("delete"); }} />
      )}
      {bulkEdit && (
        <BulkOperatorModal count={selection.count} onClose={() => setBulkEdit(false)}
                           apply={bulk.apply} />
      )}
      {login && <LoginModal asOperator onClose={() => setLogin(false)} />}
      {loginInto && (
        <LoginModal asOperator targetId={loginInto} onClose={() => setLoginInto(null)} />
      )}
      {(creating || edited) && (
        <OperatorModal operator={edited ?? null}
                       onClose={() => { setCreating(false); setEditing(null); }}
                       onLogin={() => { setLoginInto(editing); setEditing(null); }}
                       onDelete={() => { setRemoving(editing); setEditing(null); }} />
      )}
      {removed && (
        <Confirm text={t("common.confirm_delete", { name: removed.handle })}
                 onCancel={() => setRemoving(null)}
                 onConfirm={() => {
                   const id = removed.id;
                   setRemoving(null);
                   void act(() => api.operators.remove(id));
                 }} />
      )}
      {chatting && (
        <Modal wide title={`${chatting.handle} — ${t("operators.chats")}`}
               onClose={() => setChatsFor(null)}>
          <OperatorChat operator={chatting} />
        </Modal>
      )}
    </>
  );
}

/** One operator can carry several accounts, so the binding is edited from
 *  here rather than one account at a time. */
function BoundAccounts({ operator, bound }: { operator: Operator; bound: Account[] }) {
  const { snapshot, t, act } = useStore();
  const [picked, setPicked] = useState("");
  const free = (snapshot?.accounts ?? []).filter((a) => !a.operator_id);
  // An account bound meanwhile is no longer free, and no longer the choice.
  const chosen = free.some((a) => a.id === picked) ? picked : "";

  const columns: Column<Account>[] = [
    { key: "status", label: t("common.status"), width: "var(--slot-status)",
      render: (a) => <Status state={a.effective} issues={a.issues}
                             until={a.flood_until} /> },
    { key: "name", label: t("campaigns.account"),
      render: (a) => <span title={a.handle}>{a.handle}</span> },
    { key: "display", label: t("operators.display_name"), width: 200,
      render: (a) => <span className="muted" title={a.display_name}>{a.display_name}</span> },
    { key: "actions", label: "", width: "var(--w-actions-1)", className: "actions",
      render: (a) => (
        <ActionButton size="sm" variant="ghost-danger"
                      onClick={() => act(() => api.accounts.update({ id: a.id, operator_id: null }))}>
          {t("operators.unbind")}
        </ActionButton>
      ) },
  ];

  return (
    <>
      <DataTable columns={columns} rows={bound} rowKey={(a) => a.id}
                 empty={t("operators.no_accounts")} />
      {/* A disabled picker next to a note saying there is nothing to pick
          was two ways of saying the same thing. */}
      {!free.length ? (
        <p className="field-hint">{t("operators.all_bound")}</p>
      ) : (
        <div className="ab-row">
          <Select width="pick" value={chosen} onChange={setPicked} options={[
            { value: "", label: t("operators.pick_account") },
            ...free.map((a) => ({ value: a.id, label: a.handle })),
          ]} />
          <ActionButton disabled={!chosen}
                        onClick={async () => {
                          await act(() => api.accounts.update({ id: chosen, operator_id: operator.id }));
                          setPicked("");
                        }}>
            {t("operators.bind")}
          </ActionButton>
        </div>
      )}
    </>
  );
}

/** API and proxy for several operators: one request, one reconnect each.
 *  A linked operator's are its account's, and the backend sets them there. */
function BulkOperatorModal({ count, onClose, apply }: {
  /** How many were picked: the window's title, read once. */
  count: number;
  onClose: () => void;
  apply: (action: string, values: Record<string, unknown>) => Promise<unknown>;
}) {
  const { t } = useStore();
  const choices = useProfileChoices(true);
  const [picked] = useState(count);
  const [apiId, setApiId] = useState("");
  const [proxyId, setProxyId] = useState("");
  const submit = useSubmit();

  async function save() {
    const values: Record<string, unknown> = {};
    if (apiId) values.api_profile_id = apiId;
    if (proxyId) values.network_profile_id = proxyId === "none" ? "" : proxyId;
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
    </Modal>
  );
}

/** A new operator, or one's window. A draft over the live record (useDraft):
 *  Save sends what changed in one request and closes only when it went
 *  through. «Войти оператором» acts on what the window shows, so it saves
 *  the changes first. */
function OperatorModal({ operator, onClose, onDelete, onLogin }: {
  /** The live record, or null for a new one. */
  operator: Operator | null;
  onClose: () => void;
  onDelete: () => void;
  onLogin: () => void;
}) {
  const { t } = useStore();
  const choices = useProfileChoices(false);
  const linked = !!operator?.account_id;
  const form = useDraft({
    username: operator?.username ?? "",
    display_name: operator?.display_name ?? "",
    notes: operator?.notes ?? "",
    api_profile_id: operator?.api_profile_id ?? "",
    network_profile_id: operator?.network_profile_id ?? "",
  });
  const submit = useSubmit();
  const { values } = form;

  /** One request: the new operator, or what changed. */
  const store = () => submit.run(async () => {
    if (!operator) {
      await api.operators.create({ username: values.username,
                                   display_name: values.display_name, notes: values.notes });
    } else if (form.dirty) {
      await api.operators.update({ id: operator.id, ...form.changes });
    }
  });

  return (
    <Modal title={operator ? operator.handle : t("operators.new")} onClose={onClose}
           footer={<FormFooter onCancel={onClose} busy={submit.busy}
                               onConfirm={async () => { if (await store()) onClose(); }}
                               left={operator && (
             <ActionButton variant="danger" onClick={onDelete}>{t("common.delete")}</ActionButton>
           )} />}>
      <FormError message={submit.error} />
      {/* A linked operator's name is its account's: nothing to type here. */}
      {!linked && (
        <>
          <Field label={t("operators.username")}>
            <TextInput autoFocus value={values.username} placeholder="@name"
                       onChange={(v) => form.set("username", v)} />
          </Field>
          <Field label={`${t("operators.display_name")} (${t("common.optional")})`}>
            <TextInput value={values.display_name} onChange={(v) => form.set("display_name", v)} />
          </Field>
        </>
      )}

      {/* Until there is a session these decide nothing - the login form asks
          for them, and it is the only moment they matter. */}
      {(linked || operator?.logged_in) && (
        <>
          <Field label={t("accounts.api")}
                 hint={linked ? t("operators.linked_hint") : t("operators.connection_hint")}>
            <Select value={values.api_profile_id} options={choices.apis}
                    onChange={(v) => form.set("api_profile_id", v)} />
          </Field>
          <Field label={t("accounts.proxy")}>
            <Select value={values.network_profile_id} options={choices.proxies}
                    onChange={(v) => form.set("network_profile_id", v)} />
          </Field>
        </>
      )}

      <Field label={t("operators.notes")}>
        <TextArea size="short" value={values.notes} onChange={(v) => form.set("notes", v)} />
      </Field>

      {operator && !operator.logged_in && (
        <div className="modal-section">
          <p className="field-hint">
            <Status tone="warn" label={t("operators.not_logged_in")} />
          </p>
          <ActionButton disabled={submit.busy}
                        onClick={async () => { if (await store()) onLogin(); }}>
            {t("operators.sign_in_here")}
          </ActionButton>
          <p className="field-hint">
            {t("operators.sign_in_here_hint")}
            {form.dirty && ` ${t("accounts.saves_first")}`}
          </p>
        </div>
      )}
    </Modal>
  );
}
