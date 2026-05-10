# Verification artifacts

One subdirectory per phase: `phase_<N>_results/`.

Each phase's verification protocol is in `docs/VERIFICATION.md`. Drop
generated artifacts (logs, CSV exports, screenshots, benchmark output)
into the corresponding subdirectory as evidence of phase completion.

| Phase | Subdir | Required artifacts |
|---|---|---|
| 0 | `phase_0_results/` | db_schema.txt, pytest_output.txt, log_excerpt.txt |
| 1 | `phase_1_results/` | lookup_results.csv, coverage_report.md |
| 2 | `phase_2_results/` | sample_bse_response.json, sample_nse_response.json, normalized_sample.json |
| 3 | `phase_3_results/` | poller_log_10min.txt, dedup_proof.sql_output.txt, cross_exchange_groups.csv |
| 4 | `phase_4_results/` | flagged_sample_20.csv, passed_sample_20.csv, perf_benchmark.txt |
| 5 | `phase_5_results/` | classification_accuracy.csv, sla_alert_log.txt, prompt_v1_snapshot.txt |
| 6 | `phase_6_results/` | priority_distribution.csv, llm_override_examples.csv |
| 7 | `phase_7_results/` | pdf_coverage.csv, vision_sample_summaries.md |
| 8 | `phase_8_results/` | sample_summaries.md, latency_histogram.png |
| 9 | `phase_9_results/` | import_audit.csv, sample_compare.csv |
| 10 | `phase_10_results/` | dispatcher_run_log.txt, order_proof.csv |
| 11 | `phase_11_results/` | deep_dive_samples/, quota_audit.txt |
| 12 | `phase_12_results/` | screenshots/, ui_walkthrough_notes.md |
| 13 | `phase_13_results/` | soak_24h_summary.md, regression_pytest.txt, log_summary.md |

These directories are gitignored (see `.gitignore`); they're for local
audit, not the repo.
