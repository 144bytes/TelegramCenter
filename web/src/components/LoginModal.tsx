/** Login dialog driven by the backend state machine.
 *
 *  Nothing here blocks: we POST a step, then wait for the machine to tell us
 *  over SSE what it needs next.
 */
import { useEffect, useState } from "react";
import { api, errorMsg } from "../api";
import { useStore } from "../state";
import {
  ActionButton, ApiHelpLink, Field, FormError, Modal, Select, TextInput,
} from "./ui";
import type { LoginInfo } from "../types";

type Kind = "phone" | "qr" | "bot";

/** Sentinel for "I will type the keys in right here" rather than picking a
 *  saved profile. Adding the first account should not need a detour through
 *  Settings to create a profile first. */
const NEW_PROFILE = "__new__";

/** The login link is a credential, so the QR is rendered from the matrix the
 *  backend computed — it never leaves this machine. */
function QrCode({ modules }: { modules: boolean[][] | null | undefined }) {
  if (!modules?.length) return <p className="muted"><span className="spinner" /></p>;
  const size = modules.length;
  return (
    <div className="qr">
      <svg viewBox={`0 0 ${size} ${size}`} width={200} height={200}
           shapeRendering="crispEdges" role="img" aria-label="QR">
        <rect width={size} height={size} fill="#fff" />
        {modules.flatMap((row, y) =>
          row.map((on, x) => (on
            ? <rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} fill="#000" />
            : null)))}
      </svg>
    </div>
  );
}

export function LoginModal({ asOperator, onClose, targetId }: {
  asOperator: boolean;
  onClose: () => void;
  /** Sign in *into* this operator instead of adding one: the record saved as
   *  a bare @username becomes the account being signed in. */
  targetId?: string;
}) {
  const { t, snapshot, loginEvents, refresh, notify } = useStore();
  const [kind, setKind] = useState<Kind>("phone");
  const [phone, setPhone] = useState("");
  const [botToken, setBotToken] = useState("");
  const profiles = snapshot?.api_profiles ?? [];
  const proxies = snapshot?.network_profiles ?? [];
  // always start on "new": a fresh account normally means fresh keys, and
  // silently reusing the last profile is the kind of default you only notice
  // after it has already been applied
  const [apiProfileId, setApiProfileId] = useState(NEW_PROFILE);
  const [apiId, setApiId] = useState("");
  const [proxyId, setProxyId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [apiName, setApiName] = useState("");
  // with no profiles saved there is nothing to pick, so always ask for keys
  const newProfile = apiProfileId === NEW_PROFILE || profiles.length === 0;
  const [info, setInfo] = useState<LoginInfo | null>(null);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // the machine pushes its state; we only mirror it
  useEffect(() => {
    if (!info) return;
    for (let i = loginEvents.length - 1; i >= 0; i--) {
      const payload = loginEvents[i].payload as unknown as LoginInfo;
      if (payload.login_id === info.login_id) {
        setInfo(payload);
        if (payload.state === "NEED_CODE" || payload.state === "NEED_PASSWORD") {
          setAnswer("");
        }
        break;
      }
    }
  }, [loginEvents]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (info?.state === "DONE") {
      notify(t("login.done"), "ok");
      void refresh();
      onClose();
    }
  }, [info?.state]);   // eslint-disable-line react-hooks/exhaustive-deps

  async function start() {
    setError(null);
    setBusy(true);
    try {
      const started = await api.login.start({
        kind, as_operator: asOperator, phone, bot_token: botToken,
        ...(newProfile
          ? { api_id: apiId.trim(), api_hash: apiHash.trim(), api_name: apiName }
          : { api_profile_id: apiProfileId }),
        // The first authorisation goes through this proxy too, not just the
        // traffic afterwards - otherwise the account is created behind a proxy
        // but was signed in from this machine's own address.
        ...(proxyId ? { network_profile_id: proxyId } : {}),
        ...(targetId ? { target_id: targetId } : {}),
      });
      setInfo(started);
    } catch (err) {
      setError(t.msg(errorMsg(err)));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!info) return;
    setBusy(true);
    setError(null);
    try {
      if (info.state === "NEED_CODE") await api.login.code(info.login_id, answer);
      else await api.login.password(info.login_id, answer);
    } catch (err) {
      setError(t.msg(errorMsg(err)));
    } finally {
      setBusy(false);
    }
  }

  function cancel() {
    if (info && info.state !== "DONE") void api.login.cancel(info.login_id);
    onClose();
  }

  const state = info?.state;
  const waiting = state === "CONNECTING" || busy;
  const canStart =
    (!newProfile || (apiId.trim() !== "" && apiHash.trim() !== ""))
    && (kind !== "phone" || phone.trim() !== "")
    && (kind !== "bot" || botToken.trim() !== "");
  // A failed sign-in goes back to the form with everything still filled in.
  const failed = state === "ERROR" || state === "CANCELLED";

  const apiChoices = [
    ...profiles.map((p) => ({ value: p.id, label: p.name || p.id })),
    { value: NEW_PROFILE, label: t("login.api_new") },
  ];
  const proxyChoices = [
    { value: "", label: t("login.proxy_none") },
    ...proxies.map((p) => ({ value: p.id, label: p.name || p.host })),
  ];

  return (
    <Modal
      title={t("login.title")}
      onClose={cancel}
      footer={
        <>
          <ActionButton onClick={cancel}>{t("common.cancel")}</ActionButton>
          {!info && (
            <ActionButton variant="primary" onClick={start} disabled={waiting || !canStart}>
              {t("login.start")}
            </ActionButton>
          )}
          {failed && (
            <ActionButton variant="primary"
                          onClick={() => { setInfo(null); setError(null); }}>
              {t("login.retry")}
            </ActionButton>
          )}
          {(state === "NEED_CODE" || state === "NEED_PASSWORD") && (
            <ActionButton variant="primary" onClick={submit}
                          disabled={waiting || !answer.trim()}>
              {t("login.submit")}
            </ActionButton>
          )}
        </>
      }
    >
      <FormError message={error ?? (info?.error ? t.msg(info.error) : null)} />

      {!info && (
        <>
          <Field label={t("login.kind")}>
            <Select value={kind} onChange={(v) => setKind(v as Kind)} options={[
              { value: "phone", label: t("login.kind.phone") },
              { value: "qr", label: t("login.kind.qr") },
              { value: "bot", label: t("login.kind.bot") },
            ]} />
          </Field>

          {kind === "phone" && (
            <Field label={t("login.phone")}>
              <TextInput value={phone} placeholder="+380..." onChange={setPhone} />
            </Field>
          )}
          {kind === "bot" && (
            <Field label={t("login.bot_token")}>
              <TextInput value={botToken} onChange={setBotToken} />
            </Field>
          )}

          <Field label={t("login.api_profile")}>
            <Select value={apiProfileId} options={apiChoices} onChange={setApiProfileId} />
          </Field>

          {newProfile && (
            <>
              <div className="field-row">
                <Field label={t("settings.api_id")}>
                  <TextInput value={apiId} inputMode="numeric" onChange={setApiId} />
                </Field>
                <Field label={t("settings.api_hash")}>
                  <TextInput value={apiHash} mono onChange={setApiHash} />
                </Field>
              </div>
              <Field label={`${t("common.name")} (${t("common.optional")})`}>
                <TextInput value={apiName} placeholder={t("login.api_name_placeholder")}
                           onChange={setApiName} />
              </Field>
              <ApiHelpLink />
              <p className="field-hint field">{t("login.api_new_hint")}</p>
            </>
          )}

          <Field label={t("accounts.proxy")} hint={t("login.proxy_hint")}>
            <Select value={proxyId} options={proxyChoices} onChange={setProxyId} />
          </Field>

          {asOperator && <p className="field-hint">{t("login.as_operator")}</p>}
        </>
      )}

      {state === "CONNECTING" && (
        <p className="muted icon-line"><span className="spinner" />{t("login.connecting")}</p>
      )}

      {/* Which route this sign-in is taking, while it is taking it. Telegram
          reports the city a login came from minutes later, and by then there
          is nothing on our side left to compare it with. */}
      {info?.proxy_name && state !== "DONE" && (
        <p className="field-hint field">{t("login.via", { proxy: info.proxy_name })}</p>
      )}

      {state === "QR_WAIT" && info?.qr_url && (
        <div className="qr-box">
          <QrCode modules={info.qr_modules} />
          <p className="field-hint">{t("login.scan")}</p>
          <p className="mono faint qr-link">{info.qr_url}</p>
        </div>
      )}

      {(state === "NEED_CODE" || state === "NEED_PASSWORD") && (
        <Field
          label={state === "NEED_CODE" ? t("login.code") : t("login.password")}
          hint={info?.hint ? t.msg(info.hint) : undefined}
        >
          <TextInput
            invalid={!!info?.hint}
            autoFocus
            type={state === "NEED_PASSWORD" ? "password" : "text"}
            value={answer}
            onChange={setAnswer}
            onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
          />
        </Field>
      )}
    </Modal>
  );
}
