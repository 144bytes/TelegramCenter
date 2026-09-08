import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import {
  CampaignDuplicate, CampaignModal, CampaignToggle, ChannelPicker, ScheduleFields,
  scheduleOf, targetAddress,
} from "../components/CampaignModal";
import type { ScheduleDraft } from "../components/CampaignModal";
import { SentCount } from "./Accounts";
import {
  ActionButton, BulkBar, Confirm, DataTable, Field, FormError, FormFooter, Modal,
  PageHead, RangeInput, Select, Status, Switch, byId, fmtTime, fmtUntil, messageIssues,
  useBulk, useHashFocus, useSelection, useSubmit,
} from "../components/ui";
import type { Column } from "../components/ui";
import type { Key, T } from "../i18n";
import type { Campaign, CampaignTargetResult, ScheduleMode } from "../types";

/** The short word for a channel switched off inside a campaign, by why. */
const EXCLUDED_LABEL: Record<string, Key> = {
  "excluded.banned": "campaigns.label.banned",
  "delivery.not_found": "campaigns.label.not_found",
  "excluded.not_member": "campaigns.label.not_member",
  "delivery.join_to_comment": "campaigns.label.join_to_comment",
  "excluded.refused": "campaigns.label.refused",
};

/** A result row's status word when the plain one would hide the point:
 *  switched off, or skipped until Telegram lets it through. */
function resultLabel(r: CampaignTargetResult, t: T, lang: string): string | undefined {
  if (r.excluded) {
    return t(EXCLUDED_LABEL[r.excluded_reason?.code ?? ""] ?? "campaigns.label.excluded");
  }
  const waiting = r.status === "SKIPPED" && r.retry_at
    && new Date(r.retry_at).getTime() > Date.now();
  if (!waiting) return undefined;
  const time = fmtUntil(r.retry_at, lang);
  if (r.error?.code === "result.slow_mode") return t("campaigns.label.slow_mode", { time });
  if (r.error?.code === "result.muted") return t("campaigns.label.muted", { time });
  return undefined;
}

export function Campaigns() {
  const { snapshot, t, act } = useStore();
  // Windows keep ids and read the live records: see byId.
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  const [bulkEdit, setBulkEdit] = useState(false);
  const [bulkDelete, setBulkDelete] = useState(false);
  const selection = useSelection((snapshot?.campaigns ?? []).map((c) => c.id));
  const bulk = useBulk("campaigns", selection);
  useHashFocus();

  if (!snapshot) return null;
  const edited = byId(snapshot.campaigns, editing);
  const removed = byId(snapshot.campaigns, removing);
  const shown = byId(snapshot.campaigns, details);

  const accountName = (id: string) =>
    snapshot.accounts.find((a) => a.id === id)?.handle ?? "—";

  const columns: Column<Campaign>[] = [
    { key: "name", label: t("common.name"), value: (c) => c.name,
      render: (c) => <span title={c.name}>{c.name}</span> },
    { key: "account", label: t("campaigns.account"), width: 140,
      value: (c) => accountName(c.account_id),
      render: (c) => <span title={accountName(c.account_id)}>{accountName(c.account_id)}</span> },
    { key: "status", label: t("common.status"), width: 140, value: (c) => c.effective,
      render: (c) => <Status state={c.effective} issues={c.issues} /> },
    { key: "progress", label: t("campaigns.progress"), width: 96, className: "num",
      value: (c) => (c.is_recurring ? c.sent_total : c.sent_count),
      render: (c) => <SentCount campaign={c} /> },
    { key: "actions", label: "", width: "var(--w-actions-4)", className: "actions",
      render: (c) => (
        <span className="ab-row">
          <CampaignToggle campaign={c} />
          <CampaignDuplicate campaign={c} />
          <ActionButton size="sm" variant="ghost" onClick={() => setEditing(c.id)}>
            {t("common.edit")}
          </ActionButton>
          <ActionButton size="sm" variant="ghost-danger" onClick={() => setRemoving(c.id)}>
            {t("common.delete")}
          </ActionButton>
        </span>
      ) },
  ];

  return (
    <>
      <PageHead
        title={t("campaigns.title")}
        sub={t("campaigns.sub")}
        actions={
          <ActionButton variant="primary" onClick={() => setCreating(true)}>
            {t("campaigns.new")}
          </ActionButton>
        }
      />

      {/* "Start them all" is this and nothing else: tick the header box,
          press Запустить. A separate button would be a second way to start a
          campaign, and the two would drift. */}
      <BulkBar selection={selection}>
        <ActionButton size="sm" onClick={() => bulk.run("start")}>{t("campaigns.start")}</ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("pause")}>{t("campaigns.pause")}</ActionButton>
        <ActionButton size="sm" onClick={() => bulk.run("reset")}>{t("campaigns.reset")}</ActionButton>
        <ActionButton size="sm" onClick={() => setBulkEdit(true)}>{t("common.edit")}</ActionButton>
        <ActionButton size="sm" variant="danger" onClick={() => setBulkDelete(true)}>
          {t("common.delete")}
        </ActionButton>
      </BulkBar>

      <div className="card">
        {/* A row opens the campaign's results; its buttons act on it. */}
        <DataTable columns={columns} rows={snapshot.campaigns} rowKey={(c) => c.id}
                   rowId={(c) => c.id} selection={selection}
                   onRowClick={(c) => setDetails(c.id)} empty={t("campaigns.empty")} />
      </div>

      {(creating || edited) && (
        <CampaignModal campaign={edited ?? null}
                       onClose={() => { setCreating(false); setEditing(null); }} />
      )}
      {removed && (
        <Confirm text={t("common.confirm_delete", { name: removed.name })}
                 onCancel={() => setRemoving(null)}
                 onConfirm={() => {
                   const id = removed.id;
                   setRemoving(null);
                   void act(() => api.campaigns.remove(id));
                 }} />
      )}
      {bulkDelete && (
        <Confirm text={t("bulk.confirm_delete", { n: selection.count })}
                 onCancel={() => setBulkDelete(false)}
                 onConfirm={() => { setBulkDelete(false); void bulk.run("delete"); }} />
      )}
      {bulkEdit && (
        <BulkCampaignModal count={selection.count} onClose={() => setBulkEdit(false)}
                           apply={bulk.apply} />
      )}
      {shown && <ResultsModal campaign={shown} onClose={() => setDetails(null)} />}
    </>
  );
}

/** Change the same setting on several campaigns at once.
 *
 *  Every field starts as «leave it alone». A bulk form that pre-filled
 *  itself from the first selected record would quietly copy that record's
 *  schedule onto the other seventeen. Channels are the same: none picked
 *  keeps each campaign's own list, any picked replace every list.
 */
function BulkCampaignModal({ count, onClose, apply }: {
  /** How many were picked: the window's title, read once. */
  count: number;
  onClose: () => void;
  apply: (action: string, values: Record<string, unknown>) => Promise<unknown>;
}) {
  const { snapshot, t } = useStore();
  const settings = snapshot?.settings.campaign;
  const [picked] = useState(count);
  const [account, setAccount] = useState("");
  const [targets, setTargets] = useState<string[]>([]);
  const [schedule, setSchedule] = useState<ScheduleDraft>({
    mode: "" as ScheduleMode, at: "", times: "" });
  const [changeInterval, setChangeInterval] = useState(false);
  const [intervalMin, setIntervalMin] = useState(settings?.default_interval_min_sec ?? 15);
  const [intervalMax, setIntervalMax] = useState(settings?.default_interval_max_sec ?? 40);
  const submit = useSubmit();

  async function save() {
    const values: Record<string, unknown> = {};
    if (account) values.account_id = account;
    if (targets.length) values.target_ids = targets;
    if (schedule.mode) values.schedule = scheduleOf(schedule);
    if (changeInterval) {
      if (intervalMin > intervalMax) { submit.refuse(t("err.interval.inverted")); return; }
      values.interval_min_sec = intervalMin;
      values.interval_max_sec = intervalMax;
    }
    if (!Object.keys(values).length) { onClose(); return; }
    if (await submit.run(() => apply("update", values))) onClose();
  }

  return (
    <Modal wide title={t("bulk.edit_title", { n: picked })} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} />}>
      <FormError message={submit.error} />

      <Field label={t("campaigns.account")}>
        <Select value={account} onChange={setAccount} options={[
          { value: "", label: t("bulk.unchanged") },
          ...(snapshot?.accounts ?? []).map((a) => ({ value: a.id, label: a.handle })),
        ]} />
      </Field>

      <Field label={t("campaigns.targets")} hint={t("bulk.channels_hint")}>
        <ChannelPicker picked={targets} onChange={setTargets} />
      </Field>

      <ScheduleFields value={schedule} onChange={setSchedule}
                      extra={[{ value: "", label: t("bulk.unchanged") }]} />

      <Field label={t("campaigns.interval")} hint={t("campaigns.interval_hint")}>
        <span className="ab-row">
          <Switch checked={changeInterval} onChange={setChangeInterval} label={t("bulk.change")} />
          {changeInterval && (
            <RangeInput min={intervalMin} max={intervalMax} unit={t("common.seconds")}
                        onChange={(lo, hi) => { setIntervalMin(lo); setIntervalMax(hi); }} />
          )}
        </span>
      </Field>
    </Modal>
  );
}

/** What a campaign did, channel by channel. A live view, not a form: it
 *  follows the record as it sends, and its buttons act at once. */
function ResultsModal({ campaign: live, onClose }: {
  /** The live record: the page reads it from the snapshot on every render. */
  campaign: Campaign;
  onClose: () => void;
}) {
  const { snapshot, t, act, lang } = useStore();
  // A channel taken off the list leaves its result behind, and the result
  // only knows the id: say it is gone rather than print an id nobody knows.
  const name = (id: string) => {
    const target = snapshot?.targets.find((x) => x.id === id);
    if (!target) return t("campaigns.target_gone");
    return target.title || targetAddress(target);
  };

  const columns: Column<CampaignTargetResult>[] = [
    // Unticked means "this campaign no longer writes here". The channel keeps
    // its row and its history either way, so the user can undo it.
    { key: "on", label: "", width: 36, className: "pick-col",
      render: (r) => (
        <input type="checkbox" checked={!r.excluded}
               title={r.excluded ? t.msg(r.excluded_reason) : t("campaigns.target_on")}
               onChange={() => act(() => api.campaigns.toggleTarget(
                 live.id, r.target_id, !r.excluded))} />
      ) },
    { key: "target", label: t("campaigns.targets"), value: (r) => name(r.target_id),
      render: (r) => <span title={name(r.target_id)}>{name(r.target_id)}</span> },
    // The failure text is in the dot's tooltip, where it can be selected and
    // copied; as a column it stretched the table off the screen.
    { key: "status", label: t("common.status"), width: 240, value: (r) => r.status,
      render: (r) => (
        <Status state={r.status} kind="result" label={resultLabel(r, t, lang)}
                issues={r.excluded ? messageIssues(r.excluded_reason, "warning")
                                   : messageIssues(r.error,
                                                   r.status === "SKIPPED" ? "warning" : "error")} />
      ) },
    { key: "sent", label: t("campaigns.sent_at"), width: 120,
      value: (r) => r.sent_at ?? "",
      render: (r) => <span className="muted">{fmtTime(r.sent_at, lang)}</span> },
    { key: "attempts", label: t("campaigns.attempts"), width: 80, className: "num",
      value: (r) => r.attempts, render: (r) => <span className="muted">{r.attempts}</span> },
  ];

  return (
    <Modal wide title={live.name} onClose={onClose}
           footer={
             <>
               <span className="left">
                 <ActionButton onClick={() => act(() => api.campaigns.reset(live.id))}>
                   {t("campaigns.reset")}
                 </ActionButton>
               </span>
               <CampaignToggle campaign={live} footer />
               <ActionButton variant="primary" onClick={onClose}>{t("common.close")}</ActionButton>
             </>
           }>
      <FormError message={live.last_error ? t.msg(live.last_error) : null} />
      <p className="field-hint field">
        {t("campaigns.next_run")}: {fmtTime(live.next_run_at, lang, t("common.never"))}
      </p>
      <DataTable columns={columns} rows={live.results} rowKey={(r) => r.target_id}
                 rowClass={(r) => (r.excluded ? "excluded" : undefined)}
                 empty={t("campaigns.no_results")} />
    </Modal>
  );
}
