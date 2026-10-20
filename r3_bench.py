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

# Los 7 casos del banco de Alemanuel (rama propuesta/alemanuel1_Round3,
# response_strategies/hidden_scenarios.py, SPEC_VERSION 4), copiados tal cual
# para medir r3 en exactamente los mismos escenarios que su optimizador.
ALEMANUEL_CASES = {
    "a1": {
        "id": "case_01_long_ocean_detours",
        "structure": {"route_start_shift_days": 1.4},
        "disruptions": [
            {"type": "leg", "route": "S1", "sequence": 4, "start": 28, "duration": 48, "multiplier": 4.0},
            {"type": "leg", "route": "S4", "sequence": 2, "start": 172, "duration": 35, "multiplier": 6.0},
        ],
    },
    "a2": {
        "id": "case_02_central_hub_closure",
        "structure": {"demand_scale": 1.10, "demand_port_multipliers": {"Singapore": 1.35}},
        "disruptions": [
            {"type": "port", "port": "Singapore", "start": 75, "duration": 18},
            {"type": "leg", "route": "S7", "sequence": 2, "start": 88, "duration": 30, "multiplier": 3.5},
        ],
    },
    "a3": {
        "id": "case_03_scarce_fleet_high_demand",
        "structure": {"demand_scale": 1.40, "fleet_scale": 0.72, "capacity_scale": 0.90},
        "disruptions": [
            {"type": "leg", "route": "S5", "sequence": 4, "start": 42, "duration": 42, "multiplier": 3.0},
            {"type": "port", "port": "New Jersey", "start": 58, "duration": 14},
            {"type": "leg", "route": "S9", "sequence": 3, "start": 61, "duration": 27, "multiplier": 4.0},
        ],
    },
    "a5": {
        "id": "case_05_overlapping_same_service",
        "structure": {"distance_scale": 1.08},
        "disruptions": [
            {"type": "leg", "route": "S1", "sequence": 4, "start": 92, "duration": 44, "multiplier": 5.0},
            {"type": "leg", "route": "S1", "sequence": 6, "start": 104, "duration": 25, "multiplier": 4.0},
            {"type": "port", "port": "Piraeus", "start": 110, "duration": 11},
        ],
    },
    "a6": {
        "id": "case_06_changed_topology",
        "structure": {"remove_routes": ["S4"], "demand_port_multipliers": {"Los Angeles": 1.25}},
        "disruptions": [
            {"type": "leg", "route": "S9", "sequence": 3, "start": 48, "duration": 55, "multiplier": 4.5},
            {"type": "port", "port": "Busan", "start": 190, "duration": 16},
        ],
    },
    "a7": {
        "id": "case_07_renamed_slower_network",
        "structure": {"route_id_prefix": "HIDDEN-", "speed_scale": 0.82, "route_start_shift_days": 3.2, "distance_scale": 1.12},
        "disruptions": [
            {"type": "leg", "route": "S2", "sequence": 6, "start": 20, "duration": 22, "multiplier": 7.0},
            {"type": "port", "port": "Tianjin", "start": 155, "duration": 20},
        ],
    },
    "a8": {
        "id": "case_08_rapid_staggered_shocks",
        "structure": {"fleet_scale": 0.90},
        "disruptions": [
            {"type": "leg", "route": "S3", "sequence": 1, "start": 18, "duration": 7, "multiplier": 5.0},
            {"type": "leg", "route": "S6", "sequence": 3, "start": 82, "duration": 9, "multiplier": 5.5},
            {"type": "port", "port": "Kaohsiung", "start": 149, "duration": 6},
            {"type": "leg", "route": "S7", "sequence": 3, "start": 221, "duration": 8, "multiplier": 8.0},
            {"type": "port", "port": "Cartagena", "start": 305, "duration": 7},
        ],
    },
}
for _name, _case in ALEMANUEL_CASES.items():
    SCENARIOS[_name] = {"descripcion": "banco de Alemanuel: " + _case["id"], "alemanuel": _case}

# Mejor configuracion de Alemanuel (trial 28 de su optimizacion v4), para comparar.
ALEMANUEL_TRIAL28 = {"a1": -18.17, "a2": 16.12, "a3": -30.76, "a5": -19.03, "a6": 10.49, "a7": 15.61, "a8": 7.40}

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
# Los 7 casos de Alemanuel: baseline (default sin disrupciones) y r3.
PLANS["alemanuel"] = [
    (name, mode, 2026, label, {})
    for name in ALEMANUEL_CASES
    for mode, label in (("baseline", "baseline"), ("r3", "r3"))
]
PLANS["alemanuel_live0"] = [
    (name, "r3", 2026, "r3_live0", {"live": "0"}) for name in ALEMANUEL_CASES
]


# ---------------------------------------------------------------------------
# Construccion de escenarios
# ---------------------------------------------------------------------------


def build_context(scenario_name, with_disruptions, warm_up_days):
    import scenario_builders
    from maritime_data_context import Berth, DisruptionPlan, Vessel

    spec = SCENARIOS[scenario_name]
    if spec.get("alemanuel"):
        return _alemanuel_context(spec["alemanuel"], with_disruptions, warm_up_days)
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


def _alemanuel_context(case, with_disruptions, warm_up_days):
    """Misma construccion que hidden_scenarios.create_context de Alemanuel."""
    from maritime_data_context import Berth, DisruptionPlan, Vessel
    from scenario_builders.baseline_stable_scenario import BaselineStableScenario

    context = BaselineStableScenario.create()
    structure = case.get("structure", {})

    def route_named(route_id, allow_prefixed=False):
        route = next((item for item in context.initial_service_routes if item.id == route_id), None)
        if route is None and allow_prefixed:
            route = next((item for item in context.initial_service_routes if item.id.endswith(route_id)), None)
        if route is None:
            raise ValueError(f"ruta desconocida: {route_id!r}")
        return route

    def port_named(name):
        port = next((item for item in context.ports if item.name == name), None)
        if port is None:
            raise ValueError(f"puerto desconocido: {name!r}")
        return port

    demand_scale = float(structure.get("demand_scale", 1.0))
    port_multipliers = structure.get("demand_port_multipliers", {})
    for demand in context.demands:
        multiplier = demand_scale
        multiplier *= float(port_multipliers.get(demand.origin_port.name, 1.0))
        multiplier *= float(port_multipliers.get(demand.destination_port.name, 1.0))
        demand.annual_teus = max(1, int(round(demand.annual_teus * multiplier)))

    distance_scale = float(structure.get("distance_scale", 1.0))
    for leg in context.legs:
        leg.sailing_distance *= distance_scale

    speed_scale = float(structure.get("speed_scale", 1.0))
    capacity_scale = float(structure.get("capacity_scale", 1.0))
    for vessel_class in context.vessel_classes:
        vessel_class.sailing_speed *= speed_scale
        vessel_class.teu_capacity = max(1, int(round(vessel_class.teu_capacity * capacity_scale)))

    for port_name, count in structure.get("berth_counts", {}).items():
        port = port_named(port_name)
        port.berths[:] = [Berth(index=index, port=port) for index in range(int(count))]

    for route_id in set(structure.get("remove_routes", ())):
        route = route_named(route_id)
        for vessel in list(route.deployed_vessels):
            context.vessels.remove(vessel)
            vessel.vessel_class.vessels.remove(vessel)
        route.deployed_vessels.clear()
        context.service_routes.remove(route)
        context.initial_service_routes.remove(route)
        for segment in list(route.segments):
            if segment in context.partial_service_routes:
                context.partial_service_routes.remove(segment)
            if segment in segment.associated_leg.segments:
                segment.associated_leg.segments.remove(segment)

    fleet_scale = float(structure.get("fleet_scale", 1.0))
    requested = structure.get("route_fleet_counts", {})
    for route in list(context.initial_service_routes):
        desired = requested.get(route.id)
        if desired is None:
            desired = max(1, int(round(len(route.deployed_vessels) * fleet_scale)))
        desired = int(desired)
        current = len(route.deployed_vessels)
        if desired < current:
            for vessel in list(route.deployed_vessels[desired:]):
                route.deployed_vessels.remove(vessel)
                context.vessels.remove(vessel)
                vessel.vessel_class.vessels.remove(vessel)
        elif desired > current:
            vessel_class = route.deployed_vessels[0].vessel_class
            next_index = max((v.index for v in context.vessels), default=0) + 1
            for index in range(next_index, next_index + desired - current):
                vessel = Vessel(index, vessel_class, route)
                route.deployed_vessels.append(vessel)
                vessel_class.vessels.append(vessel)
                context.vessels.append(vessel)

    shift = float(structure.get("route_start_shift_days", 0.0))
    for route in context.initial_service_routes:
        route.start_day_of_week = (route.start_day_of_week + shift) % 7.0

    prefix = str(structure.get("route_id_prefix", ""))
    if prefix:
        for route in context.initial_service_routes:
            route.id = prefix + route.id

    if not with_disruptions:
        return context
    for event in case.get("disruptions", ()):
        start = float(event["start"]) + warm_up_days
        duration = float(event["duration"])
        if event["type"] == "leg":
            route = route_named(event["route"], allow_prefixed=True)
            segment = next(s for s in route.segments if s.sequence_index == int(event["sequence"]))
            context.disruption_plans.append(
                DisruptionPlan(
                    target_leg=segment.associated_leg,
                    start_offset_days=start,
                    duration_days=duration,
                    multiplier=float(event["multiplier"]),
                )
            )
        else:
            for berth in port_named(event["port"]).berths:
                context.disruption_plans.append(
                    DisruptionPlan(
                        target_berth=berth,
                        start_offset_days=start,
                        duration_days=duration,
                        close_berth=True,
                    )
                )
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


def report(root=None):
    root = Path(root) if root else RESULTS_ROOT
    results_file = root / "results.csv"
    if not root.exists():
        print(f"No hay resultados en {root}.")
        return
    rows = []
    for scenario_dir in sorted(path for path in root.iterdir() if path.is_dir()):
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
    if results_file.exists():
        with results_file.open(newline="", encoding="utf-8") as handle:
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
    _report_alemanuel(rows)
    print("\nPromedio por variante (solo comparable si cubren los mismos escenarios):")
    for label, values in sorted(by_label.items()):
        print(f"  {label:<18} {sum(values) / len(values):8.3f}  ({len(values)} corridas)")
    if results_file.exists():
        print(f"\nTEU completados y tiempos por corrida: {results_file}")


def _robust_score(losses):
    """Misma formula que el optimizador de Alemanuel."""
    import statistics

    mean = statistics.fmean(losses)
    deviation = statistics.pstdev(losses) if len(losses) > 1 else 0.0
    return mean + 0.35 * deviation + 0.25 * max(0.0, max(losses) - mean)


def _report_alemanuel(rows):
    by_label = {}
    for scenario, seed, label, loss, *_ in rows:
        if scenario in ALEMANUEL_CASES and seed == "s2026":
            by_label.setdefault(label, {})[scenario] = loss
    if not by_label:
        return
    names = list(ALEMANUEL_CASES)
    print("\nBanco de Alemanuel (7 casos, semilla 2026):")
    print(f"{'corrida':<18}" + "".join(f"{name:>9}" for name in names) + f"{'promedio':>10}{'peor':>9}{'robusto':>9}")
    candidates = dict(sorted(by_label.items()))
    candidates["alemanuel_t28"] = ALEMANUEL_TRIAL28
    for label, losses in candidates.items():
        cells = "".join(f"{losses[name]:>9.2f}" if name in losses else f"{'-':>9}" for name in names)
        if len(losses) == len(names):
            values = [losses[name] for name in names]
            summary = f"{sum(values) / len(values):>10.2f}{max(values):>9.2f}{_robust_score(values):>9.2f}"
        else:
            summary = f"{'(faltan ' + str(len(names) - len(losses)) + ' casos)':>28}"
        print(f"{label:<18}{cells}{summary}")
    print("Menor es mejor en todo. 'alemanuel_t28' son los numeros que reporto Alemanuel.")


def _summary(rows, labels, combos):
    """Promedio, peor caso y score robusto de cada variante sobre las mismas
    combinaciones escenario-semilla."""
    table = {}
    for scenario, seed, label, loss, *_ in rows:
        table[(label, scenario, seed)] = loss
    print(f"\nResumen sobre {len(combos)} combinaciones escenario-semilla (menor es mejor):")
    print(f"{'variante':<22}{'completas':>10}{'promedio':>10}{'peor':>9}{'robusto':>9}")
    results = []
    for label in labels:
        values = [table[(label, scenario, seed)] for scenario, seed in combos if (label, scenario, seed) in table]
        if len(values) == len(combos):
            mean = sum(values) / len(values)
            results.append((_robust_score(values), label, mean, max(values)))
        else:
            print(f"{label:<22}{len(values):>7}/{len(combos):<2}   (incompleta)")
    for robust, label, mean, worst in sorted(results):
        print(f"{label:<22}{len(combos):>7}/{len(combos):<2}{mean:>10.2f}{worst:>9.2f}{robust:>9.2f}")


def _collect_rows(root):
    """Loss de cada curva contra el baseline de su escenario y semilla."""
    rows = []
    for scenario_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for seed_dir in sorted(path for path in scenario_dir.iterdir() if path.is_dir()):
            baseline_path = seed_dir / "baseline.csv"
            if not baseline_path.exists():
                continue
            baseline = load_curve(baseline_path)
            for curve in sorted(seed_dir.glob("*.csv")):
                if curve.name == "baseline.csv":
                    continue
                periods = load_curve(curve)
                if len(periods) != len(baseline):
                    continue
                loss, _, _ = cumulative_loss(baseline, periods)
                rows.append((scenario_dir.name, seed_dir.name, curve.stem, loss))
    return rows


def tune(scenarios, seeds, variants, workers):
    """Corre cada variante en cada escenario y semilla (con su baseline) y
    ordena las variantes por score robusto."""
    jobs = []
    for scenario in scenarios:
        for seed in seeds:
            jobs.append(_job(scenario, "baseline", seed, "baseline", {}))
            for name, overrides in variants:
                jobs.append(_job(scenario, "r3", seed, name, overrides))
    run_jobs(jobs, workers)
    combos = [(scenario, f"s{seed}") for scenario in scenarios for seed in seeds]
    _summary(_collect_rows(RESULTS_ROOT), [name for name, _ in variants], combos)


def _parse_variant(text):
    name, _, settings = text.partition(":")
    overrides = {}
    for item in filter(None, settings.split(",")):
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
    return name.strip(), overrides


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

    tune_parser = sub.add_parser("tune", help="probar configuraciones de r3 en varios escenarios")
    tune_parser.add_argument("--scenarios", default="r2,a1,a2,a3,a5,a6,a7,a8,h3")
    tune_parser.add_argument("--seeds", default="2026")
    tune_parser.add_argument(
        "--variant", action="append", default=[],
        help="nombre:clave=valor,clave=valor (repetible; 'nombre:' sin claves = configuracion por defecto)",
    )
    tune_parser.add_argument("--workers", type=int, default=1)

    report_parser = sub.add_parser("report", help="tabla de loss")
    report_parser.add_argument("--dir", default=None, help="carpeta de resultados (por defecto Output/R3)")

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
    if arguments.command == "tune":
        scenarios = [item.strip() for item in arguments.scenarios.split(",") if item.strip()]
        unknown = [item for item in scenarios if item not in SCENARIOS]
        if unknown:
            parser.error(f"escenarios desconocidos: {unknown}")
        seeds = [int(item) for item in arguments.seeds.split(",") if item.strip()]
        variants = [_parse_variant(item) for item in arguments.variant] or [("r3_actual", {})]
        tune(scenarios, seeds, variants, max(1, arguments.workers))
        return
    if arguments.command == "report":
        report(arguments.dir)


if __name__ == "__main__":
    main()
