import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../api";
import { sourceLabel, SOURCE_ORDER } from "./blockCatalog";
import type {
  BlockCatalogResponse,
  Client,
  ClientListResponse,
  GeneratedBlock,
  GenerateReportResponse,
  ReportDetail,
  ReportListResponse,
  ReportSettingsStatus,
  ReportSummary,
  ReportBlockType,
} from "./types";

type Props = {
  token: string | null;
};

export default function ReportBuilderPage({ token }: Props) {
  const [catalog, setCatalog] = useState<ReportBlockType[]>([]);
  const [clients, setClients] = useState<Client[]>([]);
  const [selectedClientId, setSelectedClientId] = useState<string>("");

  const [showCreateClient, setShowCreateClient] = useState(false);
  const [newClientName, setNewClientName] = useState("");
  const [newClientDomain, setNewClientDomain] = useState("");

  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [generated, setGenerated] = useState<GenerateReportResponse | null>(null);
  const [comments, setComments] = useState<Record<string, string>>({});
  const [editingReportId, setEditingReportId] = useState<string | null>(null);

  const [savedReports, setSavedReports] = useState<ReportSummary[]>([]);

  const [isGenerating, setIsGenerating] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const [settings, setSettings] = useState<ReportSettingsStatus | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [clickupTokenInput, setClickupTokenInput] = useState("");
  const [isSavingToken, setIsSavingToken] = useState(false);

  const displayNameByKey = useMemo(() => {
    const map: Record<string, string> = {};
    catalog.forEach((block) => {
      map[block.key] = block.display_name;
    });
    return map;
  }, [catalog]);

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

  const loadClients = useCallback(async () => {
    if (!token) return;
    const response = await apiRequest<ClientListResponse>("/api/report-builder/clients", { token });
    setClients(response.clients);
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
        const settingsResponse = await apiRequest<ReportSettingsStatus>("/api/report-builder/settings", {
          token,
        });
        setSettings(settingsResponse);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load the report builder.");
      }
    })();
  }, [token, loadClients]);

  useEffect(() => {
    if (!selectedClientId) {
      setSavedReports([]);
      return;
    }
    void loadSavedReports(selectedClientId).catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : "Failed to load saved reports.");
    });
  }, [selectedClientId, loadSavedReports]);

  function resetReportState() {
    setGenerated(null);
    setComments({});
    setEditingReportId(null);
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
        body: { name: newClientName.trim(), domain: newClientDomain.trim() },
      });
      await loadClients();
      setSelectedClientId(client.id);
      setShowCreateClient(false);
      setNewClientName("");
      setNewClientDomain("");
      setStatus(`Client "${client.name}" created.`);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Failed to create client.");
    }
  }

  async function handleGenerate() {
    if (!token || !selectedClientId || selectedKeys.size === 0) return;
    setError(null);
    setStatus(null);
    setIsGenerating(true);
    try {
      const response = await apiRequest<GenerateReportResponse>("/api/report-builder/generate", {
        method: "POST",
        token,
        body: { client_id: selectedClientId, block_keys: Array.from(selectedKeys) },
      });
      setGenerated(response);
      setEditingReportId(null);
      const initialComments: Record<string, string> = {};
      response.blocks.forEach((block) => {
        initialComments[block.block_type_key] = "";
      });
      setComments(initialComments);
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : "Failed to generate report.");
    } finally {
      setIsGenerating(false);
    }
  }

  function blocksForSave(): GeneratedBlock[] {
    if (!generated) return [];
    return generated.blocks.map((block) => ({
      ...block,
      comment: comments[block.block_type_key] ?? "",
    }));
  }

  async function handleSave() {
    if (!token || !generated || !selectedClientId) return;
    setError(null);
    setStatus(null);
    setIsSaving(true);
    try {
      if (editingReportId) {
        await apiRequest<ReportSummary>(`/api/report-builder/reports/${editingReportId}`, {
          method: "PUT",
          token,
          body: { period_label: generated.period_label, blocks: blocksForSave() },
        });
        setStatus("Report updated.");
      } else {
        const saved = await apiRequest<ReportSummary>("/api/report-builder/reports", {
          method: "POST",
          token,
          body: {
            client_id: selectedClientId,
            period_label: generated.period_label,
            blocks: blocksForSave(),
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

  async function handleOpenReport(reportId: string) {
    if (!token) return;
    setError(null);
    setStatus(null);
    try {
      const detail = await apiRequest<ReportDetail>(`/api/report-builder/reports/${reportId}`, { token });
      setGenerated({
        client_id: detail.client_id,
        period_label: detail.period_label,
        blocks: detail.blocks,
      });
      const loadedComments: Record<string, string> = {};
      const loadedKeys = new Set<string>();
      detail.blocks.forEach((block) => {
        loadedComments[block.block_type_key] = block.comment ?? "";
        loadedKeys.add(block.block_type_key);
      });
      setComments(loadedComments);
      setSelectedKeys(loadedKeys);
      setEditingReportId(detail.id);
      setStatus(`Opened report from ${new Date(detail.updated_at).toLocaleString()}.`);
    } catch (openError) {
      setError(openError instanceof Error ? openError.message : "Failed to open report.");
    }
  }

  async function handleExport(reportId: string) {
    if (!token) return;
    setError(null);
    try {
      const response = await fetch(`/api/report-builder/reports/${reportId}/export`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        throw new Error(`Export failed with ${response.status}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      link.download = match ? match[1] : `report-${reportId}.html`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "Failed to export report.");
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

  const canGenerate = Boolean(selectedClientId) && selectedKeys.size > 0 && !isGenerating;

  return (
    <section className="page active report-builder-page">
      {error ? <div className="status-banner">{error}</div> : null}
      {status ? <div className="status-banner">{status}</div> : null}

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
              Enter your personal ClickUp API token (ClickUp → Settings → Apps → API Token). It is stored
              encrypted and used only to pull your workspace's task lists for the Work completed / Planned
              works blocks, matched to each client by name.
            </p>
            <label className="field-stack">
              <span>ClickUp API token</span>
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
          <div className="modal-actions">
            <button
              className="primary-btn"
              type="button"
              onClick={() => void handleGenerate()}
              disabled={!canGenerate}
            >
              {isGenerating ? "Generating…" : "Generate Report"}
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
          {generated.blocks.map((block) => (
            <div key={block.block_type_key} className="report-preview-block">
              <div className="report-preview-head">
                <strong>{displayNameByKey[block.block_type_key] ?? block.block_type_key}</strong>
                {block.status === "unavailable" ? (
                  <span className="report-unavailable">⚠ {block.unavailable_reason}</span>
                ) : (
                  <span className="report-ok">✓ data loaded</span>
                )}
              </div>
              {block.status === "ok" && block.data ? (
                <pre className="report-data">{JSON.stringify(block.data, null, 2)}</pre>
              ) : null}
              <label className="field-stack">
                <span>Specialist notes</span>
                <textarea
                  className="auth-input"
                  rows={2}
                  value={comments[block.block_type_key] ?? ""}
                  onChange={(event) =>
                    setComments((current) => ({
                      ...current,
                      [block.block_type_key]: event.target.value,
                    }))
                  }
                  placeholder="Add a comment (optional)…"
                />
              </label>
            </div>
          ))}
          <div className="modal-actions">
            <button
              className="primary-btn"
              type="button"
              onClick={() => void handleSave()}
              disabled={isSaving}
            >
              {isSaving ? "Saving…" : editingReportId ? "Save changes" : "Save"}
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
                      <button className="ghost-btn" type="button" onClick={() => void handleExport(report.id)}>
                        Export
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </article>
      ) : null}
    </section>
  );
}
