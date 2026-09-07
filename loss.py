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


def detail(baseline, disrupted, label):
    """Break the loss into what it penalises and what it credits.

    A total says how much was lost; this says where. Periods slower than the
    baseline add penalty, faster ones return credit, and the worst handful
    usually point straight at the window that needs work.
    """
    by_index = {p[0]: p for p in disrupted}
    penalty = credit = 0.0
    rows = []
    for index, start, end, baseline_att in baseline:
        period = by_index.get(index)
        if period is None or period[1] != start or period[2] != end:
            continue
        disruption_att = float(f"{period[3]:.2f}")
        if disruption_att <= 0:
            continue
        value = (1.0 - baseline_att / disruption_att) * (end - start + 1)
        rows.append((value, start, end, baseline_att, disruption_att))
        if value > 0:
            penalty += value
        else:
            credit += value

    print(f"\n{label}")
    print(f"  loss {penalty + credit:8.3f} = penalizacion {penalty:.3f} + credito {credit:.3f}")
    rows.sort(reverse=True)
    print("  periodos que mas penalizan:")
    for value, start, end, baseline_att, disruption_att in rows[:6]:
        print(
            f"    dias {start:>3}-{end:<3} base {baseline_att:5.2f} vs {disruption_att:5.2f}"
            f"  loss {value:+.2f}"
        )
    print("  periodos que mas acreditan:")
    for value, start, end, baseline_att, disruption_att in rows[-4:]:
        print(
            f"    dias {start:>3}-{end:<3} base {baseline_att:5.2f} vs {disruption_att:5.2f}"
            f"  loss {value:+.2f}"
        )


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    wants_detail = "--detalle" in sys.argv
    argv = [a for a in sys.argv if a != "--detalle"]
    baseline = load(argv[1])
    print(f"{'curva':<28}{'loss':>10}{'ATT medio':>12}")
    print("-" * 50)
    for path in argv[2:]:
        periods = load(path)
        att = sum(p[3] for p in periods) / len(periods)
        name = path.replace("\\", "/").split("/")[-1]
        print(f"{name:<28}{cumulative_loss(baseline, periods):>10.3f}{att:>12.3f}")

    if wants_detail:
        for path in argv[2:]:
            name = path.replace("\\", "/").split("/")[-1]
            detail(baseline, load(path), name)


if __name__ == "__main__":
    main()
