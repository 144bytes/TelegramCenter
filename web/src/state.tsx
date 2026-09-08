/** Application state.
 *
 *  There is exactly one source of truth: the snapshot the backend computes.
 *  Server events do not patch the store — they mark it stale and we refetch,
 *  so the UI can never drift into a status the backend disagrees with.
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";
import type { ReactNode } from "react";
import { ApiError, api, hasToken, subscribe } from "./api";
import { makeT } from "./i18n";
import type { Lang, T } from "./i18n";
import type { BusEvent, LogLine, Snapshot } from "./types";

const MAX_LOGS = 1500;

export interface Toast {
  id: number;
  kind: "ok" | "err";
  text: string;
}

interface Store {
  snapshot: Snapshot | null;
  logs: LogLine[];
  connected: boolean;
  ready: boolean;
  fatal: string | null;
  lang: Lang;
  t: T;
  toasts: Toast[];
  refresh: () => Promise<void>;
  notify: (text: string, kind?: "ok" | "err") => void;
  /** Run an API call, surface any error as a toast, refresh on success. */
  act: <R>(fn: () => Promise<R>, okText?: string) => Promise<R | null>;
  clearLogs: () => void;
  loginEvents: BusEvent[];
  /** Listen to raw server events. Returns an unsubscribe function.
   *
   *  The snapshot stays the source of truth for status; this is for the few
   *  places that need the event itself — the operator chat appends the
   *  message it is given rather than reloading a history it is reading. */
  onEvent: (handler: (event: BusEvent) => void) => () => void;
}

const Ctx = createContext<Store | null>(null);

export function useStore(): Store {
  const store = useContext(Ctx);
  if (!store) throw new Error("useStore outside provider");
  return store;
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [connected, setConnected] = useState(false);
  const [ready, setReady] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [loginEvents, setLoginEvents] = useState<BusEvent[]>([]);

  const pending = useRef(false);
  const queued = useRef(false);
  const toastId = useRef(0);
  const listeners = useRef(new Set<(event: BusEvent) => void>());

  const onEvent = useCallback((handler: (event: BusEvent) => void) => {
    listeners.current.add(handler);
    return () => { listeners.current.delete(handler); };
  }, []);

  const refresh = useCallback(async () => {
    if (pending.current) {
      queued.current = true;
      return;
    }
    pending.current = true;
    try {
      const next = await api.state();
      setSnapshot(next);
      setFatal(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setFatal("forbidden");
      }
    } finally {
      pending.current = false;
      setReady(true);
      if (queued.current) {
        queued.current = false;
        void refresh();
      }
    }
  }, []);

  const notify = useCallback((text: string, kind: "ok" | "err" = "ok") => {
    const id = ++toastId.current;
    setToasts((list) => [...list, { id, kind, text }]);
    setTimeout(() => setToasts((list) => list.filter((x) => x.id !== id)),
      kind === "err" ? 6000 : 2600);
  }, []);

  const act = useCallback(async <R,>(fn: () => Promise<R>, okText?: string) => {
    try {
      const result = await fn();
      if (okText) notify(okText, "ok");
      await refresh();
      return result;
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "err");
      await refresh();
      return null;
    }
  }, [notify, refresh]);

  const clearLogs = useCallback(() => setLogs([]), []);

  // initial load
  useEffect(() => {
    if (!hasToken()) {
      setFatal("no-token");
      setReady(true);
      return;
    }
    void refresh();
    api.logs().then(setLogs).catch(() => undefined);
  }, [refresh]);

  // live updates
  useEffect(() => {
    if (!hasToken()) return;
    let timer: number | undefined;

    const stop = subscribe(
      (event) => {
        for (const listener of listeners.current) {
          try {
            listener(event);
          } catch {
            /* one bad listener must not break the stream */
          }
        }
        if (event.type === "log") {
          const line = event.payload as unknown as LogLine;
          setLogs((prev) => {
            const next = prev.length >= MAX_LOGS ? prev.slice(-MAX_LOGS + 1) : prev;
            return [...next, line];
          });
          return;
        }
        if (event.type === "login") {
          setLoginEvents((prev) => [...prev.slice(-20), event]);
          return;
        }
        // A chat message changes nothing the snapshot reports, and refetching
        // the whole state on every incoming message would be pure waste.
        if (event.type === "operator.message_in") return;
        // anything else means the computed state may have moved: coalesce
        // bursts (a campaign sending to 50 channels) into one refetch
        window.clearTimeout(timer);
        timer = window.setTimeout(() => void refresh(), 180);
      },
      setConnected,
    );

    return () => {
      window.clearTimeout(timer);
      stop();
    };
  }, [refresh]);

  const lang: Lang = snapshot?.settings?.general?.language === "en" ? "en" : "ru";
  const t = useMemo(() => makeT(lang), [lang]);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const value: Store = {
    snapshot, logs, connected, ready, fatal, lang, t, toasts,
    refresh, notify, act, clearLogs, loginEvents, onEvent,
  };

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
