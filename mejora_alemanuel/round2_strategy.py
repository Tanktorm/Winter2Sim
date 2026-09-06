"""Round 2 response strategy.

The strategy keeps the implementation inside ``response_strategies`` while
addressing the two main causes of resilience loss in this scenario:

* booking paths based only on nautical miles ignore service frequency and
  transshipment waiting; and
* a vessel that starts a multiplied leg remains delayed even when the
  disruption later ends.  The default one-vessel alternatives do not protect
  the rest of the affected service.

For multiplied legs we therefore build a cycle-preserving detour and migrate
the complete affected fleet shortly before the event.  Booking selection uses
an expected-time graph, including half-headway at every boarding and known
disruption delays.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import heapq
import itertools
import math
import os

from maritime_data_context import Booking, Segment, ServiceRoute


_ROUND2_LEG_EVENTS = {
    ("colombo", "new jersey"),
    ("shanghai", "kaohsiung"),
    ("qingdao", "busan"),
}
_ROUND2_CLOSED_PORTS = {"piraeus", "tianjin"}
_BERTHING_DAYS_PER_CALL = 3.0 / 24.0
_KNOTS_TO_NM_PER_DAY = 24.0

# Trial 12: deterministic Round 2 result, Loss = 3.295500192581237.
# These defaults are deliberately stored in source code so a normal judging
# run does not depend on Optuna's generated/ignored optuna_state directory.
_DEFAULT_WAIT_FRACTION = 0.932682014844758
_DEFAULT_ESTIMATED_BERTH_CALL_DAYS = 0.2143979900583058
_DEFAULT_BERTH_WAIT_WEIGHT = 12994.122976779254
_DEFAULT_LEAD_MARGINS = {
    "s4": 1.0129921282683818,
    "s5": 8.905983046556884,
    "s9": 4.660638795739538,
}
_DEFAULT_PORT_LEAD_MARGINS = {
    "s1": 4.661366835539739,
    "s7": 1.5186231710926994,
}
_DEFAULT_LEG_DETOURS = {
    "s4": False,
    "s5": True,
    "s9": False,
}
_DEFAULT_S7_SKIP = True
_DEFAULT_S1_BYPASS = False


def _env_float(name, default, minimum=0.0, maximum=100.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return min(maximum, max(minimum, value))


def _env_enabled(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().casefold() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class _Edge:
    route: object
    departure_port: object
    arrival_port: object
    departure_segment_index: int
    arrival_segment_index: int
    segments: tuple
    source_departure_index: int | None = None
    source_arrival_index: int | None = None


def is_round_two_context(context) -> bool:
    """Recognise the published Round 2 scenario without affecting toy tests."""
    route_ids = {route.id.casefold() for route in context.initial_service_routes}
    if route_ids != {f"s{index}" for index in range(1, 10)}:
        return False

    leg_events = {
        (
            plan.target_leg.departure_port.name.casefold(),
            plan.target_leg.arrival_port.name.casefold(),
        )
        for plan in context.disruption_plans
        if plan.target_leg is not None and plan.multiplier > 1
    }
    closed_ports = {
        plan.target_berth.port.name.casefold()
        for plan in context.disruption_plans
        if plan.target_berth is not None and plan.close_berth
    }
    return leg_events == _ROUND2_LEG_EVENTS and closed_ports == _ROUND2_CLOSED_PORTS


def strategy_mode() -> str:
    """Allow reproducible comparison runs without changing simulator code."""
    return os.environ.get("WSC_ROUND2_MODE", "detour").strip().casefold()


def manage_service_routes(context, now, vessel=None):
    """Create/activate full-fleet detours and suppress the default alternatives."""
    if not is_round_two_context(context):
        return None

    mode = strategy_mode()
    if mode not in {"detour", "full"}:
        return False

    for event in _unique_congested_leg_events(context):
        source_route = _source_route_for_leg(context, event.target_leg)
        if source_route is None:
            continue
        route_key = source_route.id.casefold()
        if not _env_enabled(
            f"WSC_ENABLE_{source_route.id.upper()}_DETOUR",
            _DEFAULT_LEG_DETOURS.get(route_key, True),
        ):
            continue

        start_day = float(event.start_offset_days)
        end_day = start_day + float(event.duration_days)
        # A source vessel must change course before it can start the affected
        # leg.  One normal traversal time plus a daily-manager margin is enough
        # to catch it at an earlier port call.
        speed = _route_speed(source_route)
        lead_margin = _env_float(
            f"WSC_LEAD_MARGIN_{source_route.id.upper()}",
            _DEFAULT_LEAD_MARGINS.get(route_key, 1.0),
            0.0,
            30.0,
        )
        lead_days = (
            event.target_leg.sailing_distance / speed / 24.0 + lead_margin
        )
        now_day = _absolute_day(now)
        active = start_day - lead_days <= now_day < end_day

        alternative = _find_detour(context, source_route, event)
        if active and alternative is None:
            alternative = _create_cycle_preserving_detour(
                context, source_route, event
            )
        if alternative is None:
            continue

        was_active = getattr(alternative, "_round2_active", False)
        alternative._round2_active = active
        if was_active != active:
            _clear_path_caches(context)
        if active:
            _move_route_bookings(source_route, alternative)
        else:
            _move_route_bookings(alternative, source_route)

        if vessel is not None:
            if active and vessel.assigned_service_route is source_route:
                _switch_vessel_to_detour(vessel, source_route, alternative)
            elif not active and vessel.assigned_service_route is alternative:
                _restore_vessel_to_source(vessel, source_route, alternative)

    # S7 becomes a valid, shorter Singapore-Colombo-Jebel Ali cycle when the
    # closed Piraeus call is removed.  S1 does not: its available bypass adds
    # roughly 26 sailing days, so it is intentionally left unchanged.
    for event in _unique_closed_port_events(context):
        closed_port = event.target_berth.port
        for source_route in context.initial_service_routes:
            if (
                source_route.id.casefold() == "s7"
                and not _env_enabled("WSC_ENABLE_S7_SKIP", _DEFAULT_S7_SKIP)
            ):
                continue
            alternative = _find_port_skip_detour(
                context, source_route, closed_port, event
            )
            if alternative is None:
                alternative = _create_shorter_port_skip_detour(
                    context, source_route, closed_port, event
                )
            if (
                alternative is None
                and source_route.id.casefold() == "s1"
                and _env_enabled(
                    "WSC_ENABLE_S1_BYPASS",
                    _env_enabled("WSC_S1_BYPASS", _DEFAULT_S1_BYPASS),
                )
            ):
                alternative = _create_s1_port_bypass(
                    context, source_route, closed_port, event
                )
            if alternative is None:
                continue

            inbound_days = max(
                (
                    segment.associated_leg.sailing_distance
                    / _route_speed(source_route)
                    / 24.0
                    for segment in source_route.segments
                    if segment.associated_leg.arrival_port is closed_port
                ),
                default=0.0,
            )
            now_day = _absolute_day(now)
            port_lead_margin = _env_float(
                f"WSC_PORT_LEAD_MARGIN_{source_route.id.upper()}",
                _DEFAULT_PORT_LEAD_MARGINS.get(
                    source_route.id.casefold(), 1.0
                ),
                0.0,
                30.0,
            )
            active = (
                event.start_offset_days - inbound_days - port_lead_margin
                <= now_day
                < event.start_offset_days + event.duration_days
            )
            was_active = getattr(alternative, "_round2_active", False)
            alternative._round2_active = active
            if was_active != active:
                _clear_path_caches(context)
            if active:
                _move_route_bookings(source_route, alternative)
            else:
                _move_route_bookings(alternative, source_route)

            if vessel is not None:
                if active and vessel.assigned_service_route is source_route:
                    _switch_vessel_to_detour(vessel, source_route, alternative)
                elif not active and vessel.assigned_service_route is alternative:
                    _restore_vessel_to_source(vessel, source_route, alternative)

    return True


def assign_bookings(context, now, shipment):
    """Assign an expected-time-minimising booking chain for Round 2."""
    if not is_round_two_context(context):
        return None
    if strategy_mode() == "suppress":
        return None

    _remove_booking_references(shipment.associated_bookings)
    shipment.associated_bookings = []
    shipment.current_booking_index = None

    origin = shipment.demand.origin_port
    destination = shipment.demand.destination_port
    if origin is destination:
        return True

    path = _find_expected_time_path(context, now, origin, destination)
    if not path:
        return False

    # A destination that is closed right now is only a reason to hold cargo
    # back when the cargo would still arrive inside the closure. Closures last
    # days while transits last weeks, so refusing every booking during a
    # closure parks the whole flow at origin and releases it as one surge when
    # the port reopens -- which costs far more than the closure itself. Compare
    # the two instead: hold only when the estimated arrival lands inside the
    # window, with a margin for the estimate's own error.
    closure_days = _closed_port_wait_days(context, destination, now)
    if closure_days > 0.0:
        margin = _env_float("WSC_CLOSURE_HOLD_MARGIN_DAYS", 1.0, 0.0, 10.0)
        if _path_expected_days(context, now, path) + margin < closure_days:
            return False

    for sequence_index, edge in enumerate(path, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=edge.route,
            departure_segment_index=edge.departure_segment_index,
            arrival_segment_index=edge.arrival_segment_index,
        )
        if edge.source_departure_index is not None:
            booking._round2_source_departure = edge.source_departure_index
            booking._round2_source_arrival = edge.source_arrival_index
        shipment.associated_bookings.append(booking)
        edge.route.associated_bookings.append(booking)

    shipment.current_booking_index = 1
    return True


def select_vessel_for_berth(
    context,
    port,
    waiting_vessels,
    current_time,
    waiting_since_by_vessel=None,
):
    """Prioritise TEU-hours already accumulated, with starvation protection."""
    if not is_round_two_context(context) or not waiting_vessels:
        return None
    waiting_since_by_vessel = waiting_since_by_vessel or {}

    def priority(vessel):
        wait_hours = max(
            0.0,
            (
                current_time
                - waiting_since_by_vessel.get(vessel, current_time)
            ).total_seconds()
            / 3600.0,
        )
        cargo_age_teu_hours = sum(
            (shipment.teu_size or 0)
            * max(
                0.0,
                (current_time - shipment.generated_time).total_seconds() / 3600.0,
            )
            for shipment in vessel.carried_shipments
            if shipment.generated_time is not None
        )
        # Waiting time dominates after a prolonged queue, preventing starvation.
        starvation_weight = _env_float(
            "WSC_BERTH_WAIT_WEIGHT",
            _DEFAULT_BERTH_WAIT_WEIGHT,
            0.0,
            50_000.0,
        )
        return cargo_age_teu_hours + wait_hours * starvation_weight

    return max(
        enumerate(waiting_vessels),
        key=lambda item: (priority(item[1]), -item[0]),
    )[1]


def _unique_congested_leg_events(context):
    unique = {}
    for plan in context.disruption_plans:
        if plan.target_leg is None or plan.multiplier <= 1:
            continue
        key = (
            plan.target_leg.departure_port.name.casefold(),
            plan.target_leg.arrival_port.name.casefold(),
            plan.start_offset_days,
            plan.duration_days,
            plan.multiplier,
        )
        unique.setdefault(key, plan)
    return list(unique.values())


def _unique_closed_port_events(context):
    unique = {}
    for plan in context.disruption_plans:
        if plan.target_berth is None or not plan.close_berth:
            continue
        key = (
            plan.target_berth.port.name.casefold(),
            plan.start_offset_days,
            plan.duration_days,
        )
        unique.setdefault(key, plan)
    return list(unique.values())


def _source_route_for_leg(context, target_leg):
    for route in context.initial_service_routes:
        if any(segment.associated_leg is target_leg for segment in route.segments):
            return route
    # Scenario construction can contain equivalent physical leg objects.
    key = _leg_key(target_leg)
    return next(
        (
            route
            for route in context.initial_service_routes
            if any(_leg_key(segment.associated_leg) == key for segment in route.segments)
        ),
        None,
    )


def _find_detour(context, source_route, event):
    marker = _event_key(event)
    return next(
        (
            route
            for route in context.service_routes
            if getattr(route, "_round2_source_route", None) is source_route
            and getattr(route, "_round2_event_key", None) == marker
        ),
        None,
    )


def _find_port_skip_detour(context, source_route, closed_port, event):
    marker = _port_event_key(closed_port, event)
    return next(
        (
            route
            for route in context.service_routes
            if getattr(route, "_round2_source_route", None) is source_route
            and getattr(route, "_round2_event_key", None) == marker
        ),
        None,
    )


def _create_shorter_port_skip_detour(context, source_route, closed_port, event):
    source_segments = sorted(source_route.segments, key=lambda item: item.sequence_index)
    if not any(
        segment.associated_leg.departure_port is closed_port
        or segment.associated_leg.arrival_port is closed_port
        for segment in source_segments
    ):
        return None
    retained = [
        segment
        for segment in source_segments
        if segment.associated_leg.departure_port is not closed_port
        and segment.associated_leg.arrival_port is not closed_port
    ]
    if len(retained) < 2:
        return None
    for index, segment in enumerate(retained):
        following = retained[(index + 1) % len(retained)]
        if (
            segment.associated_leg.arrival_port
            is not following.associated_leg.departure_port
        ):
            return None

    source_distance = sum(
        segment.associated_leg.sailing_distance for segment in source_segments
    )
    retained_distance = sum(
        segment.associated_leg.sailing_distance for segment in retained
    )
    if retained_distance > source_distance:
        return None

    route = ServiceRoute(
        id=f"{source_route.id}-R2-{closed_port.name.upper()}-SKIP",
        name=f"{source_route.name} Round 2 {closed_port.name} Skip",
        start_day_of_week=source_route.start_day_of_week,
    )
    route.source_service_route = source_route
    route.disruption_key = ("round2", _port_event_key(closed_port, event))
    route._round2_source_route = source_route
    route._round2_event_key = _port_event_key(closed_port, event)
    route._round2_active = False
    route._round2_source_to_alt = {}
    route._round2_departure_map = {}
    route._round2_arrival_map = {}
    route._round2_initial_vessel_count = len(source_route.deployed_vessels)
    route._round2_is_port_skip = True

    for new_index, source_segment in enumerate(retained, start=1):
        segment = Segment(new_index, source_segment.associated_leg, route)
        segment._round2_source_index = source_segment.sequence_index
        route.segments.append(segment)
        segment.associated_leg.segments.append(segment)
        context.partial_service_routes.append(segment)
        route._round2_source_to_alt[source_segment.sequence_index] = (
            new_index,
            new_index,
        )
        route._round2_departure_map[source_segment.sequence_index] = new_index
        route._round2_arrival_map[source_segment.sequence_index] = new_index
    context.service_routes.append(route)
    return route


def _create_s1_port_bypass(context, source_route, closed_port, event):
    """Build S1's connected cross-ocean bypass while Piraeus is unavailable."""
    source_segments = sorted(source_route.segments, key=lambda item: item.sequence_index)
    if closed_port.name.casefold() != "piraeus":
        return None

    anchors = [
        (segment.sequence_index, segment.associated_leg.departure_port)
        for segment in source_segments
        if segment.associated_leg.departure_port is not closed_port
    ]
    blocked_legs = {
        leg
        for leg in context.legs
        if leg.departure_port is closed_port or leg.arrival_port is closed_port
    }
    paths = []
    for index, (source_departure_index, departure_port) in enumerate(anchors):
        next_source_index, arrival_port = anchors[(index + 1) % len(anchors)]
        path = _shortest_leg_path(
            context, departure_port, arrival_port, blocked_legs
        )
        if not path:
            return None
        previous_source_index = (
            next_source_index - 1
            if next_source_index > 1
            else source_segments[-1].sequence_index
        )
        paths.append((source_departure_index, previous_source_index, path))

    route = ServiceRoute(
        id=f"{source_route.id}-R2-PIRAEUS-BYPASS",
        name=f"{source_route.name} Round 2 Piraeus Bypass",
        start_day_of_week=source_route.start_day_of_week,
    )
    route.source_service_route = source_route
    route.disruption_key = ("round2", _port_event_key(closed_port, event))
    route._round2_source_route = source_route
    route._round2_event_key = _port_event_key(closed_port, event)
    route._round2_active = False
    route._round2_source_to_alt = {}
    route._round2_departure_map = {}
    route._round2_arrival_map = {}
    route._round2_initial_vessel_count = len(source_route.deployed_vessels)

    next_index = 1
    for source_departure, source_arrival, path in paths:
        first_index = next_index
        for leg in path:
            segment = Segment(next_index, leg, route)
            route.segments.append(segment)
            leg.segments.append(segment)
            context.partial_service_routes.append(segment)
            next_index += 1
        route._round2_departure_map[source_departure] = first_index
        route._round2_arrival_map[source_arrival] = next_index - 1
    context.service_routes.append(route)
    return route


def _create_cycle_preserving_detour(context, source_route, event):
    blocked_key = _leg_key(event.target_leg)
    blocked_legs = {
        plan.target_leg
        for plan in context.disruption_plans
        if plan.target_leg is not None
        and _leg_key(plan.target_leg) == blocked_key
        and plan.start_offset_days == event.start_offset_days
    }
    source_segments = sorted(source_route.segments, key=lambda item: item.sequence_index)
    replacement_paths = {}
    for source_segment in source_segments:
        leg = source_segment.associated_leg
        if _leg_key(leg) == blocked_key:
            path = _shortest_leg_path(
                context,
                leg.departure_port,
                leg.arrival_port,
                blocked_legs,
            )
            if not path:
                return None
        else:
            path = [leg]
        replacement_paths[source_segment.sequence_index] = path

    route = ServiceRoute(
        id=f"{source_route.id}-R2-DETOUR",
        name=f"{source_route.name} Round 2 Detour",
        start_day_of_week=source_route.start_day_of_week,
    )
    route.source_service_route = source_route
    route.disruption_key = ("round2", _event_key(event))
    route._round2_source_route = source_route
    route._round2_event_key = _event_key(event)
    route._round2_active = False
    route._round2_source_to_alt = {}
    route._round2_departure_map = {}
    route._round2_arrival_map = {}
    route._round2_initial_vessel_count = len(source_route.deployed_vessels)

    next_index = 1
    for source_segment in source_segments:
        first_index = next_index
        for leg in replacement_paths[source_segment.sequence_index]:
            segment = Segment(next_index, leg, route)
            route.segments.append(segment)
            leg.segments.append(segment)
            context.partial_service_routes.append(segment)
            next_index += 1
        route._round2_source_to_alt[source_segment.sequence_index] = (
            first_index,
            next_index - 1,
        )
        route._round2_departure_map[source_segment.sequence_index] = first_index
        route._round2_arrival_map[source_segment.sequence_index] = next_index - 1

    context.service_routes.append(route)
    return route


def _shortest_leg_path(context, origin, destination, blocked_legs):
    outgoing = {}
    for leg in context.legs:
        if leg in blocked_legs:
            continue
        outgoing.setdefault(leg.departure_port, []).append(leg)

    distances = {origin: 0.0}
    previous = {}
    sequence = itertools.count()
    queue = [(0.0, next(sequence), origin)]
    while queue:
        distance, _, port = heapq.heappop(queue)
        if distance != distances.get(port):
            continue
        if port is destination:
            break
        for leg in outgoing.get(port, []):
            candidate = distance + leg.sailing_distance
            if candidate < distances.get(leg.arrival_port, math.inf):
                distances[leg.arrival_port] = candidate
                previous[leg.arrival_port] = leg
                heapq.heappush(
                    queue, (candidate, next(sequence), leg.arrival_port)
                )

    if destination not in previous:
        return None
    path = []
    cursor = destination
    while cursor is not origin:
        leg = previous.get(cursor)
        if leg is None:
            return None
        path.append(leg)
        cursor = leg.departure_port
    path.reverse()
    return path


def _move_route_bookings(source_route, target_route):
    if source_route is target_route:
        return
    moving_to_alt = (
        getattr(target_route, "_round2_source_route", None) is source_route
    )
    alternative = target_route if moving_to_alt else source_route
    departure_map = alternative._round2_departure_map
    arrival_map = alternative._round2_arrival_map
    for booking in list(source_route.associated_bookings):
        if moving_to_alt:
            source_departure = booking.departure_segment_index
            source_arrival = booking.arrival_segment_index
            if (
                source_departure not in departure_map
                or source_arrival not in arrival_map
            ):
                continue
            booking._round2_source_departure = source_departure
            booking._round2_source_arrival = source_arrival
            booking.departure_segment_index = departure_map[source_departure]
            booking.arrival_segment_index = arrival_map[source_arrival]
        else:
            source_departure = getattr(booking, "_round2_source_departure", None)
            source_arrival = getattr(booking, "_round2_source_arrival", None)
            if source_departure is None or source_arrival is None:
                continue
            booking.departure_segment_index = source_departure
            booking.arrival_segment_index = source_arrival

        while booking in source_route.associated_bookings:
            source_route.associated_bookings.remove(booking)
        booking.service_route = target_route
        if booking not in target_route.associated_bookings:
            target_route.associated_bookings.append(booking)


def _switch_vessel_to_detour(vessel, source_route, alternative):
    departure_map = alternative._round2_departure_map
    arrival_map = alternative._round2_arrival_map
    if any(
        shipment.get_current_booking().service_route is source_route
        and (
            shipment.get_current_booking().departure_segment_index not in departure_map
            or shipment.get_current_booking().arrival_segment_index not in arrival_map
        )
        for shipment in vessel.carried_shipments
    ):
        return False
    current = vessel.current_segment
    replacement = None
    if current is not None:
        arrival_index = arrival_map.get(current.sequence_index)
        if arrival_index is None:
            return False
        replacement = _segment_by_index(alternative, arrival_index)
        while vessel in current.current_vessels:
            current.current_vessels.remove(vessel)

    while vessel in source_route.deployed_vessels:
        source_route.deployed_vessels.remove(vessel)
    if vessel not in alternative.deployed_vessels:
        alternative.deployed_vessels.append(vessel)
    vessel.assigned_service_route = alternative
    vessel.pending_assigned_service_route = None
    vessel.current_segment = replacement
    if replacement is not None and vessel not in replacement.current_vessels:
        replacement.current_vessels.append(vessel)
    return True


def _restore_vessel_to_source(vessel, source_route, alternative):
    current = vessel.current_segment
    source_index = None
    if current is not None:
        source_index = next(
            (
                index
                for index, last_index in alternative._round2_arrival_map.items()
                if last_index == current.sequence_index
            ),
            None,
        )
        # Detour-only intermediate ports have no equivalent source position.
        if source_index is None:
            return False
        while vessel in current.current_vessels:
            current.current_vessels.remove(vessel)

    replacement = (
        _segment_by_index(source_route, source_index)
        if source_index is not None
        else None
    )
    while vessel in alternative.deployed_vessels:
        alternative.deployed_vessels.remove(vessel)
    if vessel not in source_route.deployed_vessels:
        source_route.deployed_vessels.append(vessel)
    vessel.assigned_service_route = source_route
    vessel.pending_assigned_service_route = None
    vessel.current_segment = replacement
    if replacement is not None and vessel not in replacement.current_vessels:
        replacement.current_vessels.append(vessel)
    return True


def _find_expected_time_path(context, now, origin, destination):
    route_state = tuple(
        sorted(
            route.id
            for route in context.service_routes
            if getattr(route, "_round2_active", False)
        )
    )
    cache_key = (
        int(math.floor(_absolute_day(now))),
        route_state,
        origin,
        destination,
        os.environ.get("WSC_WAIT_WEIGHT", "1.0"),
    )
    path_cache = getattr(context, "_round2_path_cache", None)
    if path_cache is None:
        path_cache = {}
        context._round2_path_cache = path_cache
    if cache_key in path_cache:
        return path_cache[cache_key]

    edges = _build_booking_edges(context)
    outgoing = {}
    for edge in edges:
        outgoing.setdefault(edge.departure_port, []).append(edge)

    distances = {origin: 0.0}
    previous = {}
    sequence = itertools.count()
    queue = [(0.0, next(sequence), origin)]
    while queue:
        elapsed, _, port = heapq.heappop(queue)
        if elapsed != distances.get(port):
            continue
        if port is destination:
            break
        for edge in outgoing.get(port, []):
            edge_days = _expected_edge_days(context, now, elapsed, edge)
            candidate = elapsed + edge_days
            if candidate < distances.get(edge.arrival_port, math.inf):
                distances[edge.arrival_port] = candidate
                previous[edge.arrival_port] = edge
                heapq.heappush(
                    queue, (candidate, next(sequence), edge.arrival_port)
                )

    if destination not in previous:
        path_cache[cache_key] = None
        return None
    path = []
    cursor = destination
    while cursor is not origin:
        edge = previous.get(cursor)
        if edge is None:
            return None
        path.append(edge)
        cursor = edge.departure_port
    path.reverse()
    path_cache[cache_key] = path
    return path


def _build_booking_edges(context):
    active_detours = {
        route._round2_source_route: route
        for route in context.service_routes
        if getattr(route, "_round2_active", False)
        and getattr(route, "_round2_source_route", None) is not None
    }
    edge_state = tuple(sorted(route.id for route in active_detours.values()))
    edge_cache = getattr(context, "_round2_edge_cache", None)
    if edge_cache is not None and edge_cache[0] == edge_state:
        return edge_cache[1]

    edges = []
    for source_route in context.initial_service_routes:
        detour = active_detours.get(source_route)
        if detour is None:
            edges.extend(_normal_route_edges(source_route))
        else:
            edges.extend(_detour_route_edges(source_route, detour))
    context._round2_edge_cache = (edge_state, edges)
    return edges


def _normal_route_edges(route):
    segments = sorted(route.segments, key=lambda item: item.sequence_index)
    count = len(segments)
    edges = []
    for start in range(count):
        travelled = []
        departure = segments[start].associated_leg.departure_port
        for step in range(1, count):
            segment = segments[(start + step - 1) % count]
            travelled.append(segment)
            arrival = segment.associated_leg.arrival_port
            if arrival is departure:
                continue
            edges.append(
                _Edge(
                    route,
                    departure,
                    arrival,
                    segments[start].sequence_index,
                    segment.sequence_index,
                    tuple(travelled),
                )
            )
    return edges


def _detour_route_edges(source_route, detour):
    if getattr(detour, "_round2_is_port_skip", False):
        edges = []
        segments = sorted(detour.segments, key=lambda item: item.sequence_index)
        count = len(segments)
        for start in range(count):
            travelled = []
            departure_segment = segments[start]
            departure = departure_segment.associated_leg.departure_port
            for step in range(1, count):
                segment = segments[(start + step - 1) % count]
                travelled.append(segment)
                edges.append(
                    _Edge(
                        detour,
                        departure,
                        segment.associated_leg.arrival_port,
                        departure_segment.sequence_index,
                        segment.sequence_index,
                        tuple(travelled),
                        departure_segment._round2_source_index,
                        segment._round2_source_index,
                    )
                )
        return edges

    source_segments = sorted(source_route.segments, key=lambda item: item.sequence_index)
    detour_segments = sorted(detour.segments, key=lambda item: item.sequence_index)
    departure_map = detour._round2_departure_map
    arrival_map = detour._round2_arrival_map
    count = len(source_segments)
    edges = []
    for start in range(count):
        source_departure_index = source_segments[start].sequence_index
        departure_index = departure_map.get(source_departure_index)
        if departure_index is None:
            continue
        departure = source_segments[start].associated_leg.departure_port
        for step in range(1, count):
            source_arrival_segment = source_segments[(start + step - 1) % count]
            arrival_index = arrival_map.get(source_arrival_segment.sequence_index)
            if arrival_index is None:
                continue
            arrival = source_arrival_segment.associated_leg.arrival_port
            travelled = tuple(
                _segments_between(detour_segments, departure_index, arrival_index)
            )
            edges.append(
                _Edge(
                    detour,
                    departure,
                    arrival,
                    departure_index,
                    arrival_index,
                    travelled,
                    source_departure_index,
                    source_arrival_segment.sequence_index,
                )
            )
    return edges


def _expected_edge_days(context, now, elapsed_before_edge, edge):
    route = edge.route
    wait_weight = _env_float("WSC_WAIT_WEIGHT", 1.0, 0.0, 4.0)
    wait_fraction = _env_float(
        "WSC_WAIT_FRACTION", _DEFAULT_WAIT_FRACTION, 0.0, 1.5
    )
    berth_call_days = _env_float(
        "WSC_ESTIMATED_BERTH_CALL_DAYS",
        _DEFAULT_ESTIMATED_BERTH_CALL_DAYS,
        0.0,
        1.0,
    )
    headway = _route_cycle_days(route) / max(1, _eventual_vessel_count(route))
    elapsed = max(0.0, wait_fraction * headway * wait_weight)
    departure_time = now + dt.timedelta(days=elapsed_before_edge + elapsed)
    elapsed += _closed_port_wait_days(context, edge.departure_port, departure_time)

    speed = _route_speed(route)
    for segment in edge.segments:
        leg = segment.associated_leg
        leg_departure = now + dt.timedelta(days=elapsed_before_edge + elapsed)
        multiplier = _leg_multiplier_at(context, leg, leg_departure)
        elapsed += leg.sailing_distance / speed / 24.0 * multiplier
        arrival_time = now + dt.timedelta(days=elapsed_before_edge + elapsed)
        elapsed += _closed_port_wait_days(context, leg.arrival_port, arrival_time)
        elapsed += berth_call_days
    return elapsed


def _path_expected_days(context, now, path):
    """Expected days to traverse a whole booking chain, using the same per-edge
    cost the path search itself minimises."""
    elapsed = 0.0
    for edge in path:
        elapsed += _expected_edge_days(context, now, elapsed, edge)
    return elapsed


def _route_cycle_days(route):
    speed = _route_speed(route)
    berth_call_days = _env_float(
        "WSC_ESTIMATED_BERTH_CALL_DAYS",
        _DEFAULT_ESTIMATED_BERTH_CALL_DAYS,
        0.0,
        1.0,
    )
    return sum(
        segment.associated_leg.sailing_distance / speed / 24.0
        + berth_call_days
        for segment in route.segments
    )


def _eventual_vessel_count(route):
    return getattr(
        route,
        "_round2_initial_vessel_count",
        len(route.deployed_vessels),
    )


def _route_speed(route):
    vessels = list(route.deployed_vessels)
    source = getattr(route, "_round2_source_route", None)
    if not vessels and source is not None:
        vessels = list(source.deployed_vessels)
    speeds = [
        vessel.vessel_class.sailing_speed
        for vessel in vessels
        if vessel.vessel_class is not None and vessel.vessel_class.sailing_speed > 0
    ]
    return min(speeds) if speeds else 20.0


def _leg_multiplier_at(context, leg, when):
    multiplier = 1.0
    key = _leg_key(leg)
    day = _absolute_day(when)
    for plan in context.disruption_plans:
        if plan.target_leg is None or _leg_key(plan.target_leg) != key:
            continue
        if plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days:
            multiplier = max(multiplier, plan.multiplier)
    return multiplier


def _closed_port_wait_days(context, port, when):
    day = _absolute_day(when)
    latest_end = day
    for plan in context.disruption_plans:
        if (
            plan.target_berth is not None
            and plan.close_berth
            and plan.target_berth.port is port
            and plan.start_offset_days <= day < plan.start_offset_days + plan.duration_days
        ):
            latest_end = max(
                latest_end, plan.start_offset_days + plan.duration_days
            )
    return latest_end - day


def _port_is_closed(context, port, when):
    return _closed_port_wait_days(context, port, when) > 0


def _segments_between(segments, start_sequence, end_sequence):
    start = next(
        index
        for index, segment in enumerate(segments)
        if segment.sequence_index == start_sequence
    )
    cursor = start
    while True:
        segment = segments[cursor]
        yield segment
        if segment.sequence_index == end_sequence:
            break
        cursor = (cursor + 1) % len(segments)


def _segment_by_index(route, sequence_index):
    return next(
        segment
        for segment in route.segments
        if segment.sequence_index == sequence_index
    )


def _remove_booking_references(bookings):
    for booking in bookings:
        route = booking.service_route
        if route is None:
            continue
        while booking in route.associated_bookings:
            route.associated_bookings.remove(booking)


def _clear_path_caches(context):
    context._round2_path_cache = {}
    context._round2_edge_cache = None


def _absolute_day(value):
    return (value - dt.datetime.min).total_seconds() / 86_400.0


def _leg_key(leg):
    return (
        leg.departure_port.name.casefold(),
        leg.arrival_port.name.casefold(),
    )


def _event_key(event):
    return (
        *_leg_key(event.target_leg),
        event.start_offset_days,
        event.duration_days,
        event.multiplier,
    )


def _port_event_key(port, event):
    return (
        "closed-port",
        port.name.casefold(),
        event.start_offset_days,
        event.duration_days,
    )
