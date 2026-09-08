/** Shared primitives.
 *
 *  Every problem in this app is shown the same way: a coloured dot with the
 *  explanation in a hover tooltip. No modal alerts, no red rows — only the
 *  status itself is coloured.
 */
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
} from "react";
import { createPortal } from "react-dom";
import type { ReactNode } from "react";
import { useStore } from "../state";
import type { Effective, Issue, Level } from "../types";

/* ── dots and tooltips ─────────────────────────────────────────────── */

type Tone = "ok" | "warn" | "err" | "idle" | "busy";

/** How loud each tone is. The dot shows the loudest of what the state says and
 *  what the problems say, so a switched-off account stays red even though its
 *  only listed problem is a mere warning. */
const LOUDNESS: Record<Tone, number> = { idle: 0, ok: 1, busy: 2, warn: 3, err: 4 };

/** `kind` matters for DISABLED only. Switched off means "not working" for an
 *  account, but it is an ordinary setting for a channel, an API profile or an
 *  auto-reply, and painting those red would cry wolf. */
export function toneFor(state: Effective, kind?: "account"): Tone {
  switch (state) {
    case "READY":
    case "DONE":
      return "ok";
    case "RUNNING":
    case "CHECKING":
      return "busy";
    case "ERROR":
      return "err";
    case "DISABLED":
      return kind === "account" ? "err" : "idle";
    case "BLOCKED":
    case "RESTRICTED":
    case "PAUSED":
      return "warn";
    case "QUEUED":
      return "idle";
    case "SCHEDULED":
      return "ok";
    default:
      return "idle";
  }
}

/** A tooltip that is never clipped.
 *
 *  Rendered into <body> rather than next to its anchor: cards and scroll panes
 *  clip their overflow, and a message the user cannot finish reading is worse
 *  than no message at all. Position is taken from the anchor and nudged back
 *  inside the window, flipping below when there is no room above.
 */
export function Tooltip({ children, content }: { children: ReactNode; content: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState({ x: 0, top: 0, bottom: 0 });
  const anchor = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);

  const place = useCallback(() => {
    const el = anchor.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    setAt({ x: box.left + box.width / 2, top: box.top, bottom: box.bottom });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    // follow the anchor: the page under it can scroll while the pointer rests
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
    el.style.transform = "translate(-50%, -100%)";
    el.style.top = `${at.top - 8}px`;

    let box = el.getBoundingClientRect();
    if (box.top < margin) {            // no room above: hang it below instead
      el.style.transform = "translate(-50%, 0)";
      el.style.top = `${at.bottom + 8}px`;
      box = el.getBoundingClientRect();
    }

    let shift = 0;
    if (box.right > window.innerWidth - margin) {
      shift = window.innerWidth - margin - box.right;
    }
    if (box.left + shift < margin) shift = margin - box.left;
    if (shift) {
      const vertical = el.style.transform.includes("-100%") ? "-100%" : "0";
      el.style.transform = `translate(calc(-50% + ${shift}px), ${vertical})`;
    }
  }, [at, open, content]);

  return (
    <span
      ref={anchor}
      className="tip"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      tabIndex={0}
    >
      {children}
      {open && createPortal(
        <span className="tip-body" ref={bubble} style={{ left: at.x }}>
          {content}
        </span>,
        document.body)}
    </span>
  );
}

export function issueText(t: (k: string, p?: Record<string, unknown>) => string,
                          issue: Issue): string {
  const translated = t(`issue.${issue.code}`, issue.params);
  // an unknown code returns the key itself — fall back to the server wording
  return translated === `issue.${issue.code}` ? issue.message : translated;
}

export function IssueDot({ issues, state, kind }: {
  issues: Issue[]; state?: Effective; kind?: "account";
}) {
  const { t } = useStore();
  const worst: Level | null = issues.some((i) => i.level === "error")
    ? "error"
    : issues.length
      ? "warning"
      : null;

  const fromIssues: Tone = worst === "error" ? "err"
    : worst === "warning" ? "warn" : "idle";
  const fromState: Tone = state ? toneFor(state, kind) : "ok";
  const tone = LOUDNESS[fromIssues] >= LOUDNESS[fromState] ? fromIssues : fromState;

  if (!issues.length) return <span className={`dot ${tone}`} />;

  return (
    <Tooltip
      content={
        <>
          {issues.map((issue, i) => (
            <span className="tip-line" key={i}>
              <span className={`dot ${issue.level === "error" ? "err" : "warn"}`} />
              <span>{issueText(t, issue)}</span>
            </span>
          ))}
        </>
      }
    >
      <span className={`dot ${tone}`} />
    </Tooltip>
  );
}

export function Status({ state, issues, kind }: {
  state: Effective; issues: Issue[]; kind?: "account";
}) {
  const { t } = useStore();
  return (
    <span className="status">
      <IssueDot issues={issues} state={state} kind={kind} />
      <span className="status-text">{t(`state.${state}`)}</span>
    </span>
  );
}

/* ── form controls ─────────────────────────────────────────────────── */

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

export function Switch({ checked, onChange, label, disabled }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <label className="switch">
      <input type="checkbox" checked={checked} disabled={disabled}
             onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-track"><span className="switch-thumb" /></span>
      {label && <span>{label}</span>}
    </label>
  );
}

/** A number input that reports its value when the user is done with it.
 *
 *  Typing "100" through a live-bound input would fire three separate changes
 *  (1, 10, 100) and, where the caller saves on change, three saves. Here the
 *  text is held locally and committed on blur or Enter — and only when it
 *  actually differs, so tabbing through a field is not an edit.
 */
export function NumberField({ value, onCommit, min, max, className = "input" }: {
  value: number;
  onCommit: (value: number) => void;
  min?: number;
  max?: number;
  className?: string;
}) {
  const [text, setText] = useState(String(value));
  const shown = useRef(value);

  useEffect(() => {
    // follow changes made elsewhere, but never overwrite what is being typed
    if (value !== shown.current) {
      shown.current = value;
      setText(String(value));
    }
  }, [value]);

  function commit() {
    const parsed = Number(text.replace(",", "."));
    if (text.trim() === "" || !Number.isFinite(parsed)) {
      setText(String(value));      // nonsense: put the old value back
      return;
    }
    let next = Math.round(parsed);
    if (min !== undefined) next = Math.max(min, next);
    if (max !== undefined) next = Math.min(max, next);
    setText(String(next));
    if (next !== value) {
      shown.current = next;
      onCommit(next);
    }
  }

  return (
    <input
      className={className}
      inputMode="numeric"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}

export function NumberRange({ min, max, onChange, unit }: {
  min: number; max: number;
  onChange: (min: number, max: number) => void;
  unit?: string;
}) {
  const { t } = useStore();
  return (
    <div className="row">
      <div className="shrink muted" style={{ paddingBottom: 8 }}>{t("common.from")}</div>
      <NumberField value={min} min={0} onCommit={(v) => onChange(v, max)} />
      <div className="shrink muted" style={{ paddingBottom: 8 }}>{t("common.to")}</div>
      <NumberField value={max} min={0} onCommit={(v) => onChange(min, v)} />
      {unit && <div className="shrink muted" style={{ paddingBottom: 8 }}>{unit}</div>}
    </div>
  );
}

/* ── modal ─────────────────────────────────────────────────────────── */

export function Modal({ title, onClose, children, footer, wide }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onMouseDown={(e) => {
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        <div className="modal-head">
          <span className="modal-title">{title}</span>
          <button className="icon-btn" style={{ marginLeft: "auto" }}
                  onClick={onClose} aria-label="close">✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** Where api_id and api_hash come from. Shown wherever they are asked for,
 *  because nothing in the app can tell the user that on its own. */
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

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="form-error">{message}</div>;
}

export function Confirm({ text, onCancel, onConfirm }: {
  text: string; onCancel: () => void; onConfirm: () => void;
}) {
  const { t } = useStore();
  return (
    <Modal title={t("common.delete")} onClose={onCancel} footer={
      <>
        <button className="btn" onClick={onCancel}>{t("common.cancel")}</button>
        <button className="btn accent" onClick={onConfirm}>{t("common.delete")}</button>
      </>
    }>
      <p style={{ margin: 0 }}>{text}</p>
    </Modal>
  );
}

/* ── sortable table ────────────────────────────────────────────────── */

export interface Column<Row> {
  key: string;
  label: string;
  width?: number | string;
  sortable?: boolean;
  className?: string;
  value?: (row: Row) => string | number;
  render: (row: Row) => ReactNode;
}

export function DataTable<Row>({ columns, rows, empty, rowKey }: {
  columns: Column<Row>[];
  rows: Row[];
  empty: string;
  rowKey: (row: Row) => string;
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

  // A wide table scrolls inside its own box. Without this the page itself
  // scrolled sideways and the columns on the right were simply gone.
  return (
    <div className="table-wrap">
    <table className="table">
      <thead>
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              // the header must carry the column's own alignment too, or a
              // right-aligned numeric column ends up with a left-aligned title
              className={[col.className, col.sortable === false ? "static" : ""]
                .filter(Boolean).join(" ") || undefined}
              style={col.width ? { width: col.width } : undefined}
              onClick={() => {
                if (col.sortable === false || !col.value) return;
                setSort((cur) =>
                  cur?.key === col.key
                    ? { key: col.key, dir: cur.dir === 1 ? -1 : 1 }
                    : { key: col.key, dir: 1 });
              }}
            >
              {col.label}
              {sort?.key === col.key && (
                <span className="sort">{sort.dir === 1 ? "▲" : "▼"}</span>
              )}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((row) => (
          <tr key={rowKey(row)}>
            {columns.map((col) => (
              <td key={col.key} className={col.className}>{col.render(row)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}

/** Which kind of thing a card describes.
 *
 *  Accounts and operators share one layout on purpose - it is a single design
 *  system - but they do different jobs, and the only thing telling them apart
 *  used to be the wording inside two pills. */
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

export function useAutoScroll<E extends HTMLElement>(dep: unknown, enabled: boolean) {
  const ref = useRef<E>(null);
  useEffect(() => {
    if (enabled && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [dep, enabled]);
  return ref;
}

export function fmtTime(iso: string | null | undefined, fallback = "—"): string {
  if (!iso) return fallback;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
