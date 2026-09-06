"""Unattended calibration runner for the adaptive strategy.

Runs the simulation many times with different weights, several seeds per
configuration, and keeps appending the results to a CSV. It is meant to be
started and left alone for hours: it stops on its own when the time budget
expires, it survives being interrupted (results already written stay), and
restarting it continues the same CSV.

Usage (from the project folder, with the virtual environment active):

    python run_batch.py --hours 6
    python run_batch.py --hours 6 --workers 4 --days 180
    python run_batch.py --baseline-only

Results land in Output/Batch/results.csv, one row per configuration, sorted by
the objective with `python run_batch.py --report`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import multiprocessing as mp
import os
import random
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RESULTS_DIRECTORY = PROJECT_ROOT / "Output" / "Batch"
RESULTS_FILE = RESULTS_DIRECTORY / "results.csv"

# Calibration space. Every name matches an environment variable read by
# response_strategies/adaptive_strategy.py.
SEARCH_SPACE = {
    "WSC_TRANSFER_BUFFER_HOURS": (0.0, 48.0),
    "WSC_WAIT_WEIGHT": (0.3, 1.5),
    "WSC_QUEUE_WEIGHT": (0.0, 3.0),
    "WSC_CLOSURE_SLACK_DAYS": (0.0, 5.0),
    "WSC_MAX_TRANSFERS": (1, 3),
}

PARAMETER_NAMES = sorted(SEARCH_SPACE)

RESULT_COLUMNS = (
    ["trial", "label"]
    + PARAMETER_NAMES
    + ["seeds", "days", "att_mean", "att_stdev", "att_worst", "objective", "seconds"]
)


def run_one_simulation(job):
    """Run a single simulation. Executed inside a worker process."""
    parameters, seed, days, warm_up_days = job

    for name, value in parameters.items():
        os.environ[name] = str(value)
    os.environ["WSC_STRATEGY_ENABLED"] = os.environ.get("WSC_STRATEGY_ENABLED", "1")

    # Imported inside the worker so each process builds its own scenario and so
    # the environment variables above are already in place.
    import scenario_builders
    from simulation_model import Model

    context = scenario_builders.create_with_disruption()
    sim = Model(context, seed=seed)
    sim.warmup(period=dt.timedelta(days=warm_up_days))

    period_values = []
    period_start_time = sim.clock_time
    interval = 5
    for day in range(1, days + 1):
        sim.run(duration=dt.timedelta(days=1))
        if day % interval and day != days:
            continue
        period_values.append(
            sim.get_teu_weighted_average_transport_time_hours(
                period_start_time, sim.clock_time
            )
            / 24.0
        )
        period_start_time = sim.clock_time

    if not period_values:
        return float("nan")
    return sum(period_values) / len(period_values)


def sample_parameters(rng):
    sampled = {}
    for name, bounds in SEARCH_SPACE.items():
        low, high = bounds
        if isinstance(low, int) and isinstance(high, int):
            sampled[name] = rng.randint(low, high)
        else:
            sampled[name] = round(rng.uniform(low, high), 3)
    return sampled


def default_parameters():
    return {
        "WSC_TRANSFER_BUFFER_HOURS": 12.0,
        "WSC_WAIT_WEIGHT": 1.0,
        "WSC_QUEUE_WEIGHT": 1.0,
        "WSC_CLOSURE_SLACK_DAYS": 1.0,
        "WSC_MAX_TRANSFERS": 3,
    }


def load_completed_trials():
    if not RESULTS_FILE.is_file():
        return 0
    with RESULTS_FILE.open(newline="", encoding="utf-8") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def append_result(row):
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    is_new = not RESULTS_FILE.is_file()
    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def evaluate(pool, parameters, seeds, days, warm_up_days):
    jobs = [(parameters, seed, days, warm_up_days) for seed in seeds]
    if pool is None:
        values = [run_one_simulation(job) for job in jobs]
    else:
        values = pool.map(run_one_simulation, jobs)
    return values


def report():
    if not RESULTS_FILE.is_file():
        print("No results yet. Run the batch first.")
        return
    with RESULTS_FILE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("objective") not in (None, "", "nan")]
    rows.sort(key=lambda row: float(row["objective"]))

    print(f"{len(rows)} configurations evaluated. Best first:\n")
    header = ["objective", "att_mean", "att_stdev", "att_worst"] + PARAMETER_NAMES
    print("  ".join(f"{name:>26}" if name in PARAMETER_NAMES else f"{name:>10}"
                    for name in header))
    for row in rows[:15]:
        cells = []
        for name in header:
            width = 26 if name in PARAMETER_NAMES else 10
            cells.append(f"{row[name]:>{width}}")
        print("  ".join(cells))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=6.0,
                        help="Wall-clock budget. The runner stops after this.")
    parser.add_argument("--workers", type=int, default=0,
                        help="Parallel simulations. 0 picks CPU count minus one.")
    parser.add_argument("--seeds", type=int, nargs="+", default=[2026, 2027, 2028],
                        help="Seeds evaluated per configuration.")
    parser.add_argument("--days", type=int, default=0,
                        help="Measured days per run. 0 uses the configured value.")
    parser.add_argument("--warmup", type=int, default=0,
                        help="Warm-up days. 0 uses the configured value.")
    parser.add_argument("--trials", type=int, default=0,
                        help="Stop after this many configurations. 0 means no limit.")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Evaluate the default weights once and exit.")
    parser.add_argument("--report", action="store_true",
                        help="Print the ranking of the results already collected.")
    arguments = parser.parse_args()

    if arguments.report:
        report()
        return

    from config.simulation_config import SIMULATION_DAYS, WARM_UP_DAYS

    days = arguments.days or SIMULATION_DAYS
    warm_up_days = arguments.warmup or WARM_UP_DAYS
    workers = arguments.workers or max(1, (os.cpu_count() or 2) - 1)
    workers = min(workers, len(arguments.seeds))

    deadline = time.monotonic() + arguments.hours * 3600.0
    trial = load_completed_trials()
    rng = random.Random(20260904 + trial)

    print(f"Batch runner starting at {dt.datetime.now():%Y-%m-%d %H:%M}")
    print(f"  budget       : {arguments.hours} h")
    print(f"  seeds        : {arguments.seeds}")
    print(f"  days per run : {warm_up_days} warm-up + {days} measured")
    print(f"  workers      : {workers}")
    print(f"  results      : {RESULTS_FILE}")
    print(f"  resuming from trial {trial}")
    print()

    pool = mp.Pool(processes=workers) if workers > 1 else None
    try:
        queued = [("default", default_parameters())]
        while True:
            if time.monotonic() > deadline:
                print("Time budget spent. Stopping.")
                break
            if arguments.trials and trial >= arguments.trials:
                print("Trial limit reached. Stopping.")
                break

            if queued:
                label, parameters = queued.pop(0)
            elif arguments.baseline_only:
                break
            else:
                label, parameters = "search", sample_parameters(rng)

            trial += 1
            started = time.perf_counter()
            values = evaluate(pool, parameters, arguments.seeds, days, warm_up_days)
            seconds = time.perf_counter() - started

            clean = [value for value in values if value == value]
            if not clean:
                print(f"trial {trial}: every run failed, skipping")
                continue
            mean = statistics.fmean(clean)
            stdev = statistics.stdev(clean) if len(clean) > 1 else 0.0
            objective = mean + 0.5 * stdev

            row = {"trial": trial, "label": label}
            row.update({name: parameters[name] for name in PARAMETER_NAMES})
            row.update(
                {
                    "seeds": " ".join(str(seed) for seed in arguments.seeds),
                    "days": days,
                    "att_mean": round(mean, 4),
                    "att_stdev": round(stdev, 4),
                    "att_worst": round(max(clean), 4),
                    "objective": round(objective, 4),
                    "seconds": round(seconds, 1),
                }
            )
            append_result(row)

            remaining = max(0.0, deadline - time.monotonic()) / 3600.0
            print(
                f"trial {trial:>4} [{label:<7}] "
                f"ATT {mean:6.3f} +-{stdev:5.3f}  objective {objective:6.3f}  "
                f"({seconds/60:4.1f} min, {remaining:4.1f} h left)"
            )
            sys.stdout.flush()

            if arguments.baseline_only:
                break
    except KeyboardInterrupt:
        print("\nInterrupted. Results written so far are kept.")
    finally:
        if pool is not None:
            pool.close()
            pool.join()

    print()
    report()


if __name__ == "__main__":
    mp.freeze_support()
    main()
