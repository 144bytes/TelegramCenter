import { useState } from "react";
import { api } from "../api";
import { buttonCheckGoing } from "../checkup";
import { useStore } from "../state";
import {
  ActionButton, ApiHelpLink, Confirm, Field, FormError, FormFooter, Modal, NumberInput,
  PageHead, RangeInput, SectionHead, Select, SettingRow, SettingsGroup, Status, Switch,
  Tabs, TextArea, TextInput, byId, hashFocusId, useDraft, useHashFocus, useSavedText,
  useSubmit,
} from "../components/ui";
import type { ApiProfile, Effective, NetworkProfile } from "../types";

type Section = "general" | "autoreply" | "campaigns" | "spamcheck" | "profiles";

const SECTIONS: Section[] = ["general", "autoreply", "campaigns", "spamcheck", "profiles"];

export function SettingsPage() {
  const { snapshot, t } = useStore();
  // A link from the overview points at a profile, and profiles live behind
  // their own tab: open it before anything tries to scroll to the row.
  const [section, setSection] = useState<Section>(
    () => (hashFocusId() ? "profiles" : "general"));
  useHashFocus(() => setSection("profiles"));
  if (!snapshot) return null;

  return (
    <>
      <PageHead title={t("settings.title")} sub={t("settings.sub")} />
      <div className="settings-layout">
        <Tabs value={section} onChange={setSection}
              items={SECTIONS.map((key) => ({ key, label: t.dyn(`settings.${key}`) }))} />
        <div className="stack">
          {section === "general" && <General />}
          {section === "autoreply" && <AutoReplyDefaults />}
          {section === "campaigns" && <CampaignDefaults />}
          {section === "spamcheck" && <SpamCheck />}
          {section === "profiles" && <Profiles />}
        </div>
      </div>
    </>
  );
}

/** Reads a dotted settings path out of the snapshot. */
function current(settings: unknown, path: string): unknown {
  let node: any = settings;
  for (const part of path.split(".")) {
    if (node === null || typeof node !== "object" || !(part in node)) return undefined;
    node = node[part];
  }
  return node;
}

/** Saves settings quietly.
 *
 *  These fields write themselves as soon as you leave them, so a confirmation
 *  on every one would be constant noise. Errors still surface, and a write
 *  that changes nothing is skipped entirely.
 */
function useSave() {
  const { act, snapshot } = useStore();
  return (values: Record<string, unknown>) => {
    const settings = snapshot?.settings;
    const unchanged = Object.entries(values)
      .every(([path, value]) => current(settings, path) === value);
    if (unchanged) return Promise.resolve(null);
    return act(() => api.settings.save(values));
  };
}

/** One number setting, the same width everywhere; its unit is in the label.
 *  `off` replaces the hint while the value is 0, when 0 means «switched off». */
function NumberRow({ label, hint, off, unit, value, min = 0, max, onCommit }: {
  label: string; hint?: string; off?: string; unit?: string;
  value: number; min?: number; max?: number;
  onCommit: (value: number) => unknown;
}) {
  return (
    <SettingRow label={label} unit={unit} hint={value === 0 && off ? off : hint}>
      <NumberInput value={value} min={min} max={max} onCommit={onCommit} />
    </SettingRow>
  );
}

function RangeRow({ label, hint, unit, min, max, onChange }: {
  label: string; hint?: string; unit: string; min: number; max: number;
  onChange: (min: number, max: number) => unknown;
}) {
  return (
    <SettingRow label={label} unit={unit} hint={hint}>
      <RangeInput min={min} max={max} onChange={onChange} />
    </SettingRow>
  );
}

function SwitchRow({ label, hint, checked, onChange }: {
  label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void;
}) {
  return (
    <SettingRow label={label} hint={hint}>
      <Switch checked={checked} onChange={onChange} />
    </SettingRow>
  );
}

function General() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const g = snapshot!.settings.general;
  const a = snapshot!.settings.accounts;

  return (
    <div className="card">
      <SettingsGroup>
        <SettingRow label={t("settings.language")}>
          <Select width="pick" value={g.language}
                  onChange={(v) => save({ "general.language": v })}
                  options={[{ value: "ru", label: "Русский" }, { value: "en", label: "English" }]} />
        </SettingRow>
        <SwitchRow label={t("settings.show_log_time")} checked={g.show_log_time}
                   onChange={(v) => save({ "general.show_log_time": v })} />
        <SwitchRow label={t("settings.open_browser")} checked={g.open_browser_on_start}
                   onChange={(v) => save({ "general.open_browser_on_start": v })} />
      </SettingsGroup>

      <SettingsGroup title={t("settings.group_chats")}>
        <NumberRow label={t("settings.dialog_limit")} value={a.dialog_limit} min={1}
                   onCommit={(v) => save({ "accounts.dialog_limit": v })} />
        <NumberRow label={t("settings.message_limit")} value={a.message_limit} min={1}
                   onCommit={(v) => save({ "accounts.message_limit": v })} />
      </SettingsGroup>

      <SettingsGroup title={t("settings.group_accounts")}>
        <NumberRow label={t("settings.young_days")} unit={t("unit.days")}
                   hint={t("settings.young_days_hint")} off={t("settings.young_off")}
                   value={a.young_days} max={365}
                   onCommit={(v) => save({ "accounts.young_days": v })} />
        <RangeRow label={t("settings.probe_interval")} unit={t("unit.sec")}
                  hint={t("settings.probe_interval_hint")}
                  min={a.probe_interval_min_sec} max={a.probe_interval_max_sec}
                  onChange={(lo, hi) => save({
                    "accounts.probe_interval_min_sec": lo,
                    "accounts.probe_interval_max_sec": hi,
                  })} />
        <NumberRow label={t("settings.connect_spread")} unit={t("unit.sec")}
                   hint={t("settings.connect_spread_hint")} off={t("settings.connect_spread_off")}
                   value={a.connect_spread_sec} max={3600}
                   onCommit={(v) => save({ "accounts.connect_spread_sec": v })} />
      </SettingsGroup>
    </div>
  );
}

function AutoReplyDefaults() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.autoreply;
  return (
    <div className="card">
      <SettingsGroup>
        <NumberRow label={t("settings.faq_limit")}
                   hint={t("autoreply.faq_hint", { limit: cfg.faq_limit })}
                   value={cfg.faq_limit} max={10}
                   onCommit={(v) => save({ "autoreply.faq_limit": v })} />
      </SettingsGroup>
    </div>
  );
}

function CampaignDefaults() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.campaign;

  return (
    <div className="card">
      <SettingsGroup title={t("settings.group_gap")}>
        <RangeRow label={t("settings.default_interval")} unit={t("unit.sec")}
                  hint={t("campaigns.interval_hint")}
                  min={cfg.default_interval_min_sec} max={cfg.default_interval_max_sec}
                  onChange={(lo, hi) => save({
                    "campaign.default_interval_min_sec": lo,
                    "campaign.default_interval_max_sec": hi,
                  })} />
        <NumberRow label={t("settings.start_delay")} unit={t("unit.sec")}
                   hint={t("settings.start_delay_hint")}
                   value={cfg.start_delay_sec} max={86400}
                   onCommit={(v) => save({ "campaign.start_delay_sec": v })} />
      </SettingsGroup>

      <SettingsGroup title={t("settings.group_natural")}>
        <SwitchRow label={t("settings.auto_join")} hint={t("settings.auto_join_hint")}
                   checked={cfg.auto_join} onChange={(v) => save({ "campaign.auto_join": v })} />
        {/* Only while subscribing is on: a pause before writing means nothing
            when nothing is being joined. */}
        {cfg.auto_join && (
          <NumberRow label={t("settings.auto_join_delay")} unit={t("unit.sec")}
                     hint={t("settings.auto_join_delay_hint")}
                     value={cfg.auto_join_delay_sec} max={3600}
                     onCommit={(v) => save({ "campaign.auto_join_delay_sec": v })} />
        )}
        <SwitchRow label={t("settings.reactions")} hint={t("settings.reactions_hint")}
                   checked={cfg.reactions} onChange={(v) => save({ "campaign.reactions": v })} />
        {cfg.reactions && (
          <NumberRow label={t("settings.reaction_percent")} unit="%"
                     hint={t("settings.reaction_percent_hint")} off={t("settings.limit_off")}
                     value={cfg.reaction_percent} max={100}
                     onCommit={(v) => save({ "campaign.reaction_percent": v })} />
        )}
      </SettingsGroup>

      {/* The one that acts on its own rather than merely waiting. */}
      <SettingsGroup title={t("settings.group_guard")}>
        <NumberRow label={t("settings.error_stop_percent")} unit="%"
                   hint={t("settings.error_stop_hint")} off={t("settings.limit_off")}
                   value={cfg.error_stop_percent} max={100}
                   onCommit={(v) => save({ "campaign.error_stop_percent": v })} />
        <NumberRow label={t("settings.error_stop_min")} unit={t("unit.msg")}
                   hint={t("settings.error_stop_min_hint")} off={t("settings.limit_off")}
                   value={cfg.error_stop_min} max={1000}
                   onCommit={(v) => save({ "campaign.error_stop_min": v })} />
      </SettingsGroup>
    </div>
  );
}

function SpamCheck() {
  const { snapshot, t, act } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.spamcheck;
  const checkup = snapshot!.checkup;
  // the server writes the name its own way («SpamBot» becomes «@SpamBot»)
  const bot = useSavedText(cfg.bot);
  const limited = snapshot!.accounts.filter((a) => a.spam_state === "LIMITED");

  return (
    <div className="card">
      <SettingsGroup>
        <SwitchRow label={t("spam.enabled")} hint={t("spam.enabled_hint")}
                   checked={cfg.enabled} onChange={(v) => save({ "spamcheck.enabled": v })} />
        {/* Shown only once the check itself is on: a greyed-out control is
            still a control to read past. */}
        {cfg.enabled && (
          <SwitchRow label={t("spam.include_operators")} hint={t("spam.include_operators_hint")}
                     checked={cfg.include_operators}
                     onChange={(v) => save({ "spamcheck.include_operators": v })} />
        )}
        <SettingRow label={t("spam.bot")} hint={t("spam.bot_hint")}>
          <TextInput width="text" value={bot.text} onChange={bot.setText}
                     onBlur={() => {
                       const next = bot.text.trim();
                       if (!next) { bot.setText(cfg.bot); return; }
                       void bot.commit(() => save({ "spamcheck.bot": next }));
                     }} />
        </SettingRow>
        <NumberRow label={t("spam.delay")} hint={t("spam.delay_hint")} unit={t("unit.sec")}
                   value={cfg.delay_sec} max={3600}
                   onCommit={(v) => save({ "spamcheck.delay_sec": v })} />
        <SettingRow label={limited.length ? t("spam.limited_now", { count: limited.length })
                                          : t("spam.no_limits_known")}>
          <ActionButton disabled={buttonCheckGoing(checkup)}
                        onClick={() => act(() => api.accounts.spamCheck())}>
            {checkup.running && checkup.spam
              ? t("accounts.checking_progress", { done: checkup.done, total: checkup.total })
              : t("spam.run_now")}
          </ActionButton>
        </SettingRow>
      </SettingsGroup>

      <SettingsGroup title={t("settings.group_phrases")}>
        <PhraseList label={t("spam.clean_phrases")} hint={t("spam.clean_hint")}
                    value={cfg.clean_phrases}
                    onSave={(v) => save({ "spamcheck.clean_phrases": v })} />
        <PhraseList label={t("spam.blocked_phrases")} hint={t("spam.blocked_hint")}
                    value={cfg.blocked_phrases}
                    onSave={(v) => save({ "spamcheck.blocked_phrases": v })} />
        <PhraseList label={t("spam.limited_phrases")} hint={t("spam.limited_hint")}
                    value={cfg.limited_phrases}
                    onSave={(v) => save({ "spamcheck.limited_phrases": v })} />
      </SettingsGroup>
    </div>
  );
}

/** One phrase per line. Edited as text because that is how a list of
 *  sentences is comfortable to read and paste. */
function PhraseList({ label, hint, value, onSave }: {
  label: string; hint: string; value: string[];
  onSave: (phrases: string[]) => unknown;
}) {
  const field = useSavedText(value.join("\n"));

  function commit() {
    const phrases = field.text.split("\n").map((line) => line.trim()).filter(Boolean);
    field.setText(phrases.join("\n"));
    if (phrases.join("\n") === value.join("\n")) return;
    // emptied, the list goes back to the built-in phrases - and says so
    void field.commit(() => onSave(phrases));
  }

  return (
    <SettingRow block label={label} hint={hint}>
      <TextArea mono value={field.text} onChange={field.setText} onBlur={commit} />
    </SettingRow>
  );
}

/** The word under a profile's dot. A profile is not «ready» the way an
 *  account is: it works, or it has not been seen working yet. */
function profileLabel(t: ReturnType<typeof useStore>["t"], state: Effective): string {
  if (state === "READY") return t("settings.works");
  if (state === "OFFLINE") return t("state.UNKNOWN");
  return t.dyn(`state.${state}`);
}

function Profiles() {
  const { snapshot, t, act } = useStore();
  // Windows keep ids and read the live records: see byId.
  const [apiEdit, setApiEdit] = useState<string | null>(null);
  const [apiNew, setApiNew] = useState(false);
  const [proxyEdit, setProxyEdit] = useState<string | null>(null);
  const [proxyNew, setProxyNew] = useState(false);
  const [removing, setRemoving] = useState<{ kind: "api" | "proxy"; id: string } | null>(null);
  const editedApi = byId(snapshot!.api_profiles, apiEdit);
  const editedProxy = byId(snapshot!.network_profiles, proxyEdit);
  const removedApi = removing?.kind === "api"
    ? byId(snapshot!.api_profiles, removing.id) : undefined;
  const removedProxy = removing?.kind === "proxy"
    ? byId(snapshot!.network_profiles, removing.id) : undefined;
  const removedName = removedApi ? removedApi.name || removedApi.id
    : removedProxy ? removedProxy.name || removedProxy.host : null;

  return (
    <>
      <div className="card">
        <SectionHead title={t("settings.api_profiles")} action={
          <ActionButton onClick={() => setApiNew(true)}>{t("settings.add_api")}</ActionButton>
        } />
        {!snapshot!.api_profiles.length && <div className="empty">{t("settings.no_api")}</div>}
        {snapshot!.api_profiles.map((p) => (
          <div className="list-row" key={p.id} id={p.id}>
            <div className="list-name">
              <div className="list-title" title={p.name || p.id}>{p.name || p.id}</div>
              <div className="list-sub mono">api_id {p.api_id ?? "—"}</div>
            </div>
            <span className="list-status">
              <Status state={p.effective} issues={p.issues} label={profileLabel(t, p.effective)} />
            </span>
            {/* No «Проверить»: an API profile is checked by the accounts
                that use it. The empty slot keeps the columns in line. */}
            <span className="ab-row list-actions">
              <ActionButton size="sm" variant="ghost" onClick={() => setApiEdit(p.id)}>
                {t("common.edit")}
              </ActionButton>
              <ActionButton size="sm" variant="ghost-danger"
                            onClick={() => setRemoving({ kind: "api", id: p.id })}>
                {t("common.delete")}
              </ActionButton>
            </span>
          </div>
        ))}
      </div>

      <div className="card">
        <SectionHead title={t("settings.proxies")} action={
          <ActionButton onClick={() => setProxyNew(true)}>{t("settings.add_proxy")}</ActionButton>
        } />
        {!snapshot!.network_profiles.length && <div className="empty">{t("settings.no_proxies")}</div>}
        {snapshot!.network_profiles.map((p) => (
          <div className="list-row" key={p.id} id={p.id}>
            <div className="list-name">
              <div className="list-title" title={p.name || p.host}>{p.name || p.host}</div>
              <div className="list-sub mono">
                {p.protocol} {p.host}:{p.port}
                {p.last_latency_ms !== null && ` · ${p.last_latency_ms} ms`}
              </div>
            </div>
            <span className="list-status">
              <Status state={p.effective} issues={p.issues} label={profileLabel(t, p.effective)} />
            </span>
            <span className="ab-row list-actions">
              <ActionButton size="sm" variant="ghost"
                            onClick={() => act(() => api.profiles.probeProxy(p.id))}>
                {t("common.check")}
              </ActionButton>
              <ActionButton size="sm" variant="ghost" onClick={() => setProxyEdit(p.id)}>
                {t("common.edit")}
              </ActionButton>
              <ActionButton size="sm" variant="ghost-danger"
                            onClick={() => setRemoving({ kind: "proxy", id: p.id })}>
                {t("common.delete")}
              </ActionButton>
            </span>
          </div>
        ))}
      </div>

      {(apiNew || editedApi) && (
        <ApiModal profile={editedApi ?? null}
                  onClose={() => { setApiNew(false); setApiEdit(null); }} />
      )}
      {(proxyNew || editedProxy) && (
        <ProxyModal profile={editedProxy ?? null}
                    onClose={() => { setProxyNew(false); setProxyEdit(null); }} />
      )}
      {removing && removedName !== null && (
        <Confirm text={t("common.confirm_delete", { name: removedName })}
                 onCancel={() => setRemoving(null)}
                 onConfirm={() => {
                   const target = removing;
                   setRemoving(null);
                   void act(() => (target.kind === "api"
                     ? api.profiles.removeApi(target.id)
                     : api.profiles.removeProxy(target.id)));
                 }} />
      )}
    </>
  );
}

/** A new API profile, or one's window: a draft over the live record
 *  (useDraft), saved in one request, closed only when that went through. */
function ApiModal({ profile, onClose }: { profile: ApiProfile | null; onClose: () => void }) {
  const { t } = useStore();
  const form = useDraft({
    name: profile?.name ?? "",
    api_id: String(profile?.api_id ?? ""),
    api_hash: profile?.api_hash ?? "",
    enabled: profile?.enabled ?? true,
  });
  const submit = useSubmit();
  const { values } = form;

  async function save() {
    const ok = await submit.run(async () => {
      if (!profile) await api.profiles.saveApi(values);
      else if (form.dirty) await api.profiles.saveApi({ id: profile.id, ...form.changes });
    });
    if (ok) onClose();
  }

  return (
    <Modal title={profile ? (profile.name || profile.id) : t("settings.add_api")} onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} />}>
      <FormError message={submit.error} />
      <Field label={t("common.name")}>
        <TextInput autoFocus value={values.name} onChange={(v) => form.set("name", v)} />
      </Field>
      <div className="field-row">
        <Field label={t("settings.api_id")}>
          <TextInput value={values.api_id} inputMode="numeric"
                     onChange={(v) => form.set("api_id", v)} />
        </Field>
        <Field label={t("settings.api_hash")}>
          <TextInput mono value={values.api_hash} onChange={(v) => form.set("api_hash", v)} />
        </Field>
      </div>
      <ApiHelpLink />
      <div className="modal-section">
        <Switch checked={values.enabled} onChange={(v) => form.set("enabled", v)}
                label={values.enabled ? t("common.enabled") : t("common.disabled")} />
      </div>
    </Modal>
  );
}

/** A new proxy, or one's window: the same draft and the same one request.
 *  All its fields go in one save, so the accounts behind it reconnect once,
 *  with the whole new address. */
function ProxyModal({ profile, onClose }: { profile: NetworkProfile | null; onClose: () => void }) {
  const { t } = useStore();
  const form = useDraft({
    name: profile?.name ?? "",
    protocol: profile?.protocol ?? "SOCKS5",
    host: profile?.host ?? "",
    port: String(profile?.port ?? ""),
    username: profile?.username ?? "",
    password: profile?.password ?? "",
    enabled: profile?.enabled ?? true,
  });
  const submit = useSubmit();
  const { values } = form;

  async function save() {
    const ok = await submit.run(async () => {
      if (!profile) await api.profiles.saveProxy(values);
      else if (form.dirty) await api.profiles.saveProxy({ id: profile.id, ...form.changes });
    });
    if (ok) onClose();
  }

  return (
    <Modal title={profile ? (profile.name || profile.host) : t("settings.add_proxy")}
           onClose={onClose}
           footer={<FormFooter onCancel={onClose} onConfirm={save} busy={submit.busy} />}>
      <FormError message={submit.error} />
      <Field label={t("common.name")}>
        <TextInput autoFocus value={values.name} onChange={(v) => form.set("name", v)} />
      </Field>
      <div className="field-row">
        <Field label={t("settings.protocol")}>
          <Select value={values.protocol} onChange={(v) => form.set("protocol", v)}
                  options={["SOCKS5", "SOCKS4", "HTTP"].map((p) => ({ value: p, label: p }))} />
        </Field>
        <Field label={t("settings.host")}>
          <TextInput value={values.host} onChange={(v) => form.set("host", v)} />
        </Field>
        <Field label={t("settings.port")}>
          <TextInput value={values.port} inputMode="numeric"
                     onChange={(v) => form.set("port", v)} />
        </Field>
      </div>
      <div className="field-row">
        <Field label={`${t("settings.login_username")} (${t("common.optional")})`}>
          <TextInput value={values.username} onChange={(v) => form.set("username", v)} />
        </Field>
        <Field label={`${t("settings.password")} (${t("common.optional")})`}>
          <TextInput type="password" value={values.password}
                     onChange={(v) => form.set("password", v)} />
        </Field>
      </div>
      <div className="modal-section">
        <Switch checked={values.enabled} onChange={(v) => form.set("enabled", v)}
                label={values.enabled ? t("common.enabled") : t("common.disabled")} />
      </div>
    </Modal>
  );
}
