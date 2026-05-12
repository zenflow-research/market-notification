# C1 audit -- Gemma orchestrator integration plan

> Audit produced on **2026-05-12** from a read of `D:\gemma-retrieval\docs\`.
> Purpose: surface what's already built, what blocks `market_notification`
> adoption, and a concrete phasing recommendation that meshes with the
> existing 8-session build plan rather than competing with it.

---

## TL;DR

- The Gemma orchestrator (single broker at `127.0.0.1:11435` fronting Ollama)
  is **actively under construction** at `D:\gemma-retrieval\gemma_retrieval\orchestrator/`.
- Build cadence: **8 sessions, Session 3/8 in flight as of 2026-05-12 10:42 UTC**.
- Tags shipped: `orch-p0` (Session 1: foundation + schema) and `orch-p1`
  (Session 2: HTTP API + scheduler + Ollama runner; V1 gate passed).
- `market_notification` migration is **Session 8/8 deliverable** -- the spec
  is explicit that mn is migrated via a tested patch file + migration doc,
  *not* by direct edits from the orchestrator repo.
- Remaining work to mn adoption: 5 more orchestrator sessions + 1 migration
  session = ~6 weeks at ~1 session/week pace.
- **No action required from mn this conversation.** When the migration
  patch lands in Session 8/8, it will be a ~50-line drop-in change in the
  3 affected modules (classifier, summarizer, priority/llm_override).

---

## 1. Current state of the orchestrator

### 1.1 What's already shipped

| Tag | Session | Scope | V-gate |
|---|---|---|---|
| `orch-p0` | 1/8 | git init, package skeleton, SQLite schema (`gemma_tasks`, `gemma_task_journal`, `schema_version`), CLI entry, baseline tests | V0 (CLI prints version, DB initializes) |
| `orch-p1` | 2/8 | FastAPI app (`POST /v1/enqueue`, `GET /v1/tasks/{id}`, `GET /v1/health`), `Scheduler` daemon thread with claim+run loop, `OllamaRunner` (httpx, `/api/generate` shape) | V1 (HTTP smoke: enqueue -> claimed -> done; eval_count surfaced) |

### 1.2 What's in flight (today, 2026-05-12)

Session 3/8 -- "P2 priority scheduler + journal + queue endpoint":
- Spec's exact `UPDATE ... RETURNING` claim SQL for priority-FIFO ordering
- Journal table writes from api / scheduler / runner actors
- `GET /v1/queue` endpoint
- V2 gate: P1 task overtakes a P3 task already in queue

### 1.3 What still has to ship before mn migration

| Session | Goal | mn-relevant? |
|---|---|---|
| 3/8 | priority + journal + queue (in flight) | yes -- mn's P1 traffic depends on priority claim |
| 4/8 | sync endpoint, idempotency, cancel | yes -- mn callers want `POST /v1/enqueue/sync` (sync wait, drop-in replacement) |
| 5/8 | idle producer framework + repair_summaries producer | no -- gemma-retrieval-internal |
| 6/8 | crash recovery + watchdog | indirectly -- mn doesn't want a hung Ollama call to wedge the queue |
| 7/8 | throughput metrics + dashboard panel | yes -- mn Streamlit Health tab will surface orchestrator queue depth |
| **8/8** | **client SDK + market_notification migration patch + V6 30-min soak + V7 two-real-clients** | **directly -- this is the integration session** |

Estimated calendar: at ~1 session/week, **Session 8/8 closes ~6 weeks out** from 2026-05-12. Session pace could be tighter if no blocking decisions surface.

---

## 2. The mn migration as specified

### 2.1 What the orchestrator team will deliver (Session 8/8)

Per `ORCHESTRATOR_IMPLEMENTATION_PLAN.md` § 3.7 (Session 8/8 scope):

- **`gemma_retrieval/orchestrator_client.py`** -- a `GemmaClient` class:
  ```python
  GemmaClient(base_url="http://127.0.0.1:11435", caller="market_notification")
  client.call(prompt=..., priority=1, options={...}) -> str
  client.chat(messages=[...], priority=1, options={...}) -> str
  ```
  Drop-in for the `requests.post(...)` pattern currently used in mn.

- **`docs/MARKET_NOTIFICATION_MIGRATION.md`** -- step-by-step guide for
  applying the migration to mn.

- **A tested `.patch` file** -- applies in `G:\market_notification\` and
  edits the 3 mn modules that call Ollama directly:
  - `src/market_notification/classifier/llm_classifier.py`
  - `src/market_notification/summarizer/gemma_summarizer.py`
  - `src/market_notification/priority/llm_override.py`

- **Feature-flag wrapper** in `gemma_retrieval` itself (`GEMMA_USE_ORCHESTRATOR=1`)
  so gemma-retrieval's own callers can flip back to direct Ollama for
  comparison if needed -- mn gets an env-var equivalent
  (`MN_GEMMA_USE_ORCHESTRATOR=1`).

### 2.2 What the patch will look like (per the spec)

Each mn module currently has a block like:

```python
import requests
resp = requests.post(
    f"{ollama_url}/api/generate",
    json={"model": gemma_model, "prompt": prompt, "format": "json",
          "options": {"temperature": 0.1, "num_predict": 4096},
          "keep_alive": "24h", "think": False},
    timeout=300,
)
text = resp.json()["response"]
```

The patch replaces it with:

```python
from gemma_retrieval.orchestrator_client import GemmaClient
_CLIENT = GemmaClient(caller="market_notification_summarizer")  # caller per module
text = _CLIENT.call(
    prompt=prompt,
    priority=1,
    options={"temperature": 0.1, "num_predict": 4096, "format": "json", "think": False},
    timeout_s=300,
)
```

Total diff: ~15 lines per module, ~50 lines across the 3 modules.

### 2.3 Why "patch + doc" instead of direct edits

Per `ORCHESTRATOR_IMPLEMENTATION_PLAN.md` § 2 decision #12: "patch file +
doc, no edits to `G:\`". The orchestrator repo never touches the mn
working tree. mn's user (you) reviews the patch and applies it when
ready. This preserves cross-repo control.

---

## 3. What mn can do *now* (before Session 8/8 lands)

Nothing that the orchestrator team requires. But there's optional prep
work mn can complete in parallel:

### 3.1 Recommended: thin abstraction layer (optional, ~1 hr)

If you want to make the migration patch even smaller, you can wrap the
3 current Ollama call-sites in a per-module helper like:

```python
# src/market_notification/ops/llm_client.py
class GemmaCallable:
    """Thin wrapper around direct Ollama calls. The Session 8/8 migration
    swaps the body of `call` to use GemmaClient from the orchestrator;
    the call sites (classifier / summarizer / priority) don't change."""
    def __init__(self, caller: str):
        self.caller = caller
    def call(self, *, prompt: str, options: dict, timeout_s: int = 300) -> str:
        ...  # direct requests.post today; GemmaClient post-migration
```

Then each module imports and calls through this helper. The migration
patch becomes ~5 lines in one file instead of ~50 lines across 3.

**Not required.** The Session 8/8 patch works without this; the
migration team has factored for direct-call-site edits.

### 3.2 Recommended: monitor `D:\gemma-retrieval\docs\sessions\`

The orchestrator team writes a session log per session. Session 3, 4, 5,
6, 7 are upcoming. Each ends with a handover doc in
`D:\gemma-retrieval\docs\orchestrator_sessions\session_N_of_8_*.md`.
Skim them when they land to catch any decision that might affect mn
(e.g. `keep_alive` enforcement = `24h`, `num_predict` cap on P4 = 1024,
within-priority ordering = strict FIFO).

### 3.3 Not recommended: parallel build

Building a competing orchestrator inside mn would fork the design. The
orchestrator is intentionally cross-repo (it lives in `D:\gemma-retrieval`
because that repo also hosts the batch summarization and dashboard
that produce P3 + P4 work). Hosting it in mn would break the abstraction.

---

## 4. Risks & open questions

| Risk | Severity | Mitigation |
|---|---|---|
| Orchestrator build slips past 6 weeks | medium | mn keeps direct Ollama calls; no functional impact, just no priority queueing |
| Migration patch breaks mn V1.1 taxonomy or extended JSON envelope | medium | V7 (two-real-clients) is mn-specific and gates Session 8/8 closeout |
| Orchestrator-only Ollama traffic still leaves a single-model limit | n/a | Already understood: P1 (mn) ranks ahead of P3/P4, so latency is preserved by ordering, not by parallelism |
| `gemini-rr` deep-dive is **NOT** in scope of the orchestrator | low | gemini-rr is its own binary with its own 6-profile cache; deep-dive lane doesn't share the Gemma daemon, no integration needed |
| Streamlit Health tab needs orchestrator queue panel | low | Session 7/8 (throughput metrics + dashboard panel) -- the gemma-retrieval dashboard gets the panel; mn UI can either add the same panel or call `GET /v1/queue` directly |

### Open questions worth flagging to the orchestrator team

These are not blockers but should land in the deviation register if
decided differently from spec:

1. **Will `GemmaClient` honor mn's per-call `keep_alive` override?** Per
   `ORCHESTRATOR_IMPLEMENTATION_PLAN.md` § 2 decision #3, "broker enforces
   `24h`" -- mn never overrides this in practice today, but document it.
2. **Will the broker forward the Gemma `format: json` server-side hint?**
   mn's classifier + summarizer rely on `format: json` for envelope
   coercion. Confirmed via the Session 2 deviation row 11 ("popped to
   top-level Ollama payload") -- should be safe, but verify in V7.
3. **What's the orchestrator's behaviour when Gemma drops below 100 tok/s?**
   mn's anti-spill response is the Tier-2 monitor restart per
   `GEMMA4_MOE_REFERENCE.md` § 18.10-18.13. The orchestrator should
   detect and either (a) restart Ollama itself, or (b) surface a degraded
   `/v1/health` so mn's watcher fires DEGRADED alerts. Confirm in
   Session 6/8 (crash recovery + watchdog).

---

## 5. Recommendation

**No action required from mn this conversation.** The orchestrator team
has a clean plan and is executing. mn's cutover (currently in late shadow
mode / early A1.3 per `SHADOW_MODE.md` § 1) is independent of and parallel
to the orchestrator's adoption.

When Session 8/8 lands (~6 weeks):

1. Receive the patch file via Session 8/8 handover.
2. Apply patch in a feature branch under `G:\market_notification\`.
3. Run `scripts/_run_all_smoke.py` + `scripts/_b35_smoke.py` (still passing).
4. Run mn under the supervisor with `MN_GEMMA_USE_ORCHESTRATOR=1` for 24h
   shadow alongside the current direct-Ollama path.
5. Compare classification + summarization output between the two for a
   sample of 50 important rows. Confirm equivalence.
6. Flip `MN_GEMMA_USE_ORCHESTRATOR=1` permanently in `config/local.toml`.
7. Drop the env var requirement when V7 gate passes broker-side.

That whole sequence is < 1 day of effort on mn's side once the patch is
in hand.

---

## 6. References

- `D:\gemma-retrieval\docs\ORCHESTRATOR_PLAN.md` -- canonical spec
- `D:\gemma-retrieval\docs\ORCHESTRATOR_IMPLEMENTATION_PLAN.md` -- session breakdown + decisions
- `D:\gemma-retrieval\docs\ORCHESTRATOR_DEVIATIONS.md` -- live deviation register
- `D:\gemma-retrieval\docs\orchestrator_sessions\session_N_of_8_*.md` -- per-session handover (N = 1..8)
- `D:\gemma-retrieval\gemma_retrieval\orchestrator\` -- implementation tree
- `G:\brain\docs\memory\notifications\architecture.md` § 0.6 -- mn-side mention of the orchestrator
- `G:\market_notification\docs\PROJECT_OVERVIEW.md` § 19.1 -- Ollama integration today
- `G:\market_notification\docs\SHADOW_MODE.md` -- companion cutover playbook
