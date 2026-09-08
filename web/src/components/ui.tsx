/** The shared building blocks.
 *
 *  Every page is put together from these, so two things that mean the same
 *  look the same: one button, one card, one table, one kind of field, one
 *  status. Sizes and colours live in styles.css tokens, not here.
 */
import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from "react";
import { createPortal } from "react-dom";
import type {
  AnchorHTMLAttributes, ButtonHTMLAttributes, InputHTMLAttributes, ReactNode,
  TextareaHTMLAttributes,
} from "react";
import { api, errorMsg } from "../api";
import { changes, edited, shown, standing } from "../draft";
import type { Edits, Fields } from "../draft";
import { useStore } from "../state";
import type { T } from "../i18n";
import type {
  Effective, Issue, Level, Msg, TargetResultStatus,
} from "../types";

/* ── buttons ───────────────────────────────────────────────────────── */

/** `ghost` and `ghost-danger` are text without a frame: the actions in the
 *  rows of tables and lists. The rest are framed buttons. */
export type ButtonVariant =
  | "primary" | "secondary" | "ghost" | "ghost-danger" | "danger" | "icon";

type ButtonLook = {
  variant?: ButtonVariant;
  /** md: page heads and window footers. sm: card heads and the bulk bar,
   *  and - with `ghost` / `ghost-danger` - the text actions of rows.
   *  Each size has one width and one height, whatever the text says. */
  size?: "md" | "sm";
  /** For `icon`: the colour of a ghost button, the shape of an icon one. */
  quiet?: boolean;
};

function buttonClass({ variant = "secondary", size = "md", quiet }: ButtonLook,
                     extra?: string): string {
  return ["ab", variant, size === "sm" ? "sm" : "", quiet ? "ghost" : "",
          extra ?? ""].filter(Boolean).join(" ");
}

/** The one button of the app. */
export function ActionButton({ variant, size, quiet, className, children, title,
                               ...rest }: ButtonLook & ButtonHTMLAttributes<HTMLButtonElement>) {
  // The label is also the tooltip: a fixed width can cut a long word short.
  const label = typeof children === "string" ? children : undefined;
  return (
    <button type="button" className={buttonClass({ variant, size, quiet }, className)}
            title={title ?? label} {...rest}>
      {typeof children === "string" ? <span>{children}</span> : children}
    </button>
  );
}

/** The same button, as a link. */
export function ActionLink({ variant, size, quiet, className, children, ...rest }:
  ButtonLook & AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a className={buttonClass({ variant, size, quiet }, className)} {...rest}>
      {typeof children === "string" ? <span>{children}</span> : children}
    </a>
  );
}

/* ── status: dot, word, tooltip ────────────────────────────────────── */

type Tone = "ok" | "warn" | "err" | "idle" | "busy";

/** How loud each tone is. The dot shows the louder of what the state says
 *  and what the problems say. */
const LOUDNESS: Record<Tone, number> = { idle: 0, ok: 1, busy: 2, warn: 3, err: 4 };

const RESULT_TONE: Record<TargetResultStatus, Tone> = {
  SENT: "ok", FAILED: "err", SKIPPED: "warn", PENDING: "idle",
};

/** `result` switches to the per-channel delivery results. */
export type StatusKind = "result";

export function toneFor(state: Effective | TargetResultStatus,
                        kind?: StatusKind): Tone {
  if (kind === "result") return RESULT_TONE[state as TargetResultStatus] ?? "idle";
  switch (state) {
    case "READY":
    case "DONE":
    case "SCHEDULED":
      return "ok";
    case "RUNNING":
    case "CHECKING":
      return "busy";
    case "ERROR":
    case "AUTH_DEAD":
    case "FROZEN":
    case "BANNED":
      return "err";
    case "BLOCKED":
    case "RESTRICTED":
    case "PAUSED":
    case "WAITING":
      return "warn";
    default:
      // off, offline, queued, not checked: nothing wrong, nothing going
      return "idle";
  }
}

/** A backend message shown the way an Issue is: in the dot's tooltip. */
export function messageIssues(message: Msg | null | undefined,
                              level: Level = "error"): Issue[] {
  if (!message) return [];
  return [{ level, code: "message", source: null, params: { message } }];
}

export function issueText(t: T, issue: Issue): string {
  return t.msg({ code: `issue.${issue.code}`, params: issue.params });
}

/** A tooltip that is never clipped: rendered into <body>. */
export function Tooltip({ children, content }: { children: ReactNode; content: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState({ x: 0, top: 0, bottom: 0 });
  const anchor = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  // Closing waits a moment so the pointer can cross to the bubble: a long
  // error is worth selecting and copying.
  const closing = useRef<number | undefined>(undefined);

  const show = useCallback(() => {
    window.clearTimeout(closing.current);
    setOpen(true);
  }, []);
  const hide = useCallback(() => {
    window.clearTimeout(closing.current);
    closing.current = window.setTimeout(() => setOpen(false), 160);
  }, []);
  useEffect(() => () => window.clearTimeout(closing.current), []);

  const place = useCallback(() => {
    const el = anchor.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    setAt({ x: box.left + box.width / 2, top: box.top, bottom: box.bottom });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, place]);

  useLayoutEffect(() => {
    const el = bubble.current;
    if (!el) return;
    const margin = 10;
    el.style.left = `${at.x}px`;
    el.style.transform = "translate(-50%, -100%)";
    el.style.top = `${at.top - 8}px`;

    let box = el.getBoundingClientRect();
    if (box.top < margin) {            // no room above: hang it below
      el.style.transform = "translate(-50%, 0)";
      el.style.top = `${at.bottom + 8}px`;
      box = el.getBoundingClientRect();
    }
    let shift = 0;
    if (box.right > window.innerWidth - margin) shift = window.innerWidth - margin - box.right;
    if (box.left + shift < margin) shift = margin - box.left;
    if (shift) {
      const vertical = el.style.transform.includes("-100%") ? "-100%" : "0";
      el.style.transform = `translate(calc(-50% + ${shift}px), ${vertical})`;
    }
  }, [at, open, content]);

  return (
    <span ref={anchor} className="tip" tabIndex={0}
          onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      {open && createPortal(
        <span className="tip-body" ref={bubble} onMouseEnter={show} onMouseLeave={hide}>
          {content}
        </span>,
        document.body)}
    </span>
  );
}

export function IssueDot({ issues, state, kind, tone: forced }: {
  issues: Issue[];
  state?: Effective | TargetResultStatus;
  kind?: StatusKind;
  tone?: Tone;
}) {
  const { t } = useStore();
  const worst: Level | null = issues.some((i) => i.level === "error")
    ? "error" : issues.length ? "warning" : null;
  const fromIssues: Tone = worst === "error" ? "err" : worst === "warning" ? "warn" : "idle";
  const fromState: Tone = forced ?? (state ? toneFor(state, kind) : "ok");
  const tone = LOUDNESS[fromIssues] >= LOUDNESS[fromState] ? fromIssues : fromState;

  if (!issues.length) return <span className={`dot ${tone}`} />;
  return (
    <Tooltip content={
      <>
        {issues.map((issue, i) => (
          <span className="tip-line" key={i}>
            <span className={`dot ${issue.level === "error" ? "err" : "warn"}`} />
            <span>{issueText(t, issue)}</span>
          </span>
        ))}
      </>
    }>
      <span className={`dot ${tone}`} />
    </Tooltip>
  );
}

/** The only way a status is drawn: one dot, one word, one tooltip.
 *  `label` names the state when its own word would be wrong («Доступен»
 *  for a channel); `tone` colours it when the caller knows the answer.
 *  `until` is when a WAITING account may go again. */
export function Status({ state, issues = [], kind, label, tone, until }: {
  state?: Effective | TargetResultStatus;
  issues?: Issue[];
  kind?: StatusKind;
  label?: string;
  tone?: Tone;
  until?: string | null;
}) {
  const { t, lang } = useStore();
  const text = label ?? (state === "WAITING" && until
    ? t("accounts.waiting_until", { time: fmtUntil(until, lang) })
    : state
      ? t.dyn(kind === "result" ? `campaigns.result.${state}` : `state.${state}`)
      : "");
  return (
    <span className="status">
      <IssueDot issues={issues} state={state} kind={kind} tone={tone} />
      {text && <span className="status-text" title={text}>{text}</span>}
    </span>
  );
}

/* ── fields ────────────────────────────────────────────────────────── */

/** A caption, a control and a hint under it. */
export function Field({ label, hint, children }: {
  label?: string; hint?: string; children: ReactNode;
}) {
  return (
    <label className="field">
      {label && <span className="field-label">{label}</span>}
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function TextInput({ value, onChange, width = "full", invalid, mono, className,
                           ...rest }: {
  value: string;
  onChange: (value: string) => void;
  /** full: the width of its field. text: the one fixed width of short text. */
  width?: "full" | "text";
  invalid?: boolean;
  mono?: boolean;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  const cls = ["input", width === "text" ? "text" : "", invalid ? "invalid" : "",
               mono ? "mono" : "", className ?? ""].filter(Boolean).join(" ");
  return <input className={cls} value={value}
                onChange={(e) => onChange(e.target.value)} {...rest} />;
}

export function TextArea({ value, onChange, size, mono, ...rest }: {
  value: string;
  onChange: (value: string) => void;
  size?: "short" | "tall";
  mono?: boolean;
} & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange">) {
  const cls = ["textarea", size ?? "", mono ? "mono" : ""].filter(Boolean).join(" ");
  return <textarea className={cls} value={value}
                   onChange={(e) => onChange(e.target.value)} {...rest} />;
}

export interface Choice { value: string; label: string }

export function Select({ value, onChange, options, width = "full", disabled, title }: {
  value: string;
  onChange: (value: string) => void;
  options: Choice[];
  /** full: the width of its field. pick: the one fixed width of a picker. */
  width?: "full" | "pick";
  disabled?: boolean;
  title?: string;
}) {
  return (
    <select className={`select${width === "pick" ? " pick" : ""}`} value={value}
            disabled={disabled} title={title}
            onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

/** A text typed into and saved when the user is done with it, over a saved
 *  value that can also change elsewhere. It shows the saved value whenever
 *  that moves, and again once its own save is over - so what the server
 *  made of the text («SpamBot» stored as «@SpamBot», a refused value put
 *  back) is what the field then says. */
export function useSavedText(saved: string) {
  const [text, setText] = useState(saved);
  const [round, setRound] = useState(0);
  useEffect(() => { setText(saved); }, [saved, round]);
  /** Save with `save`, then show whatever is saved. */
  const commit = useCallback(async (save: () => unknown) => {
    await save();
    setRound((n) => n + 1);
  }, []);
  return { text, setText, commit };
}

/** A number that reports its value when the user is done with it: on blur
 *  or Enter, and only when it actually changed. Typing «100» into a live
 *  input would be three changes and three saves. */
export function NumberInput({ value, onCommit, min, max }: {
  value: number;
  onCommit: (value: number) => unknown;
  min?: number;
  max?: number;
}) {
  const field = useSavedText(String(value));

  function commit() {
    const parsed = Number(field.text.replace(",", "."));
    if (field.text.trim() === "" || !Number.isFinite(parsed)) {
      field.setText(String(value));
      return;
    }
    let next = Math.round(parsed);
    if (min !== undefined) next = Math.max(min, next);
    if (max !== undefined) next = Math.min(max, next);
    field.setText(String(next));
    if (next !== value) void field.commit(() => onCommit(next));
  }

  return (
    <input className="input num" inputMode="numeric" value={field.text}
           onChange={(e) => field.setText(e.target.value)} onBlur={commit}
           onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
  );
}

/** «от … до … с»: one control wherever a range is asked for. */
export function RangeInput({ min, max, onChange, unit, limit }: {
  min: number;
  max: number;
  onChange: (min: number, max: number) => unknown;
  unit?: string;
  /** The highest value either box accepts. */
  limit?: number;
}) {
  const { t } = useStore();
  return (
    <span className="range">
      <span className="range-word">{t("common.from")}</span>
      <NumberInput value={min} min={0} max={limit} onCommit={(v) => onChange(v, max)} />
      <span className="range-word">{t("common.to")}</span>
      <NumberInput value={max} min={0} max={limit} onCommit={(v) => onChange(min, v)} />
      {unit && <span className="range-word unit">{unit}</span>}
    </span>
  );
}

export function Switch({ checked, onChange, label, disabled, title }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <label className={`switch${disabled ? " disabled" : ""}`} title={title}
           onClick={(e) => e.stopPropagation()}>
      <input type="checkbox" checked={checked} disabled={disabled}
             onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track"><span className="switch-thumb" /></span>
      {label && <span>{label}</span>}
    </label>
  );
}

/** Keep something that has just appeared in a window in sight. A window's
 *  body scrolls, and a result or a refusal could otherwise turn up above or
 *  below the part the user is looking at. Only inside a window: on a page
 *  the user is typing on, nothing pulls the page away from them. */
export function useReveal<E extends HTMLElement>(appeared: unknown) {
  const ref = useRef<E>(null);
  useEffect(() => {
    const el = ref.current;
    if (appeared && el?.closest(".modal")) el.scrollIntoView({ block: "nearest" });
  }, [appeared]);
  return ref;
}

export function FormError({ message }: { message: string | null | undefined }) {
  const ref = useReveal<HTMLDivElement>(message);
  if (!message) return null;
  return <div className="form-error" ref={ref}>{message}</div>;
}

/** Where api_id and api_hash come from, wherever they are asked for. */
export function ApiHelpLink() {
  const { t } = useStore();
  return (
    <p className="field-hint">
      {t("api.where")}{" "}
      <a className="link" href="https://my.telegram.org/apps"
         target="_blank" rel="noreferrer noopener">my.telegram.org/apps</a>
    </p>
  );
}

/* ── settings rows ─────────────────────────────────────────────────── */

export function SettingsGroup({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="settings-group">
      {title && <p className="settings-group-title">{title}</p>}
      {children}
    </div>
  );
}

/** One setting: what it is on the left, its control on the right. `block`
 *  puts a wide control (a list of phrases) under the words instead. The
 *  unit is part of the words, so every control ends on the same line. */
export function SettingRow({ label, unit, hint, children, block }: {
  label: string; unit?: string; hint?: string; children: ReactNode; block?: boolean;
}) {
  return (
    <div className={`setting-row${block ? " block" : ""}`}>
      <div>
        <div className="setting-label">
          {label}
          {unit && <span className="setting-label-unit"> · {unit}</span>}
        </div>
        {hint && <div className="setting-hint">{hint}</div>}
      </div>
      <div className="setting-control">{children}</div>
    </div>
  );
}

/** Tabs down the side of a page, apart from the main menu. */
export function Tabs<K extends string>({ items, value, onChange }: {
  items: { key: K; label: string }[];
  value: K;
  onChange: (key: K) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((item) => (
        <button key={item.key} type="button" role="tab"
                aria-selected={value === item.key}
                className={`tab${value === item.key ? " active" : ""}`}
                onClick={() => onChange(item.key)}>
          {item.label}
        </button>
      ))}
    </div>
  );
}

/* ── windows ───────────────────────────────────────────────────────── */

// The windows open right now, newest last: Esc closes only the top one.
const openWindows: number[] = [];
let windowSeq = 0;

/** A window. It never closes on a click beside it - a stray click would
 *  lose what was typed - only on Cancel, × and Esc. */
export function Modal({ title, onClose, children, footer, wide }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const { t } = useStore();
  const id = useRef(0);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    id.current = ++windowSeq;
    openWindows.push(id.current);
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (openWindows[openWindows.length - 1] !== id.current) return;
      e.stopPropagation();
      close.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      const at = openWindows.indexOf(id.current);
      if (at >= 0) openWindows.splice(at, 1);
    };
  }, []);

  return (
    <div className="overlay">
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        <div className="modal-head">
          <span className="modal-title" title={title}>{title}</span>
          <ActionButton variant="icon" size="sm" quiet onClick={onClose}
                        aria-label={t("common.close")} title={t("common.close")}>
            ✕
          </ActionButton>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/* ── forms ─────────────────────────────────────────────────────────── */

/** The live record a window is about. A window keeps the id, never a copy
 *  of the record taken when it opened: the snapshot moves on, and a copy
 *  would go on showing - and acting on - what the record used to be. A
 *  record deleted meanwhile is simply not found, and its window goes. */
export function byId<R extends { id: string }>(list: R[], id: string | null): R | undefined {
  return id === null ? undefined : list.find((record) => record.id === id);
}

/** The draft of a window over a live record, see draft.ts. `server` is the
 *  record's values as the snapshot has them now, read on every render;
 *  `values` is what the window shows and `changes` what a save sends. */
export function useDraft<V extends Fields>(server: V) {
  const [edits, setEdits] = useState<Edits<V>>({});
  const live = standing(server, edits);
  // An edit the server has overtaken is dropped rather than kept aside: it
  // must not come back if the server returns to the value it was made on.
  const overtaken = Object.keys(live).length !== Object.keys(edits).length;
  useEffect(() => { if (overtaken) setEdits(live); });
  const changed = changes(live);
  return {
    values: shown(server, live),
    changes: changed,
    dirty: Object.keys(changed).length > 0,
    set: <K extends keyof V>(key: K, value: V[K]) =>
      setEdits((cur) => edited(server, standing(server, cur), key, value)),
  };
}

/** How a window saves: one request. The window closes only when it went
 *  through, and a refusal is said inside it, by the fields - a notice that
 *  fades while the window has closed on what was typed helps nobody. The
 *  snapshot is fetched again either way, before the window goes on. */
export function useSubmit() {
  const { refresh, t } = useStore();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (request: () => Promise<unknown>): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      await request();
      return true;
    } catch (err) {
      setError(t.msg(errorMsg(err)));
      return false;
    } finally {
      await refresh();
      setBusy(false);
    }
  }, [refresh, t]);
  /** A refusal the window finds itself, before asking the server. */
  const refuse = useCallback((text: string) => setError(text), []);
  return { busy, error, run, refuse };
}

/** Cancel and a confirming button, the footer of every form. */
export function FormFooter({ onCancel, onConfirm, confirm, busy, disabled, left }: {
  onCancel: () => void;
  onConfirm: () => void;
  confirm?: string;
  busy?: boolean;
  disabled?: boolean;
  /** Something that belongs at the far left: Delete. */
  left?: ReactNode;
}) {
  const { t } = useStore();
  return (
    <>
      {left && <span className="left">{left}</span>}
      <ActionButton onClick={onCancel}>{t("common.cancel")}</ActionButton>
      <ActionButton variant="primary" onClick={onConfirm} disabled={busy || disabled}>
        {confirm ?? t("common.save")}
      </ActionButton>
    </>
  );
}

export function Confirm({ text, onCancel, onConfirm, children }: {
  text: string; onCancel: () => void; onConfirm: () => void;
  children?: ReactNode;
}) {
  const { t } = useStore();
  return (
    <Modal title={t("common.delete")} onClose={onCancel} footer={
      <>
        <ActionButton onClick={onCancel}>{t("common.cancel")}</ActionButton>
        <ActionButton variant="danger" onClick={onConfirm}>{t("common.delete")}</ActionButton>
      </>
    }>
      <p className="field">{text}</p>
      {children}
    </Modal>
  );
}

/* ── picking several records at once ───────────────────────────────── */

/** The header checkbox of a list: empty, ticked, or half-ticked. */
export function SelectAll({ total, picked, onToggle }: {
  total: number; picked: number; onToggle: () => void;
}) {
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (box.current) box.current.indeterminate = picked > 0 && picked < total;
  }, [picked, total]);
  return (
    <input ref={box} type="checkbox" checked={picked === total && total > 0}
           onChange={onToggle} onClick={(e) => e.stopPropagation()} />
  );
}

export interface Selection {
  /** The picked ids, in the order the list shows them. */
  ids: string[];
  has: (id: string) => boolean;
  toggle: (id: string) => void;
  toggleAll: () => void;
  clear: () => void;
  count: number;
  total: number;
}

/** What is picked in one list. Filtered against the live ids on every
 *  render, so a record deleted underneath the selection drops out of it. */
export function useSelection(all: string[]): Selection {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const key = all.join("\u0000");

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const ids = useMemo(() => all.filter((id) => picked.has(id)), [key, picked]);

  const toggle = useCallback((id: string) => {
    setPicked((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setPicked((cur) => {
      const everything = all.every((id) => cur.has(id)) && all.length > 0;
      return everything ? new Set() : new Set(all);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const clear = useCallback(() => setPicked(new Set()), []);

  return {
    ids, count: ids.length, total: all.length,
    has: (id: string) => picked.has(id),
    toggle, toggleAll, clear,
  };
}

/** The strip of actions that appears once something is picked. The buttons
 *  are small ActionButtons; Delete is the danger one of the same shape. */
export function BulkBar({ selection, children }: {
  selection: Selection; children: ReactNode;
}) {
  const { t } = useStore();
  if (!selection.count) return null;
  return (
    <div className="bulk-bar">
      <span className="bulk-count">
        {t("bulk.selected", { n: selection.count, total: selection.total })}
      </span>
      {children}
      <span className="spacer" />
      <ActionButton variant="ghost" size="sm" onClick={selection.clear}>
        {t("bulk.clear")}
      </ActionButton>
    </div>
  );
}

/** One action over everything picked, and what happened said as a notice:
 *  «16 из 18» with the two named. `apply` lets a refused request through,
 *  for a window that says it inside itself; `run` says that as a notice
 *  too. */
export function useBulk(kind: string, selection: Selection) {
  const { act, notify, t } = useStore();
  const { ids, clear } = selection;
  const apply = useCallback(
    async (action: string, values?: Record<string, unknown>) => {
      const report = await api.bulk.apply(kind, ids, action, values);
      if (report.failed.length) {
        notify(t("bulk.partial", {
          done: report.done, total: report.total,
          names: report.failed.map((f) => f.name).join(", "),
        }), "err");
      } else {
        notify(t("bulk.done", { n: report.done }), "ok");
        clear();
      }
      return report;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [kind, ids.join("\u0000"), clear, notify, t]);
  const run = useCallback(
    (action: string, values?: Record<string, unknown>) => act(() => apply(action, values)),
    [act, apply]);
  return { run, apply };
}

/* ── tables ────────────────────────────────────────────────────────── */

export interface Column<Row> {
  key: string;
  label: string;
  /** Every column but one has a width; that one takes what is left. A
   *  string is a CSS length, e.g. a token: `var(--w-actions-2)`. */
  width?: number | string;
  sortable?: boolean;
  className?: string;
  value?: (row: Row) => string | number;
  render: (row: Row) => ReactNode;
}

/** The one table: Campaigns and Channels are both this. */
export function DataTable<Row>({ columns, rows, empty, rowKey, rowId, selection,
                                onRowClick, rowClass }: {
  columns: Column<Row>[];
  rows: Row[];
  empty: string;
  rowKey: (row: Row) => string;
  /** The DOM id of each row, so `useHashFocus` can find it. */
  rowId?: (row: Row) => string;
  /** A tick box column in front; the page owns the selection. */
  selection?: Selection;
  onRowClick?: (row: Row) => void;
  rowClass?: (row: Row) => string | undefined;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);

  const sorted = (() => {
    if (!sort) return rows;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.value) return rows;
    return [...rows].sort((a, b) => {
      const av = col.value!(a);
      const bv = col.value!(b);
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * sort.dir;
      return String(av).localeCompare(String(bv), undefined, { numeric: true }) * sort.dir;
    });
  })();

  if (!rows.length) return <div className="empty">{empty}</div>;

  return (
    <table className="table">
      <colgroup>
        {selection && <col className="pick-col" />}
        {columns.map((col) => (
          <col key={col.key} style={col.width ? { width: col.width } : undefined} />
        ))}
      </colgroup>
      <thead>
        <tr>
          {selection && (
            <th className="static pick-col">
              <SelectAll total={selection.total} picked={selection.count}
                         onToggle={selection.toggleAll} />
            </th>
          )}
          {columns.map((col) => (
            <th key={col.key}
                className={[col.className, col.sortable === false || !col.value ? "static" : ""]
                  .filter(Boolean).join(" ") || undefined}
                onClick={() => {
                  if (col.sortable === false || !col.value) return;
                  setSort((cur) => (cur?.key === col.key
                    ? { key: col.key, dir: cur.dir === 1 ? -1 : 1 }
                    : { key: col.key, dir: 1 }));
                }}>
              {col.label}
              {sort?.key === col.key && (
                <span className="sort">{sort.dir === 1 ? "▲" : "▼"}</span>
              )}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((row) => {
          const key = rowKey(row);
          const cls = [selection?.has(key) ? "picked" : "",
                       onRowClick ? "clickable" : "", rowClass?.(row) ?? ""]
            .filter(Boolean).join(" ");
          return (
            <tr key={key} id={rowId?.(row)} className={cls || undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}>
              {selection && (
                <td className="pick-col" onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={selection.has(key)}
                         onChange={() => selection.toggle(key)} />
                </td>
              )}
              {columns.map((col) => (
                <td key={col.key} className={col.className}
                    onClick={col.className === "actions" ? (e) => e.stopPropagation() : undefined}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/* ── account and operator cards ────────────────────────────────────── */

/** Which kind of thing a card describes: the same layout, a different job. */
export function EntityBadge({ kind }: { kind: "account" | "operator" }) {
  const { t } = useStore();
  const label = t(`entity.${kind}`);
  return (
    <span className={`entity-badge ${kind}`} title={label} aria-label={label}>
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
           stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
           strokeLinejoin="round" aria-hidden="true">
        {kind === "account"
          ? <path d="M4 12 20.5 4 13 20.5l-2-7z" />
          : <>
              <path d="M4 13a8 8 0 0 1 16 0" />
              <rect x="2.5" y="13" width="4" height="6" rx="1.6" />
              <rect x="17.5" y="13" width="4" height="6" rx="1.6" />
              <path d="M20 19v.6a2.4 2.4 0 0 1-2.4 2.4H13" />
            </>}
      </svg>
    </span>
  );
}

/** The card of an account or an operator. Both pages are this, slot for
 *  slot: tick box, chevron, badge, name and line under it, two pills, the
 *  status, Edit - and the body when it is open. */
export function EntityCard({ id, kind, open, onToggle, picked, onPick, name, sub,
                             pills, status, onEdit, children }: {
  id: string;
  kind: "account" | "operator";
  open: boolean;
  onToggle: () => void;
  picked: boolean;
  onPick: () => void;
  name: string;
  sub: string;
  pills: [string, string];
  status: ReactNode;
  onEdit: () => void;
  children: ReactNode;
}) {
  const { t } = useStore();
  return (
    <div className={`entity ${kind}`} id={id}>
      <div className="entity-head" onClick={onToggle}>
        <input type="checkbox" className="entity-pick" checked={picked}
               onClick={(e) => e.stopPropagation()} onChange={onPick} />
        <span className={`chevron${open ? " open" : ""}`}>›</span>
        <EntityBadge kind={kind} />
        <div className="entity-id">
          <div className="entity-name" title={name}>{name}</div>
          <div className="entity-sub" title={sub}>{sub}</div>
        </div>
        <span className="pill slot-1" title={pills[0]}>{pills[0]}</span>
        <span className="pill slot-2" title={pills[1]}>{pills[1]}</span>
        <span className="entity-status">{status}</span>
        <ActionButton size="sm" onClick={(e) => { e.stopPropagation(); onEdit(); }}>
          {t("common.edit")}
        </ActionButton>
      </div>
      {open && <div className="entity-body">{children}</div>}
    </div>
  );
}

/** Label over value, in a card body. */
export function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="meta-label">{label}</div>
      <div className="meta-value" title={value}>{value}</div>
    </div>
  );
}

export function MetaRow({ children }: { children: ReactNode }) {
  return <div className="meta-row">{children}</div>;
}

/** A section title with its one action at the right. */
export function SectionHead({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="section-head">
      <p className="section-title">{title}</p>
      {action}
    </div>
  );
}

/* ── page scaffolding ──────────────────────────────────────────────── */

export function PageHead({ title, sub, actions }: {
  title: string; sub?: string; actions?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

/** The second segment of the hash: "#/accounts/acc_123" -> "acc_123". */
export function hashFocusId(): string {
  return window.location.hash.replace(/^#\/?/, "").split("/")[1] ?? "";
}

/** Scroll to the record the address names, and mark it for a moment.
 *  `prepare` runs first, so a collapsed card opens before the scroll. */
export function useHashFocus(prepare?: (id: string) => void): string {
  const [id, setId] = useState(hashFocusId);
  const prepareRef = useRef(prepare);
  prepareRef.current = prepare;

  useEffect(() => {
    const onHash = () => setId(hashFocusId());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!id) return;
    prepareRef.current?.(id);
    const timer = window.setTimeout(() => {
      const el = document.getElementById(id);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("focus-flash");
      void el.offsetWidth;            // restart the animation on a re-visit
      el.classList.add("focus-flash");
      window.setTimeout(() => el.classList.remove("focus-flash"), 2200);
    }, 60);
    return () => window.clearTimeout(timer);
  }, [id]);

  return id;
}

export function useAutoScroll<E extends HTMLElement>(dep: unknown, enabled: boolean) {
  const ref = useRef<E>(null);
  useEffect(() => {
    if (enabled && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [dep, enabled]);
  return ref;
}

/** A moment to wait until: «14:35» today, the date as well on another day
 *  - a wait can last a week. In the interface's language. */
export function fmtUntil(iso: string | null | undefined, lang: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  if (d.toDateString() !== new Date().toDateString()) return fmtTime(iso, lang);
  return d.toLocaleTimeString(lang === "en" ? "en-GB" : "ru-RU",
                              { hour: "2-digit", minute: "2-digit" });
}

/** A date and time in the interface's language, not the browser's. */
export function fmtTime(iso: string | null | undefined, lang: string,
                        fallback = "—"): string {
  if (!iso) return fallback;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(lang === "en" ? "en-GB" : "ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}
