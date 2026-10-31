# Architecture decision records

Short ADRs for the choices that shape the system. Status: **accepted** unless noted.

### ADR‑001 Monorepo with a shared protocol package
Both agents import `devops_common` (uv workspace). The wire protocol, manifest spec, analyzer and compose renderer
exist once, so the server's re‑analysis is byte‑for‑byte the same logic as the laptop's.
*Alternative:* separate repos + OpenAPI client generation — more drift, slower iteration.

### ADR‑002 Deterministic facts, LLM‑assisted judgement
Which files/env vars/migrations changed is computed, not generated. LLMs summarise, review, explain and route natural
language; their output is schema‑validated and can only add caution (risk = max(floor, llm)).
*Why:* a 9B CPU model and even a 27B server model will occasionally be wrong; deploy safety cannot depend on that.

### ADR‑003 Capabilities enforced by git wrappers
`ReadOnlyGit` / `WorkspaceGit` / `MergeGit` allow‑list subcommands and flags and refuse before spawning git.
`MergeGit.merge_verified` proves merges introduce no server‑authored changes.
*Alternative:* trust prompts or separate OS users — prompts are not a security boundary; OS users are still a good
extra layer for the server deployment.

### ADR‑004 Configuration lives in the server's config store
Per‑environment, versioned, encrypted key/values (app‑wide or per component), rendered at deploy time.
*Why:* lets the server "update configs" without writing to code repos (rule R8), gives history/rollback and keeps
secrets off laptops. *Trade‑off:* config is no longer reviewed in PRs — mitigated by the dashboard history,
audit log and approvals.

### ADR‑005 Merge with git, not a hosting API (for now)
`fetch → merge --no-ff → push` works with any git host. *Planned:* optional PR‑based mode (GitHub/GitLab APIs) for
protected branches — see PLAN Phase 1.5.

### ADR‑006 Docker Compose for test environments and the first deploy target
One renderer (`devops_common.compose`) serves laptops and the server. Compose is what developers already run;
Kubernetes/Helm rendering from the same `EnvironmentSpec` is Phase 4.

### ADR‑007 Agents poll the server (inbox), the server never calls laptops
Laptops are behind NAT/VPN; polling every 5 s over HTTPS needs no inbound ports. WebSockets/SSE can replace polling
later without protocol changes.

### ADR‑008 Peer sharing through a server relay
Consent‑gated uploads of git bundles or working‑tree tarballs, sha256‑verified, expiring. *Alternative:* direct P2P
— blocked by NAT and harder to audit. LAN‑direct is an optional Phase 2 add‑on.

### ADR‑009 Web‑first UI, desktop shell later
One React bundle, served by the local daemon (developer app) and by the server (dashboard). Packaging as a desktop
app later is a thin Tauri/Electron wrapper around the same daemon + UI. *Why React:* the brief allowed React or
Angular; React + Vite gives the smallest footprint and the example apps use it too. The manifest/analyzer support
Angular projects equally.

### ADR‑010 Ollama + Ornith‑1.5‑9B on laptops
See [research/LOCAL_MODEL_SELECTION.md](research/LOCAL_MODEL_SELECTION.md). The agent speaks the OpenAI API, so
runtime and model are swappable by config.

### ADR‑011 In‑process job runner for v1
Pipelines run in a thread pool with all state in Postgres. Swap for a queue + advisory locks for multi‑replica
servers (Phase 1.5).

### ADR‑012 Secrets never reach laptops
Specs for test environments carry secret *keys* only; manifests supply local values (`env.*.local`) or the agent
stubs them. Real secrets exist only on the server (encrypted at rest) and in deployment runtimes.
