import React, { useState, useEffect } from "react";
import { Account, AvailableProfile, DiscoveredModel } from "../types";
import {
  getAccounts,
  getAvailableProfiles,
  linkAllProfiles,
  createAccount,
  updateAccount,
  deleteAccount,
  connectAccount,
  checkAccount,
  getAccountModels,
} from "../api";

export const AccountsView: React.FC = () => {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [availableProfiles, setAvailableProfiles] = useState<AvailableProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [linkingAll, setLinkingAll] = useState(false);
  const [checkingAccountIds, setCheckingAccountIds] = useState<Set<string>>(new Set());

  // Toast / Notification state (replaces alerts)
  const [toast, setToast] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);

  // Modal states
  const [showAddModal, setShowAddModal] = useState(false);
  const [newProfileRef, setNewProfileRef] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [newConcurrency, setNewConcurrency] = useState(1);
  const [newProvider, setNewProvider] = useState("antigravity");
  const [addingAccount, setAddingAccount] = useState(false);

  // Edit Account modal
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [editLabel, setEditLabel] = useState("");
  const [editConcurrency, setEditConcurrency] = useState(1);
  const [editEnabled, setEditEnabled] = useState(true);

  // Connect helper modal
  const [connectResult, setConnectResult] = useState<{ status: string; command: string; message: string } | null>(null);

  // Models modal
  const [modelsAccount, setModelsAccount] = useState<string | null>(null);
  const [discoveredModels, setDiscoveredModels] = useState<DiscoveredModel[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);

  const showToast = (message: string, type: "info" | "success" | "error" = "info") => {
    setToast({ message, type });
    setTimeout(() => {
      setToast((current) => (current?.message === message ? null : current));
    }, 6000);
  };

  const fetchData = async () => {
    try {
      setLoading(true);
      const [accs, avail] = await Promise.all([
        getAccounts(),
        getAvailableProfiles().catch(() => [] as AvailableProfile[]),
      ]);
      setAccounts(accs);
      setAvailableProfiles(avail);
    } catch (err: any) {
      showToast(err.message || "Failed to load accounts", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleLinkAll = async () => {
    setLinkingAll(true);
    try {
      const res = await linkAllProfiles();
      showToast(
        res.message || `Successfully linked ${res.linked_count} profile(s) in background!`,
        "success"
      );
      await fetchData();
    } catch (err: any) {
      showToast("Error linking profiles: " + err.message, "error");
    } finally {
      setLinkingAll(false);
    }
  };

  const handleQuickLink = async (prof: AvailableProfile) => {
    try {
      await createAccount({
        profile_ref: prof.profile_name,
        label: prof.display_label || prof.profile_name,
        provider: "antigravity",
        concurrency_limit: 1,
      });
      showToast(`Profile '${prof.profile_name}' linked! Verifying in background...`, "success");
      fetchData();
    } catch (err: any) {
      showToast("Error linking profile: " + err.message, "error");
    }
  };

  const handleAddAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProfileRef.trim()) return;
    setAddingAccount(true);
    try {
      await createAccount({
        profile_ref: newProfileRef.trim(),
        label: newLabel.trim() || newProfileRef.trim(),
        provider: newProvider,
        concurrency_limit: Number(newConcurrency) || 1,
      });
      setShowAddModal(false);
      setNewProfileRef("");
      setNewLabel("");
      showToast(`Account '${newProfileRef.trim()}' linked. Verification running in background...`, "success");
      fetchData();
    } catch (err: any) {
      showToast("Error linking account: " + err.message, "error");
    } finally {
      setAddingAccount(false);
    }
  };

  const handleOpenEdit = (acc: Account) => {
    setEditingAccount(acc);
    setEditLabel(acc.display_label || acc.profile_ref);
    setEditConcurrency(acc.concurrency_limit || 1);
    setEditEnabled(Boolean(acc.enabled));
  };

  const handleSaveEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingAccount) return;
    try {
      await updateAccount(editingAccount.account_id, {
        label: editLabel.trim() || undefined,
        concurrency_limit: Number(editConcurrency) || 1,
        enabled: editEnabled,
      });
      setEditingAccount(null);
      showToast(`Updated '${editingAccount.display_label}'`, "success");
      fetchData();
    } catch (err: any) {
      showToast("Error updating account: " + err.message, "error");
    }
  };

  const handleConnect = async (acc: Account) => {
    try {
      const res = await connectAccount(acc.account_id);
      setConnectResult(res);
    } catch (err: any) {
      showToast("Error connecting: " + err.message, "error");
    }
  };

  const handleCheck = async (acc: Account) => {
    // Non-blocking async check without alert
    setCheckingAccountIds((prev) => new Set(prev).add(acc.account_id));
    try {
      const res = await checkAccount(acc.account_id);
      showToast(
        `Checked '${acc.display_label}': ${res.status.toUpperCase()} (${res.message || "OK"})`,
        res.status === "ready" ? "success" : "info"
      );
      fetchData();
    } catch (err: any) {
      showToast(`Verification check error: ${err.message}`, "error");
    } finally {
      setCheckingAccountIds((prev) => {
        const next = new Set(prev);
        next.delete(acc.account_id);
        return next;
      });
    }
  };

  const handleViewModels = async (acc: Account) => {
    setModelsAccount(acc.display_label);
    setLoadingModels(true);
    try {
      const models = await getAccountModels(acc.account_id);
      setDiscoveredModels(models);
    } catch (err: any) {
      showToast("Error discovering models: " + err.message, "error");
      setModelsAccount(null);
    } finally {
      setLoadingModels(false);
    }
  };

  const handleDelete = async (acc: Account) => {
    if (!confirm(`Unlink account '${acc.display_label}'? Profile credentials will remain intact.`)) return;
    try {
      await deleteAccount(acc.account_id);
      showToast(`Unlinked '${acc.display_label}'`, "info");
      fetchData();
    } catch (err: any) {
      showToast("Delete error: " + err.message, "error");
    }
  };

  const getStatusBadge = (status: string) => {
    const s = (status || "unknown").toLowerCase();
    let badgeClass = "badge-gray";
    if (s === "ready") badgeClass = "badge-green";
    else if (s === "needs_login") badgeClass = "badge-yellow";
    else if (s === "unavailable") badgeClass = "badge-red";
    return <span className={`badge ${badgeClass}`}>{s.toUpperCase().replace("_", " ")}</span>;
  };

  const unlinkedProfiles = availableProfiles.filter((p) => !p.is_linked);

  return (
    <div className="view-container">
      {/* Toast Notification Banner */}
      {toast && (
        <div className={`toast-banner toast-${toast.type}`}>
          <span>{toast.message}</span>
          <button className="toast-close" onClick={() => setToast(null)}>✕</button>
        </div>
      )}

      <div className="view-header">
        <div>
          <h2>Account Profiles</h2>
          <p className="subtitle">
            Connect and configure isolated Google Antigravity account profiles for multi-worker deliberation
          </p>
        </div>
        <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
          {unlinkedProfiles.length > 0 && (
            <button
              className="btn btn-purple"
              onClick={handleLinkAll}
              disabled={linkingAll}
              title="Link all host profiles in one click"
            >
              {linkingAll ? "⚡ Linking Profiles..." : `⚡ Link All Profiles (${unlinkedProfiles.length})`}
            </button>
          )}
          <button className="btn btn-primary" onClick={() => setShowAddModal(true)}>
            + Link Custom Account
          </button>
        </div>
      </div>

      {/* Available Local Profiles Card */}
      {availableProfiles.length > 0 && (
        <div className="available-profiles-section">
          <div className="available-profiles-header">
            <div>
              <h3>Local Antigravity Profiles ({availableProfiles.length})</h3>
              <p className="subtitle" style={{ marginBottom: 0 }}>
                Discovered in local agym store. Profiles can be linked into Council instantly.
              </p>
            </div>
            {unlinkedProfiles.length > 0 ? (
              <button
                className="btn btn-sm btn-purple"
                onClick={handleLinkAll}
                disabled={linkingAll}
              >
                {linkingAll ? "Linking..." : `Link All Available (${unlinkedProfiles.length})`}
              </button>
            ) : (
              <span className="badge badge-green">All Local Profiles Linked ✓</span>
            )}
          </div>

          <div className="available-profiles-grid">
            {availableProfiles.map((prof) => (
              <div
                key={prof.profile_name}
                className={`available-profile-card ${prof.is_linked ? "linked" : ""}`}
              >
                <div>
                  <strong style={{ fontSize: "14px" }}>{prof.profile_name}</strong>
                  <div className="text-muted text-xs">
                    Model: {prof.model || "default"}
                  </div>
                </div>
                <div>
                  {prof.is_linked ? (
                    <span className="badge badge-sm badge-green">Linked ✓</span>
                  ) : (
                    <button
                      className="btn btn-sm btn-outline"
                      onClick={() => handleQuickLink(prof)}
                    >
                      + Link
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Linked Council Accounts Grid */}
      <div>
        <div style={{ marginBottom: "16px" }}>
          <h3>Linked Council Accounts ({accounts.length})</h3>
          <p className="subtitle">Accounts actively available for worker assignment and task execution</p>
        </div>

        {loading ? (
          <div className="empty-state">
            <p>Loading accounts...</p>
          </div>
        ) : accounts.length === 0 ? (
          <div className="empty-state">
            <p>No account profiles linked to Council yet.</p>
            <div style={{ display: "flex", gap: "12px" }}>
              {availableProfiles.length > 0 && (
                <button className="btn btn-purple" onClick={handleLinkAll} disabled={linkingAll}>
                  ⚡ Link All Discovered Profiles
                </button>
              )}
              <button className="btn btn-secondary" onClick={() => setShowAddModal(true)}>
                Link Manually
              </button>
            </div>
          </div>
        ) : (
          <div className="cards-grid">
            {accounts.map((acc) => {
              const isChecking = checkingAccountIds.has(acc.account_id);
              return (
                <div key={acc.account_id} className="card account-card">
                  <div className="card-top">
                    <div>
                      <h3 className="card-title">{acc.display_label}</h3>
                      <code className="text-muted text-xs">profile: {acc.profile_ref}</code>
                    </div>
                    {isChecking ? (
                      <span className="badge badge-blue">Checking...</span>
                    ) : (
                      getStatusBadge(acc.auth_status)
                    )}
                  </div>

                  <div className="card-body">
                    <div className="prop-row">
                      <span>Provider:</span>
                      <strong>{acc.provider_type}</strong>
                    </div>
                    <div className="prop-row">
                      <span>Concurrency Limit:</span>
                      <strong>{acc.concurrency_limit} worker(s)</strong>
                    </div>
                    {acc.cli_version && (
                      <div className="prop-row">
                        <span>CLI Version:</span>
                        <code className="code-inline">{acc.cli_version}</code>
                      </div>
                    )}
                    {acc.last_auth_check && (
                      <div className="prop-row">
                        <span>Last Verified:</span>
                        <span className="text-muted text-xs">
                          {new Date(acc.last_auth_check).toLocaleTimeString()}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="card-actions">
                    <button
                      className="btn btn-sm btn-outline"
                      onClick={() => handleCheck(acc)}
                      disabled={isChecking}
                      title="Verify authentication non-blockingly"
                    >
                      {isChecking ? "Checking..." : "Check Status"}
                    </button>
                    <button className="btn btn-sm btn-outline" onClick={() => handleViewModels(acc)}>
                      Models
                    </button>
                    <button className="btn btn-sm btn-outline" onClick={() => handleConnect(acc)}>
                      Connect
                    </button>
                    <button className="btn btn-sm btn-outline" onClick={() => handleOpenEdit(acc)}>
                      Edit
                    </button>
                    <button className="btn btn-sm btn-danger-outline" onClick={() => handleDelete(acc)}>
                      Unlink
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Edit Account Modal */}
      {editingAccount && (
        <div className="modal-backdrop" onClick={() => setEditingAccount(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Edit Account: {editingAccount.profile_ref}</h3>
            <form onSubmit={handleSaveEdit}>
              <div className="form-group">
                <label>Display Label</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Personal Account"
                  value={editLabel}
                  onChange={(e) => setEditLabel(e.target.value)}
                />
              </div>
              <div className="form-group">
                <label>Concurrency Limit</label>
                <input
                  type="number"
                  min="1"
                  max="8"
                  value={editConcurrency}
                  onChange={(e) => setEditConcurrency(Number(e.target.value))}
                />
              </div>
              <div className="form-group" style={{ display: "flex", flexDirection: "row", alignItems: "center", gap: 10 }}>
                <input
                  type="checkbox"
                  id="editEnabled"
                  checked={editEnabled}
                  onChange={(e) => setEditEnabled(e.target.checked)}
                />
                <label htmlFor="editEnabled" style={{ margin: 0, cursor: "pointer" }}>
                  Account Enabled
                </label>
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-secondary" onClick={() => setEditingAccount(null)}>
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary">
                  Save Changes
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Add Modal */}
      {showAddModal && (
        <div className="modal-backdrop" onClick={() => setShowAddModal(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Link Antigravity Profile</h3>
            <form onSubmit={handleAddAccount}>
              <div className="form-group">
                <label>Profile Reference Name (in agym)</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. personal, work, research_2"
                  value={newProfileRef}
                  onChange={(e) => setNewProfileRef(e.target.value)}
                />
                <small className="form-help">
                  Matches an existing profile in agym or will be initialized for Council.
                </small>
              </div>
              <div className="form-group">
                <label>Display Label</label>
                <input
                  type="text"
                  placeholder="e.g. Personal Account"
                  value={newLabel}
                  onChange={(e) => setNewLabel(e.target.value)}
                />
              </div>
              <div className="form-group">
                <label>Provider Type</label>
                <select value={newProvider} onChange={(e) => setNewProvider(e.target.value)}>
                  <option value="antigravity">Antigravity CLI (Native Google Account)</option>
                  <option value="fake">Fake Provider (Simulation / Testing)</option>
                </select>
              </div>
              <div className="form-group">
                <label>Concurrency Limit</label>
                <input
                  type="number"
                  min="1"
                  max="8"
                  value={newConcurrency}
                  onChange={(e) => setNewConcurrency(Number(e.target.value))}
                />
              </div>
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => setShowAddModal(false)}
                  disabled={addingAccount}
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary" disabled={addingAccount}>
                  {addingAccount ? "Linking..." : "Link Account"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Connect Helper Modal */}
      {connectResult && (
        <div className="modal-backdrop" onClick={() => setConnectResult(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Account Sign-In Helper</h3>
            <p className="subtitle">{connectResult.message}</p>
            {connectResult.status === "manual_required" && (
              <div className="command-box">
                <code>{connectResult.command}</code>
              </div>
            )}
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={() => setConnectResult(null)}>
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Models Modal */}
      {modelsAccount && (
        <div className="modal-backdrop" onClick={() => setModelsAccount(null)}>
          <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
            <h3>Discovered Models: {modelsAccount}</h3>
            {loadingModels ? (
              <p className="text-muted">Probing provider models...</p>
            ) : discoveredModels.length === 0 ? (
              <p className="text-muted">
                No models returned yet. Model discovery happens automatically when authenticated.
              </p>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "10px", maxHeight: "320px", overflowY: "auto" }}>
                {discoveredModels.map((m) => (
                  <div
                    key={m.id}
                    style={{
                      padding: "10px 14px",
                      background: "var(--bg-subtle)",
                      borderRadius: "var(--radius-sm)",
                      border: "1px solid var(--border-color)",
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <div>
                      <strong>{m.display_name || m.id}</strong>
                      <div className="text-muted text-xs"><code>{m.id}</code></div>
                    </div>
                    {m.context_window && (
                      <span className="badge badge-sm badge-blue">
                        {(m.context_window / 1000).toFixed(0)}k ctx
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setModelsAccount(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
