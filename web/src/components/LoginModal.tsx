/** Login dialog driven by the backend state machine.
 *
 *  Nothing here blocks: we POST a step, then wait for the machine to tell us
 *  over SSE what it needs next.
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../state";
import { ApiHelpLink, Field, FormError, Modal } from "./ui";
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
      setError(err instanceof Error ? err.message : String(err));
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
      setError(err instanceof Error ? err.message : String(err));
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

  return (
    <Modal
      title={t("login.title")}
      onClose={cancel}
      footer={
        <>
          <button className="btn" onClick={cancel}>{t("common.cancel")}</button>
          {!info && (
            <button className="btn accent" onClick={start}
                    disabled={waiting || !canStart}>
              {t("login.start")}
            </button>
          )}
          {(state === "NEED_CODE" || state === "NEED_PASSWORD") && (
            <button className="btn accent" onClick={submit}
                    disabled={waiting || !answer.trim()}>
              {t("login.submit")}
            </button>
          )}
        </>
      }
    >
      <FormError message={error ?? info?.error ?? null} />

      {!info && (
        <>
          <Field label={t("login.kind")}>
            <select className="select" value={kind}
                    onChange={(e) => setKind(e.target.value as Kind)}>
              <option value="phone">{t("login.kind.phone")}</option>
              <option value="qr">{t("login.kind.qr")}</option>
              <option value="bot">{t("login.kind.bot")}</option>
            </select>
          </Field>

          {kind === "phone" && (
            <Field label={t("login.phone")}>
              <input className="input" value={phone} placeholder="+380..."
                     onChange={(e) => setPhone(e.target.value)} />
            </Field>
          )}
          {kind === "bot" && (
            <Field label={t("login.bot_token")}>
              <input className="input" value={botToken}
                     onChange={(e) => setBotToken(e.target.value)} />
            </Field>
          )}

          <Field label={t("login.api_profile")}>
            <select className="select" value={apiProfileId}
                    onChange={(e) => setApiProfileId(e.target.value)}>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>{p.name || p.id}</option>
              ))}
              <option value={NEW_PROFILE}>{t("login.api_new")}</option>
            </select>
          </Field>

          {newProfile && (
            <>
              <div className="row">
                <Field label={t("settings.api_id")}>
                  <input className="input" value={apiId} inputMode="numeric"
                         onChange={(e) => setApiId(e.target.value)} />
                </Field>
                <Field label={t("settings.api_hash")}>
                  <input className="input mono" value={apiHash}
                         onChange={(e) => setApiHash(e.target.value)} />
                </Field>
              </div>
              <Field label={`${t("common.name")} (${t("common.optional")})`}>
                <input className="input" value={apiName}
                       placeholder={t("login.api_name_placeholder")}
                       onChange={(e) => setApiName(e.target.value)} />
              </Field>
              <ApiHelpLink />
              <p className="field-hint">{t("login.api_new_hint")}</p>
            </>
          )}

          <Field label={t("accounts.proxy")} hint={t("login.proxy_hint")}>
            <select className="select" value={proxyId}
                    onChange={(e) => setProxyId(e.target.value)}>
              <option value="">{t("login.proxy_none")}</option>
              {proxies.map((p) => (
                <option key={p.id} value={p.id}>{p.name || p.host}</option>
              ))}
            </select>
          </Field>

          {asOperator && <p className="muted" style={{ fontSize: 12.5 }}>
            {t("login.as_operator")}
          </p>}
        </>
      )}

      {state === "CONNECTING" && (
        <p className="muted icon-line"><span className="spinner" />{t("login.connecting")}</p>
      )}

      {state === "QR_WAIT" && info?.qr_url && (
        <div style={{ textAlign: "center" }}>
          <QrCode modules={info.qr_modules} />
          <p className="muted" style={{ fontSize: 12.5 }}>{t("login.scan")}</p>
          <p className="mono faint" style={{ wordBreak: "break-all" }}>{info.qr_url}</p>
        </div>
      )}

      {(state === "NEED_CODE" || state === "NEED_PASSWORD") && (
        <Field
          label={state === "NEED_CODE" ? t("login.code") : t("login.password")}
          hint={info?.hint ?? undefined}
        >
          <input
            className={`input${info?.hint ? " invalid" : ""}`}
            autoFocus
            type={state === "NEED_PASSWORD" ? "password" : "text"}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
          />
        </Field>
      )}
    </Modal>
  );
}
