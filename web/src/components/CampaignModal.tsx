import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { Field, FormError, Modal, NumberRange } from "./ui";
import type { Campaign, ScheduleMode } from "../types";

export function CampaignModal({ campaign, accountId, onClose }: {
  campaign: Campaign | null;
  accountId?: string;
  onClose: () => void;
}) {
  const { snapshot, t, act } = useStore();
  const settings = snapshot?.settings;

  const [name, setName] = useState(campaign?.name ?? "");
  const [account, setAccount] = useState(
    campaign?.account_id ?? accountId ?? snapshot?.accounts[0]?.id ?? "");
  const [targets, setTargets] = useState<string[]>(campaign?.target_ids ?? []);
  const [text, setText] = useState(campaign?.message_text ?? "");
  const [templateId, setTemplateId] = useState(campaign?.source_template_id ?? "");
  const [mode, setMode] = useState<ScheduleMode>(campaign?.schedule.mode ?? "ONCE");
  const [at, setAt] = useState(campaign?.schedule.at ?? "");
  const [times, setTimes] = useState((campaign?.schedule.times ?? []).join(", "));
  const [everyMin, setEveryMin] = useState(
    campaign?.schedule.every_sec ? Math.round(campaign.schedule.every_sec / 60) : 60);
  const [gapMin, setGapMin] = useState(
    campaign?.gap_min_sec ?? settings?.campaign.default_gap_min_sec ?? 15);
  const [gapMax, setGapMax] = useState(
    campaign?.gap_max_sec ?? settings?.campaign.default_gap_max_sec ?? 40);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function applyTemplate(id: string) {
    setTemplateId(id);
    const tpl = snapshot?.templates.find((x) => x.id === id);
    // a snapshot, not a link: from here the campaign owns this text
    if (tpl) setText(tpl.text);
  }

  function toggleTarget(id: string) {
    setTargets((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  async function save() {
    setError(null);
    setBusy(true);
    const payload: Record<string, unknown> = {
      id: campaign?.id,
      name, account_id: account, target_ids: targets,
      message_text: text, source_template_id: templateId || null,
      gap_min_sec: gapMin, gap_max_sec: gapMax,
      schedule: {
        mode,
        at: mode === "ONCE" ? (at || null) : null,
        times: mode === "DAILY"
          ? times.split(",").map((x) => x.trim()).filter(Boolean) : [],
        every_sec: mode === "INTERVAL" ? Math.max(1, everyMin) * 60 : null,
      },
    };
    try {
      if (campaign) await api.campaigns.update(payload);
      else await api.campaigns.create(payload);
      onClose();
      await act(async () => undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      wide
      title={campaign ? t("common.edit") : t("campaigns.new")}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn accent" onClick={save} disabled={busy}>
            {t("common.save")}
          </button>
        </>
      }
    >
      <FormError message={error} />

      <Field label={t("common.name")}>
        <input className="input" value={name} autoFocus
               onChange={(e) => setName(e.target.value)} />
      </Field>

      <Field label={t("campaigns.account")}>
        <select className="select" value={account}
                onChange={(e) => setAccount(e.target.value)}>
          {(snapshot?.accounts ?? []).map((a) => (
            <option key={a.id} value={a.id}>{a.handle}</option>
          ))}
        </select>
      </Field>

      <Field label={t("campaigns.from_template")} hint={t("campaigns.template_hint")}>
        <select className="select" value={templateId}
                onChange={(e) => applyTemplate(e.target.value)}>
          <option value="">{t("common.none")}</option>
          {(snapshot?.templates ?? []).map((tpl) => (
            <option key={tpl.id} value={tpl.id}>{tpl.name}</option>
          ))}
        </select>
      </Field>

      <Field label={t("campaigns.text")}>
        <textarea className="textarea" value={text}
                  onChange={(e) => setText(e.target.value)} />
      </Field>

      <Field label={`${t("campaigns.targets")} (${targets.length})`}
             hint={targets.length ? undefined : t("campaigns.no_targets_picked")}>
        <div style={{
          maxHeight: 170, overflow: "auto", border: "1px solid var(--border)",
          borderRadius: 6, padding: 8,
        }}>
          {!snapshot?.targets.length && (
            <span className="faint">{t("targets.empty")}</span>
          )}
          {(snapshot?.targets ?? []).map((target) => (
            <label key={target.id} style={{
              display: "flex", gap: 8, alignItems: "center", padding: "3px 0",
              cursor: "pointer",
            }}>
              <input type="checkbox" checked={targets.includes(target.id)}
                     onChange={() => toggleTarget(target.id)} />
              <span>{target.title || target.username}</span>
              <span className="faint mono" style={{ marginLeft: "auto" }}>
                {target.username ? `@${target.username}` : target.telegram_id}
              </span>
            </label>
          ))}
        </div>
      </Field>

      <Field label={t("campaigns.schedule")}>
        <select className="select" value={mode}
                onChange={(e) => setMode(e.target.value as ScheduleMode)}>
          <option value="ONCE">{t("campaigns.mode.ONCE")}</option>
          <option value="DAILY">{t("campaigns.mode.DAILY")}</option>
          <option value="INTERVAL">{t("campaigns.mode.INTERVAL")}</option>
        </select>
      </Field>

      {mode === "ONCE" && (
        <Field label={t("campaigns.at")}>
          <input className="input" type="datetime-local" value={at.slice(0, 16)}
                 onChange={(e) => setAt(e.target.value ? `${e.target.value}:00` : "")} />
        </Field>
      )}
      {mode === "DAILY" && (
        <Field label={t("campaigns.times")}>
          <input className="input" value={times} placeholder="09:00, 18:30"
                 onChange={(e) => setTimes(e.target.value)} />
        </Field>
      )}
      {mode === "INTERVAL" && (
        <Field label={`${t("campaigns.every")}, ${t("common.minutes")}`}>
          <input className="input" type="number" min={1} value={everyMin}
                 onChange={(e) => setEveryMin(Number(e.target.value))} />
        </Field>
      )}

      <Field label={`${t("campaigns.gap")}, ${t("common.seconds")}`}
             hint={t("campaigns.gap_hint")}>
        <NumberRange min={gapMin} max={gapMax}
                     onChange={(lo, hi) => { setGapMin(lo); setGapMax(hi); }} />
      </Field>
    </Modal>
  );
}
