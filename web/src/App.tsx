import React, { useState, useEffect } from "react";
import { AccountsView } from "./components/AccountsView";
import { AgentsView } from "./components/AgentsView";
import { WorkflowsView } from "./components/WorkflowsView";
import { LaunchView } from "./components/LaunchView";
import { DashboardView } from "./components/DashboardView";
import { initSession } from "./api";

type Tab = "accounts" | "agents" | "workflows" | "launch" | "dashboard";

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<Tab>("accounts");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  useEffect(() => {
    initSession();
  }, []);

  const handleRunLaunched = (runId: string) => {
    setActiveRunId(runId);
    setActiveTab("dashboard");
  };

  return (
    <div className="app-layout">
      {/* Top Header Navbar */}
      <header className="app-header">
        <div className="header-brand">
          <span className="brand-icon">🏛️</span>
          <div>
            <h1 className="brand-title">AGYM Council</h1>
            <span className="brand-subtitle">Multi-Account Multi-Worker Orchestration</span>
          </div>
        </div>

        <nav className="header-nav">
          <button
            className={`nav-btn ${activeTab === "accounts" ? "active" : ""}`}
            onClick={() => setActiveTab("accounts")}
          >
            Accounts
          </button>
          <button
            className={`nav-btn ${activeTab === "agents" ? "active" : ""}`}
            onClick={() => setActiveTab("agents")}
          >
            Agent Library
          </button>
          <button
            className={`nav-btn ${activeTab === "workflows" ? "active" : ""}`}
            onClick={() => setActiveTab("workflows")}
          >
            Workflows
          </button>
          <button
            className={`nav-btn ${activeTab === "launch" ? "active" : ""}`}
            onClick={() => setActiveTab("launch")}
          >
            Launch Run
          </button>
          <button
            className={`nav-btn ${activeTab === "dashboard" ? "active" : ""}`}
            onClick={() => setActiveTab("dashboard")}
          >
            Dashboard
          </button>
        </nav>
      </header>

      {/* Main Body View */}
      <main className="app-main">
        {activeTab === "accounts" && <AccountsView />}
        {activeTab === "agents" && <AgentsView />}
        {activeTab === "workflows" && <WorkflowsView />}
        {activeTab === "launch" && <LaunchView onRunLaunched={handleRunLaunched} />}
        {activeTab === "dashboard" && <DashboardView initialRunId={activeRunId} />}
      </main>
    </div>
  );
};

export default App;
