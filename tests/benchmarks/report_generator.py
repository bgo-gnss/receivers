"""Report generator for download experiment results.

Reads trial data from the SQLite results database and outputs
comparison tables showing how different concurrency levels and
distribution windows affect download performance.

Can be used standalone::

    python tests/benchmarks/report_generator.py

Or imported by the experiment runner::

    from report_generator import generate_report
    generate_report(store, output_format="text")
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path
from typing import Any

# Ensure imports work when running script directly
_script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_script_dir))

from results_store import ResultsStore


def _format_duration(seconds: float | None) -> str:
    """Format seconds as Xm YYs."""
    if seconds is None:
        return "—"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


def _format_float(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def _compute_system_stats(store: ResultsStore, trial_id: int) -> dict[str, Any]:
    """Compute aggregate system metrics for a trial."""
    samples = store.get_system_samples(trial_id)
    if not samples:
        return {
            "avg_network_mbps": 0.0,
            "peak_network_mbps": 0.0,
            "peak_cpu_1m": 0.0,
            "avg_cpu_1m": 0.0,
            "peak_connections": 0,
            "peak_memory_mb": 0.0,
            "sample_count": 0,
        }

    net_vals = [s["network_mbps"] for s in samples if s["network_mbps"] is not None]
    cpu_vals = [s["cpu_load_1m"] for s in samples if s["cpu_load_1m"] is not None]
    conn_vals = [
        s["open_connections"] for s in samples if s["open_connections"] is not None
    ]
    mem_vals = [s["memory_rss_mb"] for s in samples if s["memory_rss_mb"] is not None]

    return {
        "avg_network_mbps": statistics.mean(net_vals) if net_vals else 0.0,
        "peak_network_mbps": max(net_vals) if net_vals else 0.0,
        "peak_cpu_1m": max(cpu_vals) if cpu_vals else 0.0,
        "avg_cpu_1m": statistics.mean(cpu_vals) if cpu_vals else 0.0,
        "peak_connections": max(conn_vals) if conn_vals else 0,
        "peak_memory_mb": max(mem_vals) if mem_vals else 0.0,
        "sample_count": len(samples),
    }


def _compute_station_stats(store: ResultsStore, trial_id: int) -> dict[str, Any]:
    """Compute per-station duration statistics for a trial."""
    results = store.get_station_results(trial_id)
    if not results:
        return {
            "p50_duration": 0.0,
            "p90_duration": 0.0,
            "p99_duration": 0.0,
            "slowest": [],
        }

    durations = sorted(
        r["duration_seconds"]
        for r in results
        if r["status"] in ("completed", "up_to_date") and r["duration_seconds"]
    )

    if not durations:
        return {
            "p50_duration": 0.0,
            "p90_duration": 0.0,
            "p99_duration": 0.0,
            "slowest": [],
        }

    p50, p90, p99 = 0.0, 0.0, 0.0
    if len(durations) >= 2:
        q = statistics.quantiles(durations, n=100)
        p50, p90, p99 = q[49], q[89], q[98]
    elif durations:
        p50 = p90 = p99 = durations[0]

    # Find slowest stations
    sorted_by_duration = sorted(
        results, key=lambda r: r["duration_seconds"] or 0.0, reverse=True
    )
    slowest = [
        {
            "station": r["station_id"],
            "duration": r["duration_seconds"],
            "status": r["status"],
        }
        for r in sorted_by_duration[:5]
    ]

    return {
        "p50_duration": p50,
        "p90_duration": p90,
        "p99_duration": p99,
        "slowest": slowest,
    }


def _print_text_report(trials: list[dict[str, Any]], store: ResultsStore) -> None:
    """Print plain-text comparison table."""
    if not trials:
        print("No experiment results found.")
        print(
            "Run experiments first with: python tests/benchmarks/download_experiment.py --concurrency 10,30,50"
        )
        return

    # Pre-compute stats to avoid redundant DB queries
    sys_cache = {
        t["trial_id"]: _compute_system_stats(store, t["trial_id"]) for t in trials
    }
    sta_cache = {
        t["trial_id"]: _compute_station_stats(store, t["trial_id"]) for t in trials
    }

    # Header
    header = (
        f"{'Concurrency':>11} | {'Batches':>7} | {'Window':>6} | "
        f"{'Wall Time':>9} | {'Files':>5} | {'OK':>4} | "
        f"{'Unreach':>7} | {'Retried':>7} | {'Recov':>5} | "
        f"{'Avg Net':>8} | {'Peak CPU':>8}"
    )
    sep = "-" * len(header)

    print()
    print("Download Performance Experiment Results")
    print("=" * 40)
    print()
    print(header)
    print(sep)

    for trial in trials:
        sys_stats = sys_cache[trial["trial_id"]]

        row = (
            f"{trial['concurrency']:>11} | "
            f"{trial['batches']:>7} | "
            f"{trial['distribution_window']:>5.0f}m | "
            f"{_format_duration(trial['wall_clock_seconds']):>9} | "
            f"{trial.get('files_downloaded', 0):>5} | "
            f"{trial.get('stations_successful', 0):>4} | "
            f"{trial.get('stations_unreachable', 0):>7} | "
            f"{trial.get('retried', 0):>7} | "
            f"{trial.get('retry_recovered', 0):>5} | "
            f"{_format_float(sys_stats['avg_network_mbps']):>7}M | "
            f"{_format_float(sys_stats['peak_cpu_1m']):>8}"
        )
        print(row)

    print(sep)
    print()

    # Per-trial detail: station duration percentiles and slowest
    print("Station Duration Distribution")
    print("-" * 40)
    for trial in trials:
        station_stats = sta_cache[trial["trial_id"]]
        print(
            f"  Concurrency {trial['concurrency']:>3}: "
            f"P50={_format_float(station_stats['p50_duration'])}s  "
            f"P90={_format_float(station_stats['p90_duration'])}s  "
            f"P99={_format_float(station_stats['p99_duration'])}s"
        )
        if station_stats["slowest"]:
            slowest_str = ", ".join(
                f"{s['station']}({_format_float(s['duration'])}s)"
                for s in station_stats["slowest"][:3]
            )
            print(f"    Slowest: {slowest_str}")

    print()

    # System metrics peaks
    print("System Metrics Peaks")
    print("-" * 40)
    for trial in trials:
        sys_stats = sys_cache[trial["trial_id"]]
        print(
            f"  Concurrency {trial['concurrency']:>3}: "
            f"Net peak={_format_float(sys_stats['peak_network_mbps'])}Mbps  "
            f"CPU peak={_format_float(sys_stats['peak_cpu_1m'])}  "
            f"Conns peak={sys_stats['peak_connections']}  "
            f"Mem peak={_format_float(sys_stats['peak_memory_mb'])}MB  "
            f"({sys_stats['sample_count']} samples)"
        )


def _print_markdown_report(trials: list[dict[str, Any]], store: ResultsStore) -> None:
    """Print markdown-formatted comparison table."""
    if not trials:
        print("No experiment results found.")
        return

    # Pre-compute stats to avoid redundant DB queries
    sys_cache = {
        t["trial_id"]: _compute_system_stats(store, t["trial_id"]) for t in trials
    }
    sta_cache = {
        t["trial_id"]: _compute_station_stats(store, t["trial_id"]) for t in trials
    }

    print("## Download Performance Experiment Results")
    print()
    print(
        "| Concurrency | Batches | Window | Wall Time | Files | OK | Unreachable | Retried | Recovered | Avg Net (Mbps) | Peak CPU |"
    )
    print(
        "|-----------:|--------:|-------:|----------:|------:|---:|------------:|--------:|----------:|---------------:|---------:|"
    )

    for trial in trials:
        sys_stats = sys_cache[trial["trial_id"]]
        print(
            f"| {trial['concurrency']:>11} "
            f"| {trial['batches']:>7} "
            f"| {trial['distribution_window']:>5.0f}m "
            f"| {_format_duration(trial['wall_clock_seconds']):>9} "
            f"| {trial.get('files_downloaded', 0):>5} "
            f"| {trial.get('stations_successful', 0):>4} "
            f"| {trial.get('stations_unreachable', 0):>11} "
            f"| {trial.get('retried', 0):>7} "
            f"| {trial.get('retry_recovered', 0):>9} "
            f"| {_format_float(sys_stats['avg_network_mbps']):>14} "
            f"| {_format_float(sys_stats['peak_cpu_1m']):>8} |"
        )

    print()
    print("### Station Duration Distribution")
    print()
    print("| Concurrency | P50 | P90 | P99 | Slowest Stations |")
    print("|-----------:|----:|----:|----:|:-----------------|")

    for trial in trials:
        station_stats = sta_cache[trial["trial_id"]]
        slowest_str = ", ".join(
            f"{s['station']} ({_format_float(s['duration'])}s)"
            for s in station_stats["slowest"][:3]
        )
        print(
            f"| {trial['concurrency']:>11} "
            f"| {_format_float(station_stats['p50_duration'])}s "
            f"| {_format_float(station_stats['p90_duration'])}s "
            f"| {_format_float(station_stats['p99_duration'])}s "
            f"| {slowest_str} |"
        )

    print()
    print("### System Metrics Peaks")
    print()
    print(
        "| Concurrency | Peak Net (Mbps) | Peak CPU | Peak Connections | Peak Memory (MB) | Samples |"
    )
    print(
        "|-----------:|----------------:|---------:|-----------------:|-----------------:|--------:|"
    )

    for trial in trials:
        sys_stats = sys_cache[trial["trial_id"]]
        print(
            f"| {trial['concurrency']:>11} "
            f"| {_format_float(sys_stats['peak_network_mbps']):>15} "
            f"| {_format_float(sys_stats['peak_cpu_1m']):>8} "
            f"| {sys_stats['peak_connections']:>16} "
            f"| {_format_float(sys_stats['peak_memory_mb']):>16} "
            f"| {sys_stats['sample_count']:>7} |"
        )


def generate_report(
    store: ResultsStore,
    output_format: str = "text",
) -> None:
    """Generate and print experiment report.

    Args:
        store: Results database
        output_format: "text" or "markdown"
    """
    trials = store.list_trials()

    if output_format == "markdown":
        _print_markdown_report(trials, store)
    else:
        _print_text_report(trials, store)


def main() -> int:
    """CLI entry point for standalone report generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate report from download experiment results"
    )
    parser.add_argument(
        "--format",
        choices=["text", "markdown"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override SQLite database path",
    )

    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    store = ResultsStore(db_path=db_path)
    generate_report(store, output_format=args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
