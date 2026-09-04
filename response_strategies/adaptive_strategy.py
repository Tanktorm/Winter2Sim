"""Adaptive routing strategy.

The default strategy routes cargo with Dijkstra over nautical miles alone. It
therefore books cargo onto a leg whose sailing time has been multiplied by a
disruption, because the graph it searches cannot see the multiplier.

This strategy replaces the metric with expected transit time in days and prices
four effects the distance metric ignores:

* the disruption multiplier that will be active *at the moment the container
  would traverse the leg*, not the multiplier active right now;
* the wait for the next departure, which penalises infrequent services;
* the transshipment penalty for every change of service route; and
* the queue of cargo already booked onto the same departure. This term is
  disabled by default: it was meant to spread cargo across paths, but measured
  on this network it makes things worse at every weight tried (see
  ``Params.queue_weight``).

Measured against the same seed, the strategy does not yet beat the simulator's
default over a full year (15.856 days against 15.532). It wins in the first 120
measured days (13.562 against 13.794) and loses in the last 140 (16.448 against
15.614). The late-year deficit is unexplained and is the open problem.

Nothing here is specific to a scenario: ports, routes and disruption windows are
all read from the context. Every weight is read from the environment so a batch
runner can calibrate them without editing this file.
"""

from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math
import os

from maritime_data_context import Booking


SIMULATION_EPOCH = dt.datetime.min

# Hours a vessel spends alongside per port call. Used to price a booking that
# rides through intermediate calls.
BERTHING_DAYS_PER_CALL = 3.0 / 24.0
DEFAULT_SPEED_KNOTS = 20.0


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


class Params:
    """Calibration knobs, re-read on every call so a runner can sweep them."""

    @staticmethod
    def transfer_buffer_days():
        return _env_float("WSC_TRANSFER_BUFFER_HOURS", 12.0) / 24.0

    @staticmethod
    def wait_weight():
        return _env_float("WSC_WAIT_WEIGHT", 1.0)

    @staticmethod
    def queue_weight():
        # Measured off. Weighting the queue was meant to spread cargo across
        # paths, but on this network it feeds back on itself: the queue raises
        # the cost of the good path, cargo moves to a genuinely worse one, that
        # one saturates in turn. Full-year ATT at seed 2026 was 15.856 at weight
        # 0, 16.256 at 1.0 and 16.786 at 0.2, so the damage is not a matter of
        # picking a smaller weight. Kept as a knob for the calibration runner.
        return _env_float("WSC_QUEUE_WEIGHT", 0.0)

    @staticmethod
    def closure_slack_days():
        return _env_float("WSC_CLOSURE_SLACK_DAYS", 1.0)

    @staticmethod
    def max_transfers():
        return _env_int("WSC_MAX_TRANSFERS", 3)

    @staticmethod
    def load_refresh_hours():
        return _env_float("WSC_LOAD_REFRESH_HOURS", 6.0)

    @staticmethod
    def enabled():
        return _env_flag("WSC_STRATEGY_ENABLED", True)


class _Edge:
    """One bookable ride: board a route at one port, leave it at a later one."""

    __slots__ = (
        "route",
        "departure_port",
        "arrival_port",
        "departure_segment_index",
        "arrival_segment_index",
        "legs",
    )

    def __init__(
        self,
        route,
        departure_port,
        arrival_port,
        departure_segment_index,
        arrival_segment_index,
        legs,
    ):
        self.route = route
        self.departure_port = departure_port
        self.arrival_port = arrival_port
        self.departure_segment_index = departure_segment_index
        self.arrival_segment_index = arrival_segment_index
        self.legs = legs


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def absolute_day(when):
    """Simulation day of a datetime. Disruption offsets use the same origin."""
    return (when - SIMULATION_EPOCH).total_seconds() / 86400.0


def leg_multiplier_at(context, leg, when):
    """Sailing-time multiplier that will be active on ``leg`` at ``when``."""
    day = absolute_day(when)
    multiplier = 1.0
    for plan in context.disruption_plans:
        if plan.target_leg is not leg or plan.start_offset_days is None:
            continue
        if plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days:
            multiplier = max(multiplier, plan.multiplier)
    return multiplier


def closure_remaining_days(context, port, when):
    """Days ``port`` stays closed counting from ``when``. Zero when it is open."""
    day = absolute_day(when)
    reopening = day
    for plan in context.disruption_plans:
        if plan.target_berth is None or not plan.close_berth:
            continue
        if plan.target_berth.port is not port or plan.start_offset_days is None:
            continue
        if plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days:
            reopening = max(reopening, plan.start_offset_days + plan.duration_days)
    return reopening - day


# --------------------------------------------------------------------------
# Route properties
# --------------------------------------------------------------------------


def route_speed(route):
    speeds = [
        vessel.vessel_class.sailing_speed
        for vessel in route.deployed_vessels
        if vessel.vessel_class is not None and vessel.vessel_class.sailing_speed > 0
    ]
    return min(speeds) if speeds else DEFAULT_SPEED_KNOTS


def route_cycle_days(route):
    speed = route_speed(route)
    return sum(
        segment.associated_leg.sailing_distance / speed / 24.0 + BERTHING_DAYS_PER_CALL
        for segment in route.segments
    )


def route_headway_days(route):
    """Days between consecutive departures of a route from any of its ports."""
    vessel_count = max(1, len(route.deployed_vessels))
    return route_cycle_days(route) / vessel_count


def route_departure_capacity(route):
    capacities = [
        vessel.vessel_class.teu_capacity
        for vessel in route.deployed_vessels
        if vessel.vessel_class is not None
    ]
    return float(max(capacities)) if capacities else 1.0


# --------------------------------------------------------------------------
# Edge graph
# --------------------------------------------------------------------------


def build_edges(context):
    """Every (route, boarding port, alighting port) ride, cached per route set."""
    state = tuple(
        sorted(
            (route.id, len(route.deployed_vessels))
            for route in context.service_routes
        )
    )
    cached = getattr(context, "_adaptive_edges", None)
    if cached is not None and cached[0] == state:
        return cached[1]

    edges = []
    for route in context.service_routes:
        # Skip the alternative routes the default shipping-line strategy spins up
        # while a disruption is active. They borrow vessels from the permanent
        # routes and are dismantled once the disruption ends, so cargo booked
        # onto them is left stranded long after the window closes. A route with
        # no vessels deployed cannot carry anything either.
        if getattr(route, "source_service_route", None) is not None:
            continue
        if not route.deployed_vessels:
            continue
        segments = sorted(route.segments, key=lambda item: item.sequence_index)
        count = len(segments)
        if count == 0:
            continue
        for start in range(count):
            departure_port = segments[start].associated_leg.departure_port
            ridden = []
            for step in range(1, count + 1):
                segment = segments[(start + step - 1) % count]
                ridden.append(segment.associated_leg)
                arrival_port = segment.associated_leg.arrival_port
                if arrival_port is departure_port:
                    continue
                edges.append(
                    _Edge(
                        route,
                        departure_port,
                        arrival_port,
                        start + 1,
                        segments[(start + step - 1) % count].sequence_index,
                        tuple(ridden),
                    )
                )

    outgoing = {}
    for edge in edges:
        outgoing.setdefault(edge.departure_port, []).append(edge)
    context._adaptive_edges = (state, outgoing)
    return outgoing


# --------------------------------------------------------------------------
# Queue pressure
# --------------------------------------------------------------------------


def pending_load_map(context, now):
    """TEU booked but not yet loaded, keyed by (route id, boarding segment).

    This is the term that spreads cargo across paths: as a boarding fills up its
    cost rises, so the next shipment evaluated prefers a different route.
    Recomputed at most every ``WSC_LOAD_REFRESH_HOURS`` because it walks every
    outstanding booking.
    """
    refresh_days = max(0.5, Params.load_refresh_hours()) / 24.0
    day = absolute_day(now)
    cached = getattr(context, "_adaptive_load", None)
    if cached is not None and day - cached[0] < refresh_days:
        return cached[1]

    loads = {}
    for route in context.service_routes:
        for booking in route.associated_bookings:
            shipment = booking.shipment
            if shipment is None or shipment.carrying_vessel is not None:
                continue
            if shipment.current_booking_index != booking.sequence_index:
                continue
            key = (route.id, booking.departure_segment_index)
            loads[key] = loads.get(key, 0.0) + float(shipment.teu_size or 0)

    context._adaptive_load = (day, loads)
    return loads


# --------------------------------------------------------------------------
# Edge cost
# --------------------------------------------------------------------------


def edge_days(context, now, elapsed_before, edge, loads):
    """Expected days to board ``edge`` and ride it to its arrival port."""
    route = edge.route
    headway = route_headway_days(route)

    # Wait for the next departure. Half a headway is the mean wait for a
    # uniformly arriving container.
    elapsed = 0.5 * headway * Params.wait_weight()

    # Queue: how many boardings' worth of cargo is already ahead of us.
    queue_teu = loads.get((route.id, edge.departure_segment_index), 0.0)
    if queue_teu > 0.0:
        cycles = queue_teu / max(1.0, route_departure_capacity(route))
        elapsed += Params.queue_weight() * cycles * headway

    # Transshipment. Charged on every edge: a path of k edges pays k instead of
    # k-1, a constant offset that leaves the ranking of paths unchanged.
    elapsed += Params.transfer_buffer_days()

    # Waiting out a closure at the boarding port.
    boarding_time = now + dt.timedelta(days=elapsed_before + elapsed)
    elapsed += closure_remaining_days(context, edge.departure_port, boarding_time)

    speed = route_speed(route)
    for leg in edge.legs:
        leg_time = now + dt.timedelta(days=elapsed_before + elapsed)
        multiplier = leg_multiplier_at(context, leg, leg_time)
        elapsed += leg.sailing_distance / speed / 24.0 * multiplier
        elapsed += BERTHING_DAYS_PER_CALL
        arrival_time = now + dt.timedelta(days=elapsed_before + elapsed)
        elapsed += closure_remaining_days(context, leg.arrival_port, arrival_time)

    return elapsed


# --------------------------------------------------------------------------
# Path search
# --------------------------------------------------------------------------


def find_fastest_path(context, now, origin, destination):
    """Dijkstra over expected days. Costs depend on time already elapsed, which
    stays consistent because Dijkstra settles nodes in nondecreasing order."""
    outgoing = build_edges(context)
    loads = pending_load_map(context, now)
    max_edges = max(1, Params.max_transfers() + 1)

    best = {origin: 0.0}
    previous = {}
    hops = {origin: 0}
    tiebreak = itertools.count()
    queue = [(0.0, next(tiebreak), origin)]

    while queue:
        elapsed, _, port = heapq.heappop(queue)
        if elapsed > best.get(port, math.inf):
            continue
        if port is destination:
            break
        if hops.get(port, 0) >= max_edges:
            continue
        for edge in outgoing.get(port, ()):
            candidate = elapsed + edge_days(context, now, elapsed, edge, loads)
            if candidate < best.get(edge.arrival_port, math.inf):
                best[edge.arrival_port] = candidate
                previous[edge.arrival_port] = edge
                hops[edge.arrival_port] = hops.get(port, 0) + 1
                heapq.heappush(queue, (candidate, next(tiebreak), edge.arrival_port))

    if destination not in previous:
        return None, math.inf

    path = []
    cursor = destination
    guard = 0
    while cursor is not origin:
        edge = previous.get(cursor)
        if edge is None or guard > max_edges:
            return None, math.inf
        path.append(edge)
        cursor = edge.departure_port
        guard += 1
    path.reverse()
    return path, best[destination]


# --------------------------------------------------------------------------
# Decision points
# --------------------------------------------------------------------------


def assign_bookings(context, now, shipment):
    """Book a shipment onto the path with the lowest expected transit time."""
    if not Params.enabled():
        return None

    origin = shipment.demand.origin_port
    destination = shipment.demand.destination_port

    _detach_bookings(shipment.associated_bookings)
    shipment.associated_bookings = []
    shipment.current_booking_index = None

    if origin is destination:
        return True

    path, expected_days = find_fastest_path(context, now, origin, destination)
    if not path:
        return False

    # A closed destination is only a reason to hold cargo back when the cargo
    # would actually arrive during the closure. Holding a shipment whose transit
    # outlasts the closure wastes the whole window: the port reopens long before
    # the vessel gets there.
    closed_days = closure_remaining_days(context, destination, now)
    if closed_days > 0.0 and expected_days < closed_days - Params.closure_slack_days():
        return False

    for sequence_index, edge in enumerate(path, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=edge.route,
            departure_segment_index=edge.departure_segment_index,
            arrival_segment_index=edge.arrival_segment_index,
        )
        shipment.associated_bookings.append(booking)
        edge.route.associated_bookings.append(booking)

    shipment.current_booking_index = 1
    return True


def select_vessel_for_berth(
    context,
    port,
    waiting_vessels,
    available_berths,
    now,
    waiting_since_by_vessel=None,
):
    """Serve the vessel carrying the most TEU-days of delayed cargo.

    Called rarely (only above the congestion threshold), so this stays simple:
    weight carried cargo by how long the vessel has queued, which prevents an
    empty vessel from displacing a full one while still avoiding starvation.
    """
    if not Params.enabled() or not waiting_vessels:
        return None

    def score(vessel):
        carried = sum(
            float(shipment.teu_size or 0) for shipment in vessel.carried_shipments
        )
        waited_hours = 0.0
        if waiting_since_by_vessel:
            since = waiting_since_by_vessel.get(vessel)
            if since is not None:
                waited_hours = max(0.0, (now - since).total_seconds() / 3600.0)
        return carried * (1.0 + waited_hours / 24.0)

    return max(waiting_vessels, key=score)


def _detach_bookings(bookings):
    for booking in bookings:
        route = booking.service_route
        if route is not None and booking in route.associated_bookings:
            route.associated_bookings.remove(booking)
