import React, { useState, useEffect } from "react";
import { AgentTemplate } from "../types";
import { getAgents, createAgent, updateAgent, deleteAgent } from "../api";

export const AgentsView: React.FC = () => {
  const [agents, setAgents] = useState<AgentTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);

  const [showModal, setShowModal] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [instructions, setInstructions] = useState("");
  const [workingStyle, setWorkingStyle] = useState("");
  const [preferredModel, setPreferredModel] = useState("");

  const showToast = (message: string, type: "info" | "success" | "error" = "info") => {
    setToast({ message, type });
    setTimeout(() => {
      setToast((cur) => (cur?.message === message ? null : cur));
    }, 5000);
  };

  const fetchAgents = async () => {
    try {
      setLoading(true);
      const data = await getAgents();
      setAgents(data);
    } catch (err: any) {
      showToast(err.message, "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAgents();
  }, []);

  const openCreateModal = () => {
    setEditingId(null);
    setName("");
    setPurpose("");
    setInstructions("");
    setWorkingStyle("");
    setPreferredModel("");
    setShowModal(true);
  };

  const openEditModal = (a: AgentTemplate) => {
    setEditingId(a.id || a.template_id || null);
    setName(a.name || a.display_name || "");
    setPurpose(a.purpose || "");
    setInstructions(a.instructions || a.base_instructions || "");
    setWorkingStyle(a.working_style || "");
    setPreferredModel(a.preferred_model || "");
    setShowModal(true);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const payload: Partial<AgentTemplate> = {
        name,
        purpose,
        instructions,
        working_style: workingStyle,
        preferred_model: preferredModel || undefined,
        output_expectations: ["findings", "evidence_or_assumptions", "uncertainties", "next_action"],
      };

      if (editingId) {
        await updateAgent(editingId, payload);
        showToast(`Updated template '${name}'`, "success");
      } else {
        await createAgent(payload);
        showToast(`Created persona template '${name}'`, "success");
      }
      setShowModal(false);
      fetchAgents();
    } catch (err: any) {
      showToast("Error saving persona: " + err.message, "error");
    }
  };

  const handleDelete = async (a: AgentTemplate) => {
    const id = a.id || a.template_id;
    if (!id || !confirm(`Delete template '${a.name || a.display_name}'?`)) return;
    try {
      await deleteAgent(id);
      showToast(`Deleted template '${a.name || a.display_name}'`, "info");
      fetchAgents();
    } catch (err: any) {
      showToast("Error deleting agent: " + err.message, "error");
    }
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
          <h2>Agent Persona Library</h2>
          <p className="subtitle">Reusable roles, working styles, instructions, and output contracts</p>
        </div>
        <button className="btn btn-primary" onClick={openCreateModal}>
          + Create Persona Template
        </button>
      </div>

      {loading ? (
        <div className="empty-state">Loading agent personas...</div>
      ) : agents.length === 0 ? (
        <div className="empty-state">
          <p>No agent templates created yet.</p>
          <button className="btn btn-secondary" onClick={openCreateModal}>
            Create Default Persona
          </button>
        </div>
      ) : (
        <div className="cards-grid">
          {agents.map((a) => {
            const id = a.id || a.template_id || "";
            const displayName = a.name || a.display_name || "Unnamed Persona";
            return (
              <div key={id} className="card agent-card">
                <div className="card-top">
                  <h3 className="card-title">{displayName}</h3>
                  {a.preferred_model && <span className="badge badge-purple">{a.preferred_model}</span>}
                </div>

                <div className="card-body">
                  <p className="purpose-text">
                    <strong>Purpose:</strong> {a.purpose}
                  </p>
                  {a.working_style && (
                    <p className="style-text">
                      <strong>Style:</strong> {a.working_style}
                    </p>
                  )}
                  <div className="instructions-preview">
                    <strong>Base Instructions:</strong>
                    <pre style={{ marginTop: "4px", fontSize: "12px", background: "var(--bg-subtle)", padding: "10px", borderRadius: "var(--radius-sm)", color: "var(--text-secondary)", whiteSpace: "pre-wrap" }}>
                      {a.instructions || a.base_instructions || "None"}
                    </pre>
                  </div>
                </div>

                <div className="card-actions">
                  <button className="btn btn-sm btn-outline" onClick={() => openEditModal(a)}>
                    Edit
                  </button>
                  <button className="btn btn-sm btn-danger-outline" onClick={() => handleDelete(a)}>
                    Delete
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {showModal && (
        <div className="modal-backdrop" onClick={() => setShowModal(false)}>
          <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
            <h3>{editingId ? "Edit Persona Template" : "New Agent Persona Template"}</h3>
            <form onSubmit={handleSave}>
              <div className="form-group">
                <label>Persona Name</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Constructive Critic, Synthesizer"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label>Purpose</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Scrutinize proposals for hidden flaws, assumptions, and failure modes."
                  value={purpose}
                  onChange={(e) => setPurpose(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label>Base Instructions (System Prompt)</label>
                <textarea
                  rows={4}
                  required
                  placeholder="Be rigorous, direct, and require evidence for speculative claims..."
                  value={instructions}
                  onChange={(e) => setInstructions(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label>Working Style Guidance</label>
                <input
                  type="text"
                  placeholder="e.g. Skeptical, detail-oriented, concise"
                  value={workingStyle}
                  onChange={(e) => setWorkingStyle(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label>Preferred Model (Optional)</label>
                <input
                  type="text"
                  placeholder="e.g. gemini-2.5-pro"
                  value={preferredModel}
                  onChange={(e) => setPreferredModel(e.target.value)}
                />
              </div>

              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={() => setShowModal(false)}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary">
                  Save Template
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
