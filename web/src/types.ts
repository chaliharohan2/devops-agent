export type Risk = "low" | "medium" | "high";

export interface Component {
  id: number;
  name: string;
  kind: string;
  repo_url: string | null;
  path: string;
  manifest: Record<string, unknown>;
}

export interface Environment {
  id: number;
  name: string;
  branch: string;
  branch_overrides: Record<string, string>;
  requires_approval: boolean;
  auto_deploy: boolean;
  deploy_target: Record<string, unknown>;
  config_version: number;
}

export interface App {
  id: number;
  name: string;
  description: string;
  components: Component[];
  environments: Environment[];
}

export interface Evidence {
  file: string;
  line: number | null;
  snippet: string | null;
}

export interface ConfigChange {
  kind: string;
  component: string;
  key: string | null;
  description: string;
  evidence: Evidence[];
  is_secret_hint: boolean;
  default_value: string | null;
  requires_value: boolean;
}

export interface ChangeReport {
  app: string;
  component: string;
  source_branch: string;
  target_branch: string;
  base_sha: string;
  head_sha: string;
  commits: { sha: string; author: string; subject: string }[];
  changed_files: { path: string; status: string; additions: number; deletions: number }[];
  config_changes: ConfigChange[];
  requires_rebuild: boolean;
  requires_migration: boolean;
  warnings: string[];
  summary: string;
}

export interface Preflight {
  repo: string;
  branch: string | null;
  local_head: string | null;
  pushed_head: string | null;
  unpushed_commits: number;
  uncommitted: string[];
  problems: string[];
  warnings: string[];
}

export interface ValueNeeded {
  key: string;
  secret: boolean;
  default: string | null;
  required: boolean;
  description: string;
  evidence: string[];
}

export interface Analysis {
  app: string;
  component: string;
  environment: string;
  target_branch: string;
  preflight: Preflight;
  report?: ChangeReport;
  values_needed?: ValueNeeded[];
}

export interface Review {
  risk_level: Risk;
  summary: string;
  concerns: string[];
  missing_config: string[];
  deployment_steps: string[];
  source: string;
}

export interface ChangeRequest {
  id: number;
  app: string;
  component: string;
  environment: string;
  source_branch: string;
  target_branch: string;
  head_sha: string;
  status: string;
  requested_by: string;
  risk_level: Risk | null;
  review: Review | null;
  discrepancies: string[];
  config_keys: string[];
  merge_sha: string | null;
  deployment_id: number | null;
  error: string | null;
  timeline: { at: string; status: string; message: string }[];
  created_at: string;
  updated_at: string;
}

export interface EnvState {
  name: string;
  app: string;
  environment: string;
  project: string;
  local_components: string[];
  status: string;
  ports: Record<string, number>;
  urls: Record<string, string>;
  sources: Record<string, { path: string; mode: string; origin: string; ref?: string; sha?: string }>;
  services: string[];
  stubbed_secrets: Record<string, string[]>;
  warnings: string[];
  health: Record<string, string>;
  message: string;
  instructions: string;
  created_at: string;
  updated_at: string;
}

export interface PeerRequest {
  id: number;
  app: string;
  component: string;
  from_user: string;
  to_user: string;
  share: "branch" | "working_tree";
  ref: string | null;
  message: string;
  status: string;
  artifact_sha256: string | null;
  artifact_size: number | null;
  head_sha: string | null;
  created_at: string;
}

export interface Job {
  id: string;
  kind: string;
  status: "running" | "done" | "failed";
  result: unknown;
  error: string | null;
  log: string[];
}

export interface Deployment {
  id: number;
  app: string;
  environment: string;
  change_request_id: number | null;
  status: string;
  target: string;
  component_shas: Record<string, string>;
  config_version: number;
  plan: { compose?: unknown; urls?: Record<string, string>; warnings?: string[] };
  log: string;
  diagnosis: string | null;
  triggered_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ConfigEntry {
  scope: string;
  key: string;
  value: string | null;
  is_secret: boolean;
  description: string;
  version: number;
  updated_by: string;
  updated_at: string | null;
}

export interface AuditEvent {
  id: number;
  ts: string;
  actor: string;
  action: string;
  subject_type: string;
  subject_id: string;
  details: Record<string, unknown>;
}

export interface AgentInfo {
  id: number;
  username: string;
  hostname: string;
  version: string;
  model: string | null;
  last_seen_at: string;
  online: boolean;
}

export interface ChatStep {
  tool: string;
  arguments: Record<string, unknown>;
  result?: unknown;
  error?: string | null;
  confirmed?: boolean | null;
}
