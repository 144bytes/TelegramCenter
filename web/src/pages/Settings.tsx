import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import {
  ApiHelpLink, Confirm, Field, IssueDot, Modal, NumberField, NumberRange,
  PageHead, Switch, fmtDuration,
} from "../components/ui";
import type { ApiProfile, NetworkProfile } from "../types";

type Section = "general" | "autoreply" | "campaigns" | "spamcheck" | "profiles";

const SECTIONS: Section[] = ["general", "autoreply", "campaigns", "spamcheck",
                             "profiles"];

export function SettingsPage() {
  const { snapshot, t } = useStore();
  const [section, setSection] = useState<Section>("general");
  if (!snapshot) return null;

  return (
    <>
      <PageHead title={t("settings.title")} sub={t("settings.sub")} />
      <div style={{ display: "grid", gridTemplateColumns: "190px 1fr", gap: 16 }}>
        <div>
          {SECTIONS.map((key) => (
            <button key={key}
                    className={`nav-item${section === key ? " active" : ""}`}
                    onClick={() => setSection(key)}>
              <span>{t(`settings.${key}`)}</span>
            </button>
          ))}
        </div>
        <div>
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
 *  on every one would be constant noise — the toast is reserved for buttons
 *  the user actually presses. Errors still surface, and a write that changes
 *  nothing is skipped entirely.
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

function General() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const g = snapshot!.settings.general;
  const a = snapshot!.settings.accounts;

  return (
    <div className="card">
      <Field label={t("settings.language")}>
        <select className="select" style={{ maxWidth: 220 }} value={g.language}
                onChange={(e) => save({ "general.language": e.target.value })}>
          <option value="ru">Русский</option>
          <option value="en">English</option>
        </select>
      </Field>

      <div style={{ marginBottom: 12 }}>
        <Switch checked={g.show_log_time} label={t("settings.show_log_time")}
                onChange={(v) => save({ "general.show_log_time": v })} />
      </div>
      <div style={{ marginBottom: 18 }}>
        <Switch checked={g.open_browser_on_start} label={t("settings.open_browser")}
                onChange={(v) => save({ "general.open_browser_on_start": v })} />
      </div>

      <div className="row">
        <Field label={t("settings.dialog_limit")}>
          <NumberField value={a.dialog_limit} min={1}
                       onCommit={(v) => save({ "accounts.dialog_limit": v })} />
        </Field>
        <Field label={t("settings.message_limit")}>
          <NumberField value={a.message_limit} min={1}
                       onCommit={(v) => save({ "accounts.message_limit": v })} />
        </Field>
        <Field label={`${t("settings.probe_interval")}, ${t("common.seconds")}`}>
          <NumberField value={a.probe_interval_sec} min={60}
                       onCommit={(v) => save({ "accounts.probe_interval_sec": v })} />
        </Field>
      </div>
    </div>
  );
}

function AutoReplyDefaults() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.autoreply;
  const [limit, setLimit] = useState(cfg.faq_limit);

  return (
    <div className="card">
      <Field label={`${t("settings.faq_limit")}: ${limit}`}
             hint={t("autoreply.faq_hint", { limit })}>
        <input className="slider" type="range" min={0} max={10} value={limit}
               onChange={(e) => setLimit(Number(e.target.value))}
               onMouseUp={() => save({ "autoreply.faq_limit": limit })}
               onTouchEnd={() => save({ "autoreply.faq_limit": limit })} />
      </Field>

      <Field label={`${t("settings.default_delay")}, ${t("common.seconds")}`}
             hint={t("autoreply.delay_hint")}>
        <NumberRange
          min={cfg.default_delay_min_sec}
          max={cfg.default_delay_max_sec}
          onChange={(lo, hi) => save({
            "autoreply.default_delay_min_sec": lo,
            "autoreply.default_delay_max_sec": hi,
          })}
        />
      </Field>
      <p className="field-hint">
        {fmtDuration(cfg.default_delay_min_sec)} – {fmtDuration(cfg.default_delay_max_sec)}
      </p>
    </div>
  );
}

function CampaignDefaults() {
  const { snapshot, t } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.campaign;

  return (
    <div className="card">
      <Field label={`${t("settings.default_gap")}, ${t("common.seconds")}`}
             hint={t("campaigns.gap_hint")}>
        <NumberRange
          min={cfg.default_gap_min_sec}
          max={cfg.default_gap_max_sec}
          onChange={(lo, hi) => save({
            "campaign.default_gap_min_sec": lo,
            "campaign.default_gap_max_sec": hi,
          })}
        />
      </Field>
    </div>
  );
}

function SpamCheck() {
  const { snapshot, t, act } = useStore();
  const save = useSave();
  const cfg = snapshot!.settings.spamcheck;
  const [bot, setBot] = useState(cfg.bot);

  const limited = snapshot!.accounts.filter((a) => a.spam_state === "LIMITED");

  return (
    <div className="card">
      <div style={{ marginBottom: 14 }}>
        <Switch checked={cfg.enabled} label={t("spam.enabled")}
                onChange={(v) => save({ "spamcheck.enabled": v })} />
        <p className="field-hint">{t("spam.enabled_hint")}</p>
      </div>

      <div style={{ marginBottom: 14 }}>
        <Switch checked={cfg.include_operators}
                disabled={!cfg.enabled}
                label={t("spam.include_operators")}
                onChange={(v) => save({ "spamcheck.include_operators": v })} />
        <p className="field-hint">{t("spam.include_operators_hint")}</p>
      </div>

      <Field label={t("spam.bot")} hint={t("spam.bot_hint")}>
        <input className="input" style={{ maxWidth: 260 }} value={bot}
               onChange={(e) => setBot(e.target.value)}
               onBlur={() => {
                 const next = bot.trim();
                 if (!next) { setBot(cfg.bot); return; }
                 save({ "spamcheck.bot": next });
               }} />
      </Field>

      <Field label={`${t("spam.delay")}, ${t("common.seconds")}`}
             hint={t("spam.delay_hint")}>
        <div style={{ maxWidth: 140 }}>
          <NumberField value={cfg.delay_sec} min={0} max={3600}
                       onCommit={(v) => save({ "spamcheck.delay_sec": v })} />
        </div>
      </Field>

      <PhraseList label={t("spam.clean_phrases")}
                  hint={t("spam.clean_hint")}
                  value={cfg.clean_phrases}
                  onSave={(v) => save({ "spamcheck.clean_phrases": v })} />
      <PhraseList label={t("spam.limited_phrases")}
                  hint={t("spam.limited_hint")}
                  value={cfg.limited_phrases}
                  onSave={(v) => save({ "spamcheck.limited_phrases": v })} />

      <div style={{ borderTop: "1px solid var(--border-soft)", paddingTop: 14 }}>
        <button className="btn" disabled={snapshot!.checkup.running}
                onClick={() => act(() => api.accounts.spamCheck())}>
          {snapshot!.checkup.running
            ? t("accounts.checking_progress", {
                done: snapshot!.checkup.done, total: snapshot!.checkup.total })
            : t("spam.run_now")}
        </button>
        <p className="field-hint">
          {limited.length
            ? t("spam.limited_now", { count: limited.length })
            : t("spam.no_limits_known")}
        </p>
      </div>
    </div>
  );
}


/** One phrase per line. Edited as text because that is how a list of
 *  sentences is comfortable to read and paste. */
function PhraseList({ label, hint, value, onSave }: {
  label: string; hint: string; value: string[];
  onSave: (phrases: string[]) => void;
}) {
  const [text, setText] = useState(value.join("\n"));
  const shown = useRef(value.join("\n"));

  useEffect(() => {
    const next = value.join("\n");
    if (next !== shown.current) {      // changed elsewhere, not by this field
      shown.current = next;
      setText(next);
    }
  }, [value]);

  function commit() {
    const phrases = text.split("\n").map((line) => line.trim()).filter(Boolean);
    if (phrases.join("\n") === value.join("\n")) return;
    shown.current = phrases.join("\n");
    setText(shown.current);
    onSave(phrases);
  }

  return (
    <Field label={label} hint={hint}>
      <textarea className="textarea mono" style={{ minHeight: 120 }} value={text}
                onChange={(e) => setText(e.target.value)} onBlur={commit} />
    </Field>
  );
}


function Profiles() {
  const { snapshot, t, act } = useStore();
  const [apiEdit, setApiEdit] = useState<ApiProfile | null>(null);
  const [apiNew, setApiNew] = useState(false);
  const [proxyEdit, setProxyEdit] = useState<NetworkProfile | null>(null);
  const [proxyNew, setProxyNew] = useState(false);
  const [removing, setRemoving] = useState<
    { kind: "api" | "proxy"; id: string; name: string } | null>(null);

  return (
    <>
      <div className="card">
        <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
          <p className="card-title" style={{ margin: 0 }}>{t("settings.api_profiles")}</p>
          <button className="btn sm accent" style={{ marginLeft: "auto" }}
                  onClick={() => setApiNew(true)}>{t("settings.add_api")}</button>
        </div>
        {!snapshot!.api_profiles.length && (
          <div className="empty">{t("common.none")}</div>
        )}
        {snapshot!.api_profiles.map((p) => (
          <div key={p.id} style={{
            display: "flex", alignItems: "center", gap: 10, padding: "8px 0",
            borderTop: "1px solid var(--border-soft)",
          }}>
            <IssueDot issues={p.issues} state={p.effective} />
            <span style={{ minWidth: 150 }}>{p.name || p.id}</span>
            <span className="mono faint">{p.api_id ?? "—"}</span>
            <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              <button className="btn sm ghost"
                      onClick={() => act(() => api.profiles.probeApi(p.id))}>
                {t("common.check")}
              </button>
              <button className="btn sm ghost" onClick={() => setApiEdit(p)}>
                {t("common.edit")}
              </button>
              <button className="btn sm danger" onClick={() => setRemoving(
                { kind: "api", id: p.id, name: p.name || p.id })}>
                {t("common.delete")}
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
          <p className="card-title" style={{ margin: 0 }}>{t("settings.proxies")}</p>
          <button className="btn sm accent" style={{ marginLeft: "auto" }}
                  onClick={() => setProxyNew(true)}>{t("settings.add_proxy")}</button>
        </div>
        {!snapshot!.network_profiles.length && (
          <div className="empty">{t("common.none")}</div>
        )}
        {snapshot!.network_profiles.map((p) => (
          <div key={p.id} style={{
            display: "flex", alignItems: "center", gap: 10, padding: "8px 0",
            borderTop: "1px solid var(--border-soft)",
          }}>
            <IssueDot issues={p.issues} state={p.effective} />
            <span style={{ minWidth: 150 }}>{p.name || p.host}</span>
            <span className="mono faint">{p.protocol} {p.host}:{p.port}</span>
            {p.last_latency_ms !== null && (
              <span className="faint">{p.last_latency_ms} ms</span>
            )}
            <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
              <button className="btn sm ghost"
                      onClick={() => act(() => api.profiles.probeProxy(p.id))}>
                {t("common.check")}
              </button>
              <button className="btn sm ghost" onClick={() => setProxyEdit(p)}>
                {t("common.edit")}
              </button>
              <button className="btn sm danger" onClick={() => setRemoving(
                { kind: "proxy", id: p.id, name: p.name || p.host })}>
                {t("common.delete")}
              </button>
            </div>
          </div>
        ))}
      </div>

      {(apiNew || apiEdit) && (
        <ApiModal profile={apiEdit}
                  onClose={() => { setApiNew(false); setApiEdit(null); }} />
      )}
      {(proxyNew || proxyEdit) && (
        <ProxyModal profile={proxyEdit}
                    onClose={() => { setProxyNew(false); setProxyEdit(null); }} />
      )}
      {removing && (
        <Confirm
          text={t("common.confirm_delete", { name: removing.name })}
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const target = removing;
            setRemoving(null);
            void act(() => (target.kind === "api"
              ? api.profiles.removeApi(target.id)
              : api.profiles.removeProxy(target.id)));
          }}
        />
      )}
    </>
  );
}

function ApiModal({ profile, onClose }: {
  profile: ApiProfile | null; onClose: () => void;
}) {
  const { t, act } = useStore();
  const [name, setName] = useState(profile?.name ?? "");
  const [apiId, setApiId] = useState(String(profile?.api_id ?? ""));
  const [apiHash, setApiHash] = useState(profile?.api_hash ?? "");
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);

  async function save() {
    await act(() => api.profiles.saveApi({
      id: profile?.id, name, api_id: apiId, api_hash: apiHash, enabled }));
    onClose();
  }

  return (
    <Modal title={profile ? t("common.edit") : t("settings.add_api")} onClose={onClose}
           footer={
             <>
               <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
               <button className="btn accent" onClick={save}>{t("common.save")}</button>
             </>
           }>
      <Field label={t("common.name")}>
        <input className="input" autoFocus value={name}
               onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label={t("settings.api_id")}>
        <input className="input" value={apiId} inputMode="numeric"
               onChange={(e) => setApiId(e.target.value)} />
      </Field>
      <Field label={t("settings.api_hash")}>
        <input className="input mono" value={apiHash}
               onChange={(e) => setApiHash(e.target.value)} />
      </Field>
      <ApiHelpLink />
      <div style={{ height: 12 }} />
      <Switch checked={enabled} onChange={setEnabled}
              label={enabled ? t("common.enabled") : t("common.disabled")} />
    </Modal>
  );
}

function ProxyModal({ profile, onClose }: {
  profile: NetworkProfile | null; onClose: () => void;
}) {
  const { t, act } = useStore();
  const [name, setName] = useState(profile?.name ?? "");
  const [protocol, setProtocol] = useState(profile?.protocol ?? "SOCKS5");
  const [host, setHost] = useState(profile?.host ?? "");
  const [port, setPort] = useState(String(profile?.port ?? ""));
  const [username, setUsername] = useState(profile?.username ?? "");
  const [password, setPassword] = useState(profile?.password ?? "");
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);

  async function save() {
    await act(() => api.profiles.saveProxy({
      id: profile?.id, name, protocol, host, port, username, password, enabled }));
    onClose();
  }

  return (
    <Modal title={profile ? t("common.edit") : t("settings.add_proxy")} onClose={onClose}
           footer={
             <>
               <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
               <button className="btn accent" onClick={save}>{t("common.save")}</button>
             </>
           }>
      <Field label={t("common.name")}>
        <input className="input" autoFocus value={name}
               onChange={(e) => setName(e.target.value)} />
      </Field>
      <div className="row">
        <Field label={t("settings.protocol")}>
          <select className="select" value={protocol}
                  onChange={(e) => setProtocol(e.target.value)}>
            <option>SOCKS5</option>
            <option>SOCKS4</option>
            <option>HTTP</option>
          </select>
        </Field>
        <Field label={t("settings.host")}>
          <input className="input" value={host}
                 onChange={(e) => setHost(e.target.value)} />
        </Field>
        <Field label={t("settings.port")}>
          <input className="input" value={port} inputMode="numeric"
                 onChange={(e) => setPort(e.target.value)} />
        </Field>
      </div>
      <div className="row">
        <Field label={`${t("settings.login_username")} (${t("common.optional")})`}>
          <input className="input" value={username}
                 onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label={`${t("settings.password")} (${t("common.optional")})`}>
          <input className="input" type="password" value={password}
                 onChange={(e) => setPassword(e.target.value)} />
        </Field>
      </div>
      <Switch checked={enabled} onChange={setEnabled}
              label={enabled ? t("common.enabled") : t("common.disabled")} />
    </Modal>
  );
}
