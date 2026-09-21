import React, { useState, useEffect } from "react";
import { Account, DiscoveredModel } from "../types";
import { getAccounts, getPresets, getPreset, getModels, createRun, startRun } from "../api";

interface LaunchViewProps {
  onRunLaunched: (runId: string) => void;
}

const DEFAULT_MODELS = [
  { id: "gemini-2.5-pro", display_name: "Gemini 2.5 Pro (Deep Deliberation)" },
  { id: "gemini-2.5-flash", display_name: "Gemini 2.5 Flash (Fast Execution)" },
  { id: "claude-3-5-sonnet", display_name: "Claude 3.5 Sonnet" },
  { id: "fake-model-pro", display_name: "Fake Model Pro (Zero Cost Testing)" },
];

export const LaunchView: React.FC<LaunchViewProps> = ({ onRunLaunched }) => {
  const [presets, setPresets] = useState<any[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [models, setModels] = useState<{ id: string; display_name: string }[]>(DEFAULT_MODELS);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  const [currentWorkflow, setCurrentWorkflow] = useState<any | null>(null);

  const [runName, setRunName] = useState("");
  const [goal, setGoal] = useState("");
  const [briefInput, setBriefInput] = useState("");

  const [accountBindings, setAccountBindings] = useState<Record<string, string>>({});
  const [modelBindings, setModelBindings] = useState<Record<string, string>>({});
  const [customModelMode, setCustomModelMode] = useState<Record<string, boolean>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const init = async () => {
      try {
        const [presetsData, accountsData, modelsData] = await Promise.all([
          getPresets(),
          getAccounts(),
          getModels().catch(() => [] as DiscoveredModel[]),
        ]);
        setPresets(presetsData);
        setAccounts(accountsData);

        // Merge discovered models with defaults
        const modelMap = new Map<string, string>();
        DEFAULT_MODELS.forEach((m) => modelMap.set(m.id, m.display_name));
        modelsData.forEach((m) => {
          if (!modelMap.has(m.id)) {
            modelMap.set(m.id, m.display_name || m.id);
          }
        });
        const combinedModels = Array.from(modelMap.entries()).map(([id, display_name]) => ({
          id,
          display_name,
        }));
        setModels(combinedModels);

        if (presetsData.length > 0) {
          const firstId = presetsData[0].id;
          setSelectedPresetId(firstId);
          loadPreset(firstId, accountsData, combinedModels);
        }
      } catch (err: any) {
        setError(err.message || "Failed to initialize run setup");
      }
    };
    init();
  }, []);

  const loadPreset = async (
    id: string,
    accs: Account[],
    availableModels: { id: string; display_name: string }[]
  ) => {
    try {
      const data = await getPreset(id);
      setCurrentWorkflow(data);
      setRunName(data.name || "Council Run");
      setGoal(data.goal || "");

      // Auto bind workers to available accounts & models
      const newAccBindings: Record<string, string> = {};
      const newModelBindings: Record<string, string> = {};

      const defaultModelId = availableModels[0]?.id || "gemini-2.5-pro";

      data.workers?.forEach((w: any, idx: number) => {
        // Round-robin or first account
        const acc = accs[idx % (accs.length || 1)];
        if (acc) {
          newAccBindings[w.id] = acc.profile_ref;
        } else {
          newAccBindings[w.id] = `profile-${idx + 1}`;
        }

        // Model selection
        let chosenModel = w.model;
        if (!chosenModel || chosenModel.startsWith("<") || chosenModel.includes("fake")) {
          chosenModel = defaultModelId;
        }
        newModelBindings[w.id] = chosenModel;
      });

      setAccountBindings(newAccBindings);
      setModelBindings(newModelBindings);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handlePresetChange = (presetId: string) => {
    setSelectedPresetId(presetId);
    loadPreset(presetId, accounts, models);
  };

  const handleModelSelect = (workerId: string, value: string) => {
    if (value === "__custom__") {
      setCustomModelMode((prev) => ({ ...prev, [workerId]: true }));
      setModelBindings((prev) => ({ ...prev, [workerId]: "" }));
    } else {
      setCustomModelMode((prev) => ({ ...prev, [workerId]: false }));
      setModelBindings((prev) => ({ ...prev, [workerId]: value }));
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      // 1. Create run
      const runRes = await createRun({
        preset_id: selectedPresetId,
        name: runName,
        goal: goal,
        inputs: {
          brief: briefInput || goal,
        },
        account_bindings: accountBindings,
        model_bindings: modelBindings,
      });

      const runId = runRes.run_id;

      // 2. Start run immediately
      await startRun(runId);

      // 3. Switch to dashboard view
      onRunLaunched(runId);
    } catch (err: any) {
      setError(err.message);
      setSubmitting(false);
    }
  };

  return (
    <div className="view-container launch-container">
      <div className="view-header">
        <div>
          <h2>Launch New Council Run</h2>
          <p className="subtitle">
            Configure goal, bind participant accounts and cached models, and dispatch multi-worker deliberation
          </p>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <form onSubmit={handleSubmit} className="launch-form">
        <div className="card">
          <h3>1. Collaboration Workflow & Objective</h3>

          <div className="form-group">
            <label>Workflow Preset</label>
            <select
              value={selectedPresetId}
              onChange={(e) => handlePresetChange(e.target.value)}
            >
              {presets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.workers_count} workers, {p.stages_count} stages)
                </option>
              ))}
            </select>
            <small className="form-help">
              Each preset defines stages, release barriers, critique rounds, and synthesis roles.
            </small>
          </div>

          <div className="form-group">
            <label>Run Label / Title</label>
            <input
              type="text"
              required
              value={runName}
              onChange={(e) => setRunName(e.target.value)}
              placeholder="e.g. Competitive Architectural Audit"
            />
          </div>

          <div className="form-group">
            <label>User Objective / Goal</label>
            <textarea
              rows={3}
              required
              placeholder="What question, problem, or artifact should the council investigate?"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
          </div>

          <div className="form-group">
            <label>Brief & Evidence Context (Optional)</label>
            <textarea
              rows={3}
              placeholder="Provide background context, code snippets, reference data, or constraints for the workers..."
              value={briefInput}
              onChange={(e) => setBriefInput(e.target.value)}
            />
          </div>
        </div>

        {currentWorkflow && (
          <div className="card">
            <h3>2. Worker Account & Model Bindings</h3>
            <p className="subtitle" style={{ marginBottom: 0 }}>
              Assign isolated profile accounts and select models for each participant. Models are cached to prevent repetitive probe overhead.
            </p>

            <div className="workers-binding-list">
              {currentWorkflow.workers?.map((w: any) => {
                const isCustom = Boolean(customModelMode[w.id]);
                const selectedModel = modelBindings[w.id] || "";
                const isModelInList = models.some((m) => m.id === selectedModel);

                return (
                  <div key={w.id} className="worker-binding-row">
                    <div className="worker-info">
                      <strong style={{ fontSize: "15px" }}>{w.name}</strong>
                      <div className="text-muted text-xs" style={{ marginTop: "2px" }}>
                        Role: <code>{w.role}</code> · ID: <code>{w.id}</code>
                      </div>
                    </div>

                    <div className="binding-fields">
                      {/* Account selection */}
                      <div className="field">
                        <label>Account Profile</label>
                        <select
                          value={accountBindings[w.id] || ""}
                          onChange={(e) =>
                            setAccountBindings({ ...accountBindings, [w.id]: e.target.value })
                          }
                        >
                          {accounts.length === 0 ? (
                            <option value="default-profile">default-profile</option>
                          ) : (
                            accounts.map((acc) => (
                              <option key={acc.account_id} value={acc.profile_ref}>
                                {acc.display_label} ({acc.profile_ref})
                              </option>
                            ))
                          )}
                        </select>
                      </div>

                      {/* Model dropdown selection */}
                      <div className="field">
                        <label>Model</label>
                        {isCustom ? (
                          <div style={{ display: "flex", gap: "6px" }}>
                            <input
                              type="text"
                              required
                              placeholder="e.g. gemini-2.5-flash"
                              value={selectedModel}
                              onChange={(e) =>
                                setModelBindings({ ...modelBindings, [w.id]: e.target.value })
                              }
                            />
                            <button
                              type="button"
                              className="btn btn-sm btn-outline"
                              onClick={() => handleModelSelect(w.id, models[0]?.id || "gemini-2.5-pro")}
                              title="Switch back to cached list"
                            >
                              List
                            </button>
                          </div>
                        ) : (
                          <select
                            value={isModelInList ? selectedModel : "__custom__"}
                            onChange={(e) => handleModelSelect(w.id, e.target.value)}
                          >
                            {models.map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.display_name}
                              </option>
                            ))}
                            <option value="__custom__">Custom Model Name...</option>
                          </select>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="launch-submit-bar">
          <button type="submit" className="btn btn-primary btn-lg" disabled={submitting}>
            {submitting ? "Dispatching Council Run..." : "🚀 Launch Council Run"}
          </button>
        </div>
      </form>
    </div>
  );
};
