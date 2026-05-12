# Shadow Mode & Cutover Playbook

> Operational runbook for transitioning the notification subsystem from
> `G:\brain` (legacy, in-process) to `G:\market_notification` (production-grade,
> 3-process) without dropping a single filing.
>
> Owner: research@zenflow.finance
> Companion docs: `G:\brain\docs\memory\notifications\architecture.md` § 0
> (Evolution & New System Reference), `PROJECT_OVERVIEW.md` § 16.5 (Running).

---

## 0. Why this doc exists

The brain notification subsystem still ships rows to its `data/notifications.db`
via a Flask-embedded poller + summarizer; market_notification ships to its
own DB via 3 OS-process daemons. Both back onto the same Ollama daemon at
`127.0.0.1:11434` and can only load **one model at a time** (`OLLAMA_MAX_LOADED_MODELS=1`).
Naïve simultaneous operation thrashes the GPU.

The cutover is therefore **sequential**, not blue-green. This playbook
encodes the sequence with explicit go / no-go gates between phases, so the
work is reviewable, pausable, and reversible at every step.

The supervisor (`scripts/run_all.py`) and watcher (`scripts/watch_health.py`)
shipped with this playbook are the runtime building blocks; the
**preflight script** (`scripts/_shadow_preflight.py`) tells you what state
you're actually in before each phase.

---

## 1. State discovery -- always run first

```powershell
python G:/market_notification/scripts/_shadow_preflight.py
```

Read the output table. Map it to one of the entry-points below:

| Preflight result | You are in phase | Action |
|---|---|---|
| All CRITICAL pass, ports 8504/8505/8501 **free**, brain DB mtime recent | **A0** (brain solo) | Proceed to **A1.1** |
| All CRITICAL pass, ports **held**, brain DB mtime recent | **A1.2** (worker swap in progress) | Skip to **§5 daily checks** |
| All CRITICAL pass, ports **held**, brain DB mtime stale (> 24h) | **A1.3 or beyond** | Skip to **§6 success criteria** |
| Any CRITICAL fails | (blocked) | Resolve the FAIL, re-run preflight |
| Brain DB doesn't exist or is empty | (data-loss risk) | **STOP**, do not proceed; consult §10 rollback |

The preflight is idempotent and side-effect-free; run it as often as you
want during the cutover.

---

## 2. Phase A1.0 -- Pre-flight gate

**Goal**: every CRITICAL check on the preflight report says PASS.

| Check | What to do on FAIL |
|---|---|
| Ollama daemon responsive | `ollama serve` in a separate terminal; verify `curl http://localhost:11434/api/tags` returns JSON |
| Gemma 4 MoE model installed | `ollama pull gemma4-zenflow-moe:latest`; expect 18 GB download |
| gemini-rr binary | Re-install per `C:\Users\user\bin\` -- see `D:\claude-codex-gemini\README` |
| mn notifications.db backfilled | `python scripts/bootstrap_db.py && python scripts/backfill_brain_history.py` -- 88 minutes for 4.72 M rows |
| screener_essential.db (fundamentals) | This is brain's DB; verify the brain repo isn't moved/deleted |

**IMPORTANT** failures (port-holding processes, low disk) are usually
trivial fixes -- kill the PID, free disk -- but they will block child
spawn so resolve before launching.

When `SUMMARY: ... READY to launch shadow mode.` appears, proceed to A1.1.

---

## 3. Phase A1.1 -- Poller-only shadow (3-7 days)

**Goal**: verify mn ingests the same filings brain does, without any
contention over Ollama.

**Configuration**:

- **brain**: full production unchanged (poller, summarizer, Flask UI).
- **mn**: poller only, no worker, no UI. Ingests to its own DB without
  driving Ollama.

**Launch**:

```powershell
# Terminal 1 (mn supervisor, poller only):
python G:/market_notification/scripts/run_all.py --skip worker --skip ui

# Terminal 2 (mn watcher; alerts on poller errors only):
python G:/market_notification/scripts/watch_health.py --skip worker --skip ui
```

**Verification commands** (run daily):

```powershell
# Compare row counts between brain and mn (last 24h)
sqlite3 "G:/brain/data/notifications.db" "SELECT COUNT(*) FROM notifications WHERE fetched_at >= datetime('now','-1 day')"
sqlite3 "G:/market_notification/data/notifications.db" "SELECT COUNT(*) FROM notifications WHERE fetched_at >= datetime('now','-1 day')"

# Spot-check 5 random filings -- they should appear in both DBs
sqlite3 "G:/brain/data/notifications.db" "SELECT id, source, headline FROM notifications WHERE fetched_at >= datetime('now','-1 hour') ORDER BY RANDOM() LIMIT 5"
```

**Go gate to A1.2** (after >= 3 consecutive days):

- [ ] mn 24h ingest count is within +/- 1% of brain's
- [ ] No `poller_*_dead` rows in mn (`SELECT COUNT(*) FROM notifications WHERE pipeline_status LIKE '%_dead'`)
- [ ] mn watcher emitted < 5 DEGRADED transitions in the period
- [ ] Disk free on mn drive > 10 GB

If any **NO**: stay in A1.1 longer, investigate the divergence, then re-evaluate.

---

## 4. Phase A1.2 -- Worker swap (7-14 days)

**Goal**: mn drives the full pipeline (classify -> priority -> attachment ->
summarize -> deep-dive). Brain still ingests, but its summarizer is off.

### 4.1 Pre-step: stop brain's summarizer

Edit `G:\brain\app.py` and locate the `NotificationSummarizer(interval=...)`
startup call. Comment it out and any associated thread start. Restart
brain Flask (`python G:/brain/app.py`).

Verify brain summarizer is off:

```powershell
# brain's ai_summarized_at should stop advancing
sqlite3 "G:/brain/data/notifications.db" "SELECT MAX(ai_summarized_at) FROM notifications"
# Run twice over a 5-minute gap; the timestamp should NOT change.
```

### 4.2 Launch mn full stack

```powershell
# Terminal 1: full mn supervisor
python G:/market_notification/scripts/run_all.py

# Terminal 2: watcher
python G:/market_notification/scripts/watch_health.py
#   or with Discord alerts:
#   $env:MN_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."; python G:/market_notification/scripts/watch_health.py
```

### 4.3 Daily checks (during A1.2)

| Metric | How | Healthy |
|---|---|---|
| Health probes | `curl http://127.0.0.1:8504/health \| jq '.ok'` (and 8505) | `true` |
| Pending queue | `curl http://127.0.0.1:8505/health \| jq '.pending_total'` | trending down or stable |
| Dead lane | `curl http://127.0.0.1:8505/health \| jq '.failed_or_dead'` | `{}` (empty) |
| Throughput.done | `curl http://127.0.0.1:8505/health \| jq '.throughput_24h.done'` | growing daily |
| Cache hit rate | `sqlite3 ... "SELECT AVG(CASE WHEN gemini_cache_hit THEN 1.0 ELSE 0 END) FROM notifications WHERE gemini_dive_at >= datetime('now','-1 day')"` | >= 0.8 once warmed |
| Gemma throughput | check `D:\gemma-retrieval\docs\GEMMA4_MOE_REFERENCE.md` § 18.10 for tok/s probe | >= 140 tok/s |
| Brain idle | `sqlite3 G:/brain/data/notifications.db "SELECT MAX(ai_summarized_at) FROM notifications"` | unchanged from yesterday |

### 4.4 Analyst-quality sample (run twice a week)

Pick 20 `important` notifications from the last 24h in mn's UI. For each:

- Is the gemma_summary factually accurate vs the PDF body?
- Are gemma_key_figures preserved verbatim (no rounding)?
- For eligible categories: did the gemini-rr deep-dive run? Cache hit or fresh?
- Compare to brain's `ai_summary` on the same row -- mn should be at least as good.

Maintain a `verification/cutover_analyst_log.md` with verdicts.

### 4.5 Go gate to A1.3 (after >= 7 consecutive days)

- [ ] Health probes returned `ok=true` on >= 99% of polls
- [ ] Zero rows reached any `*_dead` state in last 72h
- [ ] At least 50 deep-dives completed with `parse_errors=[]`
- [ ] Cache hit rate >= 80% over the period
- [ ] Analyst-quality sample (>= 20 rows) passes manual review
- [ ] No Ollama spill / restart events in the period

If any **NO**: stay in A1.2, investigate, re-evaluate. **Rollback is still cheap here**: re-enable brain's summarizer thread and stop mn worker (`run_all.py --skip worker`).

---

## 5. Phase A1.3 -- Brain poller stop (cutover gate)

**Goal**: mn is the sole ingest path. Brain notifications.db is read-only
historical from this point on.

### 5.1 Pre-step

Edit `G:\brain\app.py` and comment out the `NotificationPoller(interval=...)`
auto-start (and the `start()` call). Restart brain Flask.

Verify:

```powershell
# brain's max id should stop advancing
sqlite3 "G:/brain/data/notifications.db" "SELECT MAX(id) FROM notifications"
# Run twice over 5 minutes; should NOT change.
```

### 5.2 Update mn config (optional)

If you've been running mn with brain available as a backfill / dedup
reference, no config change is needed -- the read-only access continues.
mn's poller continues with its own watermarks in
`notification_poll_state`.

### 5.3 Lock-down (recommended)

```powershell
# Make brain's notifications.db read-only at the filesystem level.
# This catches any accidental writes from re-enabled daemons.
attrib +R "G:\brain\data\notifications.db"
attrib +R "G:\brain\data\notifications.db-wal"   # if exists
attrib +R "G:\brain\data\notifications.db-shm"   # if exists
```

(Reversible with `attrib -R ...` -- not destructive.)

### 5.4 Daily checks (during A1.3)

Same as § 4.3 plus:

- Brain mtime should not change. The preflight `brain DB mtime (24h)` line
  will start saying WARN ("last modified Nh ago" with N growing) -- that
  WARN is now expected and good.

### 5.5 Go gate to A1.4 (after >= 14 consecutive days)

- [ ] All § 4.5 gates still passing
- [ ] Brain DB mtime unchanged for 14 days
- [ ] mn `pipeline_journal` has >= 100k transitions logged in the period
- [ ] No support tickets / analyst escalations about missing notifications

---

## 6. Phase A1.4 -- Brain UI decommission

**Goal**: brain Flask either stops serving notification routes or freezes
them at a 503 with a pointer to mn's UI.

### 6.1 Option A -- Hard cut

Remove `notifications_bp` from brain's `app.py` `register_blueprint(...)`
calls. The notification API endpoints return 404. The legacy
`NotificationCenter.jsx` page won't load data.

### 6.2 Option B -- Soft redirect

Keep `notifications_bp` registered but inject middleware that returns
`503 Service Unavailable` with body
`{ "moved_to": "http://127.0.0.1:8501", "via": "market_notification" }`
on every route under `/api/notifications/*`.

Pick whichever feels less invasive for the rest of brain's Flask app.

### 6.3 Archive brain notifications.db

```powershell
# After lockdown, snapshot to S3 (or wherever the master backup lives)
aws s3 cp "G:\brain\data\notifications.db" "s3://zenflow-data/archives/brain-notifications-$(Get-Date -Format yyyyMMdd).db"
```

The file stays on disk locally (read-only) for any one-off historical
queries.

---

## 7. Bringing the supervisor under Windows Task Scheduler (optional)

After A1.2 or A1.3 is stable, you can make `run_all.py` and
`watch_health.py` start at boot:

```powershell
# Register the supervisor (run as current user, on logon, restart on failure)
$action = New-ScheduledTaskAction -Execute "python.exe" `
  -Argument "G:\market_notification\scripts\run_all.py"
$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet `
  -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable
Register-ScheduledTask -TaskName "mn_supervisor" `
  -Action $action -Trigger $trigger -Settings $settings

# And the watcher (separate task)
$action = New-ScheduledTaskAction -Execute "python.exe" `
  -Argument "G:\market_notification\scripts\watch_health.py --discord-webhook https://..."
Register-ScheduledTask -TaskName "mn_health_watcher" `
  -Action $action -Trigger $trigger -Settings $settings
```

Verify with `Get-ScheduledTask -TaskName mn_*`. Test by logging out and back in.

---

## 8. Daily operator checklist

After A1.2 begins, this is the 5-minute daily ritual:

1. `python G:/market_notification/scripts/_shadow_preflight.py` -- read SUMMARY
2. Check mn Streamlit Health tab at `http://127.0.0.1:8501` -- read queue depths
3. Open Discord channel (or stdout of watch_health + push_important) -- review the day's alerts
4. `sqlite3 "G:/market_notification/data/notifications.db" "SELECT pipeline_status, COUNT(*) FROM notifications WHERE last_status_change_at >= datetime('now','-1 day') GROUP BY pipeline_status"` -- one-line health
5. Read overnight log digest: `logs/rollup-YYYY-MM-DD.md` (generated by B4 -- see § 8.2)
6. If everything is UP and queues are draining: done.

### 8.1 B3 -- Daily DB snapshot

```powershell
# WAL-checkpointed atomic snapshot, gzipped, last 14 retained locally
python G:/market_notification/scripts/backup_db.py

# With S3 retention (requires aws CLI + credentials)
python G:/market_notification/scripts/backup_db.py --s3-bucket zenflow-data \
    --s3-prefix backups/mn-notifications
```

Hook to Windows Task Scheduler for daily at 02:00 IST (after the BSE/NSE trading day).

### 8.2 B4 -- Daily log digest

```powershell
# Default: last 24h, writes logs/rollup-YYYY-MM-DD.md
python G:/market_notification/scripts/logs_rollup.py

# Custom window
python G:/market_notification/scripts/logs_rollup.py --hours 6

# Print to stdout (no file)
python G:/market_notification/scripts/logs_rollup.py --stdout
```

The digest groups similar errors into canonical patterns (`id=123` and
`id=456` fold to `id=<N>`), surfaces top-N error patterns + top-N
warnings, and includes the last 5 raw ERROR records per process for
forensic context.

### 8.3 B5 -- Push-on-important (Discord)

```powershell
# Long-running daemon; posts on each newly-completed important deep-dive
$env:MN_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/..."
python G:/market_notification/scripts/push_important.py

# Dry run (logs what it would push; no HTTP, no state advance)
python G:/market_notification/scripts/push_important.py --dry-run

# One-shot drain (cron-style)
python G:/market_notification/scripts/push_important.py --once
```

State (last_pushed_id) is persisted to `data/push_state.json` so the
pointer survives restarts. Posts use Discord embeds with the company
name, headline, ai_category, gemma_impact one-liner, and the cache-hit
status of the gemini-rr deep-dive.

---

## 9. Stop criteria (when to abort and roll back)

Any of these triggers a rollback:

| Trigger | Severity |
|---|---|
| Dead-lane count > 0 for any stage, > 24h sustained | high |
| Supervisor consecutive_failures > 5 on any child without recovery within 30 min | high |
| Gemma throughput < 100 tok/s for > 2h | medium (Ollama restart usually fixes) |
| SLA breach count > 50/day for 2 days in a row | medium |
| Analyst escalation: "I'm missing notifications I would have seen in brain" | **show-stopper** |
| Disk free < 5 GB on G: drive | **show-stopper** |

---

## 10. Rollback procedure (during A1.1 or A1.2)

Reversal is cheap up to and including A1.2:

1. Stop mn supervisor: Ctrl+C in its terminal. Wait for clean exit (<= 10 s).
2. Stop mn watcher: Ctrl+C in its terminal.
3. **In brain `app.py`**: re-enable the `NotificationSummarizer` thread (revert the comment from § 4.1). Restart brain Flask.
4. Verify brain summarizer is back: `MAX(ai_summarized_at)` advances within 2 min.
5. Brain UI at `localhost:5000/notifications` is the analyst surface again.
6. mn's DB keeps the rows it ingested; it isn't deleted -- it just stops growing.

For rollback **after** A1.3 (brain poller stopped): see § 11.

---

## 11. Late-stage rollback (after A1.3)

This is harder because brain hasn't been ingesting. If you've been in A1.3
for **less than 7 days**:

1. `attrib -R "G:\brain\data\notifications.db*"` -- unlock the files
2. Edit `G:\brain\app.py` to re-enable both poller and summarizer; restart Flask
3. Brain will resume ingesting. Backfill the missing days via brain's
   `backfill_nse_api` / `backfill_bse_api` endpoints.

If you've been in A1.3 for **more than 7 days**:

- Brain's symbol_map / filter_rules may have drifted; the simpler path is
  to import the missing days from mn's DB back to brain via a one-off
  script. There is no tested tool for this; expect 1-2 days of cleanup.
- *This is why the A1.3 gate is conservative (14 days of A1.2 first).*

---

## 12. Verification commands cheat sheet

```powershell
# State discovery -- always run first
python G:/market_notification/scripts/_shadow_preflight.py

# Quick health pulse on the worker
curl http://127.0.0.1:8505/health | python -c "import json,sys; d=json.load(sys.stdin); print('ok=',d['ok'],'pending=',d['pending_total'],'dead=',d['failed_or_dead'])"

# Pending queue depth right now
sqlite3 "G:/market_notification/data/notifications.db" "SELECT pipeline_status, COUNT(*) FROM notifications GROUP BY pipeline_status ORDER BY 2 DESC LIMIT 10"

# Recent deep-dives (mn worker quality signal)
sqlite3 "G:/market_notification/data/notifications.db" "SELECT id, ai_category, gemini_cache_hit, gemini_latency_s FROM notifications WHERE gemini_dive_at >= datetime('now','-1 day') ORDER BY gemini_dive_at DESC LIMIT 20"

# Cross-system delta during A1.1 -- brain vs mn ingest 24h
sqlite3 "G:/brain/data/notifications.db" "SELECT COUNT(*) FROM notifications WHERE fetched_at >= datetime('now','-1 day')"
sqlite3 "G:/market_notification/data/notifications.db" "SELECT COUNT(*) FROM notifications WHERE fetched_at >= datetime('now','-1 day')"

# Pipeline journal latest 10 transitions (forensic)
sqlite3 "G:/market_notification/data/notifications.db" "SELECT at, actor, from_status, to_status, duration_ms FROM pipeline_journal ORDER BY at DESC LIMIT 10"
```

---

## 13. References

- `G:\brain\docs\memory\notifications\architecture.md` § 0 -- migration map + new-system mechanics
- `PROJECT_OVERVIEW.md` § 16.5 -- invocation patterns for the supervisor + watcher
- `scripts/run_all.py` -- B1 supervisor (this playbook's launcher)
- `scripts/watch_health.py` -- B2 health watcher (this playbook's alerter)
- `scripts/_shadow_preflight.py` -- pre-launch state-discovery script
- `D:\gemma-retrieval\docs\GEMMA4_MOE_REFERENCE.md` -- Ollama Gemma anti-spill operations
- `verification/phase_13_results/` -- prior soak verification artifacts
