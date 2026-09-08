/** Mirrors what StateManager.snapshot() returns. The backend decides every
 *  status; nothing here re-derives one. */

export type Level = "error" | "warning";

export interface Issue {
  level: Level;
  code: string;
  message: string;
  source: string | null;
  params: Record<string, unknown>;
}

export type Effective =
  | "READY" | "OFFLINE" | "QUEUED" | "CHECKING" | "RESTRICTED"
  | "DISABLED" | "BLOCKED" | "ERROR"
  | "DRAFT" | "SCHEDULED" | "RUNNING" | "PAUSED" | "DONE";

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
  last_error: string | null;
  spam_state: "UNKNOWN" | "CLEAN" | "LIMITED" | "FAILED";
  spam_detail: string;
  spam_checked_at: string | null;
  handle: string;
  display_name: string;
  effective: Effective;
  issues: Issue[];
  campaign_ids: string[];
  has_own_autoreply: boolean;
}

export type ScheduleMode = "ONCE" | "DAILY" | "INTERVAL";

export interface Schedule {
  mode: ScheduleMode;
  at: string | null;
  times: string[];
  every_sec: number | null;
}

export type TargetResultStatus = "PENDING" | "SENT" | "FAILED" | "SKIPPED";

export interface CampaignTargetResult {
  target_id: string;
  status: TargetResultStatus;
  error: string | null;
  sent_at: string | null;
  attempts: number;
}

export interface Campaign {
  id: string;
  name: string;
  account_id: string;
  target_ids: string[];
  message_text: string;
  source_template_id: string | null;
  schedule: Schedule;
  gap_min_sec: number;
  gap_max_sec: number;
  raw_state: string;
  results: CampaignTargetResult[];
  created_at: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
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
  last_error: string | null;
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
  telegram_id: number | null;
  type: "CHANNEL" | "GROUP" | "USER";
  active: boolean;
  notes: string;
  last_check: string | null;
  last_check_status: string | null;
  link: string;
  effective: Effective;
  issues: Issue[];
}

export interface Template {
  id: string;
  name: string;
  text: string;
  media: string | null;
  enabled: boolean;
  created_at: string;
}

export interface ApiProfile {
  id: string;
  name: string;
  api_id: number | null;
  api_hash: string;
  enabled: boolean;
  raw_state: string;
  last_check: string | null;
  last_error: string | null;
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
  last_error: string | null;
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
  delay_min_sec: number | null;
  delay_max_sec: number | null;
}

export interface AutoReplyConfig {
  owner_id: string;
  enabled: boolean;
  delay_min_sec: number;
  delay_max_sec: number;
  rules: AutoReplyRule[];
  effective: Effective;
  issues: Issue[];
  inherited: boolean;
}

export interface Settings {
  schema_version: number;
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
    default_gap_min_sec: number;
    default_gap_max_sec: number;
  };
  spamcheck: {
    enabled: boolean;
    include_operators: boolean;
    bot: string;
    delay_sec: number;
    timeout_sec: number;
    clean_phrases: string[];
    limited_phrases: string[];
  };
  accounts: {
    dialog_limit: number;
    message_limit: number;
    probe_interval_sec: number;
  };
  operators: { after_hours: string };
  dev: { test_mode: boolean };
}

export interface Checkup {
  running: boolean;
  total: number;
  done: number;
  current: string | null;
  spam: boolean;
}

export interface Snapshot {
  accounts: Account[];
  campaigns: Campaign[];
  operators: Operator[];
  targets: Target[];
  templates: Template[];
  api_profiles: ApiProfile[];
  network_profiles: NetworkProfile[];
  auto_reply: AutoReplyConfig[];
  settings: Settings;
  checkup: Checkup;
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
  error: string | null;
  qr_url: string | null;
  qr_modules: boolean[][] | null;
  hint: string | null;
  as_operator: boolean;
  entity_id: string | null;
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
