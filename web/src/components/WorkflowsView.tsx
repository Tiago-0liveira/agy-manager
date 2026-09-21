import React, { useState, useEffect } from "react";
import { getPresets, getPreset, getWorkflows, createWorkflow, validateWorkflow } from "../api";

export const WorkflowsView: React.FC = () => {
  const [presets, setPresets] = useState<any[]>([]);
  const [workflows, setWorkflows] = useState<any[]>([]);
  const [selectedWorkflow, setSelectedWorkflow] = useState<any | null>(null);
  const [jsonText, setJsonText] = useState("");
  const [validationResult, setValidationResult] = useState<{ valid: boolean; errors: string[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);

  const showToast = (message: string, type: "info" | "success" | "error" = "info") => {
    setToast({ message, type });
    setTimeout(() => {
      setToast((cur) => (cur?.message === message ? null : cur));
    }, 5000);
  };

  const fetchData = async () => {
    try {
      setLoading(true);
      const [presetsData, workflowsData] = await Promise.all([getPresets(), getWorkflows()]);
      setPresets(presetsData);
      setWorkflows(workflowsData);
      if (presetsData.length > 0 && !selectedWorkflow) {
        loadPresetDetails(presetsData[0].id);
      }
    } catch (err: any) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const loadPresetDetails = async (presetId: string) => {
    try {
      const data = await getPreset(presetId);
      setSelectedWorkflow(data);
      setJsonText(JSON.stringify(data, null, 2));
      setValidationResult({ valid: true, errors: [] });
    } catch (err: any) {
      showToast("Error loading preset: " + err.message, "error");
    }
  };

  const handleValidateJson = async () => {
    try {
      const parsed = JSON.parse(jsonText);
      const res = await validateWorkflow(parsed);
      setValidationResult(res);
      setSelectedWorkflow(parsed);
      if (res.valid) {
        showToast("Workflow schema and graph are valid!", "success");
      }
    } catch (err: any) {
      setValidationResult({ valid: false, errors: [err.message] });
    }
  };

  const handleSaveAsCustom = async () => {
    try {
      const parsed = JSON.parse(jsonText);
      const res = await validateWorkflow(parsed);
      if (!res.valid) {
        showToast("Validation failed: " + res.errors.join(", "), "error");
        return;
      }
      const name = prompt("Enter a name for this custom workflow:", parsed.name + " (Copy)");
      if (!name) return;
      await createWorkflow({
        name,
        goal: parsed.goal || "",
        definition: parsed,
      });
      showToast("Custom workflow saved successfully!", "success");
      fetchData();
    } catch (err: any) {
      showToast("Error saving workflow: " + err.message, "error");
    }
  };

  const handleExportJson = () => {
    const blob = new Blob([jsonText], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(selectedWorkflow?.name || "workflow").toLowerCase().replace(/\s+/g, "_")}.json`;
    a.click();
    URL.revokeObjectURL(url);
    showToast("Workflow JSON exported!", "success");
  };

  const handleExportPortable = () => {
    try {
      const parsed = JSON.parse(jsonText);
      const portable = JSON.parse(JSON.stringify(parsed));
      portable.draft = true;
      if (Array.isArray(portable.workers)) {
        portable.workers.forEach((w: any, idx: number) => {
          w.account_ref = `<assign-local-profile-${idx + 1}>`;
          if (!w.model || !w.model.startsWith("<")) {
            w.model = "<choose-discovered-model>";
          }
        });
      }
      const blob = new Blob([JSON.stringify(portable, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(selectedWorkflow?.name || "workflow").toLowerCase().replace(/\s+/g, "_")}_portable.json`;
      a.click();
      URL.revokeObjectURL(url);
      showToast("Portable template exported!", "success");
    } catch (err: any) {
      showToast("Error exporting template: " + err.message, "error");
    }
  };

  const handleImportFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      try {
        const text = ev.target?.result as string;
        const parsed = JSON.parse(text);
        setJsonText(JSON.stringify(parsed, null, 2));
        setSelectedWorkflow(parsed);
        const res = await validateWorkflow(parsed);
        setValidationResult(res);
        showToast("Workflow imported successfully!", "success");
      } catch (err: any) {
        setValidationResult({ valid: false, errors: ["Failed to parse imported JSON: " + err.message] });
        showToast("Import parsing failed", "error");
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  };

  return (
    <div className="view-container">
      {toast && (
        <div className={`toast-banner toast-${toast.type}`}>
          <span>{toast.message}</span>
          <button className="toast-close" onClick={() => setToast(null)}>✕</button>
        </div>
      )}

      <div className="view-header">
        <div>
          <h2>Workflow Designer & Presets</h2>
          <p className="subtitle">Inspect, customize, validate, and export staged collaboration pipelines</p>
        </div>
        <div style={{ display: "flex", gap: "12px", flexWrap: "wrap" }}>
          <label className="btn btn-outline" style={{ cursor: "pointer", display: "inline-flex", alignItems: "center" }}>
            📂 Import JSON
            <input type="file" accept=".json" style={{ display: "none" }} onChange={handleImportFile} />
          </label>
          <button className="btn btn-outline" onClick={handleExportJson}>
            Export JSON
          </button>
          <button className="btn btn-outline" onClick={handleExportPortable} title="Export with sanitized local profile placeholders">
            Export Portable Template
          </button>
          <button className="btn btn-primary" onClick={handleSaveAsCustom}>
            Save as Custom Workflow
          </button>
        </div>
      </div>

      <div className="workflows-layout">
        {/* Sidebar list */}
        <div className="workflows-sidebar">
          <h3>Bundled Presets</h3>
          <ul className="preset-nav-list">
            {presets.map((p) => (
              <li
                key={p.id}
                className={selectedWorkflow?.name === p.name ? "active" : ""}
                onClick={() => loadPresetDetails(p.id)}
              >
                <strong>{p.name}</strong>
                <span className="text-muted text-xs">
                  {p.workers_count} workers · {p.stages_count} stages
                </span>
              </li>
            ))}
          </ul>

          {workflows.length > 0 && (
            <>
              <h3 style={{ marginTop: 24 }}>Custom Workflows</h3>
              <ul className="preset-nav-list">
                {workflows.map((w) => (
                  <li
                    key={w.workflow_id}
                    className={selectedWorkflow?.name === w.name ? "active" : ""}
                    onClick={() => {
                      setSelectedWorkflow(w.definition);
                      setJsonText(JSON.stringify(w.definition, null, 2));
                    }}
                  >
                    <strong>{w.name}</strong>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        {/* Main Content */}
        <div className="workflow-detail">
          {selectedWorkflow && (
            <>
              <div className="workflow-meta card">
                <h3>{selectedWorkflow.name}</h3>
                <p className="subtitle" style={{ marginBottom: 0 }}>{selectedWorkflow.goal}</p>
                <div style={{ display: "flex", gap: "10px", flexWrap: "wrap", marginTop: "8px" }}>
                  <span className="badge badge-blue">Mode: {selectedWorkflow.execution_mode}</span>
                  <span className="badge badge-purple">{selectedWorkflow.workers?.length || 0} Workers</span>
                  <span className="badge badge-green">{selectedWorkflow.stages?.length || 0} Stages</span>
                  <span className="badge badge-gray">Max Calls: {selectedWorkflow.limits?.max_model_calls}</span>
                </div>
              </div>

              {/* Stages Visual Progression */}
              <div className="stages-timeline" style={{ marginTop: "20px" }}>
                <h4 style={{ marginBottom: "12px", color: "var(--text-secondary)" }}>Pipeline Stage Progression</h4>
                <div className="timeline-stages">
                  {selectedWorkflow.stages?.map((stage: any, index: number) => (
                    <div key={stage.id} className="timeline-stage-card">
                      <div className="stage-num">{index + 1}</div>
                      <div className="stage-content">
                        <div className="stage-header-row">
                          <span className="stage-title">{stage.id}</span>
                          <span className="badge badge-sm badge-blue">{stage.kind}</span>
                          <span className={`badge badge-sm ${stage.context === "fresh" ? "badge-yellow" : "badge-green"}`}>
                            {stage.context} context
                          </span>
                        </div>
                        <p className="stage-inst" style={{ fontSize: "14px", color: "var(--text-secondary)" }}>{stage.instruction}</p>
                        <div className="stage-workers" style={{ marginTop: "8px", fontSize: "13px" }}>
                          Workers:{" "}
                          {stage.workers?.map((wId: string) => (
                            <code key={wId} className="worker-pill">
                              {wId}
                            </code>
                          ))}
                        </div>
                        {stage.input_stages?.length > 0 && (
                          <div className="text-muted text-xs" style={{ marginTop: "6px" }}>
                            Prerequisites: {stage.input_stages.join(", ")}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Interactive JSON & Contract Validation */}
              <div className="editor-section" style={{ marginTop: "24px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "12px" }}>
                  <h4>Workflow Specification (JSON)</h4>
                  <button className="btn btn-sm btn-outline" onClick={handleValidateJson}>
                    Validate Schema & Graph
                  </button>
                </div>
                {validationResult && (
                  <div className={`alert ${validationResult.valid ? "alert-success" : "alert-error"}`} style={{ marginBottom: "12px" }}>
                    {validationResult.valid
                      ? "✓ Workflow structure, acyclic graph dependencies, and limits are valid!"
                      : `Validation Failed: ${validationResult.errors.join("; ")}`}
                  </div>
                )}
                <textarea
                  className="code-editor"
                  rows={14}
                  value={jsonText}
                  onChange={(e) => setJsonText(e.target.value)}
                />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
