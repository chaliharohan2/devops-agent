# Local model selection (developer machines)

**Constraint:** every developer laptop has **20 GB RAM, CPU only, 512 GB disk**.
**Research date:** 31 Oct 2026.
**Decision:** **Ornith‑1.5‑9B, Q4_K_M GGUF (~5.6 GB file, ~7–8 GB resident at 16K context), served by Ollama.**
Fallback for low‑memory machines: **Gemma 4 E4B (Q4_K_M, ~3 GB)**. Optional: **Gemma 4 12B Unified** if a developer
has spare RAM.

---

## 1. What the local model actually has to do

The local agent is *deterministic‑first*. Git inspection, diff analysis, config detection, compose rendering and
health checks are plain Python (`devops_common.analysis`, `devops_common.compose`). The model is used for:

| Task | Needs |
|---|---|
| Understand requests ("set up an e2e env for my backend against staging") and pick a tool | reliable tool/function calling, instruction following |
| Ask for missing information (env name, config values) | short multi‑turn dialogue |
| Explain analysis results and testing instructions | concise summarisation of structured data |
| (later) draft a `devops-agent.yaml` for an unknown repo | code understanding |

So we need **good tool calling + code literacy at small size**, and **interactive latency on a CPU** — not
frontier reasoning. Anything the model gets wrong is caught by schemas, the read‑only git guard and human
confirmation for outward actions (deploy requests, peer requests, teardown).

## 2. RAM budget

20 GB is shared with everything a developer runs while testing:

| Consumer | Typical |
|---|---|
| OS + IDE (VS Code/JetBrains) + browser | 5–7 GB |
| Docker test environment (Postgres + FastAPI + Vite dev server) | 2–4 GB |
| Docker Desktop VM overhead (macOS/Windows) | ~1 GB |
| **Left for the model** | **≈ 8–10 GB max; target ≤ 8 GB** |

`devops_local.hardware.recommend()` encodes this: it reserves 9 GB and picks the best catalog model that fits.

CPU decoding is memory‑bandwidth bound: tokens/s ≈ effective bandwidth ÷ bytes read per token. On a
dual‑channel DDR5 laptop (~60–80 GB/s) a dense ~9B Q4 model (~5.5 GB) gives roughly **8–13 tok/s**, which is
acceptable for short agent turns. A dense 30B model would be ~2 tok/s.

## 3. Candidates evaluated

Numbers are from vendor model cards / GGUF repos unless stated; we found **no independent CPU benchmarks** for
most of them — see §6 for the benchmark we will run on the team's laptops.

| Model (released) | Type | Q4 file | Fits 20 GB with Docker? | Agentic/tool use | License | Verdict |
|---|---|---|---|---|---|---|
| **Ornith‑1.5‑9B** (DeepReinforce, Aug 2026) | dense 9B, built on Qwen3.5/Gemma 4 bases | Q4_K_M ≈ 5.6 GB (IQ4_XS 5.45 GB) | **Yes** (~7.5 GB resident) | Agentic‑coding focus: SWE‑bench Verified 70.6, Terminal‑Bench 2.1 46.2, MCP‑Atlas 54.2 (vendor); OpenAI‑style tool calls; 262K native ctx | MIT | **Chosen** |
| Gemma 4 12B Unified (Google, Jun 2026) | dense 12B multimodal | ≈ 7–7.5 GB | Tight (~9.5 GB) | Native function calling; Tau2 69.0% (official table); some community reports of tool‑call loops | Apache‑2.0 | Opt‑in alternative |
| Gemma 4 E4B (Google, Mar 2026) | ~4B effective | ≈ 3 GB | Yes, lots of headroom | Tau2 42.2% — weak tool use | Apache‑2.0 | Low‑RAM fallback |
| Gemma 4 E2B | ~2B effective | ≈ 2.9 GB (4‑bit) | Yes | Weaker still | Apache‑2.0 | Not recommended |
| Gemma 4 26B A4B (MoE) | 26B total / 4B active | ≈ 15–16 GB | **No** (no room for Docker) | Tau2 68.2% | Apache‑2.0 | Rejected (RAM) |
| Meta **Muse Glimmer 30B** (Aug 2026) | dense 30B, "always‑on local agents" | Q4_K_M ≈ 16.8–17.3 GB | **No** (~18 GB total use) | Meta reports it beats Gemma4‑31B and Qwen3.6‑27B on tool use/planning | Apache‑2.0 | Rejected: RAM, and ~1.6 tok/s on a 4‑core laptop CPU (third‑party) |
| Ornith‑1.5‑35B‑A3B (MoE) | 35B / 3B active | Q4_K_M ≈ 21.9 GB | **No** | Strong | MIT | Rejected (RAM) — good fit for a 32 GB+ machine |
| Qwen3.6‑35B‑A3B (Apr 2026) | 35B / 3B active | ≈ 20+ GB | **No** | SWE‑bench 73.4% (vendor), built‑in tool calling | Apache‑2.0 | Rejected (RAM) |
| Qwen3.5 small (0.8B/2B/4B/9B) | dense | 9B ≈ 5.5 GB | Yes | Good tool calling | Apache‑2.0 | Viable alternative to Ornith 9B (Ornith 1.5 9B is post‑trained from this family for agentic coding) |
| gpt‑oss‑20b | MoE 21B / 3.6B active, MXFP4 | ≈ 14 GB | Marginal | Good reasoning & tool calling | Apache‑2.0 | Rejected: 14 GB leaves ~6 GB for IDE + browser + Docker |
| Phi‑4‑mini (3.8B) | dense | ≈ 2.5 GB | Yes | OK | MIT | Superseded by Gemma 4 E4B / Qwen3.5‑4B |

Qwen3.8 (Aug 2026) shipped open weights only as 27B dense and a 2.4T MoE; the 27B is what the **server agent**
uses through vLLM (`cyankiwi/Qwen3.8-27B-AWQ-INT4`) and is too large for a 20 GB CPU laptop.

## 4. Why Ornith‑1.5‑9B

1. **Fits the budget with headroom**: ~5.6 GB on disk, ~7.5 GB resident with a 16K context — leaves room for
   the IDE, browser and the Docker test environment the agent itself starts.
2. **Purpose‑built for agentic coding**: its post‑training targets exactly our loop (read repo state → call tools
   → report). Vendor numbers are far above other ≤12B models on SWE‑bench/Terminal‑Bench/MCP‑Atlas. Even if
   independent results land lower, the gap to Gemma 4 E4B is large.
3. **Tool calling in the OpenAI format** works through Ollama/llama.cpp's OpenAI‑compatible endpoint, which is
   what `devops_common.llm` speaks. Our client also parses `<tool_call>{…}</tool_call>` text, so a missing
   template parser does not break the agent.
4. **MIT license** — no usage restrictions for internal tooling.
5. **Speed**: dense 9B at Q4 ≈ 8–13 tok/s on DDR5 laptops; speculative decoding builds (MTP head) exist for up
   to ~1.8× decode speed‑up if we need it later.

Risks and mitigations:

* *Benchmarks are vendor‑reported* → we keep the model swappable (one config line) and ship a catalog with two
  alternatives; §6 defines our own acceptance test.
* *Tool‑call reliability on CPU quantizations* → Q4_K_M (not lower; community guidance is to avoid < Q4 for
  agents), short tool schemas, bounded loops, deterministic fallback router (`devops_local.brain.Brain.rules`).
* *Slow first token on long prompts* → system prompt and tool schemas kept compact; tool results truncated to
  2.5K chars; context 16K (not the 262K max) to cap KV‑cache RAM.

## 5. Runtime: Ollama

* one‑line install on macOS/Linux/Windows, CPU inference via llama.cpp (AVX2/AVX‑512/NEON);
* pulls GGUFs directly from Hugging Face: `ollama pull hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M`
  (no dependency on a curated library tag);
* OpenAI‑compatible API on `127.0.0.1:11434/v1`;
* `devops-agent model pull` also creates a tuned alias **`devops-agent-slm`** with `num_ctx=16384`,
  `temperature=0.2`, so switching models never touches the agent config beyond `llm.source`.

Model sources in the catalog (`devops_local/hardware.py`): the Ornith repo (`ornith-ai/Ornith-1.5-9B-GGUF`) and
`unsloth/gemma-4-12b-it-GGUF` were confirmed during research; the Gemma 4 E4B path follows Unsloth's naming and
should be confirmed by the first `devops-agent model pull gemma-4-e4b` in the acceptance test (§6).

Any other OpenAI‑compatible server (llama.cpp `llama-server`, LM Studio) works by editing
`~/.devops-agent/config.yaml` → `llm.base_url` / `llm.model`.

Disk: model 5.6 GB + Ollama ≈ 6 GB; Docker images for typical test envs 3–10 GB — negligible against 512 GB.

## 6. Acceptance benchmark to run on a team laptop

Before rolling out, run on one standard 20 GB machine:

```bash
ollama pull hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M
ollama run hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M --verbose "Say hi"   # prints eval rate (tok/s)
devops-agent chat "what's the status of my deploys?"                       # tool-call smoke test
```

Pass criteria: ≥ 8 tok/s decode, ≤ 8 GB RSS for `ollama` while a test environment is running, correct tool
selection on the 20 scripted prompts in `docs/TESTING.md`. If Ornith fails, switch the default to Gemma 4 12B
(`devops-agent model pull gemma-4-12b`) or Qwen3.5‑9B.

## Sources

- Ornith‑1.5 release & sizes: https://datanorth.ai/news/ornith-releases-ornith-1-5 ,
  https://simonwillison.net/2026/Jun/29/ornith/
- Ornith‑1.5‑9B GGUF card (benchmarks, quants): https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF ,
  https://huggingface.co/AtomicChat/Ornith-1.5-9B-GGUF , https://huggingface.co/protoLabsAI/Ornith-1.5-9B-MTP-GGUF
- Ornith‑1.5‑35B‑A3B sizes: https://huggingface.co/bartowski/Ornith-1.5-35B-A3B-GGUF ,
  https://willitrunai.com/models/ornith-1.5-35b-a3b
- Gemma 4 releases & sizes: https://ai.google.dev/gemma/docs/releases , https://gemma4all.com/blog/gemma-4-download
- Gemma 4 12B Unified: https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/ ,
  https://huggingface.co/google/gemma-4-12B , https://news.ycombinator.com/item?id=48385906
- Gemma 4 tool-calling issues in Ollama (Apr 2026): https://gist.github.com/daniel-farina/87dc1c394b94e45bb700d27e9ea03193
- Muse Glimmer: https://ai.meta.com/research/publications/muse-glimmer-30b-an-on-device-agentic-model/ ,
  https://huggingface.co/meta-models/Muse-Glimmer-30B , https://www.phoronix.com/news/Meta-Muse-Glimmer ,
  https://computingforgeeks.com/run-muse-glimmer-locally/ , https://huggingface.co/bartowski/Muse-Glimmer-30B-GGUF
- Qwen3.6‑35B‑A3B: https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF , https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.6
- Qwen3.8: https://github.com/QwenLM/Qwen3.8 , https://en.wikipedia.org/wiki/Qwen
- Small-model roundups (gpt-oss-20b, Phi-4-mini, quantization guidance): https://www.morphllm.com/best-ollama-models ,
  https://huggingface.co/blog/daya-shankar/open-source-llm-models-to-run-locally
- CPU inference / MoE bandwidth analysis: https://inventivehq.com/blog/moe-on-cpu-benchmark ,
  https://github.com/ggml-org/llama.cpp/issues/19480
