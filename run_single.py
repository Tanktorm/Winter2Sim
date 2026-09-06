"""Run one simulation and record the ATT of every statistics period.

Unlike run_batch.py, which only keeps the mean of a run, this writes the whole
period-by-period curve so a run can be compared against a reference run at the
same seed. Output goes to Output/Analysis/att_<label>.csv.

    python run_single.py --label strategy --seed 2026
    python run_single.py --label nostrategy --seed 2026 --no-strategy
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ANALYSIS_DIRECTORY = PROJECT_ROOT / "Output" / "Analysis"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Name for the output file.")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--no-strategy", action="store_true",
                        help="Disable the user strategy to measure the default.")
    parser.add_argument("--baseline", action="store_true",
                        help="Use the scenario without disruptions.")
    parser.add_argument("--dashboard", action="store_true",
                        help="Also write the full Output/ CSV set the dashboard reads.")
    arguments = parser.parse_args()

    if arguments.no_strategy:
        os.environ["WSC_STRATEGY_ENABLED"] = "0"

    from config.simulation_config import (
        SIMULATION_DAYS,
        STATISTICS_INTERVAL_DAYS,
        WARM_UP_DAYS,
    )
    import scenario_builders
    from simulation_model import Model
    from simulation_output_csv_writer import write_all, write_att_by_period

    days = arguments.days or SIMULATION_DAYS
    warm_up_days = arguments.warmup or WARM_UP_DAYS
    interval = STATISTICS_INTERVAL_DAYS

    context = (
        scenario_builders.create()
        if arguments.baseline
        else scenario_builders.create_with_disruption()
    )
    sim = Model(context, seed=arguments.seed)

    started = time.perf_counter()
    print(f"[{arguments.label}] warm-up {warm_up_days} days...", flush=True)
    sim.warmup(period=dt.timedelta(days=warm_up_days))
    print(f"[{arguments.label}] measuring {days} days...", flush=True)

    rows = []
    period_start_day = 1
    period_start_time = sim.clock_time
    for day in range(1, days + 1):
        sim.run(duration=dt.timedelta(days=1))
        if day % interval and day != days:
            continue
        att = sim.get_teu_weighted_average_transport_time_hours(
            period_start_time, sim.clock_time
        ) / 24.0
        rows.append((len(rows) + 1, period_start_day, day, round(att, 4)))
        period_start_day = day + 1
        period_start_time = sim.clock_time
        if len(rows) % 12 == 0:
            print(
                f"[{arguments.label}] day {day}/{days} "
                f"ATT {att:.2f} ({(time.perf_counter()-started)/60:.1f} min)",
                flush=True,
            )

    if arguments.dashboard:
        # The dashboard reads the Output/ CSV set, which only main.py writes.
        # Writing it here too means a measurement run can be inspected in the
        # dashboard without paying for a second run.
        output_directory = PROJECT_ROOT / "Output"
        write_all(sim, output_directory)
        write_att_by_period(output_directory, [(r[1], r[2], r[3]) for r in rows])
        print(f"[{arguments.label}] dashboard CSVs written to {output_directory}")

    ANALYSIS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    output_path = ANALYSIS_DIRECTORY / f"att_{arguments.label}.csv"
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["PeriodIndex", "StartDay", "EndDay", "AverageTransportTime"])
        writer.writerows(rows)

    mean = sum(row[3] for row in rows) / len(rows)
    print(f"[{arguments.label}] mean ATT {mean:.4f} over {len(rows)} periods")
    print(f"[{arguments.label}] written to {output_path}")


if __name__ == "__main__":
    main()
