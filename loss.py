"""Cumulative resilience loss, the metric the challenge actually scores.

Mirrors dashboard/app.js calculateResiliencePeriods: for every statistics
period, the performance loss is 1 - baselineATT/disruptedATT, weighted by the
days in the period and summed over the run. Lower is better; zero means the
disruption cost nothing.

    python loss.py Output/Baseline_ATT_By_Statistics_Interval.csv curva1.csv ...
"""

import csv
import sys


def load(path):
    periods = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (row.get("PeriodIndex") or "").strip().isdigit():
                continue
            periods.append(
                (
                    int(row["PeriodIndex"]),
                    int(row["StartDay"]),
                    int(row["EndDay"]),
                    float(row["AverageTransportTime"]),
                )
            )
    return periods


def cumulative_loss(baseline, disrupted):
    by_index = {p[0]: p for p in disrupted}
    total = 0.0
    for index, start, end, baseline_att in baseline:
        period = by_index.get(index)
        if period is None or period[1] != start or period[2] != end:
            continue
        # The dashboard reads the CSV, which stores two decimals.
        disruption_att = float(f"{period[3]:.2f}")
        if disruption_att <= 0:
            ratio = 1.0 if baseline_att <= 0 else 0.0
        else:
            ratio = baseline_att / disruption_att
        total += (1.0 - ratio) * (end - start + 1)
    return total


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    baseline = load(sys.argv[1])
    print(f"{'curva':<28}{'loss':>10}{'ATT medio':>12}")
    print("-" * 50)
    for path in sys.argv[2:]:
        periods = load(path)
        att = sum(p[3] for p in periods) / len(periods)
        print(f"{path.split('/')[-1]:<28}{cumulative_loss(baseline, periods):>10.3f}{att:>12.3f}")


if __name__ == "__main__":
    main()
