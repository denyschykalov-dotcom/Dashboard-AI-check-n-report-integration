import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiRequest } from "../api";
import { sourceLabel, SOURCE_ORDER } from "./blockCatalog";
import { REPORT_LANGUAGES } from "./types";
import type {
  AiCommentsResponse,
  AiSearchIndustryResponse,
  AiVisibilityProject,
  AiVisibilityProjectsResponse,
  AiSummaryResponse,
  BlockCatalogResponse,
  Client,
  ClientListResponse,
  ComparisonMode,
  GeneratedBlock,
  GenerateReportResponse,
  PeriodPreset,
  PlannedWorkMode,
  ReportCustomization,
  ReportDetail,
  ReportListResponse,
  ReportSelection,
  ReportSettingsStatus,
  ReportSummary,
  ReportType,
  ReportBlockType,
  ReportLanguage,
} from "./types";

type Props = {
  token: string | null;
};

const PERIOD_OPTIONS: { value: PeriodPreset; label: string }[] = [
  { value: "last_month", label: "Last month" },
  { value: "last_3_months", label: "Last 3 months" },
];

// Each chosen comparison becomes a toggle in the exported report, so a specialist
// can hand the client one report that switches between them.
const COMPARISON_OPTIONS: { value: ComparisonMode; label: string }[] = [
  { value: "mom", label: "The previous period" },
  { value: "yoy", label: "The same period last year" },
];

const PERIOD_VALUES = PERIOD_OPTIONS.map((option) => option.value);
const COMPARISON_VALUES = COMPARISON_OPTIONS.map((option) => option.value);

function isPeriodPreset(value: string | null | undefined): value is PeriodPreset {
  return !!value && (PERIOD_VALUES as string[]).includes(value);
}

function toComparisonModes(values: string[] | null | undefined): ComparisonMode[] {
  return (values ?? []).filter((value): value is ComparisonMode =>
    (COMPARISON_VALUES as string[]).includes(value),
  );
}

const DEFAULT_CUSTOMIZATION: ReportCustomization = {
  accent: "#E6007A",
  charts: {},
  panels: {},
};

// The block whose comment *is* the executive summary at the top of the report.
const SUMMARY_BLOCK_KEY = "summary";
// The intro section Claude researches on the web — the slowest call in the flow.
const SEARCH_INDUSTRY_BLOCK_KEY = "search_industry";

/**
 * The month the metric blocks actually carry.
 *
 * Sheet-backed resolvers report the period they found, which is the newest month
 * present rather than necessarily the one that was asked for. The exported
 * report already relabels itself to match; this is so the builder can say why.
 */
function actualDataPeriod(blocks: GeneratedBlock[]): string | null {
  for (const key of ["ga4_summary", "gsc_summary", "ga4_monetization", "ga4_top_pages"]) {
    const block = blocks.find((item) => item.block_type_key === key && item.status === "ok");
    const period = (block?.data as { period?: unknown } | null | undefined)?.period;
    if (typeof period === "string" && period.trim()) return period.trim();
  }
  return null;
}

/** What Claude is doing right now, so the UI can say so instead of just hanging. */
type AiStage = null | "comments" | "summary" | "industry";

/** The editorial summary section, added when the specialist didn't select it —
 * Claude's summary needs somewhere to render. */
function summaryPlaceholderBlock(): GeneratedBlock {
  return {
    block_type_key: SUMMARY_BLOCK_KEY,
    status: "ok",
    data: { note: "", text: "" },
    unavailable_reason: null,
  };
}

export default function ReportBuilderPage({ token }: Props) {
  const [catalog, setCatalog] = useState<ReportBlockType[]>([]);
  const [clients, setClients] = useState<Client[]>([]);
  const [selectedClientId, setSelectedClientId] = useState<string>("");

  const [showCreateClient, setShowCreateClient] = useState(false);
  const [newClientName, setNewClientName] = useState("");
  const [newClientDomain, setNewClientDomain] = useState("");
  const [newClientLanguage, setNewClientLanguage] = useState<ReportLanguage>("en");
  const [isSavingLanguage, setIsSavingLanguage] = useState(false);

  // Per-client data-source links. SE Ranking needs a project id, and the
  // AI-visibility blocks need to know which AI-check project to read.
  const [aiProjects, setAiProjects] = useState<AiVisibilityProject[]>([]);
  const [seRankingInput, setSeRankingInput] = useState("");
  const [sheetInput, setSheetInput] = useState("");
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  // Blur-saving is invisible by nature, so the field itself has to show state:
  // accent-highlighted while a typed change is still unsaved, then a brief
  // confirmation once it lands.
  const [justSavedField, setJustSavedField] = useState<string | null>(null);

  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());

  const [periodPreset, setPeriodPreset] = useState<PeriodPreset>("last_month");
  const [comparisons, setComparisons] = useState<ComparisonMode[]>(["mom"]);
  const [useAdvanced, setUseAdvanced] = useState<boolean>(false);
  const [reportType, setReportType] = useState<ReportType>("monthly");
  const [dateFrom, setDateFrom] = useState<string>("");
  const [dateTo, setDateTo] = useState<string>("");
  const [reportYear, setReportYear] = useState<string>(String(new Date().getFullYear()));

  const [plannedWorkMode, setPlannedWorkMode] = useState<PlannedWorkMode>("clickup");
  const [plannedWorkText, setPlannedWorkText] = useState<string>("");
  // The client id whose saved selection has finished loading — guards the
  // auto-save effect so it never clobbers a selection before it's restored.
  const selectionLoadedFor = useRef<string | null>(null);

  const [generated, setGenerated] = useState<GenerateReportResponse | null>(null);
  const [comments, setComments] = useState<Record<string, string>>({});
  const [customization, setCustomization] = useState<ReportCustomization>(DEFAULT_CUSTOMIZATION);
  const [previewHtml, setPreviewHtml] = useState<string>("");
  const [previewExpanded, setPreviewExpanded] = useState(false);
  const [editingReportId, setEditingReportId] = useState<string | null>(null);

  const [savedReports, setSavedReports] = useState<ReportSummary[]>([]);
  // `${reportId}:${format}` of the export currently in flight, so only that
  // one button shows a loading state (PDF rendering takes a few seconds).
  const [exportingReportId, setExportingReportId] = useState<string | null>(null);
  // Deleting a saved report drops it and its blocks for everyone, so it goes
  // through a confirmation step rather than firing straight off the row button.
  const [reportPendingDelete, setReportPendingDelete] = useState<ReportSummary | null>(null);
  const [isDeletingReport, setIsDeletingReport] = useState(false);

  const [isGenerating, setIsGenerating] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [aiStage, setAiStage] = useState<AiStage>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  // Claude commentary is a draft-writing convenience: when it is unavailable the
  // report is still complete and saveable, so its failures are a notice, never
  // the page-level error.
  const [aiNotice, setAiNotice] = useState<string | null>(null);

  // Once a report already has a summary, "Save" turns into an explicit
  // "Regenerate Summary" action that asks for optional guidance first, rather
  // than silently rewriting the summary on every save.
  const [showRegenerateSummaryModal, setShowRegenerateSummaryModal] = useState(false);
  const [summaryGuidance, setSummaryGuidance] = useState("");

  const [settings, setSettings] = useState<ReportSettingsStatus | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [clickupTokenInput, setClickupTokenInput] = useState("");
  const [isSavingToken, setIsSavingToken] = useState(false);

  const selectedClient = useMemo(
    () => clients.find((client) => client.id === selectedClientId) ?? null,
    [clients, selectedClientId],
  );

  const groupedCatalog = useMemo(() => {
    const groups = new Map<string, ReportBlockType[]>();
    catalog.forEach((block) => {
      const list = groups.get(block.source) ?? [];
      list.push(block);
      groups.set(block.source, list);
    });
    return SOURCE_ORDER.filter((source) => groups.has(source)).map((source) => ({
      source,
      blocks: groups.get(source) as ReportBlockType[],
    }));
  }, [catalog]);

  const yearOptions = useMemo(() => {
    const now = new Date().getFullYear();
    return Array.from({ length: 7 }, (_, index) => String(now - index));
  }, []);

  // The timeframe portion of a generate/selection request. In preset mode the
  // backend derives the window from the period preset, so no dates are sent, and
  // the chosen comparisons ride along; in Advanced mode the custom-range /
  // full-year controls drive the window instead.
  const buildTimeframe = useCallback((): {
    period_preset: PeriodPreset | null;
    comparisons: ComparisonMode[];
    report_type: ReportType;
    date_from: string | null;
    date_to: string | null;
  } => {
    if (!useAdvanced) {
      return {
        period_preset: periodPreset,
        comparisons,
        report_type: "monthly",
        date_from: null,
        date_to: null,
      };
    }
    const base = { period_preset: null, comparisons };
    if (reportType === "yearly") {
      return { ...base, report_type: "yearly", date_from: `${reportYear}-01-01`, date_to: `${reportYear}-12-31` };
    }
    return { ...base, report_type: "monthly", date_from: dateFrom || null, date_to: dateTo || null };
  }, [useAdvanced, periodPreset, comparisons, reportType, reportYear, dateFrom, dateTo]);

  function toggleComparison(mode: ComparisonMode) {
    setComparisons((current) => {
      if (current.includes(mode)) {
        // never leave the report with no comparison to show
        return current.length > 1 ? current.filter((value) => value !== mode) : current;
      }
      // keep the catalog order so the report's toggles are always in the same order
      return COMPARISON_VALUES.filter((value) => value === mode || current.includes(value));
    });
  }

  const loadClients = useCallback(async () => {
    if (!token) return;
    const response = await apiRequest<ClientListResponse>("/api/report-builder/clients", { token });
    setClients(response.clients);
  }, [token]);

  // The AI-check projects that actually have runs, so the picker offers real
  // options instead of asking the specialist to remember a label.
  const loadAiProjects = useCallback(async () => {
    if (!token) return;
    try {
      const response = await apiRequest<AiVisibilityProjectsResponse>(
        "/api/report-builder/ai-visibility-projects",
        { token },
      );
      setAiProjects(response.projects ?? []);
    } catch {
      setAiProjects([]); // best-effort: the picker degrades to a free-text state
    }
  }, [token]);

  const loadSavedReports = useCallback(
    async (clientId: string) => {
      if (!token || !clientId) {
        setSavedReports([]);
        return;
      }
      const response = await apiRequest<ReportListResponse>(
        `/api/report-builder/clients/${clientId}/reports`,
        { token },
      );
      setSavedReports(response.reports);
    },
    [token],
  );

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        const response = await apiRequest<BlockCatalogResponse>("/api/report-builder/block-catalog", {
          token,
        });
        setCatalog(response.blocks);
        await loadClients();
        await loadAiProjects();
        const settingsResponse = await apiRequest<ReportSettingsStatus>("/api/report-builder/settings", {
          token,
        });
        setSettings(settingsResponse);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load the report builder.");
      }
    })();
  }, [token, loadClients, loadAiProjects]);

  // Keep the SE Ranking box in step with whichever client is selected.
  useEffect(() => {
    setSeRankingInput(selectedClient?.se_ranking_target ?? "");
  }, [selectedClient?.id, selectedClient?.se_ranking_target]);

  useEffect(() => {
    setSheetInput(selectedClient?.ga4_sheet_id ?? "");
  }, [selectedClient?.id, selectedClient?.ga4_sheet_id]);

  const loadSelection = useCallback(
    async (clientId: string) => {
      if (!token || !clientId) return;
      const selection = await apiRequest<ReportSelection>(
        `/api/report-builder/clients/${clientId}/selection`,
        { token },
      );
      setSelectedKeys(new Set(selection.block_keys));
      const kind: ReportType = selection.report_type === "yearly" ? "yearly" : "monthly";
      setReportType(kind);
      setDateFrom(selection.date_from ?? "");
      setDateTo(selection.date_to ?? "");
      if (kind === "yearly" && selection.date_from) {
        setReportYear(selection.date_from.slice(0, 4));
      }
      const savedComparisons = toComparisonModes(selection.comparisons);
      if (savedComparisons.length > 0) {
        setComparisons(savedComparisons);
      }
      // A saved period preset means preset mode; otherwise the specialist last
      // used the Advanced custom-range / full-year controls.
      if (isPeriodPreset(selection.period_preset)) {
        setPeriodPreset(selection.period_preset);
        setUseAdvanced(false);
      } else {
        setUseAdvanced(true);
      }
    },
    [token],
  );

  useEffect(() => {
    if (!selectedClientId) {
      setSavedReports([]);
      return;
    }
    void loadSavedReports(selectedClientId).catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : "Failed to load saved reports.");
    });
  }, [selectedClientId, loadSavedReports]);

  useEffect(() => {
    if (!selectedClientId) {
      selectionLoadedFor.current = null;
      return;
    }
    selectionLoadedFor.current = null;
    void loadSelection(selectedClientId)
      .catch(() => {
        // no saved selection yet (or load failed) — start from a clean slate
        setSelectedKeys(new Set());
      })
      .finally(() => {
        selectionLoadedFor.current = selectedClientId;
      });
  }, [selectedClientId, loadSelection]);

  // Persist the checkbox selection + timeframe per client (debounced) so
  // reopening a client restores the previous report's starting point.
  useEffect(() => {
    if (!token || !selectedClientId) return;
    if (selectionLoadedFor.current !== selectedClientId) return;
    const timeframe = buildTimeframe();
    const handle = window.setTimeout(() => {
      void apiRequest(`/api/report-builder/clients/${selectedClientId}/selection`, {
        method: "PUT",
        token,
        body: {
          block_keys: Array.from(selectedKeys),
          period_preset: timeframe.period_preset,
          comparisons: timeframe.comparisons,
          report_type: timeframe.report_type,
          date_from: timeframe.date_from,
          date_to: timeframe.date_to,
        },
      }).catch(() => {
        // best-effort persistence; a failed save must not disrupt the UI
      });
    }, 600);
    return () => window.clearTimeout(handle);
  }, [token, selectedClientId, selectedKeys, buildTimeframe]);

  // Esc leaves the full-screen preview, and the page behind it must not scroll
  // out from under the overlay.
  useEffect(() => {
    if (!previewExpanded) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setPreviewExpanded(false);
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [previewExpanded]);

  function resetReportState() {
    setGenerated(null);
    setComments({});
    setEditingReportId(null);
    setPreviewHtml("");
    setAiNotice(null);
  }

  function toggleBlock(key: string) {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  async function handleSaveClickupToken() {
    if (!token || !clickupTokenInput.trim()) return;
    setError(null);
    setStatus(null);
    setIsSavingToken(true);
    try {
      const response = await apiRequest<ReportSettingsStatus>("/api/report-builder/settings/clickup", {
        method: "PUT",
        token,
        body: { token: clickupTokenInput.trim() },
      });
      setSettings(response);
      setClickupTokenInput("");
      setStatus(
        response.clickup_username
          ? `ClickUp connected as ${response.clickup_username}.`
          : "ClickUp API key saved.",
      );
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save ClickUp API key.");
    } finally {
      setIsSavingToken(false);
    }
  }

  async function handleClearClickupToken() {
    if (!token) return;
    setError(null);
    try {
      const response = await apiRequest<ReportSettingsStatus>("/api/report-builder/settings/clickup", {
        method: "DELETE",
        token,
      });
      setSettings(response);
      setStatus("ClickUp API key removed.");
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : "Failed to remove ClickUp API key.");
    }
  }

  async function handleCreateClient() {
    if (!token) return;
    setError(null);
    try {
      const client = await apiRequest<Client>("/api/report-builder/clients", {
        method: "POST",
        token,
        body: {
          name: newClientName.trim(),
          domain: newClientDomain.trim(),
          report_language: newClientLanguage,
        },
      });
      await loadClients();
      setSelectedClientId(client.id);
      setShowCreateClient(false);
      setNewClientName("");
      setNewClientDomain("");
      setNewClientLanguage("en");
      setStatus(`Client "${client.name}" created.`);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Failed to create client.");
    }
  }

  /** Persist a per-client data-source link (SE Ranking id / AI-visibility project). */
  async function saveClientSettings(patch: {
    se_ranking_target?: string;
    ai_visibility_project?: string;
    ga4_sheet_id?: string;
  }) {
    if (!token || !selectedClientId) return;
    setError(null);
    setIsSavingSettings(true);
    try {
      const updated = await apiRequest<Client>(
        `/api/report-builder/clients/${selectedClientId}/settings`,
        { method: "PUT", token, body: patch },
      );
      setClients((current) =>
        current.map((client) => (client.id === updated.id ? updated : client)),
      );
      const savedField = Object.keys(patch)[0] ?? null;
      setJustSavedField(savedField);
      window.setTimeout(
        () => setJustSavedField((current) => (current === savedField ? null : current)),
        2500,
      );
      setStatus(
        patch.ga4_sheet_id !== undefined
          ? patch.ga4_sheet_id
            ? "GA4/GSC sheet set — regenerate to pull from it."
            : "GA4/GSC sheet cleared — it will be looked up by name in Drive again."
        : patch.ai_visibility_project !== undefined
          ? patch.ai_visibility_project
            ? `AI-visibility data will be read from project “${patch.ai_visibility_project}”.`
            : "AI-visibility data will fall back to matching the client's name."
          : patch.se_ranking_target
            ? "SE Ranking target saved — regenerate to load tracked keywords."
            : "SE Ranking target cleared.",
      );
    } catch (settingsError) {
      setError(
        settingsError instanceof Error ? settingsError.message : "Failed to save client settings.",
      );
    } finally {
      setIsSavingSettings(false);
    }
  }

  /** Change the language this client's reports are delivered in. */
  async function handleChangeLanguage(language: ReportLanguage) {
    if (!token || !selectedClientId) return;
    setError(null);
    setIsSavingLanguage(true);
    // Reflect the change immediately — the preview re-renders from it.
    setClients((current) =>
      current.map((client) =>
        client.id === selectedClientId ? { ...client, report_language: language } : client,
      ),
    );
    try {
      await apiRequest<Client>(`/api/report-builder/clients/${selectedClientId}/language`, {
        method: "PUT",
        token,
        body: { report_language: language },
      });
      await loadClients();
      const label =
        REPORT_LANGUAGES.find((option) => option.value === language)?.label ?? language;
      // The preview renders its labels from the client's language, so re-render it.
      // Commentary already drafted stays in the language it was written in until
      // "Rewrite comments" is used — say so rather than let it look like a bug.
      if (generated) {
        await refreshPreview(blocksForSave(), generated, customization);
      }
      setStatus(
        language === "en"
          ? "Reports for this client will be delivered in English."
          : `Reports for this client will be translated into ${label}.` +
              (generated
                ? " Labels updated — use “Rewrite comments” to translate the existing commentary."
                : ""),
      );
    } catch (languageError) {
      await loadClients();
      setError(
        languageError instanceof Error ? languageError.message : "Failed to change report language.",
      );
    } finally {
      setIsSavingLanguage(false);
    }
  }

  /** The report payload: each block plus the comment currently attached to it. */
  function withComments(
    blocks: GeneratedBlock[],
    commentMap: Record<string, string>,
  ): GeneratedBlock[] {
    return blocks.map((block) => ({
      ...block,
      comment: commentMap[block.block_type_key] ?? "",
    }));
  }

  function blocksForSave(): GeneratedBlock[] {
    if (!generated) return [];
    return withComments(generated.blocks, comments);
  }

  // Render the editable preview from an explicit set of blocks. Called at the
  // points where the report actually changes (generate, open, post-summary) —
  // never on every comment keystroke, which would reload the iframe mid-edit and
  // lose focus and scroll position.
  const refreshPreview = useCallback(
    async (
      blocks: GeneratedBlock[],
      meta: { period_label: string; default_comparison: string },
      custom: ReportCustomization,
    ) => {
      if (!token || !selectedClientId) {
        setPreviewHtml("");
        return;
      }
      try {
        const response = await fetch("/api/report-builder/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({
            client_id: selectedClientId,
            period_label: meta.period_label,
            default_comparison: meta.default_comparison,
            customization: custom,
            blocks,
          }),
        });
        if (response.ok) {
          setPreviewHtml(await response.text());
        }
      } catch {
        // preview is best-effort; keep the last good render on failure
      }
    },
    [token, selectedClientId],
  );

  /** Claude's draft comment for every section that has data, keyed by block key. */
  async function draftComments(
    report: { period_label: string; default_comparison: string; blocks: GeneratedBlock[] },
  ): Promise<Record<string, string>> {
    const response = await apiRequest<AiCommentsResponse>("/api/report-builder/ai/comments", {
      method: "POST",
      token,
      body: {
        client_id: selectedClientId,
        period_label: report.period_label,
        default_comparison: report.default_comparison,
        blocks: report.blocks,
      },
    });
    return response.comments ?? {};
  }

  /**
   * The month's Google-search context for the intro section.
   *
   * Its own request because it is researched with web search and takes about a
   * minute and a half — far longer than everything else. Folded into the
   * comments call it held the preview back by two minutes, which read as a blank
   * page. Returns "" when nothing reliable was found.
   */
  async function draftSearchIndustry(
    report: { period_label: string; default_comparison: string; blocks: GeneratedBlock[] },
  ): Promise<string> {
    const response = await apiRequest<AiSearchIndustryResponse>(
      "/api/report-builder/ai/search-industry",
      {
        method: "POST",
        token,
        body: {
          client_id: selectedClientId,
          period_label: report.period_label,
          default_comparison: report.default_comparison,
          blocks: report.blocks,
        },
      },
    );
    return response.text ?? "";
  }

  async function handleGenerate() {
    if (!token || !selectedClientId || selectedKeys.size === 0) return;
    setError(null);
    setStatus(null);
    setAiNotice(null);
    setIsGenerating(true);
    try {
      const timeframe = buildTimeframe();
      const response = await apiRequest<GenerateReportResponse>("/api/report-builder/generate", {
        method: "POST",
        token,
        body: {
          client_id: selectedClientId,
          block_keys: Array.from(selectedKeys),
          period_preset: timeframe.period_preset,
          comparisons: timeframe.comparisons,
          report_type: timeframe.report_type,
          date_from: timeframe.date_from,
          date_to: timeframe.date_to,
          planned_work_mode: plannedWorkMode,
          planned_work_text: plannedWorkText,
        },
      });

      let nextComments: Record<string, string> = {};
      response.blocks.forEach((block) => {
        nextComments[block.block_type_key] = "";
      });

      // Show the report as soon as the data exists. The Claude calls below take
      // up to two minutes between them, and the preview panel only renders once
      // `generated` is set — so waiting for them first left the specialist
      // looking at a blank page with no indication anything was happening.
      setComments(nextComments);
      setGenerated(response);
      setEditingReportId(null);
      await refreshPreview(withComments(response.blocks, nextComments), response, customization);
      setIsGenerating(false);

      // A client's GA4/GSC sheet may not have the requested month yet. The
      // resolvers fall back to the newest month present and the report quietly
      // relabels itself, so the specialist asks for July and is handed June with
      // no explanation. Say so rather than let them wonder why it "looks wrong".
      const dataPeriod = actualDataPeriod(response.blocks);
      if (dataPeriod && dataPeriod !== response.period_label) {
        setAiNotice(
          `This client's GA4/GSC data does not reach ${response.period_label} yet — ` +
            `the report was built from ${dataPeriod}, and is labelled ${dataPeriod} throughout.`,
        );
      }

      // Kick the web research off now rather than after the comments: the two are
      // independent, and run back to back they'd add up to over two minutes.
      // `.catch` is attached immediately so a rejection can't go unhandled while
      // we await the comments first.
      const industryPending = selectedKeys.has(SEARCH_INDUSTRY_BLOCK_KEY)
        ? draftSearchIndustry(response).catch(() => null)
        : Promise.resolve("");

      // Section commentary: fills into the preview already on screen.
      setAiStage("comments");
      try {
        nextComments = { ...nextComments, ...(await draftComments(response)) };
        setComments(nextComments);
        await refreshPreview(withComments(response.blocks, nextComments), response, customization);
        setStatus("Claude drafted the section comments — review and edit them in the preview.");
      } catch (aiError) {
        setAiNotice(
          `Claude could not draft the comments (${
            aiError instanceof Error ? aiError.message : "unknown error"
          }). The report is ready — write the comments yourself in the preview.`,
        );
      } finally {
        setAiStage(null);
      }

      // Search-industry context, started above and collected last so nothing else
      // ever waits on it.
      if (selectedKeys.has(SEARCH_INDUSTRY_BLOCK_KEY)) {
        setAiStage("industry");
        const industry = await industryPending;
        setAiStage(null);
        if (industry === null) {
          setAiNotice(
            "Claude could not research this month's search industry. Write that section yourself in the preview.",
          );
        } else if (industry) {
          nextComments = { ...nextComments, [SEARCH_INDUSTRY_BLOCK_KEY]: industry };
          setComments(nextComments);
          await refreshPreview(withComments(response.blocks, nextComments), response, customization);
          setStatus("Claude added this month's search-industry context.");
        }
      }
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : "Failed to generate report.");
    } finally {
      setIsGenerating(false);
    }
  }

  /** Re-run the comment drafting on the report currently in the preview. */
  async function handleRedraftComments() {
    if (!token || !generated || !selectedClientId) return;
    setError(null);
    setStatus(null);
    setAiNotice(null);
    setAiStage("comments");
    try {
      const drafted = await draftComments({
        period_label: generated.period_label,
        default_comparison: generated.default_comparison,
        blocks: blocksForSave(),
      });
      // The summary is Opus's job at submit time — a comment redraft leaves it alone.
      const nextComments = { ...comments, ...drafted };
      setComments(nextComments);
      await refreshPreview(withComments(generated.blocks, nextComments), generated, customization);
      setStatus("Claude rewrote the section comments.");
    } catch (aiError) {
      setAiNotice(
        aiError instanceof Error ? aiError.message : "Claude could not rewrite the comments.",
      );
    } finally {
      setAiStage(null);
    }
  }

  // Notes and per-panel config are edited *inside* the preview; the iframe posts
  // each change up so it persists on Save — without reloading the iframe (which
  // would lose focus/scroll). The preview itself only reloads on a new report.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const data = event.data as
        | { source?: string; kind?: string; key?: string | null; value?: unknown }
        | null;
      if (!data || data.source !== "report-preview") return;
      // The preview only reports notes and chart-type choices now: the accent is
      // fixed pink and per-panel text sizing was removed, so neither can change.
      if (data.kind === "note" && typeof data.key === "string") {
        const key = data.key;
        setComments((current) => ({ ...current, [key]: String(data.value ?? "") }));
      } else if (data.kind === "chart" && typeof data.key === "string") {
        const key = data.key;
        setCustomization((current) => ({ ...current, charts: { ...current.charts, [key]: String(data.value ?? "") } }));
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  /**
   * Submit: Claude reads the whole submitted report — data plus the comments the
   * specialist just reviewed — and writes the executive summary that opens it.
   * The preview is updated with that summary, and only then is the report stored
   * as the final variant. A Claude failure here degrades to saving without a
   * generated summary rather than losing the report.
   */
  async function writeSummaryAndSave(guidance: string) {
    if (!token || !generated || !selectedClientId) return;
    setError(null);
    setStatus(null);
    setAiNotice(null);
    setIsSaving(true);
    try {
      // The summary needs a section to render in; add one if it wasn't selected.
      const hasSummaryBlock = generated.blocks.some(
        (block) => block.block_type_key === SUMMARY_BLOCK_KEY,
      );
      const reportBlocks = hasSummaryBlock
        ? generated.blocks
        : [...generated.blocks, summaryPlaceholderBlock()];
      let finalComments = comments;

      setAiStage("summary");
      try {
        const response = await apiRequest<AiSummaryResponse>("/api/report-builder/ai/summary", {
          method: "POST",
          token,
          body: {
            client_id: selectedClientId,
            period_label: generated.period_label,
            default_comparison: generated.default_comparison,
            blocks: withComments(reportBlocks, comments),
            existing_summary: comments[SUMMARY_BLOCK_KEY] ?? "",
            summary_guidance: guidance,
          },
        });
        finalComments = { ...comments, [SUMMARY_BLOCK_KEY]: response.summary };
        setComments(finalComments);
        if (!hasSummaryBlock) {
          setGenerated({ ...generated, blocks: reportBlocks });
          setSelectedKeys((current) => new Set(current).add(SUMMARY_BLOCK_KEY));
        }
        // Show the specialist the summary that is about to be saved.
        await refreshPreview(
          withComments(reportBlocks, finalComments),
          generated,
          customization,
        );
        setStatus("Claude wrote the report summary — saving the final version…");
      } catch (aiError) {
        setAiNotice(
          `Claude could not write the report summary (${
            aiError instanceof Error ? aiError.message : "unknown error"
          }). Saving the report as it stands.`,
        );
      } finally {
        setAiStage(null);
      }

      const finalBlocks = withComments(reportBlocks, finalComments);
      if (editingReportId) {
        await apiRequest<ReportSummary>(`/api/report-builder/reports/${editingReportId}`, {
          method: "PUT",
          token,
          body: {
            period_label: generated.period_label,
            default_comparison: generated.default_comparison,
            customization,
            blocks: finalBlocks,
          },
        });
        setStatus("Report updated.");
      } else {
        const saved = await apiRequest<ReportSummary>("/api/report-builder/reports", {
          method: "POST",
          token,
          body: {
            client_id: selectedClientId,
            period_label: generated.period_label,
            default_comparison: generated.default_comparison,
            customization,
            blocks: finalBlocks,
          },
        });
        setEditingReportId(saved.id);
        setStatus("Report saved.");
      }
      await loadSavedReports(selectedClientId);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save report.");
    } finally {
      setIsSaving(false);
    }
  }

  /** First save of a report: writes the summary with no special guidance. */
  async function handleSave() {
    await writeSummaryAndSave("");
  }

  /**
   * Persist the report exactly as it stands — no Claude, nothing rewritten.
   *
   * This is the plain "I edited the text, keep it" action. Regenerating the
   * summary is a separate, explicit choice: rolling the two together meant every
   * save overwrote whatever the specialist had just typed into the summary.
   */
  async function handleSaveEdits() {
    if (!token || !generated || !selectedClientId) return;
    setError(null);
    setStatus(null);
    setAiNotice(null);
    setIsSaving(true);
    try {
      const finalBlocks = blocksForSave();
      if (editingReportId) {
        await apiRequest<ReportSummary>(`/api/report-builder/reports/${editingReportId}`, {
          method: "PUT",
          token,
          body: {
            period_label: generated.period_label,
            default_comparison: generated.default_comparison,
            customization,
            blocks: finalBlocks,
          },
        });
      } else {
        const saved = await apiRequest<ReportSummary>("/api/report-builder/reports", {
          method: "POST",
          token,
          body: {
            client_id: selectedClientId,
            period_label: generated.period_label,
            default_comparison: generated.default_comparison,
            customization,
            blocks: finalBlocks,
          },
        });
        setEditingReportId(saved.id);
      }
      setStatus("Report saved — your edits were kept as written.");
      await loadSavedReports(selectedClientId);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to save report.");
    } finally {
      setIsSaving(false);
    }
  }

  function openRegenerateSummaryModal() {
    setSummaryGuidance("");
    setShowRegenerateSummaryModal(true);
  }

  /** Re-run the summary (and re-save) with whatever the specialist typed. */
  async function handleRegenerateSummaryConfirm() {
    const guidance = summaryGuidance;
    setShowRegenerateSummaryModal(false);
    await writeSummaryAndSave(guidance);
  }

  async function handleOpenReport(reportId: string) {
    if (!token) return;
    setError(null);
    setStatus(null);
    setAiNotice(null);
    try {
      const detail = await apiRequest<ReportDetail>(`/api/report-builder/reports/${reportId}`, { token });
      const reopened = {
        client_id: detail.client_id,
        period_label: detail.period_label,
        default_comparison: detail.default_comparison,
        blocks: detail.blocks,
      };
      setGenerated(reopened);
      const loadedComments: Record<string, string> = {};
      const loadedKeys = new Set<string>();
      detail.blocks.forEach((block) => {
        loadedComments[block.block_type_key] = block.comment ?? "";
        loadedKeys.add(block.block_type_key);
        if (block.block_type_key === "planned_works" && block.data && (block.data as { mode?: string }).mode === "manual") {
          setPlannedWorkMode("manual");
          setPlannedWorkText(String((block.data as { text?: string }).text ?? ""));
        }
      });
      setComments(loadedComments);
      setSelectedKeys(loadedKeys);
      const reopenedCustomization = detail.customization ?? DEFAULT_CUSTOMIZATION;
      setCustomization(reopenedCustomization);
      setEditingReportId(detail.id);
      // A reopened report keeps the comments and summary it was saved with —
      // nothing is redrafted behind the specialist's back.
      await refreshPreview(
        withComments(detail.blocks, loadedComments),
        reopened,
        reopenedCustomization,
      );
      setStatus(`Opened report from ${new Date(detail.updated_at).toLocaleString()}.`);
    } catch (openError) {
      setError(openError instanceof Error ? openError.message : "Failed to open report.");
    }
  }

  async function handleDeleteReportConfirm() {
    if (!token || !reportPendingDelete) return;
    const target = reportPendingDelete;
    setError(null);
    setStatus(null);
    setIsDeletingReport(true);
    try {
      await apiRequest<void>(`/api/report-builder/reports/${target.id}`, {
        method: "DELETE",
        token,
      });
      setReportPendingDelete(null);
      // The open editor is a view of that report; keep editing it and "Save"
      // would 404 against a row that no longer exists.
      if (editingReportId === target.id) {
        setEditingReportId(null);
        setGenerated(null);
        setPreviewHtml("");
      }
      if (selectedClientId) {
        await loadSavedReports(selectedClientId);
      }
      setStatus(`Deleted the ${target.period_label} report.`);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Failed to delete report.");
    } finally {
      setIsDeletingReport(false);
    }
  }

  async function handleExport(reportId: string, format: "html" | "pdf" | "md" = "html") {
    if (!token) return;
    setError(null);
    setExportingReportId(`${reportId}:${format}`);
    try {
      const response = await fetch(`/api/report-builder/reports/${reportId}/export?format=${format}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        let detail = "";
        try {
          detail = ((await response.json()) as { detail?: string }).detail || "";
        } catch {
          // response wasn't JSON (e.g. a plain error) — fall back to the status below
        }
        throw new Error(detail || `Export failed with ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      link.download = match ? match[1] : `report-${reportId}.${format}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "Failed to export report.");
    } finally {
      setExportingReportId(null);
    }
  }

  async function handlePreview(reportId: string) {
    if (!token) return;
    setError(null);
    try {
      const response = await fetch(`/api/report-builder/reports/${reportId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        throw new Error(`Preview failed with ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      // give the new tab time to load before revoking
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (previewError) {
      setError(previewError instanceof Error ? previewError.message : "Failed to preview report.");
    }
  }

  if (!token) {
    return (
      <section className="page active">
        <div className="panel auth-gate">
          <p className="eyebrow">Authentication Required</p>
          <h3>Sign in to build client reports.</h3>
        </div>
      </section>
    );
  }

  const canGenerate =
    Boolean(selectedClientId) && selectedKeys.size > 0 && !isGenerating && aiStage === null;

  return (
    <section className="page active report-builder-page">
      {error ? <div className="status-banner">{error}</div> : null}
      {status ? <div className="status-banner">{status}</div> : null}
      {aiNotice ? <div className="status-banner">{aiNotice}</div> : null}

      {/* Integrations / settings */}
      <article className="panel report-settings-panel">
        <div className="report-settings-head">
          <div>
            <p className="eyebrow">Integrations</p>
            <h3>
              ClickUp{" "}
              {settings?.clickup_configured ? (
                <span className="report-ok">✓ connected ({settings.clickup_token_hint})</span>
              ) : (
                <span className="report-unavailable">not connected</span>
              )}
            </h3>
          </div>
          <button className="ghost-btn" type="button" onClick={() => setShowSettings((v) => !v)}>
            {showSettings ? "Hide" : "Manage"}
          </button>
        </div>
        {showSettings ? (
          <div className="report-settings-body">
            <p className="report-hint">
              Enter your personal ClickUp API token. It is stored encrypted and used only to pull your
              workspace's task lists for the Work completed / Planned works blocks, matched to each client
              by name.
            </p>
            <label className="field-stack">
              <span>
                ClickUp API token{" "}
                <span
                  className="info-badge"
                  tabIndex={0}
                  role="img"
                  aria-label="Where to find your ClickUp API token"
                  title="Where to find it: Open ClickUp → Profile → Settings → ClickUp API → Generate API Token"
                >
                  i
                </span>
              </span>
              <input
                className="auth-input"
                type="password"
                autoComplete="off"
                value={clickupTokenInput}
                onChange={(event) => setClickupTokenInput(event.target.value)}
                placeholder={settings?.clickup_configured ? "Enter a new token to replace" : "pk_..."}
              />
            </label>
            <div className="modal-actions">
              {settings?.clickup_configured ? (
                <button className="ghost-btn" type="button" onClick={() => void handleClearClickupToken()}>
                  Remove
                </button>
              ) : null}
              <button
                className="primary-btn"
                type="button"
                onClick={() => void handleSaveClickupToken()}
                disabled={isSavingToken || !clickupTokenInput.trim()}
              >
                {isSavingToken ? "Verifying…" : "Save & verify"}
              </button>
            </div>
          </div>
        ) : null}
      </article>

      {/* Step 1: client selection */}
      <article className="panel">
        <p className="eyebrow">Step 1</p>
        <h3>Choose a client</h3>
        <div className="field-stack">
          <span>Client</span>
          <select
            className="auth-input"
            value={selectedClientId}
            onChange={(event) => {
              setSelectedClientId(event.target.value);
              resetReportState();
            }}
          >
            <option value="">Select a client…</option>
            {clients.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name} ({client.domain})
              </option>
            ))}
          </select>
        </div>
        {selectedClient ? (
          <label className="field-stack">
            <span>Report language</span>
            <select
              className="auth-input"
              value={selectedClient.report_language}
              disabled={isSavingLanguage}
              onChange={(event) =>
                void handleChangeLanguage(event.target.value as ReportLanguage)
              }
            >
              {REPORT_LANGUAGES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <small className="muted">
              {selectedClient.report_language === "en"
                ? "Reports are written in English."
                : "Reports are written in English, then translated by Claude — commentary, summary and labels."}
            </small>
          </label>
        ) : null}

        {selectedClient ? (
          <>
            {/* Without a target the SE Ranking block resolves "not configured"
                and the section is dropped from the report. */}
            <label className="field-stack">
              <span>SE Ranking project — ID or domain</span>
              {/* Saves on blur, like the two selects either side of it. A separate
                  Save button here was easy to miss, so a typed id could be lost. */}
              <input
                className={`auth-input${
                  seRankingInput !== (selectedClient.se_ranking_target ?? "") ? " is-unsaved" : ""
                }`}
                value={seRankingInput}
                disabled={isSavingSettings}
                onChange={(event) => setSeRankingInput(event.target.value)}
                onBlur={() => {
                  if (seRankingInput !== (selectedClient.se_ranking_target ?? "")) {
                    void saveClientSettings({ se_ranking_target: seRankingInput });
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") event.currentTarget.blur();
                }}
                placeholder="6941585 — or a domain, e.g. tarscoboltedtank.com"
              />
              {seRankingInput !== (selectedClient.se_ranking_target ?? "") ? (
                <small className="field-pending">
                  Unsaved — press Enter or click outside the field to save.
                </small>
              ) : justSavedField === "se_ranking_target" ? (
                <small className="field-saved">Saved.</small>
              ) : null}
              <small className="muted">
                {selectedClient.se_ranking_target ? (
                  <>
                    Tracked keywords load from the SE Ranking project matching{" "}
                    <strong>{selectedClient.se_ranking_target}</strong>.
                  </>
                ) : (
                  "Not set — the SE Ranking section is skipped for this client."
                )}{" "}
                Accepts the numeric project ID (<code>6941585</code>) or the site's
                domain or name (<code>tarscoboltedtank.com</code>, <code>tarsco</code>) —
                matching ignores <code>https://</code>, <code>www.</code> and case.
                Leave empty if this client isn't tracked in SE Ranking.
              </small>
            </label>

            {/* Without an explicit id the sheet is looked up in Drive by name.
                A folder holding both "partsvu" and an abandoned "partsvu.com"
                resolves to whichever the priority order happens to hit, so the
                report can silently come from a sheet nobody updates any more. */}
            <label className="field-stack">
              <span>GA4 / GSC spreadsheet</span>
              <input
                className={`auth-input${
                  sheetInput !== (selectedClient.ga4_sheet_id ?? "") ? " is-unsaved" : ""
                }`}
                value={sheetInput}
                disabled={isSavingSettings}
                onChange={(event) => setSheetInput(event.target.value)}
                onBlur={() => {
                  if (sheetInput !== (selectedClient.ga4_sheet_id ?? "")) {
                    void saveClientSettings({ ga4_sheet_id: sheetInput });
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") event.currentTarget.blur();
                }}
                placeholder="Paste the sheet URL or ID — empty to look it up by name"
              />
              {sheetInput !== (selectedClient.ga4_sheet_id ?? "") ? (
                <small className="field-pending">
                  Unsaved — press Enter or click outside the field to save.
                </small>
              ) : justSavedField === "ga4_sheet_id" ? (
                <small className="field-saved">Saved.</small>
              ) : null}
              <small className="muted">
                {selectedClient.ga4_sheet_id ? (
                  <>
                    Reading{" "}
                    <a
                      href={`https://docs.google.com/spreadsheets/d/${selectedClient.ga4_sheet_id}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      this spreadsheet
                    </a>
                    . Open it to check it has the month you are reporting on.
                  </>
                ) : (
                  "Not set — the sheet is matched by client name in the shared Drive folder."
                )}
              </small>
            </label>

            {/* AI-visibility data is matched by project name, which rarely equals
                the client name — so let the specialist pick from what exists. */}
            <label className="field-stack">
              <span>AI-visibility project</span>
              <select
                className="auth-input"
                value={selectedClient.ai_visibility_project ?? ""}
                disabled={isSavingSettings}
                onChange={(event) =>
                  void saveClientSettings({ ai_visibility_project: event.target.value })
                }
              >
                <option value="">
                  {`Auto — match a project named “${selectedClient.name}”`}
                </option>
                {aiProjects.map((option) => (
                  <option key={option.project} value={option.project}>
                    {`${option.project} — ${option.runs} run${option.runs === 1 ? "" : "s"}`}
                    {option.last_run_at ? `, last ${option.last_run_at.slice(0, 10)}` : ""}
                  </option>
                ))}
              </select>
              <small className="muted">
                {selectedClient.ai_visibility_project
                  ? `AI Visibility blocks read from “${selectedClient.ai_visibility_project}”.`
                  : aiProjects.some(
                        (option) =>
                          option.project.trim().toLowerCase() === selectedClient.name.trim().toLowerCase(),
                      )
                    ? `Matching the project named “${selectedClient.name}”.`
                    : `No project is named “${selectedClient.name}” — pick one, or the AI Visibility blocks stay empty.`}
              </small>
            </label>
          </>
        ) : null}
        {showCreateClient ? (
          <div className="report-create-client">
            <label className="field-stack">
              <span>New client name</span>
              <input
                className="auth-input"
                value={newClientName}
                onChange={(event) => setNewClientName(event.target.value)}
                placeholder="Acme Co"
              />
            </label>
            <label className="field-stack">
              <span>Domain</span>
              <input
                className="auth-input"
                value={newClientDomain}
                onChange={(event) => setNewClientDomain(event.target.value)}
                placeholder="acme.com"
              />
            </label>
            <label className="field-stack">
              <span>Report language</span>
              <select
                className="auth-input"
                value={newClientLanguage}
                onChange={(event) => setNewClientLanguage(event.target.value as ReportLanguage)}
              >
                {REPORT_LANGUAGES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={() => setShowCreateClient(false)}>
                Cancel
              </button>
              <button
                className="primary-btn"
                type="button"
                onClick={() => void handleCreateClient()}
                disabled={!newClientName.trim() || !newClientDomain.trim()}
              >
                Create client
              </button>
            </div>
          </div>
        ) : (
          <button className="ghost-btn" type="button" onClick={() => setShowCreateClient(true)}>
            + Create new client
          </button>
        )}
      </article>

      {/* Step 2: block selection */}
      {selectedClientId ? (
        <article className="panel">
          <p className="eyebrow">Step 2</p>
          <h3>Comparison period</h3>
          <div className="report-timeframe">
            {!useAdvanced ? (
              <div className="report-timeframe-group">
                <h4>Period</h4>
                <div className="report-timeframe-modes">
                  {PERIOD_OPTIONS.map((option) => (
                    <label key={option.value} className="report-timeframe-mode">
                      <input
                        type="radio"
                        name="period-preset"
                        checked={periodPreset === option.value}
                        onChange={() => setPeriodPreset(option.value)}
                      />
                      <span>{option.label}</span>
                    </label>
                  ))}
                </div>
                <p className="report-hint">Ends with the last completed month.</p>
              </div>
            ) : (
              <>
                <div className="report-timeframe-modes">
                  <label className="report-timeframe-mode">
                    <input
                      type="radio"
                      name="report-type"
                      checked={reportType === "monthly"}
                      onChange={() => setReportType("monthly")}
                    />
                    <span>Custom range</span>
                  </label>
                  <label className="report-timeframe-mode">
                    <input
                      type="radio"
                      name="report-type"
                      checked={reportType === "yearly"}
                      onChange={() => setReportType("yearly")}
                    />
                    <span>Full year</span>
                  </label>
                </div>
                {reportType === "monthly" ? (
                  <div className="report-timeframe-range">
                    <label className="field-stack">
                      <span>From</span>
                      <input
                        className="auth-input"
                        type="date"
                        value={dateFrom}
                        max={dateTo || undefined}
                        onChange={(event) => setDateFrom(event.target.value)}
                      />
                    </label>
                    <label className="field-stack">
                      <span>To</span>
                      <input
                        className="auth-input"
                        type="date"
                        value={dateTo}
                        min={dateFrom || undefined}
                        onChange={(event) => setDateTo(event.target.value)}
                      />
                    </label>
                  </div>
                ) : (
                  <label className="field-stack report-timeframe-year">
                    <span>Year</span>
                    <select
                      className="auth-input"
                      value={reportYear}
                      onChange={(event) => setReportYear(event.target.value)}
                    >
                      {yearOptions.map((year) => (
                        <option key={year} value={year}>
                          {year}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <p className="report-hint">
                  {reportType === "monthly"
                    ? "Leave both dates empty to report the latest month available. Pick a range to aggregate across months (dates are rounded to whole months)."
                    : "Aggregates all 12 months of the selected year, compared against the prior year."}
                </p>
              </>
            )}

            <div className="report-timeframe-group">
              <h4>Compare against</h4>
              <div className="report-timeframe-modes report-comparison-presets">
                {COMPARISON_OPTIONS.map((option) => (
                  <label key={option.value} className="report-timeframe-mode">
                    <input
                      type="checkbox"
                      checked={comparisons.includes(option.value)}
                      onChange={() => toggleComparison(option.value)}
                    />
                    <span>{option.label}</span>
                  </label>
                ))}
              </div>
              <p className="report-hint">
                Pick one or more. Each one becomes a toggle in the client's report, and the first is the
                comparison the report opens on.
              </p>
            </div>

            <button className="ghost-btn" type="button" onClick={() => setUseAdvanced((value) => !value)}>
              {useAdvanced ? "← Back to the period presets" : "Advanced (custom range / full year)"}
            </button>
          </div>

          <h3>Select blocks ({selectedKeys.size} selected)</h3>
          {groupedCatalog.map((group) => (
            <div key={group.source} className="report-block-group">
              <h4>{sourceLabel(group.source)}</h4>
              <div className="report-block-options">
                {group.blocks.map((block) => (
                  <label key={block.key} className="report-block-option">
                    <input
                      type="checkbox"
                      checked={selectedKeys.has(block.key)}
                      onChange={() => toggleBlock(block.key)}
                    />
                    <span>{block.display_name}</span>
                  </label>
                ))}
              </div>
            </div>
          ))}

          {selectedKeys.has("planned_works") ? (
            <div className="report-block-group report-planned-work">
              <h4>Planned work source</h4>
              <div className="report-timeframe-modes">
                <label className="report-timeframe-mode">
                  <input
                    type="radio"
                    name="planned-work-mode"
                    checked={plannedWorkMode === "clickup"}
                    onChange={() => setPlannedWorkMode("clickup")}
                  />
                  <span>From ClickUp (Todo tasks)</span>
                </label>
                <label className="report-timeframe-mode">
                  <input
                    type="radio"
                    name="planned-work-mode"
                    checked={plannedWorkMode === "manual"}
                    onChange={() => setPlannedWorkMode("manual")}
                  />
                  <span>Manual text</span>
                </label>
              </div>
              {plannedWorkMode === "manual" ? (
                <label className="field-stack">
                  <span>Plans for the upcoming period</span>
                  <textarea
                    className="auth-input"
                    rows={4}
                    value={plannedWorkText}
                    onChange={(event) => setPlannedWorkText(event.target.value)}
                    placeholder="Describe the planned work for the next period…"
                  />
                </label>
              ) : (
                <p className="report-hint">
                  Pulls the tasks currently in the ClickUp &ldquo;Todo&rdquo; status for this client.
                </p>
              )}
            </div>
          ) : null}

          <div className="modal-actions">
            <button
              className="primary-btn"
              type="button"
              onClick={() => void handleGenerate()}
              disabled={!canGenerate}
            >
              {aiStage === "comments"
                ? "Claude is writing comments…"
                : aiStage === "industry"
                  ? "Researching the month…"
                  : isGenerating
                    ? "Generating…"
                    : "Generate Report"}
            </button>
          </div>
          {selectedKeys.size === 0 ? (
            <p className="report-hint">Select at least one block to generate a report.</p>
          ) : null}
        </article>
      ) : null}

      {/* Step 3: generated blocks + comments */}
      {generated ? (
        <article className="panel">
          <p className="eyebrow">Step 3</p>
          <h3>
            Report preview — period {generated.period_label}
            {editingReportId ? " (editing saved report)" : ""}
          </h3>
          {aiStage === "comments" || aiStage === "industry" ? (
            <p className="report-hint">
              {aiStage === "comments"
                ? "Claude is drafting the section comments — the report below is already usable, they will appear in a moment."
                : "Claude is researching this month's search industry on the web. This takes about a minute and a half; everything else is ready."}
            </p>
          ) : null}
          <p className="report-hint">
            Claude has drafted a comment for every section from the whole report's data — they are yours to
            edit. Edit the notes, and switch a section's chart type, directly in the preview below. Sections
            with no data are excluded automatically. These controls are removed from the client version — your
            notes and chart choices are kept when you Save.
          </p>
          <p className="report-hint">
            {editingReportId
              ? "“Save changes” stores the report exactly as you have edited it — nothing is rewritten. Use “Regenerate summary” only when you want Claude to write the summary again from the current numbers and comments."
              : "On Submit, Claude reads the finished report and writes the summary at the top, updates the preview with it, and then stores the final version. After that, “Save changes” keeps your edits as written."}
          </p>

          {/* The class toggles on the wrapper, never on the iframe's position in the
              tree — remounting the iframe would reload it and drop in-preview edits. */}
          <div className={`report-preview-shell${previewExpanded ? " expanded" : ""}`}>
            <div className="report-preview-toolbar">
              <span className="report-hint">
                {previewExpanded ? "Full screen — press Esc to exit" : "Scroll inside the preview to review the whole report."}
              </span>
              <button className="ghost-btn" type="button" onClick={() => setPreviewExpanded((value) => !value)}>
                {previewExpanded ? "Exit full screen" : "⤢ Full screen"}
              </button>
            </div>
            <iframe
              className="report-preview-frame"
              title="Report preview"
              srcDoc={previewHtml}
            />
          </div>

          <div className="modal-actions">
            <button
              className="ghost-btn"
              type="button"
              onClick={() => void handleRedraftComments()}
              disabled={aiStage !== null || isSaving}
            >
              {aiStage === "comments" ? "Claude is writing…" : "↻ Rewrite comments with Claude"}
            </button>
            {/* Regenerating the summary is its own action, never the only way to
                save — otherwise every save overwrites the specialist's edits.
                Before the first submit there is no summary yet, so it is hidden. */}
            {editingReportId ? (
              <button
                className="ghost-btn"
                type="button"
                onClick={openRegenerateSummaryModal}
                disabled={isSaving || aiStage !== null}
              >
                {aiStage === "summary" ? "Claude is writing the summary…" : "↻ Regenerate summary"}
              </button>
            ) : null}
            <button
              className="primary-btn"
              type="button"
              onClick={() => (editingReportId ? void handleSaveEdits() : void handleSave())}
              disabled={isSaving || aiStage !== null}
            >
              {aiStage === "summary"
                ? "Claude is writing the summary…"
                : isSaving
                  ? "Saving…"
                  : editingReportId
                    ? "Save changes"
                    : "Submit & save"}
            </button>
          </div>
        </article>
      ) : null}

      {/* Saved reports */}
      {selectedClientId ? (
        <article className="panel">
          <p className="eyebrow">Saved reports</p>
          <h3>Previously saved for this client</h3>
          {savedReports.length === 0 ? (
            <p className="report-hint">No saved reports yet.</p>
          ) : (
            <table className="report-saved-table">
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Last updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {savedReports.map((report) => (
                  <tr key={report.id}>
                    <td>{report.period_label}</td>
                    <td>{new Date(report.updated_at).toLocaleString()}</td>
                    <td className="report-saved-actions">
                      <button className="ghost-btn" type="button" onClick={() => void handleOpenReport(report.id)}>
                        Open
                      </button>
                      <button className="ghost-btn" type="button" onClick={() => void handlePreview(report.id)}>
                        Preview
                      </button>
                      <button
                        className="ghost-btn"
                        type="button"
                        onClick={() => void handleExport(report.id, "html")}
                        disabled={exportingReportId === `${report.id}:html`}
                      >
                        {exportingReportId === `${report.id}:html` ? "Exporting…" : "Export HTML"}
                      </button>
                      <button
                        className="ghost-btn"
                        type="button"
                        onClick={() => void handleExport(report.id, "pdf")}
                        disabled={exportingReportId === `${report.id}:pdf`}
                      >
                        {exportingReportId === `${report.id}:pdf` ? "Exporting…" : "Export PDF"}
                      </button>
                      <button
                        className="ghost-btn"
                        type="button"
                        onClick={() => void handleExport(report.id, "md")}
                        disabled={exportingReportId === `${report.id}:md`}
                      >
                        {exportingReportId === `${report.id}:md` ? "Exporting…" : "Export MD"}
                      </button>
                      <button
                        className="ghost-btn danger-btn"
                        type="button"
                        onClick={() => setReportPendingDelete(report)}
                        disabled={isDeletingReport}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </article>
      ) : null}

      {reportPendingDelete ? (
        <div className="modal-backdrop" onClick={() => setReportPendingDelete(null)}>
          <div className="modal-card" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Saved reports</p>
            <h3>Delete this report?</h3>
            <p>
              The <strong>{reportPendingDelete.period_label}</strong> report and all of its blocks, comments and
              summary will be removed for everyone. Already-downloaded exports are unaffected. This cannot be undone.
            </p>
            <div className="modal-actions">
              <button
                className="ghost-btn"
                type="button"
                onClick={() => setReportPendingDelete(null)}
                disabled={isDeletingReport}
              >
                Cancel
              </button>
              <button
                className="primary-btn danger-btn"
                type="button"
                onClick={() => void handleDeleteReportConfirm()}
                disabled={isDeletingReport}
              >
                {isDeletingReport ? "Deleting…" : "Delete report"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {showRegenerateSummaryModal ? (
        <div className="modal-backdrop" onClick={() => setShowRegenerateSummaryModal(false)}>
          <div className="modal-card" onClick={(event) => event.stopPropagation()}>
            <p className="eyebrow">Executive summary</p>
            <h3>Regenerate Summary</h3>
            <p>
              Claude will rewrite the summary from the report's current data and comments. Optionally tell it what to
              change or focus on — leave this blank to just rewrite it as-is.
            </p>
            <label className="field-stack">
              <span>What should Claude change? (optional)</span>
              <textarea
                className="auth-input"
                rows={4}
                value={summaryGuidance}
                onChange={(event) => setSummaryGuidance(event.target.value)}
                placeholder="e.g. Focus more on the year-over-year traffic gain, and mention the new SE Ranking keyword wins."
              />
            </label>
            <div className="modal-actions">
              <button className="ghost-btn" type="button" onClick={() => setShowRegenerateSummaryModal(false)}>
                Cancel
              </button>
              <button className="primary-btn" type="button" onClick={() => void handleRegenerateSummaryConfirm()}>
                Regenerate & Save
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
