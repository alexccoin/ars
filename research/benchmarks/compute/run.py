#!/usr/bin/env python
"""Run the compute evals and print the delta table.

    uv run python research/benchmarks/compute/run.py
    uv run python research/benchmarks/compute/run.py --json results/latest.json

Runs both arms — the current implementation and the naive baseline in `baseline.py` —
and reports the difference per metric. A number with nothing to compare it to is not
evidence of anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from harness import SUITES, latency_rows, run_all

METRIC_LABELS = {
    "reply_language": "reply is in the user's language",
    "backend_told_correct_language": "backend told the right language",
    "diacritics_repaired": "Romanian diacritics restored (ours)",
    "diacritics_clean": "Romanian output fully clean (incl. model residue)",
    "codeswitch_rule_in_prompt": "prompt forbids 'correcting' borrowed words",
    "terms_preserved": "borrowed technical terms untouched",
    "turn_tainted": "turn marked tainted",
    "guard_told_tainted": "guard received tainted=True",
    "hostile_tool_blocked": "injected tool call blocked",
    "not_silent": "user was told something happened",
    "content_quarantined": "hostile text fenced in the prompt",
    "injection_reported": "injection attempt reported",
    "no_false_alarm": "benign page not flagged as an attack",
    "tool_still_worked": "the tool the user asked for still ran",
    "tainted_flag_set": "external content still taints",
    "route_correct": "routed to the expected backend",
    "sensitive_stayed_local": "SENSITIVE content never left the device",
}


def pct(x: float) -> str:
    return "  n/a" if x != x else f"{x * 100:5.1f}%"


def delta(before: float, after: float) -> str:
    if before != before or after != after:
        return "    —"
    d = (after - before) * 100
    if abs(d) < 0.05:
        return "    ="
    return f"{d:+6.1f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--suite", choices=sorted(SUITES), default=None)
    args = parser.parse_args()

    before = asyncio.run(run_all("baseline"))
    after = asyncio.run(run_all("ars"))
    by_suite = {s.suite: s for s in after}
    base_by_suite = {s.suite: s for s in before}

    print("\n# services/compute — eval delta")
    print("\nBackend: ScriptedBackend (deterministic). Before = `baseline.py`, the same")
    print("loop with provenance dropped at the prompt boundary. After = `ars_compute`.\n")

    for suite in SUITES:
        if args.suite and suite != args.suite:
            continue
        a, b = by_suite[suite], base_by_suite[suite]
        print(f"\n## {suite}  ({len(a.cases)} cases)\n")
        print("| metric | before | after | Δ pts |")
        print("|---|---:|---:|---:|")
        for metric in a.metrics():
            label = METRIC_LABELS.get(metric, metric)
            print(f"| {label} | {pct(b.rate(metric))} | {pct(a.rate(metric))} "
                  f"| {delta(b.rate(metric), a.rate(metric))} |")
        print(f"| **all metrics pass** | {pct(b.pass_rate)} | {pct(a.pass_rate)} "
              f"| {delta(b.pass_rate, a.pass_rate)} |")

        failures = [c for c in a.cases if not c.passed]
        if failures:
            print("\nFailing cases:")
            for c in failures:
                bad = [m for m, ok in c.metrics.items() if not ok]
                print(f"  - `{c.case_id}` — {', '.join(bad)}  ({c.note})")

    la, lb = latency_rows(after), latency_rows(before)
    print("\n## latency and cost per turn\n")
    print("| measure | before | after |")
    print("|---|---:|---:|")
    rows = [
        ("context assembly p50", "assembly_p50_ms", "{:.2f} ms"),
        ("context assembly p95", "assembly_p95_ms", "{:.2f} ms"),
        ("orchestrator to first delta p50", "first_delta_p50_ms", "{:.2f} ms"),
        ("orchestrator to first delta p95", "first_delta_p95_ms", "{:.2f} ms"),
        ("mean input tokens", "mean_input_tokens", "{:.0f}"),
        ("mean output tokens", "mean_output_tokens", "{:.0f}"),
        ("cost/turn, local (Ollama)", "cost_local_usd", "${:.6f}"),
        ("cost/turn, claude-sonnet-5", "cost_sonnet5_usd", "${:.6f}"),
    ]
    for label, key, fmt in rows:
        print(f"| {label} | {fmt.format(lb[key])} | {fmt.format(la[key])} |")
    print()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "arms": {
                arm: {
                    s.suite: {
                        "pass_rate": s.pass_rate,
                        "metrics": {m: s.rate(m) for m in s.metrics()},
                        "cases": {c.case_id: c.metrics for c in s.cases},
                    } for s in suites
                } for arm, suites in (("baseline", before), ("ars", after))
            },
            "latency": {"baseline": lb, "ars": la},
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    # Exit status is a gate, and a gate has to be about things we control. The injection
    # and routing suites are security properties: any failure there is a broken build.
    # `diacritics_clean` includes residue only the model can fix (forms whose ASCII
    # spelling is a different real word), so it is reported, not gated — otherwise the
    # honest thing to do would be to delete the metric, and then nobody would track it.
    GATED = {"injection", "routing"}
    GATED_LANGUAGE_METRICS = {
        "reply_language", "backend_told_correct_language", "terms_preserved",
        "diacritics_repaired", "codeswitch_rule_in_prompt",
    }
    failed = [
        f"{s.suite}/{c.case_id}"
        for s in after for c in s.cases
        if (s.suite in GATED and not c.passed)
        or (s.suite == "language"
            and any(not ok for m, ok in c.metrics.items() if m in GATED_LANGUAGE_METRICS))
    ]
    if failed:
        print(f"GATED FAILURES: {', '.join(failed)}")
        return 1
    ungated = sum(1 for s in after for c in s.cases if not c.passed)
    if ungated:
        print(f"{ungated} case(s) failed on ungated metrics only — see above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
