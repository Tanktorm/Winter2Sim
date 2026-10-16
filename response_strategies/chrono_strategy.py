"""Earliest-arrival routing over the real sailing calendar.

The simulator's default routes cargo by nautical miles. Strategies that improve
on it, this project's earlier ones included, replace distance with *expected*
time and model the wait for a ship as half a headway. That average is where the
network's time actually goes: half-headway on the trunk services is 2.5-3.3
days, so a two-transshipment path pays six to nine days of expected waiting
against roughly twelve days of sailing.

But the wait is not really an average. Every service has a fixed rotation, a
published start offset and its vessels spread evenly around the cycle, so the
departure times from each port are *known*. This strategy computes them and
searches for the path that arrives earliest in that calendar, rather than the
path that is shortest on average. Choosing a connection that leaves in four
hours instead of one that leaves in four days is a real saving, and it is
invisible to any model that only knows the mean.

Two properties follow, and both matter for scoring:

* it lowers transport time in ordinary conditions, not only under disruption,
  and the challenge's resilience loss pays credit for every period that beats
  the undisrupted baseline; and
* it contains no port names, route ids or dates. The calendar and the
  disruptions are both read from the context at run time, so the same code
  applies to a scenario it has never seen.

Everything is a knob read from the environment so a batch runner can calibrate
without editing this file.
"""

from __future__ import annotations

import datetime as dt
import heapq
import itertools
import math
import os

from maritime_data_context import Booking


SIMULATION_EPOCH = dt.datetime.min
DEFAULT_SPEED_KNOTS = 20.0


def _env_float(name, default, minimum=0.0, maximum=1000.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return min(maximum, max(minimum, value))


def _env_int(name, default, minimum=1, maximum=8):
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = int(default)
    return min(maximum, max(minimum, value))


def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


class Params:
    """Calibration knobs, re-read per call so a runner can sweep them."""

    @staticmethod
    def enabled():
        return _env_flag("WSC_CHRONO_ENABLED", True)

    @staticmethod
    def berth_call_days():
        """Time a vessel spends alongside per port call."""
        return _env_float("WSC_BERTH_CALL_DAYS", 0.125, 0.0, 1.0)

    @staticmethod
    def connect_buffer_days():
        """Minimum slack between discharge and the next departure.

        A connection that leaves the same minute cargo lands is not really
        catchable; this is how much margin a transshipment needs to be counted.
        """
        return _env_float("WSC_CONNECT_BUFFER_HOURS", 8.0, 0.0, 72.0) / 24.0

    @staticmethod
    def transfer_penalty_days():
        """Extra cost charged per transshipment beyond its measured time.

        Each transfer is also a place where cargo can be stranded when
        something slips, which the arrival-time arithmetic alone does not see.
        """
        return _env_float("WSC_TRANSFER_PENALTY_HOURS", 6.0, 0.0, 72.0) / 24.0

    @staticmethod
    def max_legs():
        return _env_int("WSC_MAX_LEGS", 4, 1, 8)

    @staticmethod
    def closure_hold_margin_days():
        return _env_float("WSC_CLOSURE_HOLD_MARGIN_DAYS", 1.0, 0.0, 30.0)

    @staticmethod
    def keep_plan_in_transit():
        """Whether to hold the assigned chain instead of letting the default
        strategy rebuild it with its distance-only metric."""
        return _env_flag("WSC_KEEP_PLAN", True)


# --------------------------------------------------------------------------
# Absolute time
# --------------------------------------------------------------------------


def absolute_day(when):
    """Simulation day of a datetime; disruption offsets share this origin."""
    return (when - SIMULATION_EPOCH).total_seconds() / 86400.0


def leg_multiplier_at(context, leg, day):
    multiplier = 1.0
    for plan in context.disruption_plans:
        if plan.target_leg is not leg or plan.start_offset_days is None:
            continue
        if plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days:
            multiplier = max(multiplier, plan.multiplier)
    return multiplier


def closure_end_day(context, port, day):
    """Day the port reopens, or ``day`` when it is already open."""
    reopening = day
    for plan in context.disruption_plans:
        if plan.target_berth is None or not plan.close_berth:
            continue
        if plan.target_berth.port is not port or plan.start_offset_days is None:
            continue
        if plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days:
            reopening = max(reopening, plan.start_offset_days + plan.duration_days)
    return reopening


# --------------------------------------------------------------------------
# The sailing calendar
# --------------------------------------------------------------------------


class _Timetable:
    """Departure phases and headway of every service, derived from the network.

    A route's vessels are spread evenly around its rotation, so departures from
    a given segment repeat every ``cycle / vessels`` days. The phase is the
    route's published start offset plus the time the rotation takes to reach
    that segment.
    """

    __slots__ = ("headway", "phase_by_segment", "speed", "capacity")

    def __init__(self, route, berth_call_days):
        segments = sorted(route.segments, key=lambda item: item.sequence_index)
        speeds = [
            vessel.vessel_class.sailing_speed
            for vessel in route.deployed_vessels
            if vessel.vessel_class is not None and vessel.vessel_class.sailing_speed > 0
        ]
        self.speed = min(speeds) if speeds else DEFAULT_SPEED_KNOTS
        capacities = [
            vessel.vessel_class.teu_capacity
            for vessel in route.deployed_vessels
            if vessel.vessel_class is not None
        ]
        self.capacity = float(max(capacities)) if capacities else 1.0

        elapsed = float(getattr(route, "start_day_of_week", 0.0) or 0.0)
        self.phase_by_segment = {}
        for segment in segments:
            self.phase_by_segment[segment.sequence_index] = elapsed
            leg = segment.associated_leg
            elapsed += leg.sailing_distance / self.speed / 24.0 + berth_call_days

        cycle = elapsed - float(getattr(route, "start_day_of_week", 0.0) or 0.0)
        vessel_count = max(1, len(route.deployed_vessels))
        self.headway = max(cycle / vessel_count, 1e-6)

    def next_departure(self, segment_index, not_before_day):
        """First departure from ``segment_index`` at or after the given day."""
        phase = self.phase_by_segment.get(segment_index)
        if phase is None:
            return not_before_day
        if not_before_day <= phase:
            return phase
        periods = math.ceil((not_before_day - phase) / self.headway)
        return phase + periods * self.headway


def timetables(context):
    berth_call_days = Params.berth_call_days()
    state = (
        tuple(sorted((r.id, len(r.deployed_vessels)) for r in context.service_routes)),
        round(berth_call_days, 6),
    )
    cached = getattr(context, "_chrono_timetables", None)
    if cached is not None and cached[0] == state:
        return cached[1]
    tables = {
        route.id: _Timetable(route, berth_call_days)
        for route in context.service_routes
        if route.segments
    }
    context._chrono_timetables = (state, tables)
    return tables


# --------------------------------------------------------------------------
# Bookable rides
# --------------------------------------------------------------------------


class _Ride:
    """Board a route at one port, leave it at a later one."""

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


def rides_by_port(context):
    """Every bookable ride, grouped by boarding port and cached per fleet state."""
    state = tuple(
        sorted((route.id, len(route.deployed_vessels)) for route in context.service_routes)
    )
    cached = getattr(context, "_chrono_rides", None)
    if cached is not None and cached[0] == state:
        return cached[1]

    outgoing = {}
    for route in context.service_routes:
        # Alternative routes the shipping-line strategy spins up during a
        # disruption borrow vessels and are dismantled afterwards, so cargo
        # booked onto them can be stranded once the window closes.
        if getattr(route, "source_service_route", None) is not None:
            continue
        if not route.deployed_vessels:
            continue
        segments = sorted(route.segments, key=lambda item: item.sequence_index)
        count = len(segments)
        for start in range(count):
            departure_port = segments[start].associated_leg.departure_port
            ridden = []
            for step in range(1, count + 1):
                segment = segments[(start + step - 1) % count]
                ridden.append(segment.associated_leg)
                arrival_port = segment.associated_leg.arrival_port
                if arrival_port is departure_port:
                    continue
                outgoing.setdefault(departure_port, []).append(
                    _Ride(
                        route,
                        departure_port,
                        arrival_port,
                        segments[start].sequence_index,
                        segment.sequence_index,
                        tuple(ridden),
                    )
                )
    context._chrono_rides = (state, outgoing)
    return outgoing


# --------------------------------------------------------------------------
# Earliest arrival
# --------------------------------------------------------------------------


def ride_arrival_day(context, ride, table, ready_day):
    """When cargo ready at ``ready_day`` would land, taking this ride.

    Returns ``(arrival_day, departure_day)``. Disruptions are evaluated at the
    moment each leg is actually sailed, so a window that will have closed by
    the time the ship gets there costs nothing.
    """
    departure_day = table.next_departure(ride.departure_segment_index, ready_day)
    # A closed boarding port cannot be worked until it reopens.
    reopening = closure_end_day(context, ride.departure_port, departure_day)
    if reopening > departure_day:
        departure_day = table.next_departure(ride.departure_segment_index, reopening)

    day = departure_day
    berth_call_days = Params.berth_call_days()
    for leg in ride.legs:
        day += leg.sailing_distance / table.speed / 24.0 * leg_multiplier_at(
            context, leg, day
        )
        day = max(day, closure_end_day(context, leg.arrival_port, day))
        day += berth_call_days
    return day, departure_day


def find_earliest_path(context, now, origin, destination):
    """Time-dependent Dijkstra minimising the day cargo lands at ``destination``.

    Waiting for a later departure never makes an earlier one unavailable, so
    arrival time is non-decreasing in ready time and Dijkstra stays valid.
    """
    outgoing = rides_by_port(context)
    tables = timetables(context)
    max_legs = Params.max_legs()
    connect_buffer = Params.connect_buffer_days()
    transfer_penalty = Params.transfer_penalty_days()

    start_day = absolute_day(now)
    best = {origin: start_day}
    previous = {}
    legs_used = {origin: 0}
    order = itertools.count()
    queue = [(start_day, next(order), origin)]

    while queue:
        ready_day, _, port = heapq.heappop(queue)
        if ready_day > best.get(port, math.inf):
            continue
        if port is destination:
            break
        if legs_used.get(port, 0) >= max_legs:
            continue
        for ride in outgoing.get(port, ()):
            table = tables.get(ride.route.id)
            if table is None:
                continue
            # Cargo cannot make a connection that leaves the instant it lands.
            earliest = ready_day if port is origin else ready_day + connect_buffer
            arrival_day, _ = ride_arrival_day(context, ride, table, earliest)
            # The penalty ranks paths without corrupting the arrival estimate.
            ranked = arrival_day + (0.0 if port is origin else transfer_penalty)
            if ranked < best.get(ride.arrival_port, math.inf):
                best[ride.arrival_port] = ranked
                previous[ride.arrival_port] = ride
                legs_used[ride.arrival_port] = legs_used.get(port, 0) + 1
                heapq.heappush(queue, (ranked, next(order), ride.arrival_port))

    if destination not in previous:
        return None, math.inf

    path = []
    cursor = destination
    guard = 0
    while cursor is not origin:
        ride = previous.get(cursor)
        if ride is None or guard > max_legs:
            return None, math.inf
        path.append(ride)
        cursor = ride.departure_port
        guard += 1
    path.reverse()
    return path, best[destination] - start_day


# --------------------------------------------------------------------------
# Decision points
# --------------------------------------------------------------------------


def assign_bookings(context, now, shipment):
    """Book the chain that lands the cargo earliest in the sailing calendar."""
    if not Params.enabled():
        return None

    origin = shipment.demand.origin_port
    destination = shipment.demand.destination_port

    _detach(shipment.associated_bookings)
    shipment.associated_bookings = []
    shipment.current_booking_index = None

    if origin is destination:
        return True

    path, transit_days = find_earliest_path(context, now, origin, destination)
    if not path:
        return False

    # Holding cargo for a closed destination only pays when it would still
    # arrive inside the window. Closures last days and transits last weeks, so
    # a blanket refusal parks the flow at origin and releases it as one surge.
    day = absolute_day(now)
    closed_until = closure_end_day(context, destination, day)
    if closed_until > day:
        remaining = closed_until - day
        if transit_days + Params.closure_hold_margin_days() < remaining:
            return False

    for sequence_index, ride in enumerate(path, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=ride.route,
            departure_segment_index=ride.departure_segment_index,
            arrival_segment_index=ride.arrival_segment_index,
        )
        shipment.associated_bookings.append(booking)
        ride.route.associated_bookings.append(booking)

    shipment.current_booking_index = 1
    return True


def adjust_bookings_before_cargo_handling(context, now, vessel):
    """Hold the assigned chain instead of letting the default rebuild it.

    Returning ``None`` here hands the decision to the default strategy, whose
    distance-only rebuild undoes the schedule-aware chain this strategy chose.
    """
    if not Params.enabled() or not Params.keep_plan_in_transit():
        return None
    return True


def select_vessel_for_berth(
    context,
    port,
    waiting_vessels,
    available_berths,
    now,
    waiting_since_by_vessel=None,
):
    """Serve whoever carries the most delayed cargo.

    Called only above the congestion threshold, which this network rarely
    reaches, so it stays deliberately simple: weight carried TEU by how long
    the vessel has queued, so a full ship wins but an empty one is not starved.
    """
    if not Params.enabled() or not waiting_vessels:
        return None

    def score(vessel):
        carried = sum(
            float(shipment.teu_size or 0)
            for shipment in getattr(vessel, "carried_shipments", ())
        )
        waited_hours = 0.0
        if waiting_since_by_vessel:
            since = waiting_since_by_vessel.get(vessel)
            if since is not None:
                waited_hours = max(0.0, (now - since).total_seconds() / 3600.0)
        return carried * (1.0 + waited_hours / 24.0)

    return max(waiting_vessels, key=score)


def _detach(bookings):
    for booking in bookings:
        route = booking.service_route
        if route is not None and booking in route.associated_bookings:
            route.associated_bookings.remove(booking)
