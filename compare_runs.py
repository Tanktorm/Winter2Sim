"""Compare the period-by-period ATT curves produced by run_single.py.

    python compare_runs.py estrategia default sin_disrupcion

Prints the mean, the spread and the worst period of each run, and the
difference of every run against the first one given.
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

ANALYSIS_DIRECTORY = Path(__file__).resolve().parent / "Output" / "Analysis"


def load(label):
    path = ANALYSIS_DIRECTORY / f"att_{label}.csv"
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        return [float(row["AverageTransportTime"]) for row in csv.DictReader(handle)]


def main():
    labels = sys.argv[1:] or ["estrategia", "default", "sin_disrupcion"]
    series = {}
    for label in labels:
        values = load(label)
        if values:
            series[label] = values
        else:
            print(f"(missing: att_{label}.csv)")

    if not series:
        return

    print(f"{'run':<18}{'periods':>9}{'mean':>9}{'stdev':>9}{'best':>9}{'worst':>9}")
    print("-" * 63)
    for label, values in series.items():
        print(
            f"{label:<18}{len(values):>9}{statistics.fmean(values):>9.3f}"
            f"{statistics.stdev(values):>9.3f}{min(values):>9.3f}{max(values):>9.3f}"
        )

    reference = labels[0]
    if reference in series and len(series) > 1:
        base = statistics.fmean(series[reference])
        print()
        for label, values in series.items():
            if label == reference:
                continue
            mean = statistics.fmean(values)
            delta = base - mean
            print(
                f"{reference} vs {label}: {delta:+.3f} d "
                f"({delta / mean * 100:+.1f}%)"
            )


if __name__ == "__main__":
    main()
