/** The operator chat.
 *
 *  Scrolling follows Telegram's rule, which is about respecting the reader:
 *  while you are at the bottom the view follows new messages, and the moment
 *  you scroll up it stops dead — nothing, not even your own send, drags you
 *  back down. Coming back to the bottom, by the button or by hand, turns
 *  following back on.
 *
 *  New messages arrive on the existing event stream: the backend publishes
 *  `operator.message_in` carrying the whole message, so one bubble is appended
 *  instead of the history being reloaded. Reloading is what used to lose the
 *  reader's place, and it is why nothing here polls.
 */
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
} from "react";
import { api } from "../api";
import { useStore } from "../state";
import type { Dialog, MediaInfo, Message, Operator } from "../types";

/** How close to the bottom still counts as "at the bottom". A couple of
 *  pixels of rounding must not switch following off. */
const BOTTOM_SLACK = 48;
/** How close to the top starts loading older history. */
const TOP_SLACK = 64;

// ── icons ───────────────────────────────────────────────────────────────
// Plain inline SVG on purpose: no icon library is worth a dependency for six
// glyphs. All of them share one box and inherit currentColor.
function Svg({ children, size = 18 }: { children: React.ReactNode; size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none"
         stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"
         strokeLinejoin="round" aria-hidden="true">{children}</svg>
  );
}

const IconClip = () => (
  <Svg><path d="M21.4 11.05 12.25 20.2a5.5 5.5 0 0 1-7.78-7.78l9.2-9.2a3.67 3.67 0 1 1 5.18 5.18l-9.2 9.2a1.83 1.83 0 0 1-2.6-2.6l8.5-8.48" /></Svg>
);
const IconPhoto = () => (
  <Svg><rect x="3" y="4" width="18" height="16" rx="2.5" /><circle cx="8.5" cy="9.5" r="1.6" /><path d="m3.5 17 5-5 4.5 4.5 3-3 4.5 4.5" /></Svg>
);
const IconDoc = () => (
  <Svg><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></Svg>
);
const IconPin = () => (
  <Svg><path d="M20 10c0 5.5-8 12-8 12s-8-6.5-8-12a8 8 0 0 1 16 0Z" /><circle cx="12" cy="10" r="2.8" /></Svg>
);
const IconMic = () => (
  <Svg><rect x="9" y="2.5" width="6" height="11.5" rx="3" /><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3.5" /></Svg>
);
const IconCam = () => (
  <Svg><rect x="2.5" y="6" width="13" height="12" rx="2.5" /><path d="m15.5 10.5 6-3.5v10l-6-3.5z" /></Svg>
);
const IconSmile = () => (
  <Svg><circle cx="12" cy="12" r="9" /><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0" /><circle cx="9" cy="9.8" r="1" fill="currentColor" stroke="none" /><circle cx="15" cy="9.8" r="1" fill="currentColor" stroke="none" /></Svg>
);
const IconDown = () => (
  <Svg size={20}><path d="M12 5v14M6 13l6 6 6-6" /></Svg>
);
const IconReply = () => (
  <Svg size={16}><path d="M9 14 4 9l5-5" /><path d="M4 9h8a8 8 0 0 1 8 8v2" /></Svg>
);
const IconCopy = () => (
  <Svg size={16}><rect x="9" y="9" width="11" height="11" rx="2.2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></Svg>
);
const IconSend = () => (
  <Svg><path d="M4 12 20.5 4 13 20.5l-2-7z" /></Svg>
);

// ── emoji ───────────────────────────────────────────────────────────────
// A useful handful rather than a reproduction of Telegram's picker: enough to
// reach for without leaving the keyboard, small enough to stay instant.
const EMOJI: Array<[string, string[]]> = [
  ["emoji.smileys", "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 😉 😊 😇 🥰 😍 😘 😋 😛 🤗 🤔 😐 😴 😌 😔 😢 😭 😤 😡 🥵 🥶 😱 🤯 😳 🙃 😬 🙄 😎 🤓 🥳".split(" ")],
  ["emoji.gestures", "👍 👎 👌 🤝 🙏 👏 🙌 💪 ✍️ 👋 ✌️ 🤞 👆 👇 👈 👉 ☝️ 🫶".split(" ")],
  ["emoji.hearts", "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💔 ❣️ 💕 💞 💯 ✨ ⭐ 🔥 💫 🎉".split(" ")],
  ["emoji.objects", "📞 📱 💻 ⌨️ 🖥️ 📷 🎥 📎 📁 📄 📊 💰 💳 🔑 🔒 ⏰ 📅 📌".split(" ")],
  ["emoji.symbols", "✅ ❌ ⚠️ ❗ ❓ ➕ ➖ ✔️ ⏳ 🔔 🔕 🔄 ⬆️ ⬇️ ▶️ ⏸️ 🆗 🆕".split(" ")],
];

// ── helpers ─────────────────────────────────────────────────────────────
const URL_RE = /(https?:\/\/[^\s<>"']+|t\.me\/[^\s<>"']+)/gi;

/** Turn links into anchors without touching the rest of the text.
 *
 *  Built as React nodes rather than HTML: the message is text the other side
 *  wrote, and it must never be interpreted as markup. */
function withLinks(text: string): React.ReactNode {
  const parts = text.split(URL_RE);
  return parts.map((part, index) => {
    if (index % 2 === 0) return part;
    const href = part.startsWith("http") ? part : `https://${part}`;
    return (
      <a key={index} href={href} target="_blank" rel="noopener noreferrer">
        {part}
      </a>
    );
  });
}

function hhmm(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function dayKey(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toDateString();
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined,
    { day: "numeric", month: "long", year: "numeric" });
}

/** Put text on the clipboard, by whichever route is available.
 *
 *  The modern API is the right one, but it is refused without a user gesture
 *  and missing entirely outside a secure context. The old selection trick has
 *  neither restriction, so it stands behind it rather than the copy simply
 *  failing.
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    /* fall through to the older route */
  }
  const holder = document.createElement("textarea");
  holder.value = text;
  holder.setAttribute("readonly", "");
  holder.style.position = "fixed";
  holder.style.opacity = "0";
  document.body.appendChild(holder);
  holder.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  holder.remove();
  return ok;
}

function fmtSize(bytes: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── message body ────────────────────────────────────────────────────────
function MediaChip({ media, t }: { media: MediaInfo; t: (k: string) => string }) {
  const icon = media.kind === "photo" || media.kind === "sticker" || media.kind === "gif"
    ? <IconPhoto />
    : media.kind === "video" || media.kind === "video_note" ? <IconCam />
    : media.kind === "voice" || media.kind === "audio" ? <IconMic />
    : <IconDoc />;
  const size = fmtSize(media.size);
  return (
    <span className="media-chip">
      {icon}
      <span className="truncate">
        {t(`media.${media.kind}`)}
        {media.name && `: ${media.name}`}
      </span>
      {size && <span className="faint">{size}</span>}
    </span>
  );
}

/** One line describing a message, for a quote or the composer strip. */
function summarise(message: Message, t: (k: string) => string): string {
  const line = (message.text || "").split("\n").find((l) => l.trim());
  if (line) return line;
  if (message.media) return t(`media.${message.media.kind}`);
  return t("chat.unsupported");
}

function Bubble({ message, quoted, names, flash, t, onMenu, onJump }: {
  message: Message;
  /** The message this one answers, when it is loaded. */
  quoted: Message | null;
  /** Who to name on a quote: [mine, theirs]. */
  names: [string, string];
  /** True while this message is being pointed at after a jump. */
  flash: boolean;
  t: (k: string) => string;
  onMenu: (event: React.MouseEvent, message: Message) => void;
  onJump: (id: number) => void;
}) {
  if (message.service) {
    return (
      <div className="msg-service">
        {message.text || t("chat.service_message")}
      </div>
    );
  }
  const empty = !message.text && !message.media;
  return (
    <div className={`msg${message.out ? " out" : ""}${flash ? " flash" : ""}`}
         data-mid={message.id}
         onContextMenu={(e) => onMenu(e, message)}>
      <div className="msg-bubble">
        {message.reply_to != null && (
          // Pressing it goes to the message being answered, the way Telegram
          // does. Disabled when that message is not loaded: there would be
          // nowhere to go.
          <button type="button" className="msg-quote" disabled={!quoted}
                  title={quoted ? t("chat.go_to_message") : t("chat.quote_unloaded")}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (quoted) onJump(quoted.id);
                  }}>
            <span className="msg-quote-bar" />
            <span className="msg-quote-body">
              <span className="msg-quote-author">
                {quoted ? names[quoted.out ? 0 : 1] : t("chat.quote_lost")}
              </span>
              <span className="truncate">
                {quoted ? summarise(quoted, t) : t("chat.quote_unloaded")}
              </span>
            </span>
          </button>
        )}
        {message.media && <MediaChip media={message.media} t={t} />}
        {message.text && <span className="msg-text">{withLinks(message.text)}</span>}
        {empty && <span className="faint">{t("chat.unsupported")}</span>}
        <span className="msg-time">{hhmm(message.date)}</span>
      </div>
    </div>
  );
}

// ── attachment menu ─────────────────────────────────────────────────────
interface AttachItem {
  key: string;
  icon: React.ReactNode;
  enabled: boolean;
  accept?: string;
}

const ATTACH: AttachItem[] = [
  { key: "chat.attach_media", icon: <IconPhoto />, enabled: true,
    accept: "image/*,video/*" },
  { key: "chat.attach_document", icon: <IconDoc />, enabled: true, accept: "*/*" },
  { key: "chat.attach_location", icon: <IconPin />, enabled: false },
  { key: "chat.attach_voice", icon: <IconMic />, enabled: false },
  { key: "chat.attach_video_note", icon: <IconCam />, enabled: false },
];

// ── the chat ────────────────────────────────────────────────────────────
export function OperatorChat({ operator }: { operator: Operator }) {
  const { t, notify, onEvent } = useStore();

  const [dialogs, setDialogs] = useState<Dialog[] | null>(null);
  const [peer, setPeer] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[] | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [older, setOlder] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [atBottom, setAtBottom] = useState(true);
  const [menu, setMenu] = useState(false);
  const [emoji, setEmoji] = useState(false);
  // What the next send answers, and where the right-click menu is open.
  const [replyTo, setReplyTo] = useState<Message | null>(null);
  // Which message is being pointed at right after a jump.
  const [flashId, setFlashId] = useState<number | null>(null);
  const [msgMenu, setMsgMenu] = useState<
    { x: number; y: number; message: Message } | null>(null);

  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const msgMenuRef = useRef<HTMLDivElement>(null);
  // Which chat the in-flight request belongs to: clicking two dialogs quickly
  // must not paint the first one's history into the second one.
  const requestRef = useRef(0);
  // Read inside layout effects, where state would be a render behind.
  const stickRef = useRef(true);
  // True while the jump button's smooth scroll is still travelling. Without
  // it the scroll events the animation itself fires read as "the user is up
  // here" and switch following straight back off.
  const jumpingRef = useRef(false);
  const peerRef = useRef<number | null>(null);
  const keepRef = useRef<number | null>(null);
  const acceptRef = useRef("*/*");
  // Where the caret has to end up after React writes the new value. Setting it
  // from a rAF callback ran too early: assigning a textarea's value moves the
  // caret to the end, so every emoji after the first one was appended there.
  const caretRef = useRef<number | null>(null);
  const flashTimer = useRef<number | undefined>(undefined);

  peerRef.current = peer;

  // ── dialogs ───────────────────────────────────────────────────────────
  useEffect(() => {
    let alive = true;
    api.operators.dialogs(operator.id)
      .then((rows) => { if (alive) setDialogs(rows); })
      .catch((err) => {
        if (!alive) return;
        setDialogs([]);
        notify(err instanceof Error ? err.message : String(err), "err");
      });
    return () => { alive = false; };
  }, [operator.id, notify]);

  // ── live updates, for as long as the chat is on screen ────────────────
  useEffect(() => {
    let alive = true;
    api.operators.watch(operator.id).catch(() => {
      // Not fatal: history and sending still work, only the live feed is off.
      if (alive) notify(t("chat.live_off"), "err");
    });
    return () => {
      alive = false;
      void api.operators.unwatch(operator.id).catch(() => undefined);
    };
  }, [operator.id, notify, t]);

  useEffect(() => onEvent((event) => {
    if (event.type !== "operator.message_in") return;
    const { operator_id: opId, peer_id: peerId, message } = event.payload;
    if (opId !== operator.id || peerId !== peerRef.current || !message) return;
    setMessages((prev) => {
      if (prev === null) return prev;
      // Telegram can deliver the same update twice; an id we already show is
      // the same message, not a new one.
      if (prev.some((m) => m.id === message.id)) return prev;
      return [...prev, message as Message];
    });
  }), [onEvent, operator.id]);

  // ── opening a chat ────────────────────────────────────────────────────
  const openChat = useCallback(async (id: number) => {
    const ticket = ++requestRef.current;
    setPeer(id);
    peerRef.current = id;
    setMessages(null);
    setHasMore(true);
    setReplyTo(null);
    setMsgMenu(null);
    setFlashId(null);
    window.clearTimeout(flashTimer.current);
    stickRef.current = true;
    setAtBottom(true);
    try {
      const rows = await api.operators.messages(operator.id, id);
      if (ticket !== requestRef.current) return;    // a later chat won
      setMessages(rows);
      setHasMore(rows.length > 0);
    } catch (err) {
      if (ticket !== requestRef.current) return;
      setMessages([]);
      notify(err instanceof Error ? err.message : String(err), "err");
    }
  }, [operator.id, notify]);

  // ── scrolling ─────────────────────────────────────────────────────────
  const scrollToBottom = useCallback((smooth: boolean) => {
    const el = listRef.current;
    if (!el) return;
    // Following is switched on right away, so a message arriving mid-animation
    // is followed. But "we are at the bottom" is not claimed here: a smooth
    // scroll takes time, and saying it early hid the button while the view had
    // not moved yet. onScroll reports arrival when it actually happens.
    stickRef.current = true;
    jumpingRef.current = smooth;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }, []);

  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK;
    setAtBottom(bottom);
    if (msgMenu) setMsgMenu(null);
    if (jumpingRef.current) {
      // Mid-flight: the position is the animation's, not a decision the reader
      // made, so following stays on until we land.
      if (bottom) jumpingRef.current = false;
      return;
    }
    // Reaching the bottom by hand turns following back on, exactly as if the
    // button had been pressed. Leaving it switches following off.
    stickRef.current = bottom;
    if (el.scrollTop <= TOP_SLACK) void loadOlder();
  }

  /** Any real scrolling gesture hands control back to the reader, even in the
   *  middle of the button's animation. */
  function onUserScrollGesture() {
    jumpingRef.current = false;
  }

  /** Go to the message a quote points at and mark it, the way Telegram does.
   *
   *  Following new messages is switched off on the way: having jumped to
   *  something older, being yanked back down by an arrival would undo the
   *  very thing that was asked for.
   */
  function jumpTo(id: number) {
    const list = listRef.current;
    const el = list?.querySelector(`[data-mid="${id}"]`);
    if (!list || !el) return;
    stickRef.current = false;
    jumpingRef.current = false;
    // The offset is worked out here rather than left to scrollIntoView: that
    // walks every scrollable ancestor, so it can shift the modal as well as
    // the list, and it is the one scroll call this environment quietly drops.
    const box = list.getBoundingClientRect();
    const mark = el.getBoundingClientRect();
    const top = Math.max(0, list.scrollTop + (mark.top - box.top)
      - Math.max(0, (box.height - mark.height) / 2));
    // Near enough to follow with the eye, animate; further than a couple of
    // screens, go straight there. Watching a smooth scroll travel thousands
    // of pixels is not helpful, and Telegram does not do it either.
    const far = Math.abs(top - list.scrollTop) > box.height * 2;
    list.scrollTo({ top, behavior: far ? "auto" : "smooth" });
    // Where we land is known here, so the way back is offered at once rather
    // than a scroll event later.
    setAtBottom(list.scrollHeight - top - box.height <= BOTTOM_SLACK);
    setFlashId(id);
    window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlashId(null), 1500);
  }

  const loadOlder = useCallback(async () => {
    const el = listRef.current;
    if (!el || older || !hasMore || peerRef.current === null) return;
    const first = messages?.[0];
    if (!first) return;
    setOlder(true);
    // Remember how tall the list was: after older messages are prepended the
    // view is put back on the same message, so reading does not jump.
    keepRef.current = el.scrollHeight - el.scrollTop;
    try {
      const rows = await api.operators.messages(
        operator.id, peerRef.current, first.id);
      if (rows.length === 0) {
        setHasMore(false);
        keepRef.current = null;
        return;
      }
      setMessages((prev) => (prev === null ? rows : [...rows, ...prev]));
    } catch (err) {
      keepRef.current = null;
      notify(err instanceof Error ? err.message : String(err), "err");
    } finally {
      setOlder(false);
    }
  }, [older, hasMore, messages, operator.id, notify]);

  // Runs before the browser paints, so neither the follow nor the restore is
  // ever visible as a jump.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el || messages === null) return;
    if (keepRef.current !== null) {
      el.scrollTop = el.scrollHeight - keepRef.current;
      keepRef.current = null;
      return;
    }
    if (stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // ── sending ───────────────────────────────────────────────────────────
  function append(message: Message) {
    setMessages((prev) => (prev === null ? [message]
      : prev.some((m) => m.id === message.id) ? prev : [...prev, message]));
  }

  /** Every send goes through here.
   *
   *  It is the only place that knows what the message answers, so a reply
   *  means the same thing for text, for a picture, and for the voice note
   *  that does not exist yet. The reply is cleared only once Telegram has
   *  accepted the message - a failed send keeps what you were answering.
   */
  async function deliver(send: (replyTo?: number) => Promise<Message>) {
    if (peer === null || busy) return;
    setBusy(true);
    try {
      append(await send(replyTo?.id));
      setText("");
      setReplyTo(null);
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "err");
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  async function send() {
    const body = text.trim();
    if (!body) return;
    await deliver((reply) =>
      api.operators.send(operator.id, peer as number, body, reply));
  }

  function pickFile(accept: string) {
    acceptRef.current = accept;
    setMenu(false);
    // The input is re-created with the right accept on the next render, so the
    // click waits for it.
    window.setTimeout(() => fileRef.current?.click(), 0);
  }

  async function onFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";            // same file twice in a row must work
    if (!file) return;
    const caption = text.trim();
    await deliver((reply) =>
      api.operators.sendFile(operator.id, peer as number, file, caption, reply));
  }

  // ── the menu on a message ───────────────────────────────────────────
  function openMsgMenu(event: React.MouseEvent, message: Message) {
    event.preventDefault();
    setMenu(false);
    setEmoji(false);
    setMsgMenu({ x: event.clientX, y: event.clientY, message });
  }

  function startReply(message: Message) {
    setMsgMenu(null);
    setReplyTo(message);
    inputRef.current?.focus();
  }

  async function copyText(message: Message) {
    setMsgMenu(null);
    if (await copyToClipboard(message.text)) {
      notify(t("chat.copied"), "ok");
    } else {
      notify(t("chat.copy_failed"), "err");
    }
  }

  // ── emoji ─────────────────────────────────────────────────────────────
  function insertEmoji(symbol: string) {
    const el = inputRef.current;
    if (!el) {
      setText((prev) => prev + symbol);
      return;
    }
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? start;
    setText(el.value.slice(0, start) + symbol + el.value.slice(end));
    caretRef.current = start + symbol.length;
  }

  // Runs after the new value is in the DOM, which is the only moment the caret
  // can be placed and stay put.
  useLayoutEffect(() => {
    const at = caretRef.current;
    const el = inputRef.current;
    if (at === null || !el) return;
    caretRef.current = null;
    el.focus();
    el.setSelectionRange(at, at);
  }, [text]);

  // ── closing the popups ────────────────────────────────────────────────
  useEffect(() => {
    if (!menu && !emoji && !msgMenu && !replyTo) return;
    function away(event: MouseEvent) {
      // The menu is not "outside" itself: closing it on the mousedown that
      // starts a click meant the item unmounted before its click landed, and
      // choosing anything from the menu quietly did nothing.
      if (msgMenu && !msgMenuRef.current?.contains(event.target as Node)) {
        setMsgMenu(null);
      }
      if (!composerRef.current?.contains(event.target as Node)) {
        setMenu(false);
        setEmoji(false);
      }
    }
    function esc(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      // One step back at a time: close what is open, then let go of the
      // message being answered, and only after that does Escape reach the
      // window and close the chat. Without stopping it here, one press shut
      // the whole chat while merely dismissing a menu.
      event.stopPropagation();
      if (menu || emoji || msgMenu) {
        setMenu(false); setEmoji(false); setMsgMenu(null);
      } else {
        setReplyTo(null);
      }
    }
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menu, emoji, msgMenu, replyTo]);

  // ── render ────────────────────────────────────────────────────────────
  const locked = peer === null || busy;
  const peerName = dialogs?.find((d) => d.id === peer)?.name ?? "";
  const selfName = operator.display_name || operator.handle;
  let lastDay = "";

  return (
    <div className="chat-layout">
      <div className="chat-list">
        {dialogs === null && (
          <div className="chat-list-note muted icon-line">
            <span className="spinner" />
            {t("common.loading")}
          </div>
        )}
        {dialogs?.length === 0 && (
          <div className="chat-list-note faint">{t("operators.no_chats")}</div>
        )}
        {dialogs?.map((d) => (
          <button key={d.id} type="button"
                  className={`chat-item${peer === d.id ? " active" : ""}`}
                  onClick={() => void openChat(d.id)}>
            <span className="chat-item-name truncate">{d.name}</span>
            {d.username && (
              <span className="faint mono truncate">@{d.username}</span>
            )}
          </button>
        ))}
      </div>

      <div className="chat-pane">
        <div className="chat-messages" ref={listRef} onScroll={onScroll}
             onWheel={onUserScrollGesture}
             onTouchStart={onUserScrollGesture}
             onKeyDown={onUserScrollGesture}>
          {peer === null && (
            <div className="chat-placeholder faint">{t("operators.pick_chat")}</div>
          )}
          {peer !== null && messages === null && (
            <div className="chat-placeholder muted"><span className="spinner" /></div>
          )}
          {messages?.length === 0 && (
            <div className="chat-placeholder faint">{t("chat.empty")}</div>
          )}

          {older && (
            <div className="chat-older muted icon-line">
              <span className="spinner" />
              {t("chat.loading_older")}
            </div>
          )}
          {messages?.map((m) => {
            const day = dayKey(m.date);
            const divider = day && day !== lastDay;
            lastDay = day || lastDay;
            return (
              <div key={m.id}>
                {divider && <div className="chat-day">{dayLabel(m.date)}</div>}
                <Bubble message={m} t={t} onMenu={openMsgMenu}
                       onJump={jumpTo} flash={flashId === m.id}
                       names={[selfName, peerName]}
                       quoted={m.reply_to != null
                         ? messages.find((x) => x.id === m.reply_to) ?? null
                         : null} />
              </div>
            );
          })}
        </div>

        {/* Shown whenever the reader is above the bottom, the way Telegram
            offers the way back rather than forcing it. */}
        {peer !== null && !atBottom && (
          <button type="button" className="chat-jump" title={t("chat.to_bottom")}
                  aria-label={t("chat.to_bottom")}
                  onClick={() => scrollToBottom(true)}>
            <IconDown />
          </button>
        )}

        {/* Right-click on a message, the way Telegram does it. Fixed to the
            cursor and clamped so it never opens off-screen. */}
        {msgMenu && (
          <div className="msg-menu" role="menu" ref={msgMenuRef}
               style={{
                 left: Math.min(msgMenu.x, window.innerWidth - 210),
                 top: Math.min(msgMenu.y, window.innerHeight - 110),
               }}>
            <button type="button" role="menuitem" className="msg-menu-item"
                    onClick={() => startReply(msgMenu.message)}>
              <IconReply />
              <span>{t("chat.reply")}</span>
            </button>
            <button type="button" role="menuitem" className="msg-menu-item"
                    disabled={!msgMenu.message.text}
                    onClick={() => void copyText(msgMenu.message)}>
              <IconCopy />
              <span>{t("chat.copy_text")}</span>
            </button>
          </div>
        )}

        <div className="chat-compose" ref={composerRef}>
          <input type="file" ref={fileRef} accept={acceptRef.current}
                 style={{ display: "none" }} onChange={onFile} />

          {/* What the next send answers - text, a file, anything later. */}
          {replyTo && (
            <div className="reply-strip">
              <IconReply />
              <span className="reply-bar" />
              <span className="reply-body">
                <span className="reply-author">
                  {replyTo.out ? selfName : peerName}
                </span>
                <span className="truncate">{summarise(replyTo, t)}</span>
              </span>
              <button type="button" className="icon-btn"
                      aria-label={t("common.cancel")} title={t("common.cancel")}
                      onClick={() => setReplyTo(null)}>
                ✕
              </button>
            </div>
          )}

          <div className="chat-compose-row">
          <div className="chat-pop-anchor">
            {menu && (
              <div className="chat-attach-menu" role="menu">
                {ATTACH.map((item) => (
                  <button key={item.key} type="button" role="menuitem"
                          className="chat-attach-item"
                          disabled={!item.enabled}
                          aria-disabled={!item.enabled}
                          title={item.enabled ? "" : t("chat.soon")}
                          onClick={item.enabled
                            ? () => pickFile(item.accept ?? "*/*")
                            : undefined}>
                    {item.icon}
                    <span>{t(item.key)}</span>
                    {!item.enabled && (
                      <span className="chat-attach-soon">{t("chat.soon")}</span>
                    )}
                  </button>
                ))}
              </div>
            )}
            <button type="button" className="icon-btn" disabled={locked}
                    aria-label={t("chat.attach")} title={t("chat.attach")}
                    onClick={() => { setEmoji(false); setMenu((v) => !v); }}>
              <IconClip />
            </button>
          </div>

          <div className="chat-input-wrap">
            <textarea
              ref={inputRef} className="chat-input" rows={1} value={text}
              disabled={locked}
              placeholder={t("operators.message_placeholder")}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                // Enter sends, Shift+Enter breaks the line — a multi-line
                // message has to be possible to write.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="chat-pop-anchor right">
              {emoji && (
                <div className="chat-emoji">
                  {EMOJI.map(([group, list]) => (
                    <div key={group}>
                      <div className="chat-emoji-title">{t(group)}</div>
                      <div className="chat-emoji-grid">
                        {list.map((symbol) => (
                          <button key={symbol} type="button"
                                  className="chat-emoji-btn"
                                  onClick={() => insertEmoji(symbol)}>
                            {symbol}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <button type="button" className="icon-btn" disabled={locked}
                      aria-label={t("chat.emoji")} title={t("chat.emoji")}
                      onClick={() => { setMenu(false); setEmoji((v) => !v); }}>
                <IconSmile />
              </button>
            </div>
          </div>

          <button type="button" className="icon-btn send"
                  disabled={locked || !text.trim()}
                  aria-label={t("operators.send")} title={t("operators.send")}
                  onClick={send}>
            {busy ? <span className="spinner" /> : <IconSend />}
          </button>
          </div>
        </div>
      </div>
    </div>
  );
}
