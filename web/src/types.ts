/** Mirrors what StateManager.snapshot() returns. The backend decides every
 *  status; nothing here re-derives one. */

export type Level = "error" | "warning";

/** A message from the backend: a dictionary code and its parameters. */
export type { Msg } from "./i18n";
import type { Msg } from "./i18n";

export interface Issue {
  level: Level;
  code: string;
  source: string | null;
  params: Record<string, unknown>;
}

export type Effective =
  | "READY" | "OFFLINE" | "QUEUED" | "CHECKING" | "RESTRICTED"
  | "DISABLED" | "BLOCKED" | "ERROR" | "AUTH_DEAD" | "FROZEN" | "BANNED"
  | "UNKNOWN" | "WAITING" | "DRAFT" | "SCHEDULED" | "RUNNING" | "PAUSED" | "DONE";

export interface Account {
  id: string;
  key: string;
  session_file: string;
  telegram_id: number | null;
  username: string | null;
  phone: string;
  first_name: string;
  last_name: string;
  api_profile_id: string | null;
  network_profile_id: string | null;
  operator_id: string | null;
  raw_state: string;
  disabled: boolean;
  created_at: string;
  last_check_at: string | null;
  last_error: Msg | null;
  spam_state: "UNKNOWN" | "CLEAN" | "LIMITED" | "BLOCKED" | "FAILED";
  spam_detail: Msg | null;
  spam_checked_at: string | null;
  /** Why the app switched this account off, if it was the app. Cleared the
   *  moment the user decides anything about it. */
  stop_note: Msg | null;
  /** When Telegram was last seen refusing a method because the account is
   *  frozen. A health check cannot see this - `get_me` answers anyway. */
  frozen_at: string | null;
  frozen_detail: Msg | null;
  /** Telegram told the account to wait until then (FloodWait). */
  flood_until: string | null;
  handle: string;
  display_name: string;
  effective: Effective;
  issues: Issue[];
  campaign_ids: string[];
  has_own_autoreply: boolean;
  /** The operator made from this account, if there is one. */
  linked_operator_id: string | null;
  /** Why `@operator` cannot be used in this account's own auto-reply, or
   *  null when it can. Decided by StateManager.operator_gap(). */
  operator_gap: Issue | null;
}

export type ScheduleMode = "LOOP" | "ONCE" | "DAILY";

export interface Schedule {
  mode: ScheduleMode;
  at: string | null;
  times: string[];
}

/** One of the texts a campaign rotates through. The id is stable across
 *  edits, which is what lets `last_message_id` keep meaning something. */
export interface CampaignMessage {
  id: string;
  text: string;
  /** A picture sent with this text, as a path inside the app's media folder.
   *  Empty when the message is text alone. */
  file: string;
}

export type TargetResultStatus = "PENDING" | "SENT" | "FAILED" | "SKIPPED";

export interface CampaignTargetResult {
  target_id: string;
  status: TargetResultStatus;
  error: Msg | null;
  sent_at: string | null;
  attempts: number;
  /** Not before this moment — from a slow mode or flood limit Telegram named
   *  in seconds. */
  retry_at: string | null;
  /** Refusals in a row from this chat while the account was a member. */
  refusals: number;
  /** Switched off inside this campaign: the row stays, nothing is sent. */
  excluded: boolean;
  excluded_reason: Msg | null;
}

export interface Campaign {
  id: string;
  name: string;
  account_id: string;
  target_ids: string[];
  messages: CampaignMessage[];
  /** Which message the previous cycle used. The backend picks the next one;
   *  this is here so the interface can show what happened, never to decide. */
  last_message_id: string | null;
  schedule: Schedule;
  interval_min_sec: number;
  interval_max_sec: number;
  raw_state: string;
  results: CampaignTargetResult[];
  created_at: string;
  next_run_at: string | null;
  last_run_at: string | null;
  /** Why it stopped. A campaign stops one way and starts one way, which is
   *  the user pressing Start. */
  last_error: Msg | null;
  effective: Effective;
  issues: Issue[];
  sent_count: number;
  /** Everything the campaign has ever sent; survives each recurring cycle. */
  sent_total: number;
  is_recurring: boolean;
  failed_count: number;
  total_count: number;
}

export interface Operator {
  id: string;
  /** Made from this account: name, session, API, proxy and state are its. */
  account_id: string | null;
  username: string;
  display_name: string;
  key: string;
  session_file: string;
  telegram_id: number | null;
  notes: string;
  api_profile_id: string | null;
  network_profile_id: string | null;
  raw_state: string;
  last_check_at: string | null;
  last_error: Msg | null;
  /** A linked operator's account is waiting on Telegram until then. */
  flood_until?: string | null;
  handle: string;
  logged_in: boolean;
  effective: Effective;
  issues: Issue[];
  account_ids: string[];
}

export interface Target {
  id: string;
  title: string;
  username: string;
  /** The hash of a t.me/+… link, for a private chat that has no username. */
  invite: string;
  telegram_id: number | null;
  type: "CHANNEL" | "GROUP" | "USER";
  active: boolean;
  notes: string;
  last_check: string | null;
  /** Why the last check failed; null when it passed or never ran. */
  last_check_error: Msg | null;
  link: string;
  effective: Effective;
  issues: Issue[];
}

export interface ApiProfile {
  id: string;
  name: string;
  api_id: number | null;
  api_hash: string;
  enabled: boolean;
  effective: Effective;
  issues: Issue[];
}

export interface NetworkProfile {
  id: string;
  name: string;
  protocol: string;
  host: string;
  port: number;
  username: string;
  password: string;
  enabled: boolean;
  raw_state: string;
  last_check: string | null;
  last_latency_ms: number | null;
  last_error: Msg | null;
  effective: Effective;
  issues: Issue[];
}

export type AutoReplyKind = "FIRST_MESSAGE" | "PERIODIC" | "FAQ";

export interface AutoReplyRule {
  id: string;
  kind: AutoReplyKind;
  enabled: boolean;
  match: string;
  response: string;
}

export interface AutoReplyConfig {
  owner_id: string;
  enabled: boolean;
  delay_min_sec: number;
  delay_max_sec: number;
  rules: AutoReplyRule[];
  effective: Effective;
  issues: Issue[];
  /** Why `@operator` cannot be used in this text, or null when it can. For
   *  the shared default this is a warning about accounts that have no
   *  operator, not a refusal - the ones that have one answer normally. */
  operator_gap: Issue | null;
}

export interface Settings {
  general: {
    language: "ru" | "en";
    show_log_time: boolean;
    open_browser_on_start: boolean;
  };
  autoreply: {
    faq_limit: number;
    default_delay_min_sec: number;
    default_delay_max_sec: number;
  };
  campaign: {
    default_interval_min_sec: number;
    default_interval_max_sec: number;
    start_delay_sec: number;
    auto_join: boolean;
    auto_join_delay_sec: number;
    /** Leave a reaction in the chat now and then, and how often. Broadcast
     *  accounts only — never on an operator's behalf. */
    reactions: boolean;
    reaction_percent: number;
    /** The account switches itself off when at least `error_stop_min`
     *  attempts were made in the last hour and this share of them failed. */
    error_stop_percent: number;
    error_stop_min: number;
  };
  spamcheck: {
    enabled: boolean;
    include_operators: boolean;
    bot: string;
    delay_sec: number;
    timeout_sec: number;
    clean_phrases: string[];
    limited_phrases: string[];
    blocked_phrases: string[];
  };
  accounts: {
    dialog_limit: number;
    message_limit: number;
    /** Warn when an account added this recently is already sending. */
    young_days: number;
    /** How long the background check waits between rounds — a random number
     *  of seconds inside this range, so the pulse is not a metronome. */
    probe_interval_min_sec: number;
    probe_interval_max_sec: number;
    /** How far apart accounts reach Telegram when several of them are about
     *  to: each waits a random moment inside [0, this]. 0 = all at once. */
    connect_spread_sec: number;
  };
}

export interface Checkup {
  running: boolean;
  total: number;
  done: number;
  current: string | null;
  spam: boolean;
  /** Which page's records this run is about. One run at a time, so a page
   *  shows progress only when the run belongs to it. `background` is the
   *  app's own round, which a button stops; empty when nothing ran yet. */
  kind: "" | "accounts" | "operators" | "targets" | "spam" | "background";
}

/** One row of the overview list. The backend decides what counts as one
 *  problem - see StateManager.problems() - so a dead session cannot turn
 *  into four lines again. */
export interface Problem {
  key: string;
  /** The record to scroll to: "#/<route>/<id>". */
  id: string;
  route: string;
  title: string;
  roles: string[];
  issues: Issue[];
  blocked_campaigns: number;
}

export interface Snapshot {
  accounts: Account[];
  campaigns: Campaign[];
  operators: Operator[];
  targets: Target[];
  problems: Problem[];
  api_profiles: ApiProfile[];
  network_profiles: NetworkProfile[];
  auto_reply: AutoReplyConfig[];
  settings: Settings;
  checkup: Checkup;
  /** Where the app keeps its files, so the interface can say so. */
  paths: { logs: string; data: string };
}

/** What a bulk action did. One failure never cancels the rest, so a report
 *  with both a count and a list of refusals is the normal outcome. */
export interface BulkReport {
  done: number;
  total: number;
  failed: { id: string; name: string; error: Msg }[];
}

export interface LogLine {
  ts: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  message: string;
  module: string | null;
}

export type LoginState =
  | "IDLE" | "CONNECTING" | "QR_WAIT" | "NEED_CODE"
  | "NEED_PASSWORD" | "DONE" | "ERROR" | "CANCELLED";

export interface LoginInfo {
  login_id: string;
  kind: "phone" | "qr" | "bot";
  state: LoginState;
  error: Msg | null;
  qr_url: string | null;
  qr_modules: boolean[][] | null;
  hint: Msg | null;
  as_operator: boolean;
  entity_id: string | null;
  /** How this sign-in is reaching Telegram. Shown while it runs, so "did
   *  the login go through the proxy?" is answerable at the time. */
  proxy_name: Msg | null;
}

export interface Dialog {
  id: number;
  name: string;
  username: string | null;
}

export type MediaKind =
  | "photo" | "video" | "voice" | "video_note"
  | "audio" | "gif" | "sticker" | "document";

export interface MediaInfo {
  kind: MediaKind;
  name: string;
  size: number;
}

export interface Message {
  id: number;
  out: boolean;
  sender: string;
  text: string;
  date: string;
  /** Present when the message carries an attachment. Nothing is downloaded —
   *  the chat shows a typed chip so a picture is not an empty bubble. */
  media?: MediaInfo | null;
  /** "X joined the group" and friends: no author, rendered centred. */
  service?: boolean;
  /** The message this one answers, quoted above it. */
  reply_to?: number | null;
}

export interface BusEvent {
  type: string;
  payload: Record<string, any>;
  ts: number;
}
