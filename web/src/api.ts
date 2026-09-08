/** API client.
 *
 *  The runtime token arrives once, in the URL the tray menu opens. We
 *  move it into sessionStorage and strip it from the address bar immediately,
 *  then send it as a header on every call.
 *
 *  The event stream uses fetch + ReadableStream rather than EventSource
 *  precisely because EventSource cannot set headers — this way the token never
 *  has to travel in a query string.
 */
import type {
  BulkReport, BusEvent, Campaign, Checkup, Dialog, LoginInfo, LogLine, Message,
  Msg, Snapshot,
} from "./types";

const TOKEN_KEY = "tc_token";
const HEADER = "X-TC-Token";

function captureToken(): string {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("t");
  if (fromUrl) {
    try {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
    } catch {
      /* private mode: fall back to the in-memory copy below */
    }
    url.searchParams.delete("t");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    return fromUrl;
  }
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

let token = captureToken();

export function hasToken(): boolean {
  return token.length > 0;
}

/** A refusal from the backend: a message the dictionaries word. */
export class ApiError extends Error {
  status: number;
  msg: Msg;
  constructor(msg: Msg, status: number) {
    super(msg.code);
    this.msg = msg;
    this.status = status;
  }
}

function asMsg(error: unknown, status: number): Msg {
  if (error && typeof error === "object" && "code" in error) return error as Msg;
  return { code: "raw", params: { text: `HTTP ${status}` } };
}

/** What to tell the user about a failed call, whatever threw. */
export function errorMsg(err: unknown): Msg {
  if (err instanceof ApiError) return err.msg;
  return { code: "raw", params: { text: err instanceof Error ? err.message : String(err) } };
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      [HEADER]: token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });

  const text = await res.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) throw new ApiError(asMsg(data?.error, res.status), res.status);
  return data as T;
}

/** Send one file as a raw body.
 *
 *  Not JSON: base64 would inflate the file by a third and hold it in memory
 *  twice. Metadata rides in headers rather than the query string — a caption
 *  is something a person wrote, and user content has no business in a URL.
 */
async function upload<T>(path: string, file: File,
                         headers: Record<string, string>): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { [HEADER]: token, "Content-Type": "application/octet-stream",
               ...headers },
    body: file,
    cache: "no-store",
  });
  const text = await res.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) throw new ApiError(asMsg(data?.error, res.status), res.status);
  return data as T;
}

export const api = {
  state: () => request<Snapshot>("/api/state"),
  logs: (limit = 300) => request<LogLine[]>(`/api/logs?limit=${limit}`),

  accounts: {
    /** Whatever the account's window changed - API, proxy, operator, the
     *  switch, its own auto-reply - in one request. Only the keys sent. */
    update: (body: Record<string, unknown>) => request("/api/accounts/update", body),
    /** Deletes the account, its session file and its own auto-reply.
     *  Nothing is left behind - a session file that stayed was found again
     *  by start-up discovery, and the account reappeared. */
    remove: (id: string, keepOperator = false) =>
      request("/api/accounts/delete", { id, keep_operator: keepOperator }),
    check: (id: string) => request<{ ok: boolean; checkup: Checkup }>(
      "/api/accounts/check", { id }),
    probeAll: () => request<{ ok: boolean; checkup: Checkup }>(
      "/api/accounts/probe_all", {}),
    spamCheck: () => request<{ ok: boolean; checkup: Checkup }>(
      "/api/accounts/spam_check", {}),
    promote: (id: string) => request("/api/accounts/promote", { id }),
    rescan: () => request<{ accounts: number; operators: number }>(
      "/api/accounts/rescan", {}),
  },

  campaigns: {
    create: (body: Record<string, unknown>) => request("/api/campaigns/create", body),
    update: (body: Record<string, unknown>) => request("/api/campaigns/update", body),
    remove: (id: string) => request("/api/campaigns/delete", { id }),
    start: (id: string) => request("/api/campaigns/start", { id }),
    pause: (id: string) => request("/api/campaigns/pause", { id }),
    /** A switched-off copy of an existing campaign. */
    /** `copyWord` goes into the copy's name, in the interface's language. */
    duplicate: (id: string, copyWord: string) =>
      request<Campaign>("/api/campaigns/duplicate", { id, copy_word: copyWord }),
    reset: (id: string) => request("/api/campaigns/reset", { id }),
    /** Store a picture for a message and return where it was put. The app
     *  keeps its own copy, so moving the original cannot break a campaign. */
    upload: (file: File) =>
      upload<{ path: string; name: string }>("/api/campaigns/upload", file, {
        "X-TC-Name": encodeURIComponent(file.name),
      }),
    /** Switch one channel off, or back on, inside this campaign. */
    toggleTarget: (id: string, targetId: string, excluded: boolean) =>
      request("/api/campaigns/target_toggle",
              { id, target_id: targetId, excluded }),
  },

  operators: {
    create: (body: Record<string, unknown>) => request("/api/operators/create", body),
    update: (body: Record<string, unknown>) => request("/api/operators/update", body),
    /** Deletes the operator and its session file. */
    remove: (id: string) => request("/api/operators/delete", { id }),
    dialogs: (id: string) =>
      request<Dialog[]>(`/api/operators/dialogs?id=${encodeURIComponent(id)}`),
    /** One page of history, oldest first. `offset` asks for what came
     *  before that message id — how scrolling up loads older history. */
    messages: (id: string, peer: number, offset = 0) =>
      request<Message[]>(
        `/api/operators/messages?id=${encodeURIComponent(id)}&peer=${peer}`
        + (offset ? `&offset=${offset}` : "")),
    /** `replyTo` is the id of the message being answered — the same argument
     *  on every kind of send, so a reply means one thing throughout. */
    send: (id: string, peerId: number, text: string, replyTo?: number) =>
      request<Message>("/api/operators/send",
        { id, peer_id: peerId, text, ...(replyTo ? { reply_to: replyTo } : {}) }),
    sendFile: (id: string, peerId: number, file: File, caption = "",
               replyTo?: number) =>
      upload<Message>("/api/operators/send_file", file, {
        "X-TC-Operator": id,
        "X-TC-Peer": String(peerId),
        "X-TC-Name": encodeURIComponent(file.name),
        "X-TC-Caption": encodeURIComponent(caption),
        ...(replyTo ? { "X-TC-Reply-To": String(replyTo) } : {}),
      }),
    probeAll: () => request<{ ok: boolean; checkup: Checkup }>(
      "/api/operators/probe_all", {}),
    /** Session files are shared, so this is the same scan the accounts page
     *  runs - it discovers both kinds. */
    rescan: () => request<{ accounts: number; operators: number }>(
      "/api/accounts/rescan", {}),
    /** Listen on this operator's session while the chat is open. */
    watch: (id: string) => request<{ ok: boolean }>("/api/operators/watch", { id }),
    /** Presence, driven by what the person is doing rather than by the send:
     *  opening a chat puts the account online, typing shows the indicator,
     *  closing it goes quiet. */
    enter: (id: string, peerId: number) =>
      request("/api/operators/enter", { id, peer_id: peerId }),
    typing: (id: string, peerId: number) =>
      request("/api/operators/typing", { id, peer_id: peerId }),
    leave: (id: string) => request("/api/operators/leave", { id }),
    unwatch: (id: string) => request("/api/operators/unwatch", { id }),
  },

  autoreply: {
    save: (body: Record<string, unknown>) => request("/api/autoreply/save", body),
    reset: (ownerId: string) => request("/api/autoreply/reset", { owner_id: ownerId }),
  },

  profiles: {
    saveApi: (body: Record<string, unknown>) => request("/api/profiles/api/save", body),
    removeApi: (id: string) => request("/api/profiles/api/delete", { id }),
    saveProxy: (body: Record<string, unknown>) =>
      request("/api/profiles/proxy/save", body),
    removeProxy: (id: string) => request("/api/profiles/proxy/delete", { id }),
    probeProxy: (id: string) => request("/api/profiles/proxy/probe", { id }),
  },

  targets: {
    add: (text: string, title = "") => request("/api/targets/add", { text, title }),
    update: (body: Record<string, unknown>) => request("/api/targets/update", body),
    remove: (id: string) => request("/api/targets/delete", { id }),
    /** Starts the shared check run and returns at once, exactly as the
     *  accounts page does - checking every channel takes minutes. */
    check: (id?: string) => request<{ ok: boolean; checkup: Checkup }>(
      "/api/targets/check", id ? { id } : {}),
  },

  login: {
    /** `target_id` fills an existing operator in rather than adding one. */
    start: (body: Record<string, unknown>) =>
      request<LoginInfo>("/api/login/start", body),
    code: (loginId: string, code: string) =>
      request<LoginInfo>("/api/login/code", { login_id: loginId, code }),
    password: (loginId: string, password: string) =>
      request<LoginInfo>("/api/login/password", { login_id: loginId, password }),
    cancel: (loginId: string) => request("/api/login/cancel", { login_id: loginId }),
  },

  settings: {
    save: (values: Record<string, unknown>) => request("/api/settings", { values }),
  },

  /** One action over many records. Always one request: twenty separate calls
   *  would be twenty refetches and no way to report what refused. */
  bulk: {
    apply: (kind: string, ids: string[], action: string,
            values?: Record<string, unknown>) =>
      request<BulkReport>("/api/bulk/apply", { kind, ids, action, values }),
    /** Checking goes to the shared check run, which already takes a list. */
    check: (kind: string, ids: string[]) =>
      request<{ ok: boolean; checkup: Checkup }>("/api/bulk/check", { kind, ids }),
  },
};

/** Subscribe to the server event stream. Returns a stop function. */
export function subscribe(
  onEvent: (event: BusEvent) => void,
  onStatus: (connected: boolean) => void,
): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let attempt = 0;

  async function connect(): Promise<void> {
    if (stopped) return;
    controller = new AbortController();
    try {
      const res = await fetch("/api/events", {
        headers: { [HEADER]: token },
        signal: controller.signal,
        cache: "no-store",
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

      onStatus(true);
      attempt = 0;

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let split: number;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data: ")) continue;   // ": ping" heartbeats
            try {
              onEvent(JSON.parse(line.slice(6)) as BusEvent);
            } catch {
              /* a malformed frame must not kill the stream */
            }
          }
        }
      }
    } catch {
      /* fall through to the retry below */
    }

    if (stopped) return;
    onStatus(false);
    attempt += 1;
    const backoff = Math.min(1000 * 2 ** Math.min(attempt, 4), 10000);
    setTimeout(connect, backoff);
  }

  void connect();

  return () => {
    stopped = true;
    controller?.abort();
  };
}
