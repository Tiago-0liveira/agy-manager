export interface Account {
  account_id: string;
  provider_type: string;
  profile_ref: string;
  display_label: string;
  enabled: number | boolean;
  auth_status: string;
  last_auth_check?: string;
  cli_version?: string;
  advisory_usage?: any;
  concurrency_limit: number;
}

export interface AvailableProfile {
  profile_name: string;
  home: string;
  model: string;
  is_linked: boolean;
  account_id?: string;
  auth_status: string;
  display_label: string;
  concurrency_limit: number;
}

export interface DiscoveredModel {
  id: string;
  display_name: string;
  context_window?: number;
  capabilities?: string[];
}

export interface AgentTemplate {
  id?: string;
  template_id?: string;
  name?: string;
  display_name?: string;
  purpose: string;
  instructions?: string;
  base_instructions?: string;
  working_style?: string;
  output_expectations?: string[];
  preferred_model?: string;
  suggested_capabilities?: string[];
}

export interface WorkerConfig {
  id: string;
  name: string;
  account_ref: string;
  model: string;
  role: string;
  instructions: string;
  task: string;
}

export interface StageConfig {
  id: string;
  kind: "independent" | "critique" | "revise" | "synthesize" | "audit";
  workers: string[];
  input_stages: string[];
  instruction: string;
  context: "fresh" | "continue";
  release: string;
  failure_policy: string;
  required_sections: string[];
}

export interface WorkflowConfig {
  schema_version: number;
  draft: boolean;
  name: string;
  goal: string;
  inputs: { id: string; description: string; required: boolean; value?: string }[];
  limits: {
    global_concurrency: number;
    per_account_concurrency: number;
    max_model_calls: number;
    max_retries_per_task: number;
    max_wall_seconds: number;
    automatic_account_switching: boolean;
  };
  execution_mode: string;
  workers: WorkerConfig[];
  stages: StageConfig[];
  final_stage: string;
}

export interface Run {
  run_id: string;
  name: string;
  goal: string;
  status: "DRAFT" | "READY" | "RUNNING" | "PAUSING" | "PAUSED" | "NEEDS_ATTENTION" | "COMPLETED" | "BUDGET_EXHAUSTED" | "CANCELLED" | "FAILED";
  current_stage_id?: string;
  model_calls_made: number;
  dissent_recorded: number | boolean;
  final_deliverable_artifact_id?: string;
  started_at?: string;
  finished_at?: string;
  created_at: string;
  config?: WorkflowConfig;
  stages?: any[];
  workers?: any[];
  issues?: any[];
}

export interface CouncilEvent {
  sequence: number;
  event_id: string;
  run_id: string;
  stage_id?: string;
  worker_id?: string;
  attempt_id?: string;
  type: string;
  timestamp: string;
  payload: any;
}

export interface Artifact {
  artifact_id: string;
  run_id: string;
  stage_id?: string;
  worker_id?: string;
  attempt_id?: string;
  name: string;
  content_hash: string;
  byte_size: number;
  media_type: string;
  released: number | boolean;
  created_at: string;
}
