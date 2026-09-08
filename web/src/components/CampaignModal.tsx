import { useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import {
  ActionButton, Field, FormError, FormFooter, Modal, RangeInput, SelectAll,
  Select, TextArea, TextInput, useSubmit,
} from "./ui";
import type { T } from "../i18n";
import type { Campaign, ScheduleMode, Target } from "../types";

/** The most messages one campaign holds - the number the backend refuses
 *  above, so the form can say so before anything is sent. */
export const MESSAGE_CAP = 100;

/** The line between two messages in a pasted list. A line of its own,
 *  spaces around it allowed: a blank line is something adverts contain. */
const SEPARATOR = /^[ \t]*===[ \t]*$/m;

/** A line of equals signs that is not exactly ===: a separator typed wrong. */
const NEAR_MISS = /^=+$/;

export interface ParseResult {
  messages: string[];
  error: string | null;
}

/** Split a pasted list into messages, or say why it cannot be split.
 *
 *  One place for the separator and the limit: `existing` is how many the
 *  campaign already has, and together they must not go over the cap. */
export function parsePack(raw: string, existing: number, t: T): ParseResult {
  const text = (raw ?? "").split("\r\n").join("\n");
  if (!text.trim()) return { messages: [], error: null };

  const wrong = text.split("\n").map((line) => line.trim())
    .find((line) => NEAR_MISS.test(line) && line !== "===");
  if (wrong !== undefined) {
    return { messages: [], error: t("campaigns.pack_bad_separator", { got: wrong }) };
  }

  const messages = text.split(SEPARATOR).map((part) => part.trim()).filter(Boolean);
  if (existing + messages.length > MESSAGE_CAP) {
    return {
      messages: [],
      error: t("campaigns.pack_too_many",
               { n: existing + messages.length, limit: MESSAGE_CAP }),
    };
  }
  return { messages, error: null };
}

/** How a channel is written in lists: @username, +invite, or the id. */
export function targetAddress(target: Target): string {
  if (target.username) return `@${target.username.replace(/^@/, "")}`;
  if (target.invite) return `+${target.invite}`;
  return String(target.telegram_id ?? "");
}

/* ── the buttons a campaign row carries ────────────────────────────── */

/** Start or stop, whichever applies: the row, the card and the results
 *  window all draw this, so it never offers to start a running campaign. */
export function CampaignToggle({ campaign, footer = false }: {
  campaign: Campaign;
  /** A framed button in a window's footer; text in a row. */
  footer?: boolean;
}) {
  const { t, act } = useStore();
  const running = campaign.raw_state === "SCHEDULED" || campaign.raw_state === "RUNNING";
  const look = footer ? {} : { size: "sm" as const, variant: "ghost" as const };
  return running ? (
    <ActionButton {...look} onClick={() => act(() => api.campaigns.pause(campaign.id))}>
      {t("campaigns.pause")}
    </ActionButton>
  ) : (
    <ActionButton {...look} onClick={() => act(() => api.campaigns.start(campaign.id))}>
      {t("campaigns.start")}
    </ActionButton>
  );
}

export function CampaignDuplicate({ campaign }: { campaign: Campaign }) {
  const { t, act, notify } = useStore();
  async function duplicate() {
    const copy = await act(() => api.campaigns.duplicate(
      campaign.id, t("campaigns.copy_word")));
    if (copy) notify(t("campaigns.duplicated", { name: copy.name }), "ok");
  }
  return (
    <ActionButton size="sm" variant="ghost" onClick={duplicate}>
      {t("campaigns.duplicate")}
    </ActionButton>
  );
}

/* ── picking channels ──────────────────────────────────────────────── */

/** The channels a campaign sends to. The same picker in the campaign form
 *  and in the bulk form. */
export function ChannelPicker({ picked, onChange }: {
  picked: string[];
  onChange: (ids: string[]) => void;
}) {
  const { snapshot, t } = useStore();
  const all = snapshot?.targets ?? [];
  if (!all.length) return <div className="pick-list faint">{t("targets.empty")}</div>;

  const toggle = (id: string) => onChange(
    picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id]);

  return (
    <div className="pick-list">
      <label className="pick-row pick-all">
        <SelectAll total={all.length} picked={picked.length}
                   onToggle={() => onChange(
                     picked.length === all.length ? [] : all.map((x) => x.id))} />
        <span className="grow">{t("campaigns.select_all")}</span>
        <span className="faint">
          {t("campaigns.selected", { n: picked.length, total: all.length })}
        </span>
      </label>
      {all.map((target) => (
        <label key={target.id} className="pick-row">
          <input type="checkbox" checked={picked.includes(target.id)}
                 onChange={() => toggle(target.id)} />
          <span className="grow">{target.title || targetAddress(target)}</span>
          {target.title && <span className="faint mono">{targetAddress(target)}</span>}
        </label>
      ))}
    </div>
  );
}

/* ── the schedule ──────────────────────────────────────────────────── */

export interface ScheduleDraft { mode: ScheduleMode; at: string; times: string }

export function scheduleOf(draft: ScheduleDraft) {
  return {
    mode: draft.mode,
    at: draft.mode === "ONCE" ? (draft.at || null) : null,
    times: draft.mode === "DAILY"
      ? draft.times.split(",").map((x) => x.trim()).filter(Boolean) : [],
  };
}

/** The mode and, beside it, the one thing that mode needs. */
export function ScheduleFields({ value, onChange, extra }: {
  value: ScheduleDraft;
  onChange: (next: ScheduleDraft) => void;
  /** Extra choices in front of the modes: «leave as is» in the bulk form. */
  extra?: { value: string; label: string }[];
}) {
  const { t } = useStore();
  const modes: { value: string; label: string }[] = [
    ...(extra ?? []),
    { value: "LOOP", label: t("campaigns.mode.LOOP") },
    { value: "ONCE", label: t("campaigns.mode.ONCE") },
    { value: "DAILY", label: t("campaigns.mode.DAILY") },
  ];
  return (
    <div className="field-row">
      <Field label={t("campaigns.schedule")}>
        <Select value={value.mode} options={modes}
                onChange={(mode) => onChange({ ...value, mode: mode as ScheduleMode })} />
      </Field>
      {value.mode === "ONCE" && (
        <Field label={t("campaigns.at")}>
          <input className="input" type="datetime-local" value={value.at.slice(0, 16)}
                 onChange={(e) => onChange({
                   ...value, at: e.target.value ? `${e.target.value}:00` : "" })} />
        </Field>
      )}
      {value.mode === "DAILY" && (
        <Field label={t("campaigns.times")}>
          <TextInput value={value.times} placeholder="09:00, 18:30"
                     onChange={(times) => onChange({ ...value, times })} />
        </Field>
      )}
      {/* the mode keeps half the row whatever sits beside it */}
      {(value.mode === "LOOP" || !value.mode) && <div className="field" />}
    </div>
  );
}

/* ── the form ──────────────────────────────────────────────────────── */

/** One message of the form. `key` keeps the list stable while typing; `id`
 *  is the server's, and sending it back keeps the rotation's memory. */
interface MessageRow {
  key: string;
  id: string | null;
  text: string;
  /** Where the app stored the picture, if there is one. */
  file: string;
  /** A picture picked here and not yet uploaded - it goes up on Save, so
   *  closing the form leaves nothing on disk. */
  picked?: File;
}

let seq = 0;
const newRow = (text: string): MessageRow => ({ key: `new_${++seq}`, id: null, text, file: "" });

export function CampaignModal({ campaign, accountId, onClose }: {
  campaign: Campaign | null;
  accountId?: string;
  onClose: () => void;
}) {
  const { snapshot, t } = useStore();
  const settings = snapshot?.settings;

  const [name, setName] = useState(campaign?.name ?? "");
  const [account, setAccount] = useState(
    campaign?.account_id || accountId || snapshot?.accounts[0]?.id || "");
  const [targets, setTargets] = useState<string[]>(campaign?.target_ids ?? []);
  const [schedule, setSchedule] = useState<ScheduleDraft>({
    mode: campaign?.schedule.mode ?? "LOOP",
    at: campaign?.schedule.at ?? "",
    times: (campaign?.schedule.times ?? []).join(", "),
  });
  const [intervalMin, setIntervalMin] = useState(
    campaign?.interval_min_sec ?? settings?.campaign.default_interval_min_sec ?? 15);
  const [intervalMax, setIntervalMax] = useState(
    campaign?.interval_max_sec ?? settings?.campaign.default_interval_max_sec ?? 40);
  const [messages, setMessages] = useState<MessageRow[]>(() =>
    (campaign?.messages ?? []).map((m) => ({
      key: m.id, id: m.id, text: m.text, file: m.file ?? "",
    })));
  const [pasting, setPasting] = useState(false);
  const submit = useSubmit();

  const accounts = (snapshot?.accounts ?? []).map((a) => ({ value: a.id, label: a.handle }));
  if (!accounts.length) accounts.push({ value: "", label: t("campaigns.no_accounts") });

  /** Upload the pictures picked in this form, now that it is being saved. A
   *  picture that fails to upload stops the save rather than vanishing. */
  async function stored(rows: MessageRow[]): Promise<MessageRow[]> {
    const out: MessageRow[] = [];
    for (const row of rows) {
      if (!row.picked) { out.push(row); continue; }
      const saved = await api.campaigns.upload(row.picked);
      out.push({ ...row, file: saved.path, picked: undefined });
    }
    return out;
  }

  async function save() {
    const ok = await submit.run(async () => {
      const rows = await stored(messages.filter((m) => m.text.trim() || m.file || m.picked));
      const payload: Record<string, unknown> = {
        id: campaign?.id,
        name, account_id: account, target_ids: targets,
        messages: rows.map((m) => (m.id
          ? { id: m.id, text: m.text, file: m.file }
          : { text: m.text, file: m.file })),
        interval_min_sec: intervalMin, interval_max_sec: intervalMax,
        schedule: scheduleOf(schedule),
      };
      if (campaign) await api.campaigns.update(payload);
      else await api.campaigns.create(payload);
    });
    if (ok) onClose();
  }

  return (
    <Modal wide title={campaign ? campaign.name : t("campaigns.new")} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} />}>
      <FormError message={submit.error} />

      <Field label={t("common.name")}>
        <TextInput value={name} autoFocus onChange={setName} />
      </Field>

      <Field label={t("campaigns.account")}>
        <Select value={account} options={accounts} onChange={setAccount} />
      </Field>

      <Field label={t("campaigns.targets")}
             hint={targets.length ? undefined : t("campaigns.no_targets_picked")}>
        <ChannelPicker picked={targets} onChange={setTargets} />
      </Field>

      <ScheduleFields value={schedule} onChange={setSchedule} />

      <Field label={t("campaigns.interval")} hint={t("campaigns.interval_hint")}>
        <RangeInput min={intervalMin} max={intervalMax} unit={t("common.seconds")}
                    onChange={(lo, hi) => { setIntervalMin(lo); setIntervalMax(hi); }} />
      </Field>

      <Messages rows={messages} onChange={setMessages} onPaste={() => setPasting(true)} />

      {pasting && (
        <PackModal existing={messages.length} onClose={() => setPasting(false)}
                   onAdd={(texts) => {
                     setMessages((cur) => [...cur, ...texts.map(newRow)]);
                     setPasting(false);
                   }} />
      )}
    </Modal>
  );
}

/** The texts of a campaign, folded into one line until opened. Opened, it
 *  is a list of one-line previews that scrolls inside itself; a row opens
 *  into an editor when clicked. */
function Messages({ rows, onChange, onPaste }: {
  rows: MessageRow[];
  onChange: (rows: MessageRow[]) => void;
  onPaste: () => void;
}) {
  const { t } = useStore();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const full = rows.length >= MESSAGE_CAP;

  const update = (key: string, patch: Partial<MessageRow>) =>
    onChange(rows.map((m) => (m.key === key ? { ...m, ...patch } : m)));

  function addOne() {
    const row = newRow("");
    onChange([...rows, row]);
    setOpen(true);
    setEditing(row.key);
  }

  return (
    <div className={`messages${open ? " open" : ""}`}>
      <div className="messages-head">
        <button type="button" className="messages-toggle" onClick={() => setOpen(!open)}>
          <span className={`chevron${open ? " open" : ""}`}>›</span>
          {t("campaigns.messages", { n: rows.length })}
        </button>
        <ActionButton onClick={addOne} disabled={full}>{t("campaigns.add_message")}</ActionButton>
        <ActionButton onClick={onPaste} disabled={full}>{t("campaigns.add_messages")}</ActionButton>
      </div>
      {open && (
        <div className="messages-list">
          {!rows.length && <p className="note message-edit">{t("campaigns.messages_hint")}</p>}
          {rows.map((row, i) => (
            <div className="message" key={row.key}>
              <div className="message-line"
                   onClick={() => setEditing(editing === row.key ? null : row.key)}>
                <span className="message-no">{i + 1}</span>
                <span className={`message-preview${row.text.trim() ? "" : " empty-text"}`}>
                  {row.text.trim().split("\n")[0] || t("campaigns.message_empty")}
                </span>
                {(row.file || row.picked) && <span className="message-pic">📎</span>}
                <ActionButton variant="icon" size="sm" quiet title={t("common.delete")}
                              onClick={(e) => {
                                e.stopPropagation();
                                onChange(rows.filter((m) => m.key !== row.key));
                              }}>
                  ✕
                </ActionButton>
              </div>
              {editing === row.key && (
                <div className="message-edit">
                  <TextArea value={row.text} autoFocus
                            placeholder={t("campaigns.message_placeholder", { n: i + 1 })}
                            onChange={(text) => update(row.key, { text })} />
                  <Attachment file={row.file} picked={row.picked}
                              onPick={(picked) => update(row.key, { file: "", picked })}
                              onClear={() => update(row.key, { file: "", picked: undefined })} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** The picture of one message: attach it, see its name, take it off. */
function Attachment({ file, picked, onPick, onClear }: {
  file: string;
  picked?: File;
  onPick: (file: File) => void;
  onClear: () => void;
}) {
  const { t } = useStore();
  const input = useRef<HTMLInputElement>(null);
  const name = picked ? picked.name : file.split(/[\\/]/).pop() ?? "";
  return (
    <div className="message-file">
      <input ref={input} type="file" accept="image/*" hidden
             onChange={(e) => {
               const chosen = e.target.files?.[0];
               e.target.value = "";
               if (chosen) onPick(chosen);
             }} />
      {file || picked ? (
        <>
          <span className="faint truncate" title={name}>📎 {name}</span>
          <ActionButton size="sm" variant="ghost" onClick={onClear}>
            {t("campaigns.file_clear")}
          </ActionButton>
        </>
      ) : (
        <ActionButton size="sm" variant="ghost" onClick={() => input.current?.click()}>
          {t("campaigns.file_add")}
        </ActionButton>
      )}
    </div>
  );
}

/** Paste a whole list of messages at once. Nothing is added while the text
 *  cannot be split cleanly: a half-parsed list looks fine and is not. */
function PackModal({ existing, onClose, onAdd }: {
  existing: number;
  onClose: () => void;
  onAdd: (texts: string[]) => void;
}) {
  const { t } = useStore();
  const [text, setText] = useState("");
  const parsed = useMemo(() => parsePack(text, existing, t), [text, existing, t]);
  const ready = parsed.messages.length > 0 && !parsed.error;

  return (
    <Modal title={t("campaigns.pack_title")} onClose={onClose}
           footer={<FormFooter onCancel={onClose} disabled={!ready}
                               onConfirm={() => onAdd(parsed.messages)}
                               confirm={t("campaigns.pack_add", { n: parsed.messages.length })} />}>
      <p className="field-hint field">{t("campaigns.pack_hint")}</p>
      <pre className="pack-example">{t("campaigns.pack_example")}</pre>
      <Field hint={parsed.messages.length && !parsed.error
        ? t("campaigns.pack_found", { n: parsed.messages.length }) : undefined}>
        <TextArea size="tall" value={text} autoFocus
                  placeholder={t("campaigns.pack_placeholder")} onChange={setText} />
      </Field>
      <FormError message={parsed.error} />
    </Modal>
  );
}
