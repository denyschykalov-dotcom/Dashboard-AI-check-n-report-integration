import { useEffect, useMemo, useRef, useState } from "react";

import { apiRequest } from "./api";
import { hasSupabaseConfig, initialAuthRedirectHref, supabase } from "./supabase";

type Page = "overview" | "service" | "outputs" | "history";

type DraftForm = {
  keyword: string;
  domain: string;
  brand: string;
  prompt: string;
  project: string;
};

type ServiceRow = DraftForm & {
  id: string;
};

type Profile = {
  id: string;
  user_id: string;
  username: string;
  email: string;
  is_admin: boolean;
  created_at: string;
};

type ResultItem = {
  run_id: string;
  user_id: string;
  username: string;
  project: string | null;
  keyword: string;
  domain: string;
  brand: string;
  prompt: string;
  status: string;
  created_at: string;
  completed_iterations: number;
  total_iterations: number;
  gpt_domain_mention: boolean;
  gem_domain_mention: boolean;
  gpt_brand_mention: boolean;
  gem_brand_mention: boolean;
  response_count_avg: number | null;
  brand_list: string | null;
  citation_format: string | null;
  sentiment_analysis: string | null;
};

type PaginatedResponse<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
};

type ActiveRunsResponse = {
  run_ids: string[];
  total_runs: number;
};

type FailedRunsResponse = {
  items: RunRecord[];
  total_runs: number;
};

type ProjectListResponse = {
  projects: string[];
};

type OverviewUserOption = {
  user_id: string;
  username: string;
};

type UserOptionsResponse = {
  users: OverviewUserOption[];
};

type BulkRunActionResponse = {
  run_ids: string[];
  total_runs: number;
  status: string;
};

type HistoryForwardResponse = {
  run_ids: string[];
  total_runs: number;
  outputs_updated: number;
  results_updated: number;
  target_user_id: string;
};

type HistoryForwardDialog = {
  users: OverviewUserOption[];
  selectedUserId: string;
};

type DraftResponse = DraftForm & {
  updated_at: string;
  rows?: DraftForm[];
};

type OverviewWindowStats = {
  total_results: number;
  brand_matches: number;
  domain_matches: number;
  users: number;
  spend_usd: number;
};

type OverviewStats = {
  user_half_year: OverviewWindowStats;
  user_active_runs: number;
  global_last_month: OverviewWindowStats;
  global_projects: number;
};

type MonthlyOverviewItem = {
  month: string;
  label: string;
  brand_matches: number;
  domain_matches: number;
  total_runs: number;
  spend_usd: number;
};

type OverviewSummary = {
  is_admin: boolean;
  stats: OverviewStats;
  project_options: string[];
  user_options: OverviewUserOption[];
  selected_project: string | null;
  selected_user_id: string | null;
  monthly: MonthlyOverviewItem[];
};

type RunRecord = {
  id: string;
  user_id: string;
  username: string | null;
  keyword: string;
  domain: string;
  brand: string;
  prompt: string;
  project: string | null;
  status: string;
  total_iterations: number;
  completed_iterations: number;
  error_messages: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

type RunOutputRecord = {
  id: string;
  user_id: string;
  run_id: string;
  iteration_number: number;
  gpt_output: string | null;
  gem_output: string | null;
  gpt_domain_mention: boolean;
  gem_domain_mention: boolean;
  gpt_brand_mention: boolean;
  gem_brand_mention: boolean;
  response_count: number | null;
  brand_list: string | null;
  citation_format: string | null;
  openai_generation_cost_usd: number | null;
  gemini_generation_cost_usd: number | null;
  gemini_analysis_cost_usd: number | null;
  estimated_total_cost_usd: number | null;
  project: string | null;
  created_at: string;
};

type RunResultRecord = {
  id: string;
  user_id: string;
  run_id: string;
  project: string | null;
  gpt_domain_mention: boolean;
  gem_domain_mention: boolean;
  gpt_brand_mention: boolean;
  gem_brand_mention: boolean;
  response_count_avg: number | null;
  brand_list: string | null;
  citation_format: string | null;
  sentiment_analysis: string | null;
  gemini_sentiment_cost_usd: number | null;
  estimated_total_cost_usd: number | null;
  created_at: string;
};

type RunDetail = {
  run: RunRecord;
  outputs: RunOutputRecord[];
  result: RunResultRecord | null;
  estimated_total_cost_usd: number | null;
};

type ReportKind = "history" | "outputs";

type ReportExportDialog = {
  kind: ReportKind;
  projects: string[];
  selectedProjects: string[];
};

type PendingServiceRow = {
  id: string;
  row: DraftForm;
  index: number;
};

type StartProjectGroup = {
  project: string;
  label: string;
  rows: PendingServiceRow[];
};

type StartProjectDialog = {
  rows: PendingServiceRow[];
  selectedRowIds: string[];
};

type AuthMode = "sign_in" | "sign_up";

type AuthForm = {
  username: string;
  email: string;
  password: string;
};

type SpecialAuthRoute = "reset-password";

type ResetPasswordStatus = "idle" | "loading" | "ready" | "success" | "error";

type AuthRedirectSnapshot = {
  type: string | null;
  tokenHash: string | null;
  errorCode: string | null;
  errorDescription: string | null;
  hasAccessToken: boolean;
  hasRefreshToken: boolean;
  hasCode: boolean;
};

type AuthFlashState = {
  message: string;
  openAuthModal?: boolean;
};

const emptyDraft: DraftForm = {
  keyword: "",
  domain: "",
  brand: "",
  prompt: "",
  project: "",
};

const ADD_PROJECT_OPTION_VALUE = "__add_project__";
const AUTH_REDIRECT_ORIGIN = "https://dashboard.rankberry.marketing";
const PASSWORD_RESET_ROUTE = "reset-password";
const PASSWORD_RESET_REDIRECT_URL = `${AUTH_REDIRECT_ORIGIN}/`;
const PASSWORD_RESET_PATH = `/${PASSWORD_RESET_ROUTE}`;
const PASSWORD_RESET_FLOW_KEY = "rankberry-dashboard-password-reset-flow";
const AUTH_FLASH_KEY = "rankberry-dashboard-auth-flash";
const AUTH_URL_PARAM_KEYS = [
  "access_token",
  "refresh_token",
  "expires_in",
  "expires_at",
  "token_type",
  "type",
  "token_hash",
  "code",
  "error",
  "error_code",
  "error_description",
];

function normalizePathname(pathname: string) {
  if (!pathname || pathname === "/") {
    return "/";
  }
  return pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

function getSpecialAuthRouteFromPath(pathname: string | null): SpecialAuthRoute | null {
  if (!pathname) {
    return null;
  }

  const normalizedPathname = normalizePathname(pathname);
  if (normalizedPathname === PASSWORD_RESET_PATH) {
    return "reset-password";
  }
  return null;
}

function mergeSearchParams(target: URLSearchParams, source: URLSearchParams) {
  source.forEach((value, key) => {
    target.set(key, value);
  });
}

function getHashPathname(hash: string) {
  if (!hash.startsWith("#")) {
    return null;
  }

  const fragment = hash.slice(1);
  if (!fragment.startsWith("/")) {
    return null;
  }

  const stopIndexes = ["?", "#", "&"]
    .map((marker) => fragment.indexOf(marker))
    .filter((index) => index > -1);
  const stopIndex = stopIndexes.length ? Math.min(...stopIndexes) : fragment.length;
  return fragment.slice(0, stopIndex);
}

function getHashSearchParams(hash: string) {
  const params = new URLSearchParams();
  if (!hash.startsWith("#")) {
    return params;
  }

  const fragment = hash.slice(1);
  const candidates: string[] = [];
  if (!fragment.startsWith("/") && fragment.includes("=")) {
    candidates.push(fragment);
  }

  const nestedHashIndex = fragment.indexOf("#");
  if (nestedHashIndex > -1) {
    candidates.push(fragment.slice(nestedHashIndex + 1));
  }

  const queryIndex = fragment.indexOf("?");
  if (queryIndex > -1) {
    candidates.push(fragment.slice(queryIndex + 1));
  }

  const hashRouteParamIndex = fragment.indexOf("&");
  if (fragment.startsWith("/") && hashRouteParamIndex > -1) {
    candidates.push(fragment.slice(hashRouteParamIndex + 1));
  }

  candidates.forEach((candidate) => {
    mergeSearchParams(params, new URLSearchParams(candidate));
  });
  return params;
}

function readAuthRedirectParams(url: URL) {
  const params = getHashSearchParams(url.hash);
  mergeSearchParams(params, url.searchParams);
  return params;
}

function getSpecialAuthRoute(href: string): SpecialAuthRoute | null {
  const url = new URL(href);
  const authParams = readAuthRedirectParams(url);

  return (
    getSpecialAuthRouteFromPath(url.pathname)
    || getSpecialAuthRouteFromPath(getHashPathname(url.hash))
    || (authParams.get("type") === "recovery" ? "reset-password" : null)
  );
}

function readAuthRedirectSnapshot(href: string): AuthRedirectSnapshot {
  const url = new URL(href);
  const params = readAuthRedirectParams(url);

  return {
    type: params.get("type"),
    tokenHash: params.get("token_hash"),
    errorCode: params.get("error_code"),
    errorDescription: params.get("error_description"),
    hasAccessToken: params.has("access_token"),
    hasRefreshToken: params.has("refresh_token"),
    hasCode: params.has("code"),
  };
}

function hasAuthRedirectAttempt(snapshot: AuthRedirectSnapshot) {
  return Boolean(
    snapshot.type
    || snapshot.tokenHash
    || snapshot.hasAccessToken
    || snapshot.hasRefreshToken
    || snapshot.hasCode,
  );
}

function scrubAuthRedirectUrl() {
  if (typeof window === "undefined") {
    return;
  }

  const url = new URL(window.location.href);
  let hasChanges = false;

  AUTH_URL_PARAM_KEYS.forEach((key) => {
    if (url.searchParams.has(key)) {
      url.searchParams.delete(key);
      hasChanges = true;
    }
  });

  if (url.hash.startsWith("#")) {
    const hashParams = new URLSearchParams(url.hash.slice(1));
    AUTH_URL_PARAM_KEYS.forEach((key) => {
      if (hashParams.has(key)) {
        hashParams.delete(key);
        hasChanges = true;
      }
    });
    url.hash = hashParams.toString() ? `#${hashParams.toString()}` : "";
  }

  if (hasChanges) {
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }
}

function setAuthFlowFlag(key: string) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.setItem(key, "1");
  } catch {
    // Ignore storage failures so the auth flow still works.
  }
}

function hasAuthFlowFlag(key: string) {
  if (typeof window === "undefined") {
    return false;
  }
  try {
    return window.sessionStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function clearAuthFlowFlag(key: string) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Ignore storage failures so cleanup never blocks the user.
  }
}

function writeAuthFlashState(value: AuthFlashState) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.setItem(AUTH_FLASH_KEY, JSON.stringify(value));
  } catch {
    // Ignore storage failures and continue the redirect.
  }
}

function readAuthFlashState(): AuthFlashState | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const rawValue = window.sessionStorage.getItem(AUTH_FLASH_KEY);
    if (!rawValue) {
      return null;
    }
    window.sessionStorage.removeItem(AUTH_FLASH_KEY);
    const parsedValue = JSON.parse(rawValue) as AuthFlashState | null;
    if (!parsedValue?.message) {
      return null;
    }
    return parsedValue;
  } catch {
    return null;
  }
}

function toErrorText(error: unknown) {
  return error instanceof Error ? error.message.toLowerCase() : String(error || "").toLowerCase();
}

function getFriendlyPasswordResetLinkError(details?: string | null) {
  const message = String(details || "").toLowerCase();
  if (message.includes("expired")) {
    return "This password reset link has expired. Request a new reset email and try again.";
  }
  if (message.includes("already") || message.includes("used")) {
    return "This password reset link has already been used. Request a new reset email if you still need to change your password.";
  }
  return "This password reset link is invalid or no longer available. Request a new reset email and try again.";
}

function getFriendlyPasswordUpdateError(error: unknown) {
  const message = toErrorText(error);
  if (message.includes("same password")) {
    return "Choose a different password than the one you used before.";
  }
  if (message.includes("password")) {
    return "Choose a stronger password and try again.";
  }
  return "We could not update your password. Request a new reset email and try again.";
}

function validateNewPassword(password: string, confirmation: string) {
  if (password.length < 8) {
    return "Use at least 8 characters.";
  }
  if (!/[A-Za-z]/.test(password) || !/\d/.test(password)) {
    return "Use at least one letter and one number.";
  }
  if (password !== confirmation) {
    return "Password confirmation must match.";
  }
  return "";
}

function createServiceRow(values: Partial<DraftForm> = {}): ServiceRow {
  return {
    id: typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
    keyword: values.keyword || "",
    domain: values.domain || "",
    brand: values.brand || "",
    prompt: values.prompt || "",
    project: values.project || "",
  };
}

function toDraftForm(row: ServiceRow | DraftForm | null | undefined): DraftForm {
  return {
    keyword: row?.keyword || "",
    domain: row?.domain || "",
    brand: row?.brand || "",
    prompt: row?.prompt || "",
    project: row?.project || "",
  };
}

function hasAnyRowValue(row: DraftForm) {
  return Object.values(row).some((value) => value.trim());
}

function normalizeDraftRows(rows: Array<ServiceRow | DraftForm> | null | undefined): DraftForm[] {
  const normalized = (rows || []).map((row) => toDraftForm(row));
  return normalized.length ? normalized : [toDraftForm(emptyDraft)];
}

function draftRowsFromResponse(response: DraftResponse): DraftForm[] {
  return normalizeDraftRows(response.rows && response.rows.length ? response.rows : [{
    keyword: response.keyword || "",
    domain: response.domain || "",
    brand: response.brand || "",
    prompt: response.prompt || "",
    project: response.project || "",
  }]);
}

function normalizeProjectName(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function collectUniqueProjects(values: Array<string | null | undefined>) {
  const seen = new Set<string>();
  return values
    .map((value) => (value || "").trim())
    .filter((value) => {
      if (!value || seen.has(value)) {
        return false;
      }
      seen.add(value);
      return true;
    })
    .sort((left, right) => left.localeCompare(right, undefined, { sensitivity: "base" }));
}

function preserveServiceInputViewport() {
  if (typeof document === "undefined" || typeof window === "undefined") {
    return;
  }

  const activeElement = document.activeElement;
  if (!(activeElement instanceof HTMLElement) || !activeElement.closest(".service-row-list")) {
    return;
  }

  const scrollX = window.scrollX;
  const scrollY = window.scrollY;
  window.requestAnimationFrame(() => {
    if (document.activeElement === activeElement && Math.abs(window.scrollY - scrollY) > 24) {
      window.scrollTo(scrollX, scrollY);
    }
  });
}


function normalizeCsvHeader(value: string) {
  return value.replace(/^\uFEFF/, "").trim().toLowerCase();
}

function parseCsvTable(input: string) {
  const rows: string[][] = [];
  const normalized = input.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  let currentRow: string[] = [];
  let currentValue = "";
  let insideQuotes = false;

  for (let index = 0; index < normalized.length; index += 1) {
    const char = normalized[index];

    if (insideQuotes) {
      if (char === '"') {
        if (normalized[index + 1] === '"') {
          currentValue += '"';
          index += 1;
        } else {
          insideQuotes = false;
        }
      } else {
        currentValue += char;
      }
      continue;
    }

    if (char === '"') {
      insideQuotes = true;
    } else if (char === ",") {
      currentRow.push(currentValue);
      currentValue = "";
    } else if (char === "\n") {
      currentRow.push(currentValue);
      rows.push(currentRow);
      currentRow = [];
      currentValue = "";
    } else {
      currentValue += char;
    }
  }

  if (insideQuotes) {
    throw new Error("CSV file is not closed properly.");
  }

  currentRow.push(currentValue);
  if (currentRow.some((cell) => cell.length) || !rows.length) {
    rows.push(currentRow);
  }

  return rows.filter((row, index) => row.some((cell) => cell.trim()) || index === 0);
}

function parseServiceRowsCsv(input: string): DraftForm[] {
  const table = parseCsvTable(input);
  const [header, ...bodyRows] = table;
  if (!header || !header.length) {
    throw new Error("CSV file is empty.");
  }

  const headerIndex = new Map(header.map((cell, index) => [normalizeCsvHeader(cell), index]));
  const requiredColumns: Array<keyof DraftForm> = ["keyword", "domain", "brand", "prompt", "project"];
  const missingColumns = requiredColumns.filter((column) => !headerIndex.has(column));
  if (missingColumns.length) {
    throw new Error(`CSV is missing columns: ${missingColumns.map((column) => column[0].toUpperCase() + column.slice(1)).join(", ")}`);
  }

  const rows = bodyRows
    .map((row) => ({
      keyword: String(row[headerIndex.get("keyword") ?? -1] || "").trim(),
      domain: String(row[headerIndex.get("domain") ?? -1] || "").trim(),
      brand: String(row[headerIndex.get("brand") ?? -1] || "").trim(),
      prompt: String(row[headerIndex.get("prompt") ?? -1] || "").trim(),
      project: String(row[headerIndex.get("project") ?? -1] || "").trim(),
    }))
    .filter((row) => hasAnyRowValue(row));

  if (!rows.length) {
    throw new Error("CSV did not include any filled rows.");
  }

  return rows;
}


function getPreferredUsername(email: string | null | undefined, username: string | null | undefined) {
  const cleanedUsername = String(username || "").trim();
  if (cleanedUsername) {
    return cleanedUsername;
  }

  const localPart = String(email || "").split("@", 1)[0].trim();
  if (!localPart) {
    return "User";
  }

  const spacedLocalPart = localPart.replace(/[._-]+/g, " ").trim();
  return spacedLocalPart || localPart;
}

function getLocalDateValue(date = new Date()) {
  const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 10);
}

function formatLocalDateLabel(value: string) {
  return new Date(`${value}T12:00:00`).toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function formatCsvDate(value: string | null | undefined) {
  if (!value) {
    return "";
  }
  return new Date(value).toISOString();
}

function escapeCsv(value: string) {
  if (/[",\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function downloadCsv(filename: string, headers: string[], rows: string[][]) {
  const csv = [headers, ...rows]
    .map((row) => row.map((value) => escapeCsv(value)).join(","))
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  downloadBlob(blob, filename);
}

const navItems: Array<{ key: Page; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "service", label: "AI Visibility Input" },
  { key: "outputs", label: "AI Visibility Outputs" },
  { key: "history", label: "History" },
];

const queueRows = [
  { queue: "Draft Autosave", status: "Live", tone: "green", items: "1", owner: "FastAPI" },
  { queue: "Queued Runs", status: "DB-backed", tone: "amber", items: "Shared", owner: "Worker" },
  { queue: "Final Results", status: "Materialized", tone: "green", items: "1 row/run", owner: "Postgres" },
];

const activityRows = [
  { time: "Profile", text: "Supabase session drives user identity" },
  { time: "Draft", text: "Current form state is autosaved per user" },
  { time: "Worker", text: "Three iterations are processed asynchronously" },
  { time: "Results", text: "History and outputs read from aggregated rows" },
];

function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "-";
  }
  return new Date(value).toLocaleString();
}

function formatNumber(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "-";
  }
  return value.toFixed(2);
}

function formatUsd(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "-";
  }
  if (value > 0 && value < 0.0001) {
    return "<$0.0001";
  }
  return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
}

function summarizeMentions(item: ResultItem | RunOutputRecord | RunResultRecord) {
  const labels: string[] = [];
  if (item.gpt_brand_mention) labels.push("GPT brand");
  if (item.gpt_domain_mention) labels.push("GPT domain");
  if (item.gem_brand_mention) labels.push("Gemini brand");
  if (item.gem_domain_mention) labels.push("Gemini domain");
  return labels.length ? labels.join(" | ") : "None";
}

function statusTone(status: string) {
  if (status === "completed") return "green";
  if (status === "failed") return "red";
  return "amber";
}

function truncateText(value: string | null | undefined, limit = 140) {
  if (!value) {
    return "-";
  }
  return value.length > limit ? `${value.slice(0, limit)}...` : value;
}

function formatStartProjectLabel(project: string) {
  const cleanedProject = project.trim();
  return cleanedProject || "No project";
}

function groupStartProjectRows(rows: PendingServiceRow[]): StartProjectGroup[] {
  const grouped = new Map<string, PendingServiceRow[]>();

  for (const row of rows) {
    const project = row.row.project.trim();
    const key = project || "";
    const existingRows = grouped.get(key);
    if (existingRows) {
      existingRows.push(row);
    } else {
      grouped.set(key, [row]);
    }
  }

  return [...grouped.entries()]
    .map(([project, groupedRows]) => ({
      project,
      label: formatStartProjectLabel(project),
      rows: groupedRows.slice().sort((left, right) => left.index - right.index),
    }))
    .sort((left, right) => {
      if (!left.project && right.project) {
        return 1;
      }
      if (left.project && !right.project) {
        return -1;
      }
      return left.label.localeCompare(right.label, undefined, { sensitivity: "base" });
    });
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function loadImage(source: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("Could not load the overview screenshot image."));
    image.src = source;
  });
}

function copyComputedStyles(source: Element, target: Element) {
  if (source instanceof HTMLElement && target instanceof HTMLElement) {
    const computedStyle = window.getComputedStyle(source);
    target.style.cssText = "";
    for (let index = 0; index < computedStyle.length; index += 1) {
      const property = computedStyle[index];
      target.style.setProperty(property, computedStyle.getPropertyValue(property), computedStyle.getPropertyPriority(property));
    }
  }

  const sourceChildren = Array.from(source.children);
  const targetChildren = Array.from(target.children);
  sourceChildren.forEach((child, index) => {
    const targetChild = targetChildren[index];
    if (targetChild) {
      copyComputedStyles(child, targetChild);
    }
  });
}

async function captureElementAsJpeg(element: HTMLElement, filename: string) {
  if (typeof window === "undefined") {
    throw new Error("Screenshot capture is only available in the browser.");
  }

  if (document.fonts) {
    await document.fonts.ready;
  }

  const rect = element.getBoundingClientRect();
  const width = Math.max(1, Math.ceil(rect.width));
  const height = Math.max(1, Math.ceil(Math.max(element.scrollHeight, rect.height)));

  const source = element.cloneNode(true) as HTMLElement;
  source.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
  copyComputedStyles(element, source);
  source.style.width = `${width}px`;
  source.style.height = `${height}px`;
  source.style.overflow = "hidden";
  source.style.backgroundColor = window.getComputedStyle(document.body).backgroundColor || "#ffffff";

  const svg = new Blob(
    [
      `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%">${new XMLSerializer().serializeToString(source)}</foreignObject></svg>`,
    ],
    { type: "image/svg+xml;charset=utf-8" },
  );
  const svgUrl = URL.createObjectURL(svg);

  try {
    const image = await loadImage(svgUrl);
    const scale = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);

    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("Canvas support is unavailable.");
    }

    const backgroundColor = window.getComputedStyle(document.body).backgroundColor;
    context.scale(scale, scale);
    context.fillStyle = backgroundColor && backgroundColor !== "rgba(0, 0, 0, 0)" ? backgroundColor : "#ffffff";
    context.fillRect(0, 0, width, height);
    context.drawImage(image, 0, 0, width, height);

    const blob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((output) => {
        if (output) {
          resolve(output);
        } else {
          reject(new Error("Could not create the JPG screenshot."));
        }
      }, "image/jpeg", 0.95);
    });

    downloadBlob(blob, filename);
  } finally {
    URL.revokeObjectURL(svgUrl);
  }
}


async function captureOverviewScreenshotJpeg(
  data: {
    width: number;
    isAdmin: boolean;
    projectLabel: string;
    userLabel: string;
    chartRangeMonths: number;

    highlights: Array<{ label: string; value: string; meta: string }>;
    monthly: MonthlyOverviewItem[];
  },
  filename: string,
) {
  if (typeof document === "undefined" || typeof window === "undefined") {
    throw new Error("Screenshot capture is only available in the browser.");
  }

  const width = Math.max(1440, Math.round(data.width || 1440));
  const padding = 56;
  const innerWidth = width - padding * 2;

  const chartGap = 20;
  const chartCardHeight = 392;
  const chartCardWidth = (innerWidth - chartGap) / 2;
  const spendCardHeight = 334;
  const highlightCols = 3;
  const highlightGap = 16;
  const highlightCardHeight = 128;
  const highlightRows = Math.max(1, Math.ceil(data.highlights.length / highlightCols));
  const highlightGridHeight = highlightRows * highlightCardHeight + Math.max(0, highlightRows - 1) * highlightGap;
  const chartsHeight = data.isAdmin ? chartCardHeight + 20 + spendCardHeight : chartCardHeight;
  const totalHeight = Math.ceil(padding + chartsHeight + 28 + highlightGridHeight + 82 + padding);

  const canvas = document.createElement("canvas");
  const scale = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(totalHeight * scale);

  const ctx = canvas.getContext("2d") as CanvasRenderingContext2D;
  if (!ctx) {
    throw new Error("Canvas support is unavailable.");
  }

  ctx.scale(scale, scale);
  ctx.imageSmoothingQuality = "high";
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.lineCap = "round";

  const fonts = {
    sans: '"Segoe UI", Arial, sans-serif',
    mono: '"Consolas", "Courier New", monospace',
  };

  const colors = {
    bgTop: "#faf7ee",
    bgMiddle: "#f6f2e5",
    bgBottom: "#f3ecf8",
    panel: "#fbf9f0",
    line: "rgba(87, 58, 34, 0.12)",
    shadow: "rgba(46, 25, 99, 0.12)",
    text: "#2e1963",
    textStrong: "#241452",
    muted: "#7e67a4",
    accent: "#c132cf",
    accentStrong: "#7235db",
    highlight: "#ebe573",
    gold: "#c39c3d",
  };

  function roundRectPath(x: number, y: number, w: number, h: number, radius: number) {
    const r = Math.max(0, Math.min(radius, w / 2, h / 2));
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function fillPanel(x: number, y: number, w: number, h: number, radius = 24) {
    ctx.save();
    ctx.shadowColor = colors.shadow;
    ctx.shadowBlur = 22;
    ctx.shadowOffsetY = 10;
    roundRectPath(x, y, w, h, radius);
    ctx.fillStyle = colors.panel;
    ctx.fill();
    ctx.restore();

    ctx.save();
    roundRectPath(x, y, w, h, radius);
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();
  }

  function wrapLines(text: string, font: string, maxWidth: number, maxLines = 2) {
    const trimmed = text.trim();
    if (!trimmed) {
      return [""];
    }

    ctx.font = font;
    const words = trimmed.split(/\s+/);
    const lines: string[] = [];
    let current = "";

    for (const word of words) {
      const candidate = current ? `${current} ${word}` : word;
      if (!current || ctx.measureText(candidate).width <= maxWidth) {
        current = candidate;
      } else {
        lines.push(current);
        current = word;
      }
    }

    if (current) {
      lines.push(current);
    }

    if (lines.length <= maxLines) {
      return lines;
    }

    const clipped = lines.slice(0, maxLines);
    let lastLine = clipped[maxLines - 1];
    while (lastLine.length > 0 && ctx.measureText(`${lastLine}...`).width > maxWidth) {
      lastLine = lastLine.slice(0, -1);
    }
    clipped[maxLines - 1] = `${lastLine || ""}...`;
    return clipped;
  }

  function drawTextBlock(
    text: string,
    x: number,
    y: number,
    options: {
      font: string;
      color: string;
      maxWidth: number;
      maxLines?: number;
      lineHeight?: number;
      align?: CanvasTextAlign;
    },
  ) {
    const lines = wrapLines(text, options.font, options.maxWidth, options.maxLines ?? 2);
    const fontSizeMatch = options.font.match(/(\d+)px/);
    const fontSize = fontSizeMatch ? Number(fontSizeMatch[1]) : 16;
    const lineHeight = options.lineHeight ?? Math.round(fontSize * 1.25);
    ctx.font = options.font;
    ctx.fillStyle = options.color;
    ctx.textAlign = options.align ?? "left";
    lines.forEach((line, index) => {
      ctx.fillText(line, x, y + index * lineHeight);
    });
    return lines.length * lineHeight;
  }

  function drawChip(x: number, y: number, label: string, color: string) {
    ctx.font = `600 12px ${fonts.sans}`;
    const textWidth = ctx.measureText(label).width;
    const chipWidth = textWidth + 28;
    const chipHeight = 28;

    fillPanel(x, y, chipWidth, chipHeight, 999);
    ctx.save();
    roundRectPath(x, y, chipWidth, chipHeight, 999);
    ctx.clip();
    ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
    ctx.fillRect(x, y, chipWidth, chipHeight);
    ctx.restore();

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x + 12, y + chipHeight / 2, 4, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = colors.textStrong;
    ctx.fillText(label, x + 20, y + 7);
    return chipWidth;
  }

  function drawStatCard(stat: { label: string; value: string; meta: string }, x: number, y: number, w: number, h: number, accent: string) {
    fillPanel(x, y, w, h, 24);

    ctx.fillStyle = accent;
    roundRectPath(x, y, w, 6, 3);
    ctx.fill();

    drawTextBlock(stat.label.toUpperCase(), x + 18, y + 18, {
      font: `600 12px ${fonts.mono}`,
      color: colors.muted,
      maxWidth: w - 36,
      maxLines: 1,
      lineHeight: 16,
    });

    ctx.font = `700 38px ${fonts.sans}`;
    ctx.fillStyle = colors.textStrong;
    ctx.fillText(stat.value, x + 18, y + 48);

    drawTextBlock(stat.meta, x + 18, y + 98, {
      font: `500 13px ${fonts.sans}`,
      color: colors.muted,
      maxWidth: w - 36,
      maxLines: 2,
      lineHeight: 17,
    });
  }

  function drawHighlightCard(item: { label: string; value: string; meta: string }, x: number, y: number, w: number, h: number, accent: string) {
    fillPanel(x, y, w, h, 22);

    ctx.fillStyle = accent;
    roundRectPath(x, y, w, 5, 2.5);
    ctx.fill();

    drawTextBlock(item.label, x + 16, y + 16, {
      font: `600 13px ${fonts.mono}`,
      color: colors.muted,
      maxWidth: w - 32,
      maxLines: 1,
      lineHeight: 16,
    });

    ctx.font = `700 28px ${fonts.sans}`;
    ctx.fillStyle = colors.textStrong;
    ctx.fillText(item.value, x + 16, y + 44);

    drawTextBlock(item.meta, x + 16, y + 82, {
      font: `500 12px ${fonts.sans}`,
      color: colors.muted,
      maxWidth: w - 32,
      maxLines: 2,
      lineHeight: 16,
    });
  }

  function drawTwoSeriesChartCard(
    x: number,
    y: number,
    w: number,
    h: number,
    title: string,
    eyebrow: string,
    seriesA: number[],
    seriesB: number[],
    labels: string[],
    legendA: { label: string; color: string },
    legendB: { label: string; color: string },
    statusText: string,
  ) {
    fillPanel(x, y, w, h, 26);

    drawTextBlock(eyebrow, x + 20, y + 18, {
      font: `600 12px ${fonts.mono}`,
      color: colors.muted,
      maxWidth: w - 260,
      maxLines: 1,
      lineHeight: 16,
    });

    drawTextBlock(title, x + 20, y + 40, {
      font: `700 22px ${fonts.sans}`,
      color: colors.textStrong,
      maxWidth: w - 260,
      maxLines: 2,
      lineHeight: 24,
    });

    drawChip(x + w - 238, y + 18, legendA.label, legendA.color);
    drawChip(x + w - 124, y + 18, legendB.label, legendB.color);

    const plotLeft = x + 20;
    const plotTop = y + 102;
    const plotRight = x + w - 20;
    const plotBottom = y + h - 74;
    const plotWidth = plotRight - plotLeft;
    const plotHeight = plotBottom - plotTop;
    const maxValue = Math.max(1, ...seriesA, ...seriesB);
    const count = Math.max(1, labels.length);
    const cellWidth = plotWidth / count;
    const barWidth = Math.max(8, Math.min(18, cellWidth * 0.22));
    const pairGap = Math.max(4, Math.min(10, cellWidth * 0.08));

    ctx.strokeStyle = "rgba(87, 58, 34, 0.10)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plotLeft, plotBottom);
    ctx.lineTo(plotRight, plotBottom);
    ctx.stroke();

    if (!labels.length) {
      drawTextBlock("No monthly data available.", plotLeft + plotWidth / 2, plotTop + plotHeight / 2 - 10, {
        font: `500 14px ${fonts.sans}`,
        color: colors.muted,
        maxWidth: plotWidth,
        maxLines: 1,
        lineHeight: 18,
        align: "center",
      });
    } else {
      labels.forEach((label, index) => {
        const cellCenter = plotLeft + cellWidth * (index + 0.5);
        const leftBarHeight = Math.round((seriesA[index] / maxValue) * plotHeight);
        const rightBarHeight = Math.round((seriesB[index] / maxValue) * plotHeight);
        const leftBarX = Math.round(cellCenter - barWidth - pairGap / 2);
        const rightBarX = Math.round(cellCenter + pairGap / 2);
        const leftBarY = Math.round(plotBottom - leftBarHeight);
        const rightBarY = Math.round(plotBottom - rightBarHeight);

        ctx.fillStyle = legendA.color;
        roundRectPath(leftBarX, leftBarY, barWidth, Math.max(2, leftBarHeight), 8);
        ctx.fill();

        ctx.fillStyle = legendB.color;
        roundRectPath(rightBarX, rightBarY, barWidth, Math.max(2, rightBarHeight), 8);
        ctx.fill();

        if (leftBarHeight > 14) {
          ctx.fillStyle = colors.textStrong;
          ctx.font = `600 11px ${fonts.sans}`;
          ctx.fillText(String(seriesA[index]), leftBarX + barWidth / 2 - ctx.measureText(String(seriesA[index])).width / 2, leftBarY - 16);
        }

        if (rightBarHeight > 14) {
          ctx.fillStyle = colors.textStrong;
          ctx.font = `600 11px ${fonts.sans}`;
          ctx.fillText(String(seriesB[index]), rightBarX + barWidth / 2 - ctx.measureText(String(seriesB[index])).width / 2, rightBarY - 16);
        }

        ctx.fillStyle = colors.muted;
        ctx.font = `600 12px ${fonts.mono}`;
        const labelWidth = ctx.measureText(label).width;
        ctx.fillText(label, cellCenter - labelWidth / 2, plotBottom + 14);
      });
    }

    drawTextBlock(statusText, x + 20, y + h - 42, {
      font: `500 13px ${fonts.sans}`,
      color: colors.muted,
      maxWidth: w - 40,
      maxLines: 2,
      lineHeight: 17,
    });
  }

  function drawSingleSeriesChartCard(
    x: number,
    y: number,
    w: number,
    h: number,
    title: string,
    eyebrow: string,
    series: number[],
    labels: string[],
    legend: { label: string; color: string },
    statusText: string,
    formatValue: (value: number) => string,
  ) {
    fillPanel(x, y, w, h, 26);

    drawTextBlock(eyebrow, x + 20, y + 18, {
      font: `600 12px ${fonts.mono}`,
      color: colors.muted,
      maxWidth: w - 220,
      maxLines: 1,
      lineHeight: 16,
    });

    drawTextBlock(title, x + 20, y + 40, {
      font: `700 22px ${fonts.sans}`,
      color: colors.textStrong,
      maxWidth: w - 220,
      maxLines: 2,
      lineHeight: 24,
    });

    drawChip(x + w - 220, y + 18, legend.label, legend.color);

    const plotLeft = x + 20;
    const plotTop = y + 102;
    const plotRight = x + w - 20;
    const plotBottom = y + h - 74;
    const plotWidth = plotRight - plotLeft;
    const plotHeight = plotBottom - plotTop;
    const maxValue = Math.max(1, ...series);
    const count = Math.max(1, labels.length);
    const cellWidth = plotWidth / count;
    const barWidth = Math.max(10, Math.min(22, cellWidth * 0.26));

    ctx.strokeStyle = "rgba(87, 58, 34, 0.10)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plotLeft, plotBottom);
    ctx.lineTo(plotRight, plotBottom);
    ctx.stroke();

    if (!labels.length) {
      drawTextBlock("No monthly data available.", plotLeft + plotWidth / 2, plotTop + plotHeight / 2 - 10, {
        font: `500 14px ${fonts.sans}`,
        color: colors.muted,
        maxWidth: plotWidth,
        maxLines: 1,
        lineHeight: 18,
        align: "center",
      });
    } else {
      labels.forEach((label, index) => {
        const cellCenter = plotLeft + cellWidth * (index + 0.5);
        const barHeight = Math.round((series[index] / maxValue) * plotHeight);
        const barX = Math.round(cellCenter - barWidth / 2);
        const barY = Math.round(plotBottom - barHeight);

        ctx.fillStyle = legend.color;
        roundRectPath(barX, barY, barWidth, Math.max(2, barHeight), 8);
        ctx.fill();

        if (barHeight > 14) {
          ctx.fillStyle = colors.textStrong;
          ctx.font = `600 11px ${fonts.sans}`;
          const valueLabel = formatValue(series[index]);
          const valueWidth = ctx.measureText(valueLabel).width;
          ctx.fillText(valueLabel, cellCenter - valueWidth / 2, barY - 16);
        }

        ctx.fillStyle = colors.muted;
        ctx.font = `600 12px ${fonts.mono}`;
        const labelWidth = ctx.measureText(label).width;
        ctx.fillText(label, cellCenter - labelWidth / 2, plotBottom + 14);
      });
    }

    drawTextBlock(statusText, x + 20, y + h - 42, {
      font: `500 13px ${fonts.sans}`,
      color: colors.muted,
      maxWidth: w - 40,
      maxLines: 2,
      lineHeight: 17,
    });
  }

  const background = ctx.createLinearGradient(0, 0, width, totalHeight);
  background.addColorStop(0, colors.bgTop);
  background.addColorStop(0.5, colors.bgMiddle);
  background.addColorStop(1, colors.bgBottom);
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, totalHeight);

  const pinkGlow = ctx.createRadialGradient(width * 0.14, totalHeight * 0.12, 0, width * 0.14, totalHeight * 0.12, width * 0.26);
  pinkGlow.addColorStop(0, "rgba(244, 195, 247, 0.55)");
  pinkGlow.addColorStop(1, "rgba(244, 195, 247, 0)");
  ctx.fillStyle = pinkGlow;
  ctx.beginPath();
  ctx.arc(width * 0.14, totalHeight * 0.12, width * 0.26, 0, Math.PI * 2);
  ctx.fill();

  const goldGlow = ctx.createRadialGradient(width * 0.9, totalHeight * 0.82, 0, width * 0.9, totalHeight * 0.82, width * 0.28);
  goldGlow.addColorStop(0, "rgba(247, 239, 193, 0.58)");
  goldGlow.addColorStop(1, "rgba(247, 239, 193, 0)");
  ctx.fillStyle = goldGlow;
  ctx.beginPath();
  ctx.arc(width * 0.9, totalHeight * 0.82, width * 0.28, 0, Math.PI * 2);
  ctx.fill();

  const chartsY = padding;
  const monthlyLabels = data.monthly.map((item) => item.label);
  const matchBrandValues = data.monthly.map((item) => item.brand_matches);
  const matchDomainValues = data.monthly.map((item) => item.domain_matches);
  const activityTotalValues = data.monthly.map((item) => item.total_runs);
  const activityCombinedValues = data.monthly.map((item) => item.brand_matches + item.domain_matches);
  const spendValues = data.monthly.map((item) => item.spend_usd);

  drawTwoSeriesChartCard(
    padding,
    chartsY,
    chartCardWidth,
    chartCardHeight,
    "Brand and domain matches",
    `Last ${data.chartRangeMonths} months`,
    matchBrandValues,
    matchDomainValues,
    monthlyLabels,
    { label: "Brand matches", color: colors.accent },
    { label: "Domain matches", color: colors.accentStrong },
    `Showing matches for ${data.projectLabel}${data.isAdmin ? ` - ${data.userLabel}` : ""} over the last ${data.chartRangeMonths} months`,
  );

  drawTwoSeriesChartCard(
    padding + chartCardWidth + chartGap,
    chartsY,
    chartCardWidth,
    chartCardHeight,
    "Runs and total match load",
    `Last ${data.chartRangeMonths} months`,
    activityTotalValues,
    activityCombinedValues,
    monthlyLabels,
    { label: "Total runs", color: colors.gold },
    { label: "Brand + domain", color: colors.accent },
    `Showing activity for ${data.projectLabel}${data.isAdmin ? ` - ${data.userLabel}` : ""} over the last ${data.chartRangeMonths} months`,
  );

  if (data.isAdmin) {
    drawSingleSeriesChartCard(
      padding,
      chartsY + chartCardHeight + 20,
      innerWidth,
      spendCardHeight,
      "Spendings",
      `Last ${data.chartRangeMonths} months`,
      spendValues,
      monthlyLabels,
      { label: "Estimated spend", color: colors.gold },
      `Showing spend for ${data.projectLabel}${data.isAdmin ? ` - ${data.userLabel}` : ""} over the last ${data.chartRangeMonths} months`,
      (value) => formatUsd(value),
    );
  }

  const highlightsY = chartsY + chartsHeight + 28;
  fillPanel(padding, highlightsY, innerWidth, highlightGridHeight + 82, 28);

  ctx.fillStyle = colors.muted;
  ctx.font = `600 12px ${fonts.mono}`;
  ctx.fillText("Snapshot", padding + 22, highlightsY + 18);

  ctx.fillStyle = colors.textStrong;
  ctx.font = `700 22px ${fonts.sans}`;
  ctx.fillText(data.isAdmin ? "Selected scope summary" : "Selected project summary", padding + 22, highlightsY + 40);

  data.highlights.forEach((item, index) => {
    const col = index % highlightCols;
    const row = Math.floor(index / highlightCols);
    const cardWidth = (innerWidth - highlightGap * (highlightCols - 1)) / highlightCols;
    const x = padding + col * (cardWidth + highlightGap);
    const y = highlightsY + 82 + row * (highlightCardHeight + highlightGap);
    const accent = [colors.accent, colors.accentStrong, colors.gold][index % 3];
    drawHighlightCard(item, x, y, cardWidth, highlightCardHeight, accent);
  });

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((output) => {
      if (output) {
        resolve(output);
      } else {
        reject(new Error("Could not create the JPG screenshot."));
      }
    }, "image/jpeg", 0.95);
  });

  downloadBlob(blob, filename);
}

export default function App() {
  const specialAuthRoute = useMemo<SpecialAuthRoute | null>(() => {
    if (typeof window === "undefined") {
      return null;
    }
    return getSpecialAuthRoute(initialAuthRedirectHref || window.location.href);
  }, []);
  const authRedirectSnapshot = useMemo<AuthRedirectSnapshot>(() => {
    if (typeof window === "undefined") {
      return {
        type: null,
        tokenHash: null,
        errorCode: null,
        errorDescription: null,
        hasAccessToken: false,
        hasRefreshToken: false,
        hasCode: false,
      };
    }
    return readAuthRedirectSnapshot(initialAuthRedirectHref || window.location.href);
  }, []);
  const [page, setPage] = useState<Page>("overview");
  const [authMode, setAuthMode] = useState<AuthMode>("sign_in");
  const [authForm, setAuthForm] = useState<AuthForm>({ username: "", email: "", password: "" });
  const [sessionToken, setSessionToken] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [serviceRows, setServiceRows] = useState<ServiceRow[]>([createServiceRow()]);
  const [draftReady, setDraftReady] = useState(false);
  const [draftStatus, setDraftStatus] = useState("Autosave ready");
  const [historyData, setHistoryData] = useState<PaginatedResponse<ResultItem>>({ items: [], page: 1, page_size: 10, total: 0 });
  const [outputData, setOutputData] = useState<PaginatedResponse<ResultItem>>({ items: [], page: 1, page_size: 10, total: 0 });
  const [historyFilters, setHistoryFilters] = useState({ project: "", prompt: "", user: "", date_from: "", date_to: "", page: 1 });
  const [outputFilters, setOutputFilters] = useState({ project: "", prompt: "", page: 1 });
  const [selectedRunDetail, setSelectedRunDetail] = useState<RunDetail | null>(null);
  const [activeRunDetails, setActiveRunDetails] = useState<RunDetail[]>([]);
  const [activeRunIds, setActiveRunIds] = useState<string[]>([]);
  const [failedRuns, setFailedRuns] = useState<RunRecord[]>([]);
  const [overviewSummary, setOverviewSummary] = useState<OverviewSummary | null>(null);
  const [overviewProject, setOverviewProject] = useState("");
  const [overviewUserId, setOverviewUserId] = useState("");
  const [chartRangeMonths, setChartRangeMonths] = useState<6 | 12>(12);
  const [reportExportDialog, setReportExportDialog] = useState<ReportExportDialog | null>(null);
  const [historyForwardDialog, setHistoryForwardDialog] = useState<HistoryForwardDialog | null>(null);
  const [isHistoryForwardMode, setIsHistoryForwardMode] = useState(false);
  const [isHistoryInputMode, setIsHistoryInputMode] = useState(false);
  const [selectedHistoryRunIds, setSelectedHistoryRunIds] = useState<string[]>([]);
  const [startProjectDialog, setStartProjectDialog] = useState<StartProjectDialog | null>(null);
  const [isCsvImportOpen, setIsCsvImportOpen] = useState(false);
  const [isCsvDragging, setIsCsvDragging] = useState(false);
  const [isImportingCsv, setIsImportingCsv] = useState(false);
  const [csvImportError, setCsvImportError] = useState("");
  const [isAuthOpen, setIsAuthOpen] = useState(false);
  const [isProfileMenuOpen, setIsProfileMenuOpen] = useState(false);
  const [isStartingRun, setIsStartingRun] = useState(false);
  const [isStoppingRuns, setIsStoppingRuns] = useState(false);
  const [isContinuingRuns, setIsContinuingRuns] = useState(false);
  const [isRetryingFailedRuns, setIsRetryingFailedRuns] = useState(false);
  const [isLoadingDraft, setIsLoadingDraft] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [isLoadingOutputs, setIsLoadingOutputs] = useState(false);
  const [isLoadingOverview, setIsLoadingOverview] = useState(false);
  const [isExportingOverviewScreenshot, setIsExportingOverviewScreenshot] = useState(false);
  const [isLoadingRunDetail, setIsLoadingRunDetail] = useState(false);
  const [isPreparingReport, setIsPreparingReport] = useState<ReportKind | null>(null);
  const [isExportingHistory, setIsExportingHistory] = useState(false);
  const [isExportingOutputs, setIsExportingOutputs] = useState(false);
  const [isLoadingForwardUsers, setIsLoadingForwardUsers] = useState(false);
  const [isForwardingHistory, setIsForwardingHistory] = useState(false);
  const [isAuthSubmitting, setIsAuthSubmitting] = useState(false);
  const [isSendingPasswordReset, setIsSendingPasswordReset] = useState(false);
  const [authError, setAuthError] = useState("");
  const [authMessage, setAuthMessage] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [resetPasswordStatus, setResetPasswordStatus] = useState<ResetPasswordStatus>(specialAuthRoute === "reset-password" ? "loading" : "idle");
  const [resetPasswordMessage, setResetPasswordMessage] = useState("");
  const [resetPasswordForm, setResetPasswordForm] = useState({ password: "", confirmPassword: "" });
  const [isUpdatingPassword, setIsUpdatingPassword] = useState(false);
  const [authRouteUserEmail, setAuthRouteUserEmail] = useState<string | null>(null);
  const [validationWarning, setValidationWarning] = useState("");
  const [projectCreator, setProjectCreator] = useState<{ rowId: string; value: string } | null>(null);
  const [projectDuplicatePrompt, setProjectDuplicatePrompt] = useState<{ rowId: string; value: string; existingValue: string } | null>(null);
  const profileMenuRef = useRef<HTMLDivElement | null>(null);
  const overviewCaptureRef = useRef<HTMLDivElement | null>(null);
  const overviewLoadRequestIdRef = useRef(0);
  const csvFileInputRef = useRef<HTMLInputElement | null>(null);
  const lastSavedDraftRef = useRef(JSON.stringify([emptyDraft]));
  const serviceRowsDirtyRef = useRef(false);
  const serviceRowsRef = useRef(serviceRows);
  const serviceRowsRevisionRef = useRef(0);
  const lastSavedServiceRowsRevisionRef = useRef(0);

  const currentUsername = profile?.username ?? null;
  const currentUserEmail = (profile?.email ?? authForm.email.trim()) || null;
  const isAdmin = Boolean(profile?.is_admin);
  const visibleNavItems = navItems.filter((item) => {
    if (isAdmin && (item.key === "service" || item.key === "outputs")) {
      return false;
    }
    return true;
  });
  const profileLetter = currentUsername?.charAt(0).toUpperCase() || "?";
  const outputLocalDate = getLocalDateValue();
  const outputLocalDateLabel = formatLocalDateLabel(outputLocalDate);
  const projectOptions = useMemo(() => {
    return collectUniqueProjects([...(overviewSummary?.project_options || []), ...serviceRows.map((row) => row.project)]);
  }, [overviewSummary, serviceRows]);
  const overviewUserOptions = overviewSummary?.user_options || [];
  const selectedOverviewProjectLabel = overviewSummary?.selected_project || "All projects";
  const selectedOverviewUserLabel = overviewSummary?.selected_user_id
    ? overviewUserOptions.find((item) => item.user_id === overviewSummary.selected_user_id)?.username || "Selected user"
    : "All users";

  function replaceServiceRows(rows: ServiceRow[], options: { bumpRevision?: boolean; markSaved?: boolean } = {}) {
    if (options.bumpRevision) {
      serviceRowsRevisionRef.current += 1;
    }
    if (options.markSaved) {
      lastSavedServiceRowsRevisionRef.current = serviceRowsRevisionRef.current;
      serviceRowsDirtyRef.current = false;
    }
    serviceRowsRef.current = rows;
    setServiceRows(rows);
  }

  function updateServiceRows(updater: (current: ServiceRow[]) => ServiceRow[]) {
    const nextRows = updater(serviceRowsRef.current.length ? serviceRowsRef.current : serviceRows);
    serviceRowsRef.current = nextRows;
    setServiceRows(nextRows);
  }

  useEffect(() => {
    serviceRowsRef.current = serviceRows;
  }, [serviceRows]);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      try {
        const { data: { session } } = await supabase.auth.getSession();
        if (cancelled) return;
        setSessionToken(session?.access_token ?? null);
        if (session) {
          setAuthForm((current) => ({
            ...current,
            username: getPreferredUsername(session.user.email, String(session.user.user_metadata.username || current.username || "")),
            email: session.user.email || current.email || "",
          }));
          if (specialAuthRoute) {
            return;
          }
          await syncProfile(getPreferredUsername(session.user.email, String(session.user.user_metadata.username || "")), session.access_token, true);
        }
      } catch (error) {
        if (!cancelled) {
          setAuthError(error instanceof Error ? error.message : "Could not restore the saved session.");
        }
      }
    };

    void bootstrap();

    const { data: { subscription } } = supabase.auth.onAuthStateChange(async (_event, session) => {
      if (cancelled) return;
      setSessionToken(session?.access_token ?? null);
      if (!session) {
        if (specialAuthRoute) {
          return;
        }
        setProfile(null);
        replaceServiceRows([createServiceRow()], { bumpRevision: true, markSaved: true });
        setDraftReady(false);
        setDraftStatus("Autosave ready");
        lastSavedDraftRef.current = JSON.stringify([emptyDraft]);
        serviceRowsDirtyRef.current = false;
        setHistoryData({ items: [], page: 1, page_size: 10, total: 0 });
        setOutputData({ items: [], page: 1, page_size: 10, total: 0 });
        setSelectedRunDetail(null);
        setActiveRunDetails([]);
        setActiveRunIds([]);
        setFailedRuns([]);
        setOverviewSummary(null);
        setOverviewProject("");
        setOverviewUserId("");
        setReportExportDialog(null);
        setHistoryForwardDialog(null);
        setIsHistoryForwardMode(false);
        setIsHistoryInputMode(false);
        setSelectedHistoryRunIds([]);
        setStartProjectDialog(null);
        setIsCsvImportOpen(false);
        setIsCsvDragging(false);
        setIsImportingCsv(false);
        setCsvImportError("");
        setIsPreparingReport(null);
        setProjectCreator(null);
        setProjectDuplicatePrompt(null);
        return;
      }

      setAuthForm((current) => ({
        ...current,
        username: getPreferredUsername(session.user.email, String(session.user.user_metadata.username || current.username || "")),
        email: session.user.email || current.email || "",
        password: "",
      }));
      if (specialAuthRoute) {
        return;
      }
      try {
        await syncProfile(getPreferredUsername(session.user.email, String(session.user.user_metadata.username || "")), session.access_token, true);
      } catch (error) {
        setAuthError(error instanceof Error ? error.message : "Could not sync profile.");
      }
    });

    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, [specialAuthRoute]);

  useEffect(() => {
    if (specialAuthRoute) {
      return;
    }

    const flashState = readAuthFlashState();
    if (!flashState) {
      return;
    }

    setStatusMessage(flashState.message);
    if (flashState.openAuthModal) {
      openAuthModal("sign_in");
    }
  }, [specialAuthRoute]);

  useEffect(() => {
    if (!specialAuthRoute) {
      return;
    }

    let cancelled = false;
    let sawPasswordRecovery = authRedirectSnapshot.type === "recovery";

    const setResetPasswordError = (message: string) => {
      clearAuthFlowFlag(PASSWORD_RESET_FLOW_KEY);
      if (cancelled) return;
      setResetPasswordStatus("error");
      setResetPasswordMessage(message);
    };

    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "PASSWORD_RECOVERY") {
        sawPasswordRecovery = true;
        setAuthFlowFlag(PASSWORD_RESET_FLOW_KEY);
        if (cancelled) return;
        setAuthRouteUserEmail(session?.user.email ?? null);
        setResetPasswordStatus("ready");
        setResetPasswordMessage("");
      }
    });

    const initializeSpecialAuthRoute = async () => {
      if (!hasSupabaseConfig) {
        setResetPasswordError("Supabase Auth is not configured for this app.");
        return;
      }

      if (authRedirectSnapshot.errorCode || authRedirectSnapshot.errorDescription) {
        scrubAuthRedirectUrl();
        setResetPasswordError(getFriendlyPasswordResetLinkError(authRedirectSnapshot.errorDescription || authRedirectSnapshot.errorCode));
        return;
      }

      if (authRedirectSnapshot.tokenHash) {
        if (authRedirectSnapshot.type !== "recovery") {
          scrubAuthRedirectUrl();
          setResetPasswordError("This password reset link is not valid for this page.");
          return;
        }

        const { data, error } = await supabase.auth.verifyOtp({
          token_hash: authRedirectSnapshot.tokenHash,
          type: "recovery",
        });
        scrubAuthRedirectUrl();
        if (error || !data.session) {
          setResetPasswordError(getFriendlyPasswordResetLinkError(error?.message));
          return;
        }

        sawPasswordRecovery = true;
        setAuthFlowFlag(PASSWORD_RESET_FLOW_KEY);
        if (cancelled) return;
        setAuthRouteUserEmail(data.user?.email ?? null);
      }

      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        scrubAuthRedirectUrl();
        if (hasAuthRedirectAttempt(authRedirectSnapshot) || sawPasswordRecovery || hasAuthFlowFlag(PASSWORD_RESET_FLOW_KEY)) {
          setResetPasswordError("This password reset link is invalid or has expired. Request a new reset email and try again.");
        } else {
          setResetPasswordError("Open the password reset link from your email to choose a new password.");
        }
        return;
      }

      const { data: { user }, error } = await supabase.auth.getUser();
      if (error || !user) {
        setResetPasswordError(getFriendlyPasswordResetLinkError(error?.message));
        return;
      }

      scrubAuthRedirectUrl();
      if (cancelled) return;

      setAuthRouteUserEmail(user.email ?? null);

      setAuthFlowFlag(PASSWORD_RESET_FLOW_KEY);
      setResetPasswordStatus("ready");
      setResetPasswordMessage("");
    };

    void initializeSpecialAuthRoute();

    return () => {
      cancelled = true;
      subscription.unsubscribe();
    };
  }, [authRedirectSnapshot, specialAuthRoute]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (profileMenuRef.current && !profileMenuRef.current.contains(event.target as Node)) {
        setIsProfileMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    if (!profile || !sessionToken) return;
    void loadHistory(sessionToken);
  }, [profile, sessionToken, historyFilters.project, historyFilters.prompt, historyFilters.user, historyFilters.date_from, historyFilters.date_to, historyFilters.page]);

  useEffect(() => {
    if (!selectedHistoryRunIds.length) return;
    const visibleRunIds = new Set(historyData.items.map((item) => item.run_id));
    setSelectedHistoryRunIds((current) => current.filter((runId) => visibleRunIds.has(runId)));
  }, [historyData.items, selectedHistoryRunIds.length]);

  useEffect(() => {
    if (!profile || !sessionToken || profile.is_admin) return;
    void loadOutputs(sessionToken);
  }, [profile, sessionToken, outputFilters.project, outputFilters.prompt, outputFilters.page]);

  useEffect(() => {
    if (!profile || !sessionToken) return;
    void loadOverview(sessionToken);
  }, [profile, sessionToken, overviewProject, overviewUserId]);

  useEffect(() => {
    if (!profile || !sessionToken || profile.is_admin) return;
    void loadActiveRuns(sessionToken);
  }, [profile, sessionToken]);

  useEffect(() => {
    if (!profile || !sessionToken || profile.is_admin) return;
    void loadFailedRuns(sessionToken);
  }, [profile, sessionToken]);

  useEffect(() => {
    if (!draftReady || !profile || !sessionToken || profile.is_admin) return;

    const draftRows = normalizeDraftRows(serviceRows);
    const primaryDraft = draftRows[0] || toDraftForm(emptyDraft);
    const serializedDraft = JSON.stringify(draftRows);
    const saveRevision = serviceRowsRevisionRef.current;
    if (serializedDraft === lastSavedDraftRef.current) return;

    const timeoutId = window.setTimeout(async () => {
      try {
        if (serviceRowsRevisionRef.current !== saveRevision) {
          return;
        }
        await apiRequest("/api/drafts/current", {
          method: "PUT",
          token: sessionToken,
          body: {
            ...primaryDraft,
            rows: draftRows,
          },
        });
        if (serviceRowsRevisionRef.current === saveRevision) {
          lastSavedDraftRef.current = serializedDraft;
          lastSavedServiceRowsRevisionRef.current = saveRevision;
          serviceRowsDirtyRef.current = false;
        }
        preserveServiceInputViewport();
        setDraftStatus("Draft saved");
      } catch (error) {
        preserveServiceInputViewport();
        setDraftStatus(error instanceof Error ? `Draft save failed: ${error.message}` : "Draft save failed");
      }
    }, 1800);

    return () => window.clearTimeout(timeoutId);
  }, [serviceRows, draftReady, profile, sessionToken]);

  useEffect(() => {
    if (profile?.is_admin && (page === "service" || page === "outputs")) {
      setPage("overview");
    }
  }, [page, profile?.is_admin]);

  useEffect(() => {
    if (!isAdmin && overviewUserId) {
      setOverviewUserId("");
    }
  }, [isAdmin, overviewUserId]);

  useEffect(() => {
    if (!activeRunIds.length || !sessionToken) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const detailResults = await Promise.all(
          activeRunIds.map(async (runId) => apiRequest<RunDetail>(`/api/runs/${runId}`, { token: sessionToken })),
        );
        if (cancelled) return;

        const activeDetails = detailResults.filter((detail) => ["queued", "running", "stopped"].includes(detail.run.status));
        const completedOrFailed = detailResults.filter((detail) => ["completed", "failed"].includes(detail.run.status));
        const nextActiveIds = activeDetails.map((detail) => detail.run.id);

        setActiveRunDetails(activeDetails);
        if (nextActiveIds.length !== activeRunIds.length || nextActiveIds.some((runId, index) => runId !== activeRunIds[index])) {
          setActiveRunIds(nextActiveIds);
        }

        if (completedOrFailed.length) {
          await Promise.all([loadHistory(sessionToken), loadOutputs(sessionToken), loadOverview(sessionToken), loadFailedRuns(sessionToken)]);
          const failedMessages = completedOrFailed
            .filter((detail) => detail.run.status === "failed" && detail.run.error_messages)
            .map((detail) => detail.run.error_messages);
          if (failedMessages.length) {
            setStatusMessage(failedMessages.join(" | "));
          }
        }
      } catch (error) {
        if (!cancelled) {
          setStatusMessage(error instanceof Error ? error.message : "Could not refresh active runs.");
        }
      }
    };

    void poll();
    const intervalId = window.setInterval(() => {
      void poll();
    }, 4000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [activeRunIds, sessionToken]);

  async function syncProfile(username: string, token: string, silent = false) {
    const cleanedUsername = username.trim();
    if (!cleanedUsername) {
      return;
    }

    const profileResponse = await apiRequest<Profile>("/api/profile/upsert", {
      method: "POST",
      token,
      body: { username: cleanedUsername },
    });

    setProfile(profileResponse);
    setSessionToken(token);
    setAuthForm((current) => ({ ...current, username: profileResponse.username, email: profileResponse.email || current.email, password: "" }));
    setAuthError("");
    setAuthMessage("");
    setStatusMessage("");
    setOverviewUserId("");
    setIsAuthOpen(false);

    if (profileResponse.is_admin) {
      replaceServiceRows([createServiceRow()], { bumpRevision: true, markSaved: true });
      setDraftReady(false);
      setDraftStatus("Admin account");
      lastSavedDraftRef.current = JSON.stringify([emptyDraft]);
      serviceRowsDirtyRef.current = false;
      setOutputData({ items: [], page: 1, page_size: 10, total: 0 });
      setSelectedRunDetail(null);
      setActiveRunDetails([]);
      setActiveRunIds([]);
      setFailedRuns([]);
      await Promise.all([loadHistory(token), loadOverview(token)]);
      setPage("overview");
      return;
    }

    await Promise.all([loadDraft(token), loadHistory(token), loadOutputs(token), loadOverview(token), loadActiveRuns(token), loadFailedRuns(token)]);
    if (!silent) {
      setPage("service");
    }
  }

  async function loadDraft(token = sessionToken) {
    if (!token) return;

    const loadStartedAtRevision = serviceRowsRevisionRef.current;
    const loadStartedAtSavedRevision = lastSavedServiceRowsRevisionRef.current;
    setIsLoadingDraft(true);
    try {
      const response = await apiRequest<DraftResponse>("/api/drafts/current", { token });
      const restoredRows = draftRowsFromResponse(response);
      if (
        !serviceRowsDirtyRef.current
        && serviceRowsRevisionRef.current === loadStartedAtRevision
        && lastSavedServiceRowsRevisionRef.current === loadStartedAtSavedRevision
        && lastSavedServiceRowsRevisionRef.current === loadStartedAtRevision
      ) {
        replaceServiceRows(restoredRows.map((row) => createServiceRow(row)), { markSaved: true });
        lastSavedDraftRef.current = JSON.stringify(restoredRows);
        serviceRowsDirtyRef.current = false;
        setDraftStatus("Draft restored");
      } else {
        setDraftStatus("Local rows kept");
      }
      setDraftReady(true);
    } catch (error) {
      setDraftReady(true);
      setDraftStatus(error instanceof Error ? error.message : "Could not load draft");
    } finally {
      setIsLoadingDraft(false);
    }
  }

  async function loadHistory(token = sessionToken) {
    if (!token) return;

    setIsLoadingHistory(true);
    try {
      const response = await apiRequest<PaginatedResponse<ResultItem>>("/api/history", {
        token,
        query: {
          project: historyFilters.project || undefined,
          prompt: historyFilters.prompt || undefined,
          user: profile?.is_admin ? historyFilters.user || undefined : undefined,
          date_from: historyFilters.date_from || undefined,
          date_to: historyFilters.date_to || undefined,
          page: historyFilters.page,
          page_size: historyData.page_size,
        },
      });
      setHistoryData(response);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load history.");
    } finally {
      setIsLoadingHistory(false);
    }
  }

  async function loadOutputs(token = sessionToken) {
    if (!token) return;

    setIsLoadingOutputs(true);
    try {
      const response = await apiRequest<PaginatedResponse<ResultItem>>("/api/outputs", {
        token,
        query: {
          project: outputFilters.project || undefined,
          prompt: outputFilters.prompt || undefined,
          local_date: outputLocalDate,
          tz_offset_minutes: new Date().getTimezoneOffset(),
          page: outputFilters.page,
          page_size: outputData.page_size,
        },
      });
      setOutputData(response);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load outputs.");
    } finally {
      setIsLoadingOutputs(false);
    }
  }

  async function loadOverview(token = sessionToken) {
    if (!token) return;

    const requestId = overviewLoadRequestIdRef.current + 1;
    overviewLoadRequestIdRef.current = requestId;
    setIsLoadingOverview(true);
    try {
      const response = await apiRequest<OverviewSummary>("/api/overview/summary", {
        token,
        query: {
          project: overviewProject || undefined,
          user_id: isAdmin ? overviewUserId || undefined : undefined,
        },
      });
      if (overviewLoadRequestIdRef.current === requestId) {
        setOverviewSummary(response);
      }
    } catch (error) {
      if (overviewLoadRequestIdRef.current === requestId) {
        setStatusMessage(error instanceof Error ? error.message : "Could not load overview stats.");
      }
    } finally {
      if (overviewLoadRequestIdRef.current === requestId) {
        setIsLoadingOverview(false);
      }
    }
  }

  async function loadActiveRuns(token = sessionToken) {
    if (!token) return;

    try {
      const response = await apiRequest<ActiveRunsResponse>("/api/runs/active", { token });
      setActiveRunIds(response.run_ids);
      if (!response.run_ids.length) {
        setActiveRunDetails([]);
      }
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load active runs.");
    }
  }

  async function loadFailedRuns(token = sessionToken) {
    if (!token) return;

    try {
      const response = await apiRequest<FailedRunsResponse>("/api/runs/failed", { token });
      setFailedRuns(response.items);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load failed runs.");
    }
  }

  async function openRunDetail(runId: string) {
    if (!sessionToken) return;

    setIsLoadingRunDetail(true);
    try {
      const detail = await apiRequest<RunDetail>(`/api/runs/${runId}`, { token: sessionToken });
      setSelectedRunDetail(detail);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load run detail.");
    } finally {
      setIsLoadingRunDetail(false);
    }
  }

  function startHistoryForwardMode() {
    setIsHistoryForwardMode(true);
    setIsHistoryInputMode(false);
    setSelectedHistoryRunIds([]);
    setStatusMessage("");
  }

  function cancelHistoryForwardMode() {
    setIsHistoryForwardMode(false);
    setIsHistoryInputMode(false);
    setSelectedHistoryRunIds([]);
    setHistoryForwardDialog(null);
  }

  function startHistoryInputMode() {
    setIsHistoryInputMode(true);
    setIsHistoryForwardMode(false);
    setHistoryForwardDialog(null);
    setSelectedHistoryRunIds([]);
    setStatusMessage("");
  }

  function toggleHistoryRunSelection(runId: string) {
    setSelectedHistoryRunIds((current) => (
      current.includes(runId)
        ? current.filter((item) => item !== runId)
        : [...current, runId]
    ));
  }

  async function openHistoryForwardDialog() {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }
    if (!selectedHistoryRunIds.length) {
      setValidationWarning("Select at least one history row to forward.");
      return;
    }

    setIsLoadingForwardUsers(true);
    setStatusMessage("");
    try {
      const response = await apiRequest<UserOptionsResponse>("/api/users/options", { token: sessionToken });
      const users = response.users.filter((user) => user.user_id !== profile?.user_id);
      setHistoryForwardDialog({
        users,
        selectedUserId: users[0]?.user_id || "",
      });
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load users.");
    } finally {
      setIsLoadingForwardUsers(false);
    }
  }

  async function confirmHistoryForward() {
    if (!sessionToken || !historyForwardDialog) {
      return;
    }
    if (!historyForwardDialog.selectedUserId) {
      setValidationWarning("Choose a user to forward selected rows to.");
      return;
    }

    setIsForwardingHistory(true);
    setStatusMessage("");
    try {
      const response = await apiRequest<HistoryForwardResponse>("/api/history/forward", {
        method: "POST",
        token: sessionToken,
        body: {
          run_ids: selectedHistoryRunIds,
          target_user_id: historyForwardDialog.selectedUserId,
        },
      });
      const targetUser = historyForwardDialog.users.find((user) => user.user_id === response.target_user_id);
      setHistoryForwardDialog(null);
      setIsHistoryForwardMode(false);
      setIsHistoryInputMode(false);
      setSelectedHistoryRunIds([]);
      await Promise.all([loadHistory(sessionToken), loadOverview(sessionToken)]);
      setStatusMessage(
        `${response.total_runs} history row${response.total_runs === 1 ? "" : "s"} forwarded to ${targetUser?.username || "selected user"}.`,
      );
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not forward selected rows.");
    } finally {
      setIsForwardingHistory(false);
    }
  }

  function addSelectedHistoryRowsToInputs() {
    if (!selectedHistoryRunIds.length) {
      setValidationWarning("Select at least one history row to add to inputs.");
      return;
    }

    const selectedIds = new Set(selectedHistoryRunIds);
    const rowsToAdd = historyData.items
      .filter((item) => selectedIds.has(item.run_id))
      .map((item) => createServiceRow({
        keyword: item.keyword,
        domain: item.domain,
        brand: item.brand,
        prompt: item.prompt,
        project: item.project || "",
      }));

    if (!rowsToAdd.length) {
      setValidationWarning("Selected rows are not visible anymore. Select rows again.");
      return;
    }

    serviceRowsDirtyRef.current = true;
    serviceRowsRevisionRef.current += 1;
    updateServiceRows((current) => {
      const hasExistingInput = current.some((row) => hasAnyRowValue(toDraftForm(row)));
      return hasExistingInput ? [...current, ...rowsToAdd] : rowsToAdd;
    });
    setIsHistoryInputMode(false);
    setSelectedHistoryRunIds([]);
    setPage("service");
    setStatusMessage(`${rowsToAdd.length} row${rowsToAdd.length === 1 ? "" : "s"} added to inputs.`);
  }

  function updateAuthField(field: keyof AuthForm, value: string) {
    setAuthForm((current) => ({ ...current, [field]: value }));
  }

  function openAuthModal(mode: AuthMode) {
    setAuthMode(mode);
    setAuthError("");
    setAuthMessage("");
    setIsAuthOpen(true);
  }

  function goToDashboardHome() {
    if (typeof window !== "undefined") {
      window.location.assign("/");
    }
  }

  function updateResetPasswordField(field: "password" | "confirmPassword", value: string) {
    setResetPasswordForm((current) => ({ ...current, [field]: value }));
    if (resetPasswordStatus === "ready" && resetPasswordMessage) {
      setResetPasswordMessage("");
    }
  }

  async function sendPasswordResetEmail() {
    if (!hasSupabaseConfig) {
      setAuthError("Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY.");
      return;
    }

    const email = authForm.email.trim();
    if (!email) {
      setAuthError("Enter your email address first.");
      return;
    }

    setIsSendingPasswordReset(true);
    setAuthError("");
    setAuthMessage("");

    try {
      const { error } = await supabase.auth.resetPasswordForEmail(email, {
        redirectTo: PASSWORD_RESET_REDIRECT_URL,
      });
      if (error) {
        throw error;
      }

      setAuthMessage("If that email exists, a password reset link has been sent.");
    } catch {
      setAuthError("We could not send a reset email right now. Please try again.");
    } finally {
      setIsSendingPasswordReset(false);
    }
  }

  async function submitResetPassword() {
    if (resetPasswordStatus !== "ready" || isUpdatingPassword) {
      return;
    }

    const validationMessage = validateNewPassword(resetPasswordForm.password, resetPasswordForm.confirmPassword);
    if (validationMessage) {
      setResetPasswordMessage(validationMessage);
      return;
    }

    setIsUpdatingPassword(true);
    setResetPasswordMessage("");

    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        throw new Error("Auth session missing.");
      }

      const { error } = await supabase.auth.updateUser({
        password: resetPasswordForm.password,
      });
      if (error) {
        throw error;
      }

      clearAuthFlowFlag(PASSWORD_RESET_FLOW_KEY);
      setResetPasswordForm({ password: "", confirmPassword: "" });
      setResetPasswordStatus("success");
      setResetPasswordMessage("Password updated successfully. Redirecting you to sign in...");
      await supabase.auth.signOut();
      window.setTimeout(() => {
        writeAuthFlashState({
          message: "Password updated. Sign in with your new password.",
          openAuthModal: true,
        });
        goToDashboardHome();
      }, 900);
    } catch (error) {
      setResetPasswordMessage(getFriendlyPasswordUpdateError(error));
    } finally {
      setIsUpdatingPassword(false);
    }
  }

  async function submitAuth() {
    if (!hasSupabaseConfig) {
      setAuthError("Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY.");
      return;
    }

    const email = authForm.email.trim();
    const password = authForm.password;
    const username = authForm.username.trim();

    if (!email) {
      setAuthError("Email is required.");
      return;
    }
    if (!password) {
      setAuthError("Password is required.");
      return;
    }
    if (authMode === "sign_up" && !username) {
      setAuthError("Username is required.");
      return;
    }

    setIsAuthSubmitting(true);
    setAuthError("");
    setAuthMessage("");

    try {
      if (authMode === "sign_up") {
        const { data, error } = await supabase.auth.signUp({
          email,
          password,
          options: {
            data: { username },
          },
        });
        if (error) {
          throw error;
        }

        setAuthForm((current) => ({ ...current, email, username }));
        if (data.session) {
          await syncProfile(username, data.session.access_token);
        } else {
          setAuthMessage("Account created. Confirm your email, then sign in.");
          setAuthMode("sign_in");
        }
      } else {
        const { data, error } = await supabase.auth.signInWithPassword({
          email,
          password,
        });
        if (error || !data.session) {
          throw error || new Error("Supabase did not return a session.");
        }

        const sessionUsername = getPreferredUsername(data.user.email, String(data.user.user_metadata.username || username || ""));
        await syncProfile(sessionUsername, data.session.access_token);
      }
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "Authentication failed.");
    } finally {
      setIsAuthSubmitting(false);
      setAuthForm((current) => ({ ...current, password: "" }));
    }
  }

  async function logout() {
    await supabase.auth.signOut();
    setProfile(null);
    setSessionToken(null);
    setAuthForm({ username: "", email: "", password: "" });
    setAuthError("");
    setAuthMessage("");
    replaceServiceRows([createServiceRow()], { bumpRevision: true, markSaved: true });
    setDraftReady(false);
    setDraftStatus("Autosave ready");
    lastSavedDraftRef.current = JSON.stringify([emptyDraft]);
    serviceRowsDirtyRef.current = false;
    setHistoryData({ items: [], page: 1, page_size: 10, total: 0 });
    setOutputData({ items: [], page: 1, page_size: 10, total: 0 });
    setSelectedRunDetail(null);
    setActiveRunDetails([]);
    setActiveRunIds([]);
    setFailedRuns([]);
    setOverviewSummary(null);
    setOverviewProject("");
    setOverviewUserId("");
    setReportExportDialog(null);
    setHistoryForwardDialog(null);
    setIsHistoryForwardMode(false);
    setIsHistoryInputMode(false);
    setSelectedHistoryRunIds([]);
    setStartProjectDialog(null);
    setIsCsvImportOpen(false);
    setIsCsvDragging(false);
    setIsImportingCsv(false);
    setCsvImportError("");
    setIsPreparingReport(null);
    setProjectCreator(null);
    setProjectDuplicatePrompt(null);
    setStatusMessage("");
    setIsProfileMenuOpen(false);
    setPage("overview");
  }

  function updateServiceRow(rowId: string, field: keyof DraftForm, value: string) {
    serviceRowsDirtyRef.current = true;
    serviceRowsRevisionRef.current += 1;
    updateServiceRows((current) => current.map((row) => (row.id === rowId ? { ...row, [field]: value } : row)));
  }

  function addServiceRow() {
    serviceRowsDirtyRef.current = true;
    serviceRowsRevisionRef.current += 1;
    updateServiceRows((current) => [...current, createServiceRow()]);
  }

  function duplicateServiceRow() {
    serviceRowsDirtyRef.current = true;
    serviceRowsRevisionRef.current += 1;
    updateServiceRows((current) => {
      const source = current[current.length - 1] || createServiceRow();
      return [...current, createServiceRow(toDraftForm(source))];
    });
  }

  function deleteServiceRow(rowId: string) {
    serviceRowsDirtyRef.current = true;
    serviceRowsRevisionRef.current += 1;
    updateServiceRows((current) => {
      if (current.length === 1) {
        return [createServiceRow()];
      }
      return current.filter((row) => row.id !== rowId);
    });
  }

  function openProjectCreator(rowId: string, value = "") {
    setProjectCreator({ rowId, value });
  }

  function closeProjectCreator() {
    setProjectCreator(null);
  }

  function commitProjectSelection(rowId: string, value: string) {
    updateServiceRow(rowId, "project", value);
    setProjectCreator(null);
    setProjectDuplicatePrompt(null);
  }

  function submitProjectCreator() {
    if (!projectCreator) {
      return;
    }

    const candidate = projectCreator.value.trim();
    if (!candidate) {
      setValidationWarning("Project name is required.");
      return;
    }

    const exactMatch = projectOptions.find((project) => project === candidate);
    if (exactMatch) {
      commitProjectSelection(projectCreator.rowId, exactMatch);
      return;
    }

    const candidateKey = normalizeProjectName(candidate);
    if (!candidateKey) {
      setValidationWarning("Project name must include letters or numbers.");
      return;
    }

    const similarProject = projectOptions.find((project) => normalizeProjectName(project) === candidateKey);
    if (similarProject) {
      setProjectCreator(null);
      setProjectDuplicatePrompt({
        rowId: projectCreator.rowId,
        value: candidate,
        existingValue: similarProject,
      });
      return;
    }

    commitProjectSelection(projectCreator.rowId, candidate);
  }

  function resolveDuplicateProjectChoice(shouldContinue: boolean) {
    if (!projectDuplicatePrompt) {
      return;
    }

    const pendingProject = projectDuplicatePrompt;
    setProjectDuplicatePrompt(null);
    if (shouldContinue) {
      commitProjectSelection(pendingProject.rowId, pendingProject.value);
      return;
    }

    openProjectCreator(pendingProject.rowId, pendingProject.value);
  }

  function handleProjectSelectChange(rowId: string, value: string) {
    if (value === ADD_PROJECT_OPTION_VALUE) {
      openProjectCreator(rowId);
      return;
    }

    updateServiceRow(rowId, "project", value);
  }

  async function openReportExportDialogFor(kind: ReportKind) {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    setStatusMessage("");
    setIsPreparingReport(kind);

    try {
      const response = await apiRequest<ProjectListResponse>("/api/projects", { token: sessionToken });
      setReportExportDialog({
        kind,
        projects: response.projects,
        selectedProjects: [...response.projects],
      });
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not load projects for report export.");
    } finally {
      setIsPreparingReport(null);
    }
  }

  function toggleReportProject(project: string) {
    setReportExportDialog((current) => {
      if (!current) {
        return current;
      }
      const selected = new Set(current.selectedProjects);
      if (selected.has(project)) {
        selected.delete(project);
      } else {
        selected.add(project);
      }
      return {
        ...current,
        selectedProjects: current.projects.filter((item) => selected.has(item)),
      };
    });
  }

  function selectAllReportProjects() {
    setReportExportDialog((current) => (current ? { ...current, selectedProjects: [...current.projects] } : current));
  }

  function clearReportProjects() {
    setReportExportDialog((current) => (current ? { ...current, selectedProjects: [] } : current));
  }

  function toggleStartProjectSelection(project: string) {
    setStartProjectDialog((current) => {
      if (!current) {
        return current;
      }
      const cleanedProject = project.trim();
      const projectRowIds = current.rows
        .filter((item) => item.row.project.trim() === cleanedProject)
        .map((item) => item.id);
      if (!projectRowIds.length) {
        return current;
      }

      const selected = new Set(current.selectedRowIds);
      const allSelected = projectRowIds.every((rowId) => selected.has(rowId));
      if (allSelected) {
        projectRowIds.forEach((rowId) => selected.delete(rowId));
      } else {
        projectRowIds.forEach((rowId) => selected.add(rowId));
      }

      return {
        ...current,
        selectedRowIds: current.rows.filter((item) => selected.has(item.id)).map((item) => item.id),
      };
    });
  }

  function toggleStartProjectRowSelection(rowId: string) {
    setStartProjectDialog((current) => {
      if (!current) {
        return current;
      }
      const selected = new Set(current.selectedRowIds);
      if (selected.has(rowId)) {
        selected.delete(rowId);
      } else {
        selected.add(rowId);
      }
      return {
        ...current,
        selectedRowIds: current.rows.filter((item) => selected.has(item.id)).map((item) => item.id),
      };
    });
  }

  function selectAllStartProjects() {
    setStartProjectDialog((current) => (current ? { ...current, selectedRowIds: current.rows.map((item) => item.id) } : current));
  }

  function clearStartProjects() {
    setStartProjectDialog((current) => (current ? { ...current, selectedRowIds: [] } : current));
  }

  function openCsvImportModal() {
    setCsvImportError("");
    setIsCsvDragging(false);
    setIsCsvImportOpen(true);
    if (csvFileInputRef.current) {
      csvFileInputRef.current.value = "";
    }
  }

  function closeCsvImportModal() {
    setIsCsvImportOpen(false);
    setIsCsvDragging(false);
    setIsImportingCsv(false);
    setCsvImportError("");
    if (csvFileInputRef.current) {
      csvFileInputRef.current.value = "";
    }
  }

  async function importCsvFile(file: File | null | undefined) {
    if (!file) {
      return;
    }

    if (!file.name.toLowerCase().endsWith(".csv")) {
      setCsvImportError("Please choose a CSV file.");
      return;
    }

    setIsImportingCsv(true);
    setCsvImportError("");

    try {
      const importedRows = parseServiceRowsCsv(await file.text());
      serviceRowsDirtyRef.current = true;
      serviceRowsRevisionRef.current += 1;
      const importRevision = serviceRowsRevisionRef.current;

      if (sessionToken && profile && !profile.is_admin) {
        const response = await apiRequest<DraftResponse>("/api/drafts/current/append", {
          method: "POST",
          token: sessionToken,
          body: { rows: importedRows },
        });
        const nextDraftRows = draftRowsFromResponse(response);
        replaceServiceRows(nextDraftRows.map((row) => createServiceRow(row)));
        if (serviceRowsRevisionRef.current === importRevision) {
          lastSavedDraftRef.current = JSON.stringify(nextDraftRows);
          lastSavedServiceRowsRevisionRef.current = importRevision;
          serviceRowsDirtyRef.current = false;
        }
        setDraftStatus("Draft saved");
      } else {
        const nextDraftRows = normalizeDraftRows([...normalizeDraftRows(serviceRowsRef.current), ...importedRows]);
        replaceServiceRows(nextDraftRows.map((row) => createServiceRow(row)));
      }

      closeCsvImportModal();
      setStatusMessage(`${importedRows.length} rows appended from CSV.`);
    } catch (error) {
      setCsvImportError(error instanceof Error ? error.message : "Could not import CSV file.");
      setIsImportingCsv(false);
    } finally {
      if (csvFileInputRef.current) {
        csvFileInputRef.current.value = "";
      }
    }
  }

  async function queueServiceRows(candidateRows: PendingServiceRow[], token: string) {
    setIsStartingRun(true);
    setStatusMessage("");
    const queuedRunIds: string[] = [];

    try {
      for (const item of candidateRows) {
        const response = await apiRequest<{ run_id: string; status: string }>("/api/runs/start", {
          method: "POST",
          token,
          body: item.row,
        });
        queuedRunIds.push(response.run_id);
      }

      if (queuedRunIds.length) {
        setActiveRunIds((current) => [...new Set([...current, ...queuedRunIds])]);
      }
      setPage("outputs");
      await Promise.all([loadHistory(token), loadOutputs(token), loadOverview(token)]);
      setStatusMessage(`${queuedRunIds.length} runs queued.`);
    } catch (error) {
      if (queuedRunIds.length) {
        setActiveRunIds((current) => [...new Set([...current, ...queuedRunIds])]);
        setPage("outputs");
        await Promise.all([loadHistory(token), loadOutputs(token), loadOverview(token)]);
        setStatusMessage(`${queuedRunIds.length} runs queued before the batch stopped: ${error instanceof Error ? error.message : "Unknown error."}`);
      } else {
        setValidationWarning(error instanceof Error ? error.message : "Could not start the batch.");
      }
    } finally {
      setIsStartingRun(false);
    }
  }

  async function exportReport(kind: ReportKind, selectedProjects: string[]) {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return false;
    }

    const setBusy = kind === "history" ? setIsExportingHistory : setIsExportingOutputs;
    setBusy(true);
    setStatusMessage("");

    try {
      const endpoint = kind === "history" ? "/api/history" : "/api/outputs";
      const baseQuery = kind === "history"
        ? {
          project: historyFilters.project || undefined,
          prompt: historyFilters.prompt || undefined,
          user: isAdmin ? historyFilters.user || undefined : undefined,
          date_from: historyFilters.date_from || undefined,
          date_to: historyFilters.date_to || undefined,
        }
        : {
          project: outputFilters.project || undefined,
          prompt: outputFilters.prompt || undefined,
          local_date: outputLocalDate,
          tz_offset_minutes: new Date().getTimezoneOffset(),
        };

      const pageSize = 100;
      let pageNumber = 1;
      let total = 0;
      const items: ResultItem[] = [];

      do {
        const response = await apiRequest<PaginatedResponse<ResultItem>>(endpoint, {
          token: sessionToken,
          query: {
            ...baseQuery,
            page: pageNumber,
            page_size: pageSize,
          },
        });
        total = response.total;
        items.push(...response.items);
        pageNumber += 1;
      } while (items.length < total);

      const filteredItems = selectedProjects.length
        ? items.filter((item) => item.project && selectedProjects.includes(item.project))
        : items;

      downloadCsv(
        `${kind}-report-${new Date().toISOString().slice(0, 10)}.csv`,
        ["Created", "Project", "Keyword", "Prompt", "Mentions", "Response Avg", "Citation", "Sentiment"],
        filteredItems.map((item) => [
          formatCsvDate(item.created_at),
          item.project || "",
          item.keyword,
          item.prompt || "",
          summarizeMentions(item),
          item.response_count_avg === null || item.response_count_avg === undefined ? "" : String(item.response_count_avg),
          item.citation_format || "",
          item.sentiment_analysis || "",
        ]),
      );
      setStatusMessage(`${filteredItems.length} rows exported from ${kind}.`);
      return true;
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : `Could not export ${kind}.`);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function confirmReportExport() {
    if (!reportExportDialog) {
      return;
    }

    const didExport = await exportReport(reportExportDialog.kind, reportExportDialog.selectedProjects);
    if (didExport) {
      setReportExportDialog(null);
    }
  }

  async function captureOverviewScreenshot() {
    if (!overviewCaptureRef.current) {
      setStatusMessage("Overview screenshot is not ready yet.");
      return;
    }

    setIsExportingOverviewScreenshot(true);
    setStatusMessage("");

    try {
      await captureOverviewScreenshotJpeg(
        {
          width: overviewCaptureRef.current.getBoundingClientRect().width,
          isAdmin: overviewSummary?.is_admin ?? false,
          projectLabel: selectedOverviewProjectLabel,
          userLabel: selectedOverviewUserLabel,
          chartRangeMonths,
          highlights: overviewHighlights,
          monthly: visibleMonthly,
        },
        `overview-${new Date().toISOString().slice(0, 10)}.jpg`,
      );
      setStatusMessage("Overview JPG downloaded.");
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not save the overview screenshot.");
    } finally {
      setIsExportingOverviewScreenshot(false);
    }
  }

  async function confirmStartProjectSelection() {
    if (!startProjectDialog) {
      return;
    }
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    const selectedRowsById = new Set(startProjectDialog.selectedRowIds);
    const selectedRows = startProjectDialog.rows.filter(({ id }) => selectedRowsById.has(id));

    if (!selectedRows.length) {
      setValidationWarning("Select at least one row to continue.");
      return;
    }

    setStartProjectDialog(null);
    await queueServiceRows(selectedRows, sessionToken);
  }

  async function handleStartService() {
    if (!profile || !sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    const candidateRows = serviceRows
      .map((row, index) => ({ id: row.id, row: toDraftForm(row), index: index + 1 }))
      .filter(({ row }) => hasAnyRowValue(row));

    if (!candidateRows.length) {
      setValidationWarning("Add at least one filled row before starting the batch.");
      return;
    }

    for (const item of candidateRows) {
      const missingFields = ["keyword", "domain", "brand", "prompt"].filter(
        (field) => !item.row[field as keyof DraftForm].trim(),
      );
      if (missingFields.length) {
        setValidationWarning(`Row ${item.index} is missing ${missingFields.join(", ")}.`);
        return;
      }
    }

    if (candidateRows.length > 1) {
      setStartProjectDialog({
        rows: candidateRows,
        selectedRowIds: candidateRows.map((item) => item.id),
      });
      return;
    }

    await queueServiceRows(candidateRows, sessionToken);
  }

  async function handleStopRuns() {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    setIsStoppingRuns(true);
    setStatusMessage("");
    try {
      const response = await apiRequest<BulkRunActionResponse>("/api/runs/stop", {
        method: "POST",
        token: sessionToken,
      });
      setActiveRunIds(response.run_ids);
      await Promise.all([loadActiveRuns(sessionToken), loadOverview(sessionToken)]);
      setStatusMessage(response.total_runs ? `${response.total_runs} runs stopped.` : "No queued, running, or stopped runs found.");
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not stop runs.");
    } finally {
      setIsStoppingRuns(false);
    }
  }

  async function handleContinueRuns() {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    setIsContinuingRuns(true);
    setStatusMessage("");
    try {
      const response = await apiRequest<BulkRunActionResponse>("/api/runs/continue", {
        method: "POST",
        token: sessionToken,
      });
      setActiveRunIds(response.run_ids);
      await Promise.all([loadActiveRuns(sessionToken), loadHistory(sessionToken), loadOutputs(sessionToken), loadOverview(sessionToken)]);
      setStatusMessage(
        response.total_runs
          ? `${response.total_runs} runs re-queued. Previous incomplete outputs for your user were cleared.`
          : "No queued, running, or stopped runs found.",
      );
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not continue runs.");
    } finally {
      setIsContinuingRuns(false);
    }
  }

  async function handleRetryFailedRuns() {
    if (!sessionToken) {
      openAuthModal("sign_in");
      return;
    }

    setIsRetryingFailedRuns(true);
    setStatusMessage("");
    try {
      const response = await apiRequest<BulkRunActionResponse>("/api/runs/retry-failed", {
        method: "POST",
        token: sessionToken,
      });
      setFailedRuns([]);
      setActiveRunIds((current) => [...new Set([...current, ...response.run_ids])]);
      await Promise.all([loadActiveRuns(sessionToken), loadFailedRuns(sessionToken), loadHistory(sessionToken), loadOutputs(sessionToken), loadOverview(sessionToken)]);
      setStatusMessage(
        response.total_runs
          ? `${response.total_runs} failed runs were re-queued. Previous partial outputs for those failed runs were cleared.`
          : "No failed runs found.",
      );
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not retry failed runs.");
    } finally {
      setIsRetryingFailedRuns(false);
    }
  }

  const stats = useMemo(() => {
    const globalLastMonth = overviewSummary?.stats.global_last_month;
    const monthly = overviewSummary?.monthly || [];
    const rangeMonthly = chartRangeMonths === 6 ? monthly.slice(-6) : monthly;
    const rangeStats = {
      totalResults: rangeMonthly.reduce((sum, item) => sum + item.total_runs, 0),
      brandMatches: rangeMonthly.reduce((sum, item) => sum + item.brand_matches, 0),
      domainMatches: rangeMonthly.reduce((sum, item) => sum + item.domain_matches, 0),
      spendUsd: rangeMonthly.reduce((sum, item) => sum + item.spend_usd, 0),
    };
    const runningCount = activeRunDetails.filter((detail) => detail.run.status === "running").length;
    const queuedCount = activeRunDetails.filter((detail) => detail.run.status === "queued").length;
    const stoppedCount = activeRunDetails.filter((detail) => detail.run.status === "stopped").length;
    const trackedRunCount = activeRunDetails.length;
    const activeRunCount = Math.max(overviewSummary?.stats.user_active_runs ?? 0, trackedRunCount);
    const adminScopeLabel = selectedOverviewUserLabel === "All users" ? "all users" : selectedOverviewUserLabel;

    if (overviewSummary?.is_admin) {
      return [
        { label: "All Results", value: String(rangeStats.totalResults), meta: `Last ${chartRangeMonths} months for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        { label: "All Brand Matches", value: String(rangeStats.brandMatches), meta: `Last ${chartRangeMonths} months for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        { label: "All Domain Matches", value: String(rangeStats.domainMatches), meta: `Last ${chartRangeMonths} months for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        { label: `${chartRangeMonths}M Spend`, value: formatUsd(rangeStats.spendUsd), meta: `Last ${chartRangeMonths} months for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        {
          label: "Active Runs",
          value: String(activeRunCount),
          meta: activeRunCount
            ? `${activeRunCount} queued or running for ${adminScopeLabel}`
            : `No queued or running runs for ${adminScopeLabel}`,
        },
        { label: "30D Results", value: String(globalLastMonth?.total_results ?? 0), meta: `Last 30 days for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        { label: "30D Users", value: String(globalLastMonth?.users ?? 0), meta: `Last 30 days for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
        { label: "30D Spend", value: formatUsd(globalLastMonth?.spend_usd), meta: `Last 30 days for ${adminScopeLabel} on ${selectedOverviewProjectLabel}` },
      ];
    }

    return [
      { label: "My Results", value: String(rangeStats.totalResults), meta: `Last ${chartRangeMonths} months on ${selectedOverviewProjectLabel}` },
      { label: "My Brand Matches", value: String(rangeStats.brandMatches), meta: `Last ${chartRangeMonths} months on ${selectedOverviewProjectLabel}` },
      { label: "My Domain Matches", value: String(rangeStats.domainMatches), meta: `Last ${chartRangeMonths} months on ${selectedOverviewProjectLabel}` },
      {
        label: "Project Spend",
        value: formatUsd(rangeStats.spendUsd),
        meta: `Last ${chartRangeMonths} months on ${selectedOverviewProjectLabel}`,
      },
      { label: "All Results", value: String(globalLastMonth?.total_results ?? 0), meta: `Last 30 days on ${selectedOverviewProjectLabel}` },
      { label: "All Brand Matches", value: String(globalLastMonth?.brand_matches ?? 0), meta: `Last 30 days on ${selectedOverviewProjectLabel}` },
      { label: "All Domain Matches", value: String(globalLastMonth?.domain_matches ?? 0), meta: `Last 30 days on ${selectedOverviewProjectLabel}` },
      { label: "All Users", value: String(globalLastMonth?.users ?? 0), meta: `Last 30 days on ${selectedOverviewProjectLabel}` },
    ];
  }, [activeRunDetails, chartRangeMonths, overviewSummary, selectedOverviewProjectLabel, selectedOverviewUserLabel]);

  const visibleMonthly = useMemo(() => {
    const monthly = overviewSummary?.monthly || [];
    return chartRangeMonths === 6 ? monthly.slice(-6) : monthly;
  }, [chartRangeMonths, overviewSummary]);

  const chartRangeLabel = `${chartRangeMonths} Months`;
  const chartRangeShortLabel = `${chartRangeMonths}M`;
  const isRefreshingOverview = isLoadingOverview && overviewSummary !== null;

  const matchChartMax = useMemo(() => {
    const values = visibleMonthly.flatMap((item) => [item.brand_matches, item.domain_matches]);
    return Math.max(1, ...values);
  }, [visibleMonthly]);

  const activityChartMax = useMemo(() => {
    const values = visibleMonthly.flatMap((item) => [item.total_runs, item.brand_matches + item.domain_matches]);
    return Math.max(1, ...values);
  }, [visibleMonthly]);

  const spendChartMax = useMemo(() => {
    const values = visibleMonthly.map((item) => item.spend_usd);
    const maxValue = Math.max(0, ...values);
    return maxValue > 0 ? maxValue : 1;
  }, [visibleMonthly]);

  const overviewHighlights = useMemo(() => {
    const totalRuns = visibleMonthly.reduce((sum, item) => sum + item.total_runs, 0);
    const totalBrandMatches = visibleMonthly.reduce((sum, item) => sum + item.brand_matches, 0);
    const totalDomainMatches = visibleMonthly.reduce((sum, item) => sum + item.domain_matches, 0);
    const totalSpend = visibleMonthly.reduce((sum, item) => sum + item.spend_usd, 0);
    const bestMonth = visibleMonthly.reduce<(typeof visibleMonthly)[number] | null>(
      (best, item) => {
        const currentScore = item.total_runs + item.brand_matches + item.domain_matches;
        const bestScore = best ? best.total_runs + best.brand_matches + best.domain_matches : -1;
        return currentScore > bestScore ? item : best;
      },
      null,
    );
    const latestMonth = visibleMonthly[visibleMonthly.length - 1] || null;

    if (overviewSummary?.is_admin) {
      return [
        { label: "Scope", value: selectedOverviewUserLabel, meta: "Admin analytics access" },
        { label: "Project", value: selectedOverviewProjectLabel, meta: "Current overview filter" },
        { label: `${chartRangeShortLabel} total runs`, value: String(totalRuns), meta: "Across the visible period" },
        { label: `${chartRangeShortLabel} total spend`, value: formatUsd(totalSpend), meta: "Across the visible period" },
        { label: "Best month", value: bestMonth?.label || "-", meta: bestMonth ? `${bestMonth.total_runs} runs / ${formatUsd(bestMonth.spend_usd)}` : "No activity yet" },
        { label: "Latest month", value: latestMonth?.label || "-", meta: latestMonth ? `${latestMonth.total_runs} runs / ${formatUsd(latestMonth.spend_usd)}` : "No activity yet" },
      ];
    }

    return [
      { label: "Project", value: selectedOverviewProjectLabel, meta: "Current overview filter" },
      { label: `${chartRangeShortLabel} total runs`, value: String(totalRuns), meta: "Across the visible period" },
      { label: `${chartRangeShortLabel} brand matches`, value: String(totalBrandMatches), meta: "Across the visible period" },
      { label: `${chartRangeShortLabel} domain matches`, value: String(totalDomainMatches), meta: "Across the visible period" },
      { label: "Best month", value: bestMonth?.label || "-", meta: bestMonth ? `${bestMonth.total_runs} runs / ${bestMonth.brand_matches + bestMonth.domain_matches} matches` : "No activity yet" },
      { label: "Latest month", value: latestMonth?.label || "-", meta: latestMonth ? `${latestMonth.total_runs} runs / ${latestMonth.brand_matches + latestMonth.domain_matches} matches` : "No activity yet" },
    ];
  }, [chartRangeShortLabel, overviewSummary, selectedOverviewProjectLabel, selectedOverviewUserLabel, visibleMonthly]);

  const startProjectGroups = useMemo(() => {
    if (!startProjectDialog) {
      return [];
    }
    return groupStartProjectRows(startProjectDialog.rows);
  }, [startProjectDialog]);

  if (specialAuthRoute === "reset-password") {
    return (
      <div className="auth-route-shell">
        <section className="modal-card auth-route-card">
          <div className="brand-block auth-route-brand">
            <div className="brand-badge">RB</div>
            <div>
              <p className="eyebrow">Workspace</p>
              <h1>Rankberry</h1>
            </div>
          </div>

          <p className="eyebrow">Password Recovery</p>
          <h3>Reset your password</h3>
          <p>
            {resetPasswordStatus === "ready"
              ? `Set a new password${authRouteUserEmail ? ` for ${authRouteUserEmail}` : ""}.`
              : "We're checking your secure password reset link."}
          </p>

          {resetPasswordStatus === "loading" ? <p className="status-banner">Checking your reset link...</p> : null}
          {resetPasswordStatus === "error" ? <p className="auth-error">{resetPasswordMessage}</p> : null}
          {resetPasswordStatus === "success" ? <p className="status-banner">{resetPasswordMessage}</p> : null}

          {resetPasswordStatus === "ready" ? (
            <form
              className="auth-route-form"
              onSubmit={(event) => {
                event.preventDefault();
                void submitResetPassword();
              }}
            >
              <label className="field-stack">
                <span>New password</span>
                <input
                  className="auth-input"
                  type="password"
                  autoComplete="new-password"
                  value={resetPasswordForm.password}
                  onChange={(event) => updateResetPasswordField("password", event.target.value)}
                />
              </label>
              <label className="field-stack">
                <span>Confirm new password</span>
                <input
                  className="auth-input"
                  type="password"
                  autoComplete="new-password"
                  value={resetPasswordForm.confirmPassword}
                  onChange={(event) => updateResetPasswordField("confirmPassword", event.target.value)}
                />
              </label>
              {resetPasswordMessage ? <p className="auth-error">{resetPasswordMessage}</p> : null}
              <p className="inline-status">Use at least 8 characters, including a letter and a number.</p>
              <div className="modal-actions auth-route-actions">
                <button className="ghost-btn" type="button" onClick={goToDashboardHome}>
                  Back to dashboard
                </button>
                <button className="primary-btn" type="submit" disabled={isUpdatingPassword}>
                  {isUpdatingPassword ? "Updating password..." : "Update password"}
                </button>
              </div>
            </form>
          ) : (
            <div className="modal-actions auth-route-actions">
              <button className="ghost-btn" type="button" onClick={goToDashboardHome}>
                Back to dashboard
              </button>
            </div>
          )}
        </section>
      </div>
    );
  }

  return (
    <>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="brand-block">
            <div className="brand-badge">RB</div>
            <div>
              <p className="eyebrow">Workspace</p>
              <h1>Rankberry</h1>
            </div>
          </div>

          <nav className="nav">
            {visibleNavItems.map((item) => (
              <button
                key={item.key}
                className={`nav-link ${page === item.key ? "active" : ""}`}
                onClick={() => setPage(item.key)}
                type="button"
              >
                {item.label}
              </button>
            ))}
          </nav>
        </aside>

        <main className="main-content">
          <header className="topbar">
            <div>
              <p className="eyebrow">Control Center</p>
              <h2>{visibleNavItems.find((item) => item.key === page)?.label || (isAdmin ? "Overview" : navItems.find((item) => item.key === page)?.label)}</h2>
            </div>

            <div className="topbar-right">
              {currentUsername ? (
                <div className="profile-menu-wrap" ref={profileMenuRef}>
                  <button
                    className={`profile-btn ${isAdmin ? "admin-profile-btn" : ""}`}
                    type="button"
                    onClick={() => setIsProfileMenuOpen((current) => !current)}
                    aria-label="Open profile menu"
                  >
                    {profileLetter}
                  </button>

                  {isProfileMenuOpen ? (
                    <div className="profile-menu">
                      <button
                        type="button"
                        onClick={() => {
                          setPage("history");
                          setIsProfileMenuOpen(false);
                        }}
                      >
                        History
                      </button>
                      <button type="button" onClick={() => void logout()}>
                        Log out
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : (
                <button className="primary-btn" type="button" onClick={() => openAuthModal("sign_in")}>
                  Sign In
                </button>
              )}
            </div>
          </header>

          {statusMessage ? <div className="status-banner">{statusMessage}</div> : null}

          {page === "overview" && (
            <section className="page active overview-page">
              <div className="hero-card hero-dashboard-card">
                <div>
                  <p className="eyebrow">{isAdmin ? "Admin Dashboard" : "Main Dashboard"}</p>
                  <h3>{isAdmin ? "Cross-user statistics" : "Project statistics"}</h3>
                </div>
                <div className="hero-dashboard-controls">
                  {currentUsername && page === "overview" ? (
                    <div className="hero-filter-group">
                      <label className="field-stack hero-project-filter">
                        <span>Project</span>
                        <select className="auth-input overview-project-select" value={overviewProject} onChange={(event) => setOverviewProject(event.target.value)}>
                          <option value="">All projects</option>
                          {projectOptions.map((project) => (
                            <option key={project} value={project}>{project}</option>
                          ))}
                        </select>
                      </label>
                      {isAdmin ? (
                        <label className="field-stack hero-project-filter">
                          <span>User</span>
                          <select className="auth-input overview-user-select" value={overviewUserId} onChange={(event) => setOverviewUserId(event.target.value)}>
                            <option value="">All users</option>
                            {overviewUserOptions.map((item) => (
                              <option key={item.user_id} value={item.user_id}>{item.username}</option>
                            ))}
                          </select>
                        </label>
                      ) : null}
                    </div>
                  ) : null}
                  <div className={`hero-pill ${isAdmin ? "admin-pill" : ""}`}>
                    {currentUsername
                      ? (isAdmin ? `Admin analytics for ${currentUserEmail || currentUsername}` : `Logged in as ${currentUsername}`)
                      : "Sign in to unlock services"}
                  </div>
                  {currentUsername && page === "overview" ? (
                    <button className="ghost-btn screenshot-btn" type="button" onClick={() => void captureOverviewScreenshot()} disabled={isExportingOverviewScreenshot}>
                      {isExportingOverviewScreenshot ? "Saving JPG..." : "Screenshot"}
                    </button>
                  ) : null}
                </div>
              </div>

              {!currentUsername ? (
                <div className="panel auth-gate">
                  <p className="eyebrow">Authentication Required</p>
                  <h3>Sign in to access the dashboard.</h3>
                  <p>Use Supabase Auth with email and password. The admin mode is reserved for one exact email only.</p>
                  <button className="primary-btn" type="button" onClick={() => openAuthModal("sign_in")}>
                    Sign In
                  </button>
                </div>
              ) : (
                <>
                  <div ref={overviewCaptureRef} className="overview-screenshot-area">
                    <div className={`stats-grid ${isRefreshingOverview ? "is-refreshing" : ""}`} aria-busy={isRefreshingOverview}>
                      {stats.map((stat) => (
                        <article key={stat.label} className="stat-card">
                          <p>{stat.label}</p>
                          <strong>{stat.value}</strong>
                          <span>{stat.meta}</span>
                        </article>
                      ))}
                    </div>

                    <div className={`overview-grid ${isAdmin ? "" : "common-overview-grid"}`}>
                    <article className="panel chart-panel">
                      <div className="panel-heading">
                        <div>
                          <p className="eyebrow">{`Last ${chartRangeLabel}`}</p>
                          <h3>Brand and domain matches</h3>
                        </div>
                        <div className="chart-range-toggle" role="group" aria-label="Toggle chart range">
                          <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 12 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(12)}>
                            12M
                          </button>
                          <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 6 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(6)}>
                            6M
                          </button>
                        </div>
                      </div>
                      <div className="chart-legend">
                        <span><i className="legend-swatch brand-bar" />Brand matches</span>
                        <span><i className="legend-swatch domain-bar" />Domain matches</span>
                      </div>
                      <div className="chart-layout">
                        <div className={`chart-bars monthly-chart month-count-${visibleMonthly.length}`} aria-label="Monthly match chart">
                          {visibleMonthly.map((item) => (
                            <div key={`${item.month}-matches`} className="chart-month two-series-chart">
                              <div className="chart-month-values" aria-hidden="true">
                                <span className="chart-value brand-value">{item.brand_matches}</span>
                                <span className="chart-value domain-value">{item.domain_matches}</span>
                              </div>
                              <div className="chart-month-bars two-series-bars">
                                <span className="brand-bar" style={{ height: `${(item.brand_matches / matchChartMax) * 100}%` }} title={`Brand matches: ${item.brand_matches}`} />
                                <span className="domain-bar" style={{ height: `${(item.domain_matches / matchChartMax) * 100}%` }} title={`Domain matches: ${item.domain_matches}`} />
                              </div>
                              <strong>{item.label}</strong>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="inline-status">{isLoadingOverview ? "Loading overview..." : `Showing matches for ${selectedOverviewProjectLabel}${isAdmin ? ` - ${selectedOverviewUserLabel}` : ""} over the last ${chartRangeMonths} months`}</div>
                    </article>

                    <article className="panel chart-panel">
                      <div className="panel-heading">
                        <div>
                          <p className="eyebrow">{`Last ${chartRangeLabel}`}</p>
                          <h3>Runs and total match load</h3>
                        </div>
                        <div className="chart-range-toggle" role="group" aria-label="Toggle chart range">
                          <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 12 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(12)}>
                            12M
                          </button>
                          <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 6 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(6)}>
                            6M
                          </button>
                        </div>
                      </div>
                      <div className="chart-legend">
                        <span><i className="legend-swatch total-bar" />Total runs</span>
                        <span><i className="legend-swatch combined-bar" />Brand + domain matches</span>
                      </div>
                      <div className="chart-layout">
                        <div className={`chart-bars monthly-chart month-count-${visibleMonthly.length}`} aria-label="Monthly activity chart">
                          {visibleMonthly.map((item) => (
                            <div key={`${item.month}-activity`} className="chart-month two-series-chart">
                              <div className="chart-month-values" aria-hidden="true">
                                <span className="chart-value total-value">{item.total_runs}</span>
                                <span className="chart-value combined-value">{item.brand_matches + item.domain_matches}</span>
                              </div>
                              <div className="chart-month-bars two-series-bars">
                                <span className="total-bar" style={{ height: `${(item.total_runs / activityChartMax) * 100}%` }} title={`Total runs: ${item.total_runs}`} />
                                <span className="combined-bar" style={{ height: `${((item.brand_matches + item.domain_matches) / activityChartMax) * 100}%` }} title={`Brand + domain matches: ${item.brand_matches + item.domain_matches}`} />
                              </div>
                              <strong>{item.label}</strong>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div className="inline-status">{isLoadingOverview ? "Loading overview..." : `Showing activity for ${selectedOverviewProjectLabel}${isAdmin ? ` - ${selectedOverviewUserLabel}` : ""} over the last ${chartRangeMonths} months`}</div>
                    </article>

                    {isAdmin ? (
                      <article className="panel chart-panel">
                        <div className="panel-heading">
                          <div>
                            <p className="eyebrow">{`Last ${chartRangeLabel}`}</p>
                            <h3>Spendings</h3>
                          </div>
                          <div className="chart-range-toggle" role="group" aria-label="Toggle chart range">
                            <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 12 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(12)}>
                              12M
                            </button>
                            <button className={`ghost-btn chart-range-btn ${chartRangeMonths === 6 ? "active" : ""}`} type="button" onClick={() => setChartRangeMonths(6)}>
                              6M
                            </button>
                          </div>
                        </div>
                        <div className="chart-legend">
                          <span><i className="legend-swatch spend-bar" />Estimated spend</span>
                        </div>
                        <div className="chart-layout">
                          <div className={`chart-bars monthly-chart month-count-${visibleMonthly.length}`} aria-label="Monthly spending chart">
                            {visibleMonthly.map((item) => (
                              <div key={`${item.month}-spend`} className="chart-month single-series-chart">
                                <div className="chart-month-values" aria-hidden="true">
                                  <span className="chart-value spend-value">{formatUsd(item.spend_usd)}</span>
                                </div>
                                <div className="chart-month-bars single-series-bars">
                                  <span className="spend-bar" style={{ height: `${(item.spend_usd / spendChartMax) * 100}%` }} title={`Estimated spend: ${formatUsd(item.spend_usd)}`} />
                                </div>
                                <strong>{item.label}</strong>
                              </div>
                            ))}
                          </div>
                        </div>
                        <div className="inline-status">{isLoadingOverview ? "Loading overview..." : `Showing spend for ${selectedOverviewProjectLabel}${isAdmin ? ` - ${selectedOverviewUserLabel}` : ""} over the last ${chartRangeMonths} months`}</div>
                      </article>
                    ) : null}
                    <article className="panel overview-insights-panel">
                      <div className="panel-heading">
                        <div>
                          <p className="eyebrow">Snapshot</p>
                          <h3>{isAdmin ? "Selected scope summary" : "Selected project summary"}</h3>
                        </div>
                      </div>
                      <div className="overview-highlights">
                        {overviewHighlights.map((item) => (
                          <div key={item.label} className="overview-highlight-card">
                            <p>{item.label}</p>
                            <strong>{item.value}</strong>
                            <span>{item.meta}</span>
                          </div>
                        ))}
                      </div>
                    </article>
                  </div>
                  </div>
                </>
              )}
            </section>
          )}

          {page === "service" && (
            <section className="page active">
              <div className="page-header-card">
                <div>
                  <p className="eyebrow">Service Page 1</p>
                  <h3>Run builder</h3>
                </div>
                <p>Build a multi-row batch, and queue one run per row.</p>
              </div>

              {!currentUsername ? (
                <div className="panel auth-gate">
                  <p className="eyebrow">Service unavailable</p>
                  <h3>Login is required to use this service.</h3>
                  <p>The username is profile metadata, while the real identity comes from a Supabase session.</p>
                  <button className="primary-btn" type="button" onClick={() => openAuthModal("sign_in")}>
                    Sign In
                  </button>
                </div>
              ) : (
                <div className="panel service-panel">
                  <div className="row-list-header input-grid">
                    <span>Keyword</span>
                    <span>Domain</span>
                    <span>Brand</span>
                    <span>Prompt</span>
                    <span>Project</span>
                    <span>Delete</span>
                  </div>

                  <div className="row-list service-row-list">
                    {serviceRows.map((row) => (
                      <div key={row.id} className="row-item input-grid service-row-item">
                        <input type="text" placeholder="Keyword" value={row.keyword} onChange={(event) => updateServiceRow(row.id, "keyword", event.target.value)} />
                        <input type="text" placeholder="Domain" value={row.domain} onChange={(event) => updateServiceRow(row.id, "domain", event.target.value)} />
                        <input type="text" placeholder="Comma-separated brand variations" value={row.brand} onChange={(event) => updateServiceRow(row.id, "brand", event.target.value)} />
                        <input type="text" placeholder="Prompt" value={row.prompt} onChange={(event) => updateServiceRow(row.id, "prompt", event.target.value)} />
                        <div className="project-field">
                          <select className="auth-input" value={row.project} onChange={(event) => handleProjectSelectChange(row.id, event.target.value)}>
                            <option value="">Select project</option>
                            {projectOptions.map((project) => (
                              <option key={project} value={project}>{project}</option>
                            ))}
                            <option value={ADD_PROJECT_OPTION_VALUE}>+ Add project</option>
                          </select>
                          <button className="ghost-btn project-add-btn" type="button" onClick={() => openProjectCreator(row.id)}>
                            Add
                          </button>
                        </div>
                        <button className="delete-row-btn" type="button" onClick={() => deleteServiceRow(row.id)} aria-label="Delete row">
                          x
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="service-row-actions">
                    <button className="ghost-btn" type="button" onClick={duplicateServiceRow}>
                      Duplicate row
                    </button>
                    <button className="ghost-btn add-row-btn" type="button" onClick={addServiceRow}>
                      Add row
                    </button>
                    <button className="ghost-btn" type="button" onClick={openCsvImportModal}>
                      fill with csv
                    </button>
                    <input
                      ref={csvFileInputRef}
                      className="csv-import-input"
                      type="file"
                      accept=".csv,text/csv"
                      onChange={(event) => void importCsvFile(event.target.files?.[0])}
                    />
                  </div>

                  <div className="service-meta">
                    <span>{isLoadingDraft ? "Loading draft..." : draftStatus}</span>
                    <span>{activeRunDetails.length ? `${activeRunDetails.length} runs in progress` : "No active run."}</span>
                  </div>

                  {activeRunDetails.length ? (
                    <div className="active-run-list">
                      {activeRunDetails.map((detail) => (
                        <div key={detail.run.id} className="run-banner">
                          <span className={`tag ${statusTone(detail.run.status)}`}>{detail.run.status}</span>
                          <strong>{detail.run.keyword}</strong>
                          <span>{detail.run.completed_iterations}/{detail.run.total_iterations} iterations complete</span>
                          <button className="ghost-btn" type="button" onClick={() => void openRunDetail(detail.run.id)}>
                            Open Details
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}

              {currentUsername ? (
                <button className="floating-start-btn" onClick={() => void handleStartService()} type="button">
                  {isStartingRun ? "Queueing..." : "Start"}
                </button>
              ) : null}
            </section>
          )}

          {page === "history" && (
            <section className="page active">
              <div className="page-header-card">
                <div>
                  <p className="eyebrow">{isAdmin ? "Admin History" : "History"}</p>
                  <h3>{isAdmin ? "Cross-user run history" : "Run history"}</h3>
                </div>
                <p>{isAdmin ? "Browse completed runs across all users with project, prompt, and user filters." : "Browse completed runs in a dedicated page with filters and CSV export."}</p>
              </div>

              {!currentUsername ? (
                <div className="panel auth-gate">
                  <p className="eyebrow">History unavailable</p>
                  <h3>Login is required to view history.</h3>
                  <p>Only rows owned by the authenticated Supabase user are returned.</p>
                  <button className="primary-btn" type="button" onClick={() => openAuthModal("sign_in")}>
                    Sign In
                  </button>
                </div>
              ) : (
                <div className="panel history-page-panel">
                  <div className="filter-bar history-filter-bar">
                    <input className="auth-input" type="text" placeholder="Project" value={historyFilters.project} onChange={(event) => setHistoryFilters((current) => ({ ...current, project: event.target.value, page: 1 }))} />
                    <input className="auth-input" type="text" placeholder="Prompt" value={historyFilters.prompt} onChange={(event) => setHistoryFilters((current) => ({ ...current, prompt: event.target.value, page: 1 }))} />
                    {isAdmin ? <input className="auth-input" type="text" placeholder="User" value={historyFilters.user} onChange={(event) => setHistoryFilters((current) => ({ ...current, user: event.target.value, page: 1 }))} /> : null}
                    <input className="auth-input" type="date" value={historyFilters.date_from} onChange={(event) => setHistoryFilters((current) => ({ ...current, date_from: event.target.value, page: 1 }))} />
                    <input className="auth-input" type="date" value={historyFilters.date_to} onChange={(event) => setHistoryFilters((current) => ({ ...current, date_to: event.target.value, page: 1 }))} />
                    <div className="inline-status">{isLoadingHistory ? "Loading history..." : `${historyData.total} rows`}</div>
                    <div className="filter-actions">
                      {isHistoryForwardMode ? (
                        <>
                          <button className="ghost-btn" type="button" onClick={cancelHistoryForwardMode}>
                            cancel
                          </button>
                          <button className="primary-btn" type="button" onClick={() => void openHistoryForwardDialog()} disabled={!selectedHistoryRunIds.length || isLoadingForwardUsers}>
                            {isLoadingForwardUsers ? "Loading users..." : `forward${selectedHistoryRunIds.length ? ` (${selectedHistoryRunIds.length})` : ""}`}
                          </button>
                        </>
                      ) : isHistoryInputMode ? (
                        <>
                          <button className="ghost-btn" type="button" onClick={cancelHistoryForwardMode}>
                            cancel
                          </button>
                          <button className="primary-btn" type="button" onClick={addSelectedHistoryRowsToInputs} disabled={!selectedHistoryRunIds.length}>
                            continue{selectedHistoryRunIds.length ? ` (${selectedHistoryRunIds.length})` : ""}
                          </button>
                        </>
                      ) : (
                        <>
                          <button className="ghost-btn" type="button" onClick={startHistoryInputMode}>
                            add to inputs
                          </button>
                          <button className="ghost-btn" type="button" onClick={startHistoryForwardMode}>
                            forward to another user
                          </button>
                        </>
                      )}
                      <button className="primary-btn" type="button" onClick={() => void openReportExportDialogFor("history")}>
                        {isPreparingReport === "history" ? "Preparing..." : isExportingHistory ? "Exporting..." : "create a report"}
                      </button>
                    </div>
                  </div>

                  <div className="history-list page-history-list">
                    {historyData.items.length ? (
                      historyData.items.map((item) => {
                        const isHistorySelectionMode = isHistoryForwardMode || isHistoryInputMode;
                        const isSelectedForAction = selectedHistoryRunIds.includes(item.run_id);
                        return (
                        <section key={item.run_id} className={`history-card ${isSelectedForAction ? "history-card-selected" : ""}`}>
                          <div className="history-card-head">
                            <div className="history-card-title-row">
                              {isHistorySelectionMode ? (
                                <label className="history-select-box" aria-label={`Select ${item.keyword}`}>
                                  <input type="checkbox" checked={isSelectedForAction} onChange={() => toggleHistoryRunSelection(item.run_id)} />
                                </label>
                              ) : null}
                              <strong>{formatDateTime(item.created_at)}</strong>
                            </div>
                            <span>{isAdmin ? `${item.username} | ${item.project || "No project"}` : item.project || "No project"}</span>
                          </div>

                          <div className="history-table-wrap">
                            <table className="history-table result-table">
                              <tbody>
                                <tr>
                                  <th>Keyword</th>
                                  <td>{item.keyword}</td>
                                  <th>Status</th>
                                  <td>
                                    <span className={`tag ${statusTone(item.status)}`}>{item.status}</span>
                                  </td>
                                </tr>
                                {isAdmin ? (
                                  <tr>
                                    <th>User</th>
                                    <td>{item.username}</td>
                                    <th>Project</th>
                                    <td>{item.project || "-"}</td>
                                  </tr>
                                ) : null}
                                <tr>
                                  <th>Domain</th>
                                  <td>{item.domain}</td>
                                  <th>Mentions</th>
                                  <td>{summarizeMentions(item)}</td>
                                </tr>
                                <tr>
                                  <th>Brand List</th>
                                  <td>{item.brand_list || "-"}</td>
                                  <th>Citation</th>
                                  <td>{item.citation_format || "-"}</td>
                                </tr>
                                <tr>
                                  <th>Sentiment</th>
                                  <td colSpan={3}>{truncateText(item.sentiment_analysis, 220)}</td>
                                </tr>
                              </tbody>
                            </table>
                          </div>

                          <div className="history-actions">
                            <button className="ghost-btn small-btn" type="button" onClick={() => void openRunDetail(item.run_id)}>
                              Open Details
                            </button>
                          </div>
                        </section>
                        );
                      })
                    ) : (
                      <div className="empty-state">
                        <p>No history rows match the current filters.</p>
                      </div>
                    )}
                  </div>

                  <div className="pagination-bar">
                    <button className="ghost-btn" type="button" disabled={historyFilters.page <= 1} onClick={() => setHistoryFilters((current) => ({ ...current, page: Math.max(1, current.page - 1) }))}>
                      Previous
                    </button>
                    <span>
                      Page {historyData.page} of {Math.max(1, Math.ceil(historyData.total / historyData.page_size))}
                    </span>
                    <button className="ghost-btn" type="button" disabled={historyData.page * historyData.page_size >= historyData.total} onClick={() => setHistoryFilters((current) => ({ ...current, page: current.page + 1 }))}>
                      Next
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}

          {page === "outputs" && (
            <section className="page active">
              <div className="page-header-card">
                <div>
                  <p className="eyebrow">Service Page 2</p>
                  <h3>Today's finalized outputs</h3>
                </div>
                <p>Only results created on {outputLocalDateLabel} are shown here.</p>
              </div>

              {!currentUsername ? (
                <div className="panel auth-gate">
                  <p className="eyebrow">Service unavailable</p>
                  <h3>Login is required to view outputs.</h3>
                  <p>Only rows owned by the authenticated Supabase user are returned.</p>
                  <button className="primary-btn" type="button" onClick={() => openAuthModal("sign_in")}>
                    Sign In
                  </button>
                </div>
              ) : (
                <div className="panel output-panel">
                  <div className="filter-bar">
                    <input
                      className="auth-input"
                      type="text"
                      placeholder="Filter by project"
                      value={outputFilters.project}
                      onChange={(event) => setOutputFilters((current) => ({ ...current, project: event.target.value, page: 1 }))}
                    />
                    <input
                      className="auth-input"
                      type="text"
                      placeholder="Filter by prompt"
                      value={outputFilters.prompt}
                      onChange={(event) => setOutputFilters((current) => ({ ...current, prompt: event.target.value, page: 1 }))}
                    />
                    <div className="inline-status">
                      {isLoadingOutputs
                        ? "Loading outputs..."
                        : `${outputData.total} finalized runs on ${outputLocalDateLabel}${failedRuns.length ? ` - ${failedRuns.length} failed` : ""}`}
                    </div>
                    <div className="filter-actions">
                      <button className="primary-btn" type="button" onClick={() => void openReportExportDialogFor("outputs")}>
                        {isPreparingReport === "outputs" ? "Preparing..." : isExportingOutputs ? "Exporting..." : "create a report"}
                      </button>
                      <button
                        className="ghost-btn"
                        type="button"
                        onClick={() => void handleStopRuns()}
                        disabled={!activeRunDetails.some((detail) => ["queued", "running"].includes(detail.run.status)) || isStoppingRuns || isContinuingRuns || isRetryingFailedRuns}
                      >
                        {isStoppingRuns ? "Stopping..." : "Stop all"}
                      </button>
                      <button
                        className="primary-btn"
                        type="button"
                        onClick={() => void handleContinueRuns()}
                        disabled={!activeRunDetails.length || isStoppingRuns || isContinuingRuns || isRetryingFailedRuns}
                      >
                        {isContinuingRuns ? "Continuing..." : "Continue"}
                      </button>
                      <button
                        className="ghost-btn"
                        type="button"
                        onClick={() => void handleRetryFailedRuns()}
                        disabled={!failedRuns.length || isStoppingRuns || isContinuingRuns || isRetryingFailedRuns}
                      >
                        {isRetryingFailedRuns ? "Retrying..." : "Retry"}
                      </button>
                      <button className="ghost-btn" type="button" onClick={() => void Promise.all([loadOutputs(), loadActiveRuns(), loadFailedRuns(), loadOverview()])}>
                        Refresh
                      </button>
                    </div>
                  </div>

                  {activeRunDetails.length ? (
                    <div className="active-run-list">
                      {activeRunDetails.map((detail) => (
                        <div key={detail.run.id} className="run-banner">
                          <span className={`tag ${statusTone(detail.run.status)}`}>{detail.run.status}</span>
                          <strong>{detail.run.keyword}</strong>
                          <span>
                            {detail.run.completed_iterations}/{detail.run.total_iterations} iterations complete
                          </span>
                          <button className="ghost-btn" type="button" onClick={() => void openRunDetail(detail.run.id)}>
                            Open Details
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {failedRuns.length ? (
                    <div className="failed-run-section">
                      <div className="service-meta">
                        <span>{failedRuns.length} failed runs ready to retry</span>
                        <span>Retry clears previous partial outputs only for these failed runs.</span>
                      </div>
                      <div className="active-run-list">
                        {failedRuns.map((run) => (
                          <div key={run.id} className="run-banner">
                            <span className={`tag ${statusTone(run.status)}`}>{run.status}</span>
                            <strong>{run.keyword}</strong>
                            <span>
                              {run.completed_iterations}/{run.total_iterations} iterations complete
                            </span>
                            <span className="run-banner-note">{run.error_messages || "Run failed."}</span>
                            <button className="ghost-btn" type="button" onClick={() => void openRunDetail(run.id)}>
                              Open Details
                            </button>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <div className="history-table-wrap">
                    <table className="history-table result-table">
                      <thead>
                        <tr>
                          <th>Created</th>
                          <th>Project</th>
                          <th>Keyword</th>
                          <th>Status</th>
                          <th>Mentions</th>
                          <th>Response Avg</th>
                          <th>Citation</th>
                          <th>Sentiment</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {outputData.items.length ? (
                          outputData.items.map((item) => (
                            <tr key={item.run_id}>
                              <td>{formatDateTime(item.created_at)}</td>
                              <td>{item.project || "-"}</td>
                              <td>{item.keyword}</td>
                              <td>
                                <span className={`tag ${statusTone(item.status)}`}>{item.status}</span>
                              </td>
                              <td>{summarizeMentions(item)}</td>
                              <td>{formatNumber(item.response_count_avg)}</td>
                              <td>{item.citation_format || "-"}</td>
                              <td>{truncateText(item.sentiment_analysis, 120)}</td>
                              <td>
                                <button className="ghost-btn small-btn" type="button" onClick={() => void openRunDetail(item.run_id)}>
                                  Open Details
                                </button>
                              </td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan={9}>
                              <div className="empty-state">
                                <p>No finalized outputs for {outputLocalDateLabel}.</p>
                              </div>
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>

                  <div className="pagination-bar">
                    <button className="ghost-btn" type="button" disabled={outputFilters.page <= 1} onClick={() => setOutputFilters((current) => ({ ...current, page: Math.max(1, current.page - 1) }))}>
                      Previous
                    </button>
                    <span>
                      Page {outputData.page} of {Math.max(1, Math.ceil(outputData.total / outputData.page_size))}
                    </span>
                    <button className="ghost-btn" type="button" disabled={outputData.page * outputData.page_size >= outputData.total} onClick={() => setOutputFilters((current) => ({ ...current, page: current.page + 1 }))}>
                      Next
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}
        </main>
      </div>

      {isAuthOpen ? (
        <div className="modal-backdrop" onClick={() => setIsAuthOpen(false)}>
          <div className="modal-card" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Supabase Auth</p>
            <h3>{authMode === "sign_up" ? "Create an account" : "Sign in"}</h3>
            <p>{authMode === "sign_up" ? "Register with username, email, and password." : "Sign in with your email and password."}</p>
            {authMode === "sign_up" ? (
              <input
                className="auth-input"
                type="text"
                placeholder="Username"
                value={authForm.username}
                onChange={(event) => updateAuthField("username", event.target.value)}
              />
            ) : null}
            <input
              className="auth-input"
              type="email"
              placeholder="Email"
              value={authForm.email}
              onChange={(event) => updateAuthField("email", event.target.value)}
            />
            <input
              className="auth-input"
              type="password"
              placeholder="Password"
              value={authForm.password}
              onChange={(event) => updateAuthField("password", event.target.value)}
            />
            {authMode === "sign_in" ? (
              <div className="auth-helper-row">
                <button className="ghost-btn small-btn" type="button" onClick={() => void sendPasswordResetEmail()} disabled={isSendingPasswordReset}>
                  {isSendingPasswordReset ? "Sending reset link..." : "Forgot password?"}
                </button>
              </div>
            ) : null}
            {authError ? <p className="auth-error">{authError}</p> : null}
            {authMessage ? <p className="status-banner">{authMessage}</p> : null}
            <div className="modal-actions auth-switch-row">
              <button className="ghost-btn" type="button" onClick={() => setIsAuthOpen(false)}>
                Cancel
              </button>
              <button
                className="ghost-btn"
                type="button"
                onClick={() => {
                  setAuthMode(authMode === "sign_in" ? "sign_up" : "sign_in");
                  setAuthError("");
                  setAuthMessage("");
                  setAuthForm((current) => ({ ...current, password: "" }));
                }}
              >
                {authMode === "sign_in" ? "Need an account?" : "Have an account?"}
              </button>
              <button className="primary-btn" type="button" onClick={() => void submitAuth()}>
                {isAuthSubmitting ? "Please wait..." : authMode === "sign_up" ? "Sign Up" : "Sign In"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {reportExportDialog ? (
        <div className="modal-backdrop" onClick={() => setReportExportDialog(null)}>
          <div className="modal-card report-export-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Report export</p>
            <h3>{reportExportDialog.kind === "history" ? "Create history report" : "Create outputs report"}</h3>
            <p>Choose which projects to add to this CSV. The current page filters stay applied, and the export now includes a Prompt column.</p>

            {reportExportDialog.projects.length ? (
              <>
                <div className="report-export-toolbar">
                  <span>
                    {reportExportDialog.selectedProjects.length
                      ? `${reportExportDialog.selectedProjects.length} project${reportExportDialog.selectedProjects.length === 1 ? "" : "s"} selected`
                      : "No project boxes checked. Export will include all rows from the current view."}
                  </span>
                  <div className="filter-actions">
                    <button className="ghost-btn small-btn" type="button" onClick={selectAllReportProjects}>
                      Select all
                    </button>
                    <button className="ghost-btn small-btn" type="button" onClick={clearReportProjects}>
                      Clear
                    </button>
                  </div>
                </div>
                <div className="report-project-list">
                  {reportExportDialog.projects.map((project) => {
                    const isChecked = reportExportDialog.selectedProjects.includes(project);
                    return (
                      <label key={project} className={`report-project-option ${isChecked ? "checked" : ""}`}>
                        <input type="checkbox" checked={isChecked} onChange={() => toggleReportProject(project)} />
                        <div>
                          <strong>{project}</strong>
                          <span>add to report</span>
                        </div>
                      </label>
                    );
                  })}
                </div>
              </>
            ) : (
              <div className="empty-state">
                <p>No run projects were found for your user yet. Exporting now will use the current page rows only.</p>
              </div>
            )}

            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={() => setReportExportDialog(null)}>
                Cancel
              </button>
              <button
                className="primary-btn"
                type="button"
                onClick={() => void confirmReportExport()}
                disabled={isExportingHistory || isExportingOutputs}
              >
                {reportExportDialog.kind === "history"
                  ? (isExportingHistory ? "Exporting..." : "Export CSV")
                  : (isExportingOutputs ? "Exporting..." : "Export CSV")}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {historyForwardDialog ? (
        <div className="modal-backdrop" onClick={() => setHistoryForwardDialog(null)}>
          <div className="modal-card forward-history-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Forward history</p>
            <h3>Choose user</h3>
            <p>{selectedHistoryRunIds.length} selected row{selectedHistoryRunIds.length === 1 ? "" : "s"} will be reassigned in runs, run results, and outputs.</p>
            {historyForwardDialog.users.length ? (
              <div className="forward-user-list">
                {historyForwardDialog.users.map((user) => {
                  const isSelected = historyForwardDialog.selectedUserId === user.user_id;
                  return (
                    <label key={user.user_id} className={`report-project-option ${isSelected ? "checked" : ""}`}>
                      <input
                        type="radio"
                        name="forward-user"
                        checked={isSelected}
                        onChange={() => setHistoryForwardDialog((current) => (current ? { ...current, selectedUserId: user.user_id } : current))}
                      />
                      <div>
                        <strong>{user.username}</strong>
                        <span>{user.user_id}</span>
                      </div>
                    </label>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">
                <p>No other users were found.</p>
              </div>
            )}
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={() => setHistoryForwardDialog(null)}>
                Cancel
              </button>
              <button className="primary-btn" type="button" onClick={() => void confirmHistoryForward()} disabled={isForwardingHistory || !historyForwardDialog.selectedUserId}>
                {isForwardingHistory ? "Forwarding..." : "confirm"}
              </button>
            </div>
          </div>
        </div>
      ) : null}


      {startProjectDialog ? (
        <div className="modal-backdrop" onClick={() => setStartProjectDialog(null)}>
          <div className="modal-card start-project-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Project selection</p>
            <h3>Choose rows to run</h3>
            <p>More than one row is present in AI Visibility Input. Pick the rows you want to run, even when they are split across projects.</p>
            <p className="modal-inline-copy">Each project is shown as its own group, and you can select whole projects or individual rows.</p>
            <div className="report-export-toolbar">
              <span>
                {startProjectDialog.selectedRowIds.length
                  ? `${startProjectDialog.selectedRowIds.length} row${startProjectDialog.selectedRowIds.length === 1 ? "" : "s"} selected`
                  : "No rows selected yet."}
              </span>
              <div className="filter-actions">
                <button className="ghost-btn small-btn" type="button" onClick={selectAllStartProjects}>
                  Select all rows
                </button>
                <button className="ghost-btn small-btn" type="button" onClick={clearStartProjects}>
                  Clear selection
                </button>
              </div>
            </div>
            <div className="start-project-groups">
              {startProjectGroups.map((group) => {
                const selectedCount = group.rows.filter((row) => startProjectDialog.selectedRowIds.includes(row.id)).length;
                const isChecked = selectedCount === group.rows.length;
                return (
                  <section key={group.project || "__no_project__"} className="start-project-group">
                    <label className={`report-project-option start-project-option ${isChecked ? "checked" : ""}`}>
                      <input type="checkbox" checked={isChecked} onChange={() => toggleStartProjectSelection(group.project)} />
                      <div>
                        <strong>{group.label}</strong>
                        <span>{selectedCount} of {group.rows.length} row{group.rows.length === 1 ? "" : "s"} selected</span>
                      </div>
                    </label>
                    <div className="start-row-list">
                      {group.rows.map((row) => {
                        const isRowChecked = startProjectDialog.selectedRowIds.includes(row.id);
                        return (
                          <label key={row.id} className={`start-row-option ${isRowChecked ? "checked" : ""}`}>
                            <input type="checkbox" checked={isRowChecked} onChange={() => toggleStartProjectRowSelection(row.id)} />
                            <div>
                              <strong>Row {row.index} - {row.row.keyword || "Untitled keyword"}</strong>
                              <span>{row.row.domain || "No domain"} - {row.row.brand || "No brand"}</span>
                              <small>{truncateText(row.row.prompt, 84)}</small>
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  </section>
                );
              })}
            </div>
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={() => setStartProjectDialog(null)}>
                Cancel
              </button>
              <button className="primary-btn" type="button" onClick={() => void confirmStartProjectSelection()} disabled={isStartingRun}>
                {isStartingRun ? "Queueing..." : "Start selected rows"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {isCsvImportOpen ? (
        <div className="modal-backdrop" onClick={closeCsvImportModal}>
          <div className="modal-card csv-import-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">CSV import</p>
            <h3>Fill with CSV</h3>
            <p>Drop a CSV here or choose one from your computer. Each filled line becomes a service row automatically.</p>
            <div className="csv-column-list">
              <span>Keyword</span>
              <span>Domain</span>
              <span>Brand</span>
              <span>Prompt</span>
              <span>Project</span>
            </div>
            <div
              className={`csv-dropzone ${isCsvDragging ? "dragging" : ""}`}
              onClick={() => csvFileInputRef.current?.click()}
              onDragOver={(event) => {
                event.preventDefault();
                setIsCsvDragging(true);
              }}
              onDragLeave={(event) => {
                event.preventDefault();
                setIsCsvDragging(false);
              }}
              onDrop={(event) => {
                event.preventDefault();
                setIsCsvDragging(false);
                void importCsvFile(event.dataTransfer.files?.[0]);
              }}
            >
              <strong>{isImportingCsv ? "Importing CSV..." : "Drag and drop a CSV file here"}</strong>
              <span>or click to select a file from your PC</span>
              <button className="primary-btn" type="button" onClick={(event) => {
                event.stopPropagation();
                csvFileInputRef.current?.click();
              }} disabled={isImportingCsv}>
                {isImportingCsv ? "Importing..." : "Choose CSV"}
              </button>
            </div>
            {csvImportError ? <p className="auth-error">{csvImportError}</p> : null}
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={closeCsvImportModal}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {validationWarning ? (
        <div className="modal-backdrop" onClick={() => setValidationWarning("")}>
          <div className="modal-card warning-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Warning</p>
            <h3>Cannot continue</h3>
            <p>{validationWarning}</p>
            <div className="modal-actions">
              <button className="primary-btn" type="button" onClick={() => setValidationWarning("")}>
                OK
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {projectCreator ? (
        <div className="modal-backdrop" onClick={closeProjectCreator}>
          <div className="modal-card warning-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Project</p>
            <h3>Add project</h3>
            <p>Create a reusable project name for this row and future rows.</p>
            <input
              className="auth-input"
              type="text"
              placeholder="Project name"
              value={projectCreator.value}
              onChange={(event) => setProjectCreator((current) => (current ? { ...current, value: event.target.value } : current))}
            />
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={closeProjectCreator}>
                Cancel
              </button>
              <button className="primary-btn" type="button" onClick={submitProjectCreator}>
                Save project
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {projectDuplicatePrompt ? (
        <div className="modal-backdrop" onClick={() => resolveDuplicateProjectChoice(false)}>
          <div className="modal-card warning-modal" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Warning</p>
            <h3>Similar project already exists</h3>
            <p>
              "{projectDuplicatePrompt.existingValue}" already exists. Are you sure you want to keep "{projectDuplicatePrompt.value}" as a separate project?
            </p>
            <div className="modal-actions">
              <button className="ghost-btn subdued-confirm-btn" type="button" onClick={() => resolveDuplicateProjectChoice(true)}>
                Yes, continue
              </button>
              <button className="primary-btn" type="button" onClick={() => resolveDuplicateProjectChoice(false)}>
                No
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {selectedRunDetail ? (
        <div className="modal-backdrop" onClick={() => setSelectedRunDetail(null)}>
          <div className="history-modal detail-modal" onClick={(event) => event.stopPropagation()}>
            <div className="history-header">
              <div>
                <p className="eyebrow">Run Detail</p>
                <h3>{selectedRunDetail.run.keyword}</h3>
              </div>
              <button className="ghost-btn" type="button" onClick={() => setSelectedRunDetail(null)}>
                Close
              </button>
            </div>

            <div className="detail-grid">
              <section className="history-card">
                <div className="history-card-head">
                  <strong>Run</strong>
                  <span className={`tag ${statusTone(selectedRunDetail.run.status)}`}>{selectedRunDetail.run.status}</span>
                </div>
                <table className="history-table result-table">
                  <tbody>
                    <tr>
                      <th>Project</th>
                      <td>{selectedRunDetail.run.project || "-"}</td>
                      <th>Created</th>
                      <td>{formatDateTime(selectedRunDetail.run.created_at)}</td>
                    </tr>
                    {isAdmin ? (
                      <tr>
                        <th>User</th>
                        <td>{selectedRunDetail.run.username || "-"}</td>
                        <th>Status</th>
                        <td>{selectedRunDetail.run.status}</td>
                      </tr>
                    ) : null}
                    <tr>
                      <th>Domain</th>
                      <td>{selectedRunDetail.run.domain}</td>
                      <th>Iterations</th>
                      <td>
                        {selectedRunDetail.run.completed_iterations}/{selectedRunDetail.run.total_iterations}
                      </td>
                    </tr>
                    <tr>
                      <th>Brand</th>
                      <td>{selectedRunDetail.run.brand}</td>
                      <th>Error</th>
                      <td>{selectedRunDetail.run.error_messages || "-"}</td>
                    </tr>
                    <tr>
                      <th>Estimated spend</th>
                      <td>{formatUsd(selectedRunDetail.estimated_total_cost_usd)}</td>
                      <th>Finished</th>
                      <td>{formatDateTime(selectedRunDetail.run.finished_at)}</td>
                    </tr>
                    <tr>
                      <th>Prompt</th>
                      <td colSpan={3}>{selectedRunDetail.run.prompt}</td>
                    </tr>
                  </tbody>
                </table>
              </section>

              {selectedRunDetail.result ? (
                <section className="history-card">
                  <div className="history-card-head">
                    <strong>Final Result</strong>
                    <span>{selectedRunDetail.result.project || "No project"}</span>
                  </div>
                  <table className="history-table result-table">
                    <tbody>
                      <tr>
                        <th>Mentions</th>
                        <td>{summarizeMentions(selectedRunDetail.result)}</td>
                        <th>Response Avg</th>
                        <td>{formatNumber(selectedRunDetail.result.response_count_avg)}</td>
                      </tr>
                      <tr>
                        <th>Brand List</th>
                        <td>{selectedRunDetail.result.brand_list || "-"}</td>
                        <th>Citation</th>
                        <td>{selectedRunDetail.result.citation_format || "-"}</td>
                      </tr>
                      <tr>
                        <th>Final step spend</th>
                        <td>{formatUsd(selectedRunDetail.result.gemini_sentiment_cost_usd)}</td>
                        <th>Run total</th>
                        <td>{formatUsd(selectedRunDetail.estimated_total_cost_usd)}</td>
                      </tr>
                      <tr>
                        <th>Sentiment</th>
                        <td colSpan={3}>{selectedRunDetail.result.sentiment_analysis || "-"}</td>
                      </tr>
                    </tbody>
                  </table>
                </section>
              ) : null}

              <section className="history-card detail-wide">
                <div className="history-card-head">
                  <strong>LLM Outputs</strong>
                  <span>{selectedRunDetail.outputs.length} iterations</span>
                </div>
                <div className="detail-output-list">
                  {selectedRunDetail.outputs.map((output) => (
                    <article key={output.id} className="detail-output-card">
                      <div className="detail-output-header">
                        <strong>Iteration {output.iteration_number}</strong>
                        <span>{summarizeMentions(output)}</span>
                      </div>
                      <div className="detail-output-grid">
                        <div className="field-stack">
                          <span>GPT Output</span>
                          <div className="detail-output-body">{output.gpt_output || "-"}</div>
                        </div>
                        <div className="field-stack">
                          <span>Gemini Output</span>
                          <div className="detail-output-body">{output.gem_output || "-"}</div>
                        </div>
                      </div>
                      <div className="detail-metrics">
                        <span>Response count: {formatNumber(output.response_count)}</span>
                        <span>Brand list: {output.brand_list || "-"}</span>
                        <span>Citation: {output.citation_format || "-"}</span>
                        <span>OpenAI generation: {formatUsd(output.openai_generation_cost_usd)}</span>
                        <span>Gemini generation: {formatUsd(output.gemini_generation_cost_usd)}</span>
                        <span>Gemini analysis: {formatUsd(output.gemini_analysis_cost_usd)}</span>
                        <span>Iteration total: {formatUsd(output.estimated_total_cost_usd)}</span>
                      </div>
                    </article>
                  ))}
                </div>
              </section>

            </div>
          </div>
        </div>
      ) : null}

      {isLoadingRunDetail ? (
        <div className="modal-backdrop">
          <div className="modal-card">
            <p className="eyebrow">Loading</p>
            <h3>Fetching run detail</h3>
            <p>Please wait.</p>
          </div>
        </div>
      ) : null}
    </>
  );
}














