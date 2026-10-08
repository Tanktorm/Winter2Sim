"""Banco de pruebas para la Ronda 3 (escenario oculto).

Corre la simulacion completa en varios escenarios y semillas y calcula el loss
oficial de cada corrida contra el baseline del MISMO escenario y la MISMA
semilla (red sin disrupciones + estrategia por defecto, como hacen los jueces).

Los escenarios h1, h2 y h3 imitan lo que puede traer la ronda oculta: otras
disrupciones, otra flota, otra demanda, cierres largos y parciales.  Se arman
en memoria a partir de Input/ sin tocar ningun archivo oficial.

Comandos (desde la carpeta del proyecto):

    python r3_bench.py smoke                    prueba corta: carga y corre sin errores
    python r3_bench.py run r2 r3 --seed 2026    una corrida (escenario, modo)
    python r3_bench.py run r2 r3 --seed 2026 --set live=0 --label r3_live0
    python r3_bench.py plan rapido              lista de corridas predefinida
    python r3_bench.py plan rapido --workers 2  dos corridas a la vez
    python r3_bench.py report                   tabla de resultados

Modos: baseline (red sin disrupciones, default), default (con disrupciones,
estrategia por defecto) y r3 (con disrupciones, nuestra estrategia).

Las corridas ya terminadas no se repiten: el plan se puede cortar con Ctrl+C y
relanzar, y sigue donde quedo.  Resultados en Output/R3/.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = PROJECT_ROOT / "Output" / "R3"
RESULTS_FILE = RESULTS_ROOT / "results.csv"

# ---------------------------------------------------------------------------
# Escenarios.  Dias relativos al inicio de la medicion (como el oficial).
#   ("leg", desde, hasta, dia_inicio, duracion, multiplicador)
#   ("port", puerto, dia_inicio, duracion)          cierra todos los muelles
#   ("berth", puerto, indice_muelle, dia_inicio, duracion)   cierre parcial
# ---------------------------------------------------------------------------

SCENARIOS = {
    "r2": {
        "descripcion": "Ronda 2 oficial",
        "official": True,
    },
    "h1": {
        "descripcion": "misma red, otras disrupciones (tramo troncal compartido, hubs cerrados)",
        "disruptions": [
            ("leg", "Singapore", "Colombo", 30, 50, 4.0),
            ("port", "Singapore", 110, 10),
            ("leg", "Los Angeles", "Shanghai", 150, 60, 4.0),
            ("port", "Rotterdam", 240, 15),
            ("leg", "Tanger Med", "New Jersey", 270, 60, 3.0),
        ],
    },
    "h2": {
        "descripcion": "red modificada: otra flota, menos muelles, demanda x1.3",
        "vessels": {"S1": 7, "S5": 8, "S6": 4, "S9": 6},
        "berths": {"Singapore": 2},
        "demand_factor": 1.3,
        "disruptions": [
            ("leg", "Colombo", "Piraeus", 40, 50, 5.0),
            ("port", "Busan", 150, 12),
            ("leg", "Kaohsiung", "Los Angeles", 200, 40, 4.0),
            ("port", "Tanger Med", 280, 10),
        ],
    },
    "h3": {
        "descripcion": "estres: cierre largo, cierre parcial, eventos solapados, disrupcion en el warm-up",
        "disruptions": [
            ("leg", "Shanghai", "Shenzhen", -20, 40, 3.0),
            ("berth", "Shanghai", 0, 60, 60),
            ("port", "Tanger Med", 100, 40),
            ("leg", "Colombo", "New Jersey", 220, 60, 5.0),
            ("leg", "New Jersey", "Colombo", 240, 60, 5.0),
            ("port", "Tianjin", 300, 15),
        ],
    },
}

PLANS = {
    # Lo primero: Ronda 2, semilla 2026, las dos variantes de la estrategia.
    "rapido": [
        ("r2", "baseline", 2026, "baseline", {}),
        ("r2", "r3", 2026, "r3", {}),
        ("r2", "r3", 2026, "r3_live0", {"live": "0"}),
    ],
    # Generalidad: escenarios distintos a la Ronda 2.
    "generalidad": [
        (scenario, mode, 2026, label, overrides)
        for scenario in ("h1", "h2", "h3")
        for mode, label, overrides in (
            ("baseline", "baseline", {}),
            ("r3", "r3", {}),
            ("r3", "r3_live0", {"live": "0"}),
        )
    ],
    # Robustez a la semilla en la red de la Ronda 2.
    "semillas": [
        ("r2", mode, seed, label, overrides)
        for seed in (2027, 2028)
        for mode, label, overrides in (
            ("baseline", "baseline", {}),
            ("r3", "r3", {}),
            ("r3", "r3_live0", {"live": "0"}),
        )
    ],
    # Referencia: la estrategia por defecto (corridas lentas, opcionales).
    "default": [
        (scenario, "default", 2026, "default", {})
        for scenario in ("r2", "h1", "h2", "h3")
    ],
}
PLANS["completo"] = PLANS["rapido"] + PLANS["generalidad"] + PLANS["semillas"]


# ---------------------------------------------------------------------------
# Construccion de escenarios
# ---------------------------------------------------------------------------


def build_context(scenario_name, with_disruptions, warm_up_days):
    import scenario_builders
    from maritime_data_context import Berth, DisruptionPlan, Vessel

    spec = SCENARIOS[scenario_name]
    if spec.get("official"):
        if with_disruptions:
            return scenario_builders.create_with_disruption(warm_up_days)
        return scenario_builders.create()

    context = scenario_builders.create()
    routes = {route.id.casefold(): route for route in context.service_routes}
    ports = {port.name.casefold(): port for port in context.ports}

    for route_id, count in spec.get("vessels", {}).items():
        route = routes[route_id.casefold()]
        vessels = list(route.deployed_vessels)
        while len(route.deployed_vessels) > count:
            vessel = route.deployed_vessels.pop()
            context.vessels.remove(vessel)
            vessel.vessel_class.vessels.remove(vessel)
        next_index = max(vessel.index for vessel in context.vessels) + 1
        while len(route.deployed_vessels) < count:
            vessel = Vessel(next_index, vessels[0].vessel_class, route)
            next_index += 1
            context.vessels.append(vessel)
            vessel.vessel_class.vessels.append(vessel)
            route.deployed_vessels.append(vessel)

    for port_name, count in spec.get("berths", {}).items():
        port = ports[port_name.casefold()]
        while len(port.berths) > count:
            port.berths.pop()
        while len(port.berths) < count:
            port.berths.append(Berth(index=len(port.berths), port=port))

    factor = spec.get("demand_factor")
    if factor:
        for demand in context.demands:
            demand.annual_teus = max(1, int(round(demand.annual_teus * factor)))

    if not with_disruptions:
        return context

    for item in spec.get("disruptions", []):
        kind = item[0]
        if kind == "leg":
            _, departure, arrival, start, duration, multiplier = item
            legs = [
                leg
                for leg in context.legs
                if leg.departure_port.name.casefold() == departure.casefold()
                and leg.arrival_port.name.casefold() == arrival.casefold()
            ]
            if not legs:
                raise ValueError(f"tramo {departure}->{arrival} no existe")
            for leg in legs:
                context.disruption_plans.append(
                    DisruptionPlan(
                        target_leg=leg,
                        start_offset_days=warm_up_days + start,
                        duration_days=duration,
                        multiplier=multiplier,
                    )
                )
        elif kind in ("port", "berth"):
            port = ports[item[1].casefold()]
            if kind == "port":
                _, _, start, duration = item
                berths = list(port.berths)
            else:
                _, _, berth_index, start, duration = item
                berths = [port.berths[berth_index]]
            for berth in berths:
                context.disruption_plans.append(
                    DisruptionPlan(
                        target_berth=berth,
                        start_offset_days=warm_up_days + start,
                        duration_days=duration,
                        close_berth=True,
                    )
                )
        else:
            raise ValueError(f"tipo de disrupcion desconocido: {kind}")
    return context


# ---------------------------------------------------------------------------
# Una corrida (se ejecuta en un proceso propio)
# ---------------------------------------------------------------------------


def _use_default_strategy(fast_baseline):
    """Desactiva UserStrategy.  En el baseline (sin disrupciones) la ruta del
    default solo depende del par origen-destino, asi que se memoriza: mismo
    resultado, mucho mas rapido."""
    from maritime_data_context import Booking
    from response_strategies.default_strategy import DefaultStrategy
    from response_strategies.user_strategy import UserStrategy

    for name in (
        "select_vessel_for_berth",
        "create_alternative_service_routes",
        "assign_associated_bookings",
        "adjust_bookings_before_cargo_handling",
    ):
        setattr(UserStrategy, name, staticmethod(lambda *args, **kwargs: None))

    if not fast_baseline:
        return
    original = DefaultStrategy.assign_associated_bookings
    chains = {}

    def assign(context, now, shipment):
        if context.disruption_plans:
            return original(context, now, shipment)
        chain = chains.get(shipment.demand)
        if chain is None:
            result = original(context, now, shipment)
            if result and shipment.associated_bookings:
                chains[shipment.demand] = [
                    (b.service_route, b.departure_segment_index, b.arrival_segment_index)
                    for b in sorted(shipment.associated_bookings, key=lambda b: b.sequence_index)
                ]
            return result
        for booking in shipment.associated_bookings:
            while booking in booking.service_route.associated_bookings:
                booking.service_route.associated_bookings.remove(booking)
        shipment.associated_bookings = []
        for sequence, (route, departure, arrival) in enumerate(chain, start=1):
            booking = Booking(
                sequence_index=sequence,
                shipment=shipment,
                service_route=route,
                departure_segment_index=departure,
                arrival_segment_index=arrival,
            )
            shipment.associated_bookings.append(booking)
            route.associated_bookings.append(booking)
        shipment.current_booking_index = 1
        return True

    DefaultStrategy.assign_associated_bookings = staticmethod(assign)


def run_job(job):
    scenario, mode, seed, label, overrides = (
        job["scenario"], job["mode"], job["seed"], job["label"], job["overrides"],
    )
    for key, value in overrides.items():
        os.environ["WSC_R3_" + key.upper()] = str(value)

    from config.simulation_config import SIMULATION_DAYS, STATISTICS_INTERVAL_DAYS, WARM_UP_DAYS
    from simulation_model import Model

    if mode in ("baseline", "default"):
        _use_default_strategy(fast_baseline=(mode == "baseline"))

    warm_up_days = job.get("warmup") or WARM_UP_DAYS
    days = job.get("days") or SIMULATION_DAYS
    context = build_context(scenario, mode != "baseline", warm_up_days)
    sim = Model(context, seed=seed)

    started = time.perf_counter()
    print(f"[{label}] {scenario} s{seed}: warm-up {warm_up_days} dias...", flush=True)
    sim.warmup(period=dt.timedelta(days=warm_up_days))
    rows = []
    period_start_day = 1
    period_start_time = sim.clock_time
    for day in range(1, days + 1):
        sim.run(duration=dt.timedelta(days=1))
        if day % STATISTICS_INTERVAL_DAYS and day != days:
            continue
        att = sim.get_teu_weighted_average_transport_time_hours(period_start_time, sim.clock_time) / 24.0
        rows.append((len(rows) + 1, period_start_day, day, att))
        period_start_day = day + 1
        period_start_time = sim.clock_time
        if len(rows) % 12 == 0:
            minutes = (time.perf_counter() - started) / 60.0
            print(f"[{label}] {scenario} s{seed}: dia {day}/{days} ATT {att:.2f} ({minutes:.1f} min)", flush=True)

    measurement_start = dt.datetime.min + dt.timedelta(days=warm_up_days)
    completed = unfinished = 0
    for demand in context.demands:
        for shipment in demand.shipments:
            if shipment.completion_time is None:
                unfinished += shipment.teu_size
            elif shipment.completion_time > measurement_start:
                completed += shipment.teu_size

    path = _curve_path(scenario, seed, label, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["PeriodIndex", "StartDay", "EndDay", "AverageTransportTime"])
        for index, start, end, att in rows:
            writer.writerow([index, start, end, f"{att:.4f}"])

    minutes = (time.perf_counter() - started) / 60.0
    mean_att = sum(row[3] for row in rows) / len(rows)
    _append_result(
        {
            "scenario": scenario,
            "seed": seed,
            "label": label,
            "mode": mode,
            "days": days,
            "warmup": warm_up_days,
            "mean_att": f"{mean_att:.4f}",
            "completed_teu": completed,
            "unfinished_teu": unfinished,
            "minutes": f"{minutes:.1f}",
            "overrides": json.dumps(overrides, sort_keys=True),
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    )
    print(f"[{label}] {scenario} s{seed}: listo en {minutes:.1f} min, ATT medio {mean_att:.3f}", flush=True)


def _curve_path(scenario, seed, label, job):
    suffix = ""
    if job.get("days") or job.get("warmup"):
        suffix = f"_w{job.get('warmup') or 0}_d{job.get('days') or 0}"
    return RESULTS_ROOT / scenario / f"s{seed}" / f"{label}{suffix}.csv"


def _append_result(row):
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    exists = RESULTS_FILE.exists()
    with RESULTS_FILE.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Loss (identico al dashboard: ATT de la corrida con dos decimales)
# ---------------------------------------------------------------------------


def load_curve(path):
    periods = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (row.get("PeriodIndex") or "").strip().isdigit():
                continue
            periods.append(
                (int(row["PeriodIndex"]), int(row["StartDay"]), int(row["EndDay"]), float(row["AverageTransportTime"]))
            )
    return periods


def cumulative_loss(baseline, run):
    by_index = {period[0]: period for period in run}
    total = penalty = credit = 0.0
    for index, start, end, baseline_att in baseline:
        period = by_index.get(index)
        if period is None or period[1] != start or period[2] != end:
            continue
        run_att = float(f"{period[3]:.2f}")
        ratio = (1.0 if baseline_att <= 0 else 0.0) if run_att <= 0 else baseline_att / run_att
        value = (1.0 - ratio) * (end - start + 1)
        total += value
        if value > 0:
            penalty += value
        else:
            credit += value
    return total, penalty, credit


# ---------------------------------------------------------------------------
# Ordenes
# ---------------------------------------------------------------------------


def _job(scenario, mode, seed, label, overrides, days=0, warmup=0):
    return {
        "scenario": scenario, "mode": mode, "seed": seed, "label": label,
        "overrides": overrides, "days": days, "warmup": warmup,
    }


def run_jobs(jobs, workers):
    pending = [job for job in jobs if not _curve_path(job["scenario"], job["seed"], job["label"], job).exists()]
    skipped = len(jobs) - len(pending)
    if skipped:
        print(f"{skipped} corridas ya hechas, se saltan.")
    print(f"{len(pending)} corridas por hacer, {workers} a la vez.")
    running = []
    failures = 0
    while pending or running:
        while pending and len(running) < workers:
            job = pending.pop(0)
            command = [sys.executable, str(Path(__file__).resolve()), "_job", json.dumps(job)]
            running.append((job, subprocess.Popen(command, cwd=str(PROJECT_ROOT))))
        time.sleep(2)
        for item in list(running):
            job, process = item
            if process.poll() is None:
                continue
            running.remove(item)
            if process.returncode != 0:
                failures += 1
                print(f"!! fallo la corrida {job['scenario']} {job['label']} s{job['seed']} (codigo {process.returncode})")
    print("Plan terminado." if not failures else f"Plan terminado con {failures} fallos.")
    return failures


def report():
    if not RESULTS_ROOT.exists():
        print("No hay resultados todavia.")
        return
    rows = []
    for scenario_dir in sorted(path for path in RESULTS_ROOT.iterdir() if path.is_dir()):
        for seed_dir in sorted(path for path in scenario_dir.iterdir() if path.is_dir()):
            baseline_path = seed_dir / "baseline.csv"
            official = PROJECT_ROOT / "Output" / "Baseline_ATT_By_Statistics_Interval.csv"
            if not baseline_path.exists() and scenario_dir.name == "r2" and seed_dir.name == "s2026" and official.exists():
                baseline_path = official
            if not baseline_path.exists():
                print(f"(sin baseline para {scenario_dir.name}/{seed_dir.name}: no se puede calcular el loss)")
                continue
            baseline = load_curve(baseline_path)
            for curve in sorted(seed_dir.glob("*.csv")):
                if curve.name == "baseline.csv":
                    continue
                periods = load_curve(curve)
                if len(periods) != len(baseline):
                    continue
                loss, penalty, credit = cumulative_loss(baseline, periods)
                att = sum(period[3] for period in periods) / len(periods)
                rows.append((scenario_dir.name, seed_dir.name, curve.stem, loss, penalty, credit, att))
    if not rows:
        print("No hay corridas completas con baseline.")
        return
    completed = {}
    if RESULTS_FILE.exists():
        with RESULTS_FILE.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                completed[(row["scenario"], f"s{row['seed']}", row["label"])] = row["completed_teu"]
    print(f"{'escenario':<10}{'semilla':<8}{'corrida':<18}{'loss':>9}{'penaliz.':>10}{'credito':>10}{'ATT':>8}{'TEU compl.':>12}")
    print("-" * 85)
    for scenario, seed, label, loss, penalty, credit, att in rows:
        teu = completed.get((scenario, seed, label), "-")
        print(f"{scenario:<10}{seed:<8}{label:<18}{loss:>9.3f}{penalty:>10.3f}{credit:>10.3f}{att:>8.3f}{teu:>12}")
    by_label = {}
    for scenario, seed, label, loss, *_ in rows:
        by_label.setdefault(label, []).append(loss)
    print("\nPromedio por variante (solo comparable si cubren los mismos escenarios):")
    for label, values in sorted(by_label.items()):
        print(f"  {label:<18} {sum(values) / len(values):8.3f}  ({len(values)} corridas)")
    if RESULTS_FILE.exists():
        print(f"\nTEU completados y tiempos por corrida: {RESULTS_FILE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser("smoke", help="prueba corta de que todo corre")
    smoke.add_argument("--scenario", default="h3")

    run = sub.add_parser("run", help="una corrida")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument("mode", choices=["baseline", "default", "r3"])
    run.add_argument("--seed", type=int, default=2026)
    run.add_argument("--label", default=None)
    run.add_argument("--set", action="append", default=[], help="parametro=valor de r3_strategy.DEFAULTS")
    run.add_argument("--days", type=int, default=0)
    run.add_argument("--warmup", type=int, default=0)

    plan = sub.add_parser("plan", help="lista de corridas predefinida")
    plan.add_argument("name", choices=sorted(PLANS))
    plan.add_argument("--workers", type=int, default=1)

    sub.add_parser("report", help="tabla de loss")

    hidden = sub.add_parser("_job")
    hidden.add_argument("payload")

    arguments = parser.parse_args()
    if arguments.command == "_job":
        run_job(json.loads(arguments.payload))
        return
    if arguments.command == "smoke":
        jobs = [
            _job(arguments.scenario, "r3", 2026, "smoke_r3", {}, days=15, warmup=60),
            _job(arguments.scenario, "baseline", 2026, "smoke_baseline", {}, days=15, warmup=60),
        ]
        for job in jobs:
            path = _curve_path(job["scenario"], job["seed"], job["label"], job)
            if path.exists():
                path.unlink()
        failures = run_jobs(jobs, 1)
        print("\nPRUEBA DE HUMO OK" if not failures else "\nPRUEBA DE HUMO FALLO: revisar el error de arriba")
        return
    if arguments.command == "run":
        overrides = {}
        for item in arguments.set:
            key, _, value = item.partition("=")
            overrides[key.strip()] = value.strip()
        label = arguments.label or (arguments.mode if not overrides else arguments.mode + "_" + "_".join(f"{k}{v}" for k, v in sorted(overrides.items())))
        if arguments.mode == "baseline":
            label = "baseline"
        job = _job(arguments.scenario, arguments.mode, arguments.seed, label, overrides, arguments.days, arguments.warmup)
        run_jobs([job], 1)
        return
    if arguments.command == "plan":
        jobs = [_job(*item) for item in PLANS[arguments.name]]
        run_jobs(jobs, max(1, arguments.workers))
        return
    if arguments.command == "report":
        report()


if __name__ == "__main__":
    main()
