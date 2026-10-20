"""General response strategy for the hidden final round.

Design rules
------------
* No port names, route ids or calendar dates.  Everything is derived from the
  context at run time, so the same code runs on any network.
* Disruptions are read from ``context.disruption_plans`` (future ones too) and
  evaluated with the exact rule the simulator applies: a daily check where the
  last plan that targets a leg or a berth sets its state.
* Any unexpected error disables the failing component and hands the decision
  back to ``DefaultStrategy``.  An unknown scenario must never crash the run.

Components (every one can be switched off with an environment variable, see
``DEFAULTS``; the values below are the ones that get submitted):

1. Expected-time routing.  A time-dependent Dijkstra that minimises the
   arrival time instead of the distance.  Waiting for a vessel, port calls,
   congested legs (multiplier taken when the leg would be sailed) and closed
   ports (wait until reopening) are part of the cost.
2. Live vessel tracking (``live``).  Every vessel's arrival and departure is
   recorded, its next port calls are projected, and cargo is booked on the
   vessel that really leaves first instead of assuming an average wait.
3. Full-fleet detours (``detours``).  When sailing a congested leg is slower
   than going around it with existing legs, every vessel of the affected
   service takes the detour while the event lasts.
4. Replanning (``replan``).  When a vessel reaches a port, carried cargo (and,
   with ``rebook_waiting``, cargo waiting at that port) is rebooked only when
   the new plan arrives clearly earlier.
"""

from __future__ import annotations

import bisect
import datetime as dt
import heapq
import itertools
import math
import os
import traceback

from maritime_data_context import Booking, Segment, ServiceRoute


# ---------------------------------------------------------------------------
# Configuration.  ``WSC_R3_<NAME>`` overrides a value for experiments only;
# the judged run uses these defaults.
# ---------------------------------------------------------------------------

DEFAULTS = {
    "enabled": 1.0,
    "live": 1.0,
    "detours": 1.0,
    "replan": 1.0,
    "rebook_waiting": 1.0,
    # Average-mode cost, calibrated by the team on Round 2.
    "wait_fraction": 0.78,
    "berth_call_days": 0.334,
    # Replanning only when the new plan is at least this much earlier.
    "replan_threshold_days": 1.0,
    "replan_threshold_avg_days": 2.0,
    # Slack required to catch a vessel (live mode).
    "origin_buffer_days": 0.02,
    "connect_buffer_days": 0.25,
    "connect_buffer_per_day": 0.01,
    # Detours.
    "detour_min_gain_days": 1.0,
    "detour_lead_days": 3.0,
    "detour_max_legs": 8.0,
    # Berth priority when a port is congested.
    "berth_wait_weight": 3612.0,
    # Live mode switches itself off when one-leg predictions miss by more
    # than this on average.
    "live_max_error_days": 1.5,
    "tree_max_age_days": 0.5,
    "horizon_days": 200.0,
    # Even spacing of the vessels of each service (see _phase_delays).  The
    # simulator releases vessels every 7 days whatever the cycle length, which
    # leaves uneven headways; a vessel running too close to the one ahead takes
    # a slightly longer path made of existing legs until the gaps even out.
    # By default this only runs until shortly before the measured period.
    "phase": 1.0,
    "phase_until_day": -1.0,  # -1: WARM_UP_DAYS - 25
    "phase_min_delay_days": 0.75,
    "phase_tolerance_days": 0.3,
    # Keep spacing during the measured period too, more cautiously: cargo on
    # board pays for every adjustment there.
    "phase_measure": 1.0,
    "phase_measure_min_delay_days": 1.5,
    "phase_measure_max_extra_days": 2.5,
    "phase_measure_cooldown_cycles": 0.5,
    # No spacing while a closure/congestion is this many cycles away.
    "phase_guard_before_cycles": 1.0,
    "phase_guard_after_cycles": 1.5,
    # A vessel leaving with at most this fraction of the cargo usually loaded
    # at that call (a follower in a bunch) is spaced with the warm-up limits
    # during the measured period too: delaying it costs little.  0: off.
    "phase_light_ratio": 0.0,
}

_CONFIG = None


def _cfg(name):
    global _CONFIG
    if _CONFIG is None:
        values = dict(DEFAULTS)
        for key in DEFAULTS:
            raw = os.environ.get("WSC_R3_" + key.upper())
            if raw is None:
                continue
            try:
                values[key] = float(raw)
            except ValueError:
                pass
        _CONFIG = values
    return _CONFIG[name]


def _on(name):
    return _cfg(name) > 0.5


_LOGGED = set()


def _log(key, message):
    if key in _LOGGED:
        return
    _LOGGED.add(key)
    print(f"[R3] {message}", flush=True)


_EPOCH = dt.datetime.min
_INF = math.inf


def _day(moment):
    return (moment - _EPOCH).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Public entry points (called from user_strategy.py)
# ---------------------------------------------------------------------------


def create_alternative_service_routes(context, now, vessel=None):
    if not _on("enabled"):
        return None
    state = _state(context)
    if state is None:
        return None
    if not state.failed.get("routes"):
        try:
            state.update(now)
            if vessel is not None:
                state.switch_vessel(vessel)
                if not state.failed.get("phase") and state.is_departure(vessel):
                    try:
                        state.space_vessel(vessel, now)
                    except Exception as error:  # noqa: BLE001
                        state.fail("phase", error)
        except Exception as error:  # noqa: BLE001 - never crash the run
            state.fail("routes", error)
    if vessel is not None and not state.failed.get("tracking"):
        try:
            state.observe_route_call(vessel, now)
        except Exception as error:  # noqa: BLE001
            state.fail("tracking", error)
    # Our detours replace the default one-vessel alternatives.
    return True


def assign_associated_bookings(context, now, shipment):
    if not _on("enabled"):
        return None
    state = _state(context)
    if state is None or state.failed.get("routing"):
        return None
    try:
        return state.assign(now, shipment)
    except Exception as error:  # noqa: BLE001
        state.fail("routing", error)
        return None


def adjust_bookings_before_cargo_handling(context, now, vessel):
    if not _on("enabled"):
        return None
    state = _state(context)
    if state is None:
        return None
    if not state.failed.get("tracking"):
        try:
            state.record_arrival(vessel, now)
        except Exception as error:  # noqa: BLE001
            state.fail("tracking", error)
    if state.failed.get("routing") or state.failed.get("replan"):
        # Plans built by this strategy are kept; the default would rebuild
        # them by distance and undo the detours.
        return True if not state.failed.get("routing") else None
    try:
        state.replan_at_arrival(now, vessel)
    except Exception as error:  # noqa: BLE001
        state.fail("replan", error)
    return True


def select_vessel_for_berth(
    context, port, waiting_vessels, current_time, waiting_since_by_vessel=None
):
    """Serve first the vessel whose cargo has aged most, with a wait guard."""
    if not _on("enabled") or not waiting_vessels:
        return None
    try:
        waiting_since_by_vessel = waiting_since_by_vessel or {}
        weight = _cfg("berth_wait_weight")

        def priority(vessel):
            since = waiting_since_by_vessel.get(vessel, current_time)
            wait_hours = max(0.0, (current_time - since).total_seconds() / 3600.0)
            age = 0.0
            for shipment in vessel.carried_shipments:
                if shipment.generated_time is None:
                    continue
                hours = (current_time - shipment.generated_time).total_seconds() / 3600.0
                age += (shipment.teu_size or 0) * max(0.0, hours)
            return age + wait_hours * weight

        return max(
            enumerate(waiting_vessels), key=lambda item: (priority(item[1]), -item[0])
        )[1]
    except Exception as error:  # noqa: BLE001
        _log("berth", f"berth priority disabled: {error!r}")
        return None


def _state(context):
    state = getattr(context, "_r3_state", None)
    if state is None:
        if getattr(context, "_r3_broken", False):
            return None
        try:
            state = _State(context)
        except Exception as error:  # noqa: BLE001
            context._r3_broken = True
            _log("init", f"strategy disabled, falling back to default: {error!r}")
            traceback.print_exc()
            return None
        context._r3_state = state
    return state


# ---------------------------------------------------------------------------
# Small records
# ---------------------------------------------------------------------------


class _RouteInfo:
    """Static view of one original service route (positions are 0-based)."""

    def __init__(self, route, berth_call_days):
        self.route = route
        self.segs = sorted(route.segments, key=lambda segment: segment.sequence_index)
        self.n = len(self.segs)
        self.legs = [segment.associated_leg for segment in self.segs]
        self.dep_ports = [leg.departure_port for leg in self.legs]
        self.arr_ports = [leg.arrival_port for leg in self.legs]
        speeds = [
            vessel.vessel_class.sailing_speed
            for vessel in route.deployed_vessels
            if vessel.vessel_class is not None and vessel.vessel_class.sailing_speed > 0
        ]
        self.speed = min(speeds) if speeds else 20.0
        self.count = max(1, len(route.deployed_vessels))
        self.nominal = [leg.sailing_distance / (self.speed * 24.0) for leg in self.legs]
        cycle = sum(self.nominal) + self.n * berth_call_days
        self.headway = cycle / self.count
        self.clusters = []


class _Cluster:
    def __init__(self, start, end, paths):
        self.start = start
        self.end = end
        self.paths = paths  # source position -> list of legs
        self.variant = None


class _Track:
    __slots__ = ("mode", "seg", "t", "mult", "route", "pred")

    def __init__(self, mode, seg, t, mult, route, pred=None):
        self.mode = mode  # "port" (between arrival and departure) or "sea"
        self.seg = seg
        self.t = t
        self.mult = mult
        self.route = route
        self.pred = pred  # predicted service start at the next port (sea)


class _Timeline:
    __slots__ = ("calls", "first_event", "version", "dirty")

    def __init__(self, calls, first_event, version):
        # call = (route, position, source position or None, port, service, depart)
        self.calls = calls
        self.first_event = first_event
        self.version = version
        self.dirty = False


class _Pickups:
    __slots__ = ("key", "days", "refs")

    def __init__(self, key, days, refs):
        self.key = key
        self.days = days  # departure position -> sorted service days
        self.refs = refs  # departure position -> [(timeline, call index)]


class _Tree:
    __slots__ = ("origin", "labels", "pred", "valid_until", "paths")

    def __init__(self, origin, labels, pred):
        self.origin = origin
        self.labels = labels
        self.pred = pred
        self.valid_until = -_INF
        self.paths = {}

    def path(self, destination):
        if destination in self.paths:
            return self.paths[destination]
        edges = None
        if destination in self.pred:
            edges = []
            cursor = destination
            while cursor is not self.origin:
                edge = self.pred.get(cursor)
                if edge is None or len(edges) > 64:
                    edges = None
                    break
                edges.append(edge)
                cursor = edge[0]
            if edges is not None:
                edges.reverse()
        self.paths[destination] = edges
        return edges


# Edge tuple stored in a tree: (from_port, source_route, dep_pos, arr_pos, kind)
_RIDE = "ride"
_NORMAL = "normal"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, context):
        self.context = context
        self.failed = {}
        self.berth_call = _cfg("berth_call_days")
        self.info = {}
        for route in context.initial_service_routes:
            info = _RouteInfo(route, self.berth_call)
            if info.n >= 1:
                self.info[route] = info
        self.departures = {}
        for route, info in self.info.items():
            for pos, port in enumerate(info.dep_ports):
                self.departures.setdefault(port, []).append((route, pos))
        self.active = {route: route for route in self.info}
        self.family = {route: [route] for route in self.info}
        self._segs_cache = {}
        self._build_disruptions()
        self.transitions = []
        if _on("detours"):
            self._build_clusters()
        self.transitions = sorted(
            {c.start for info in self.info.values() for c in info.clusters}
            | {c.end for info in self.info.values() for c in info.clusters}
        )
        self.next_transition = self.transitions[0] if self.transitions else _INF
        # tracking and predictions
        self.track = {}
        self.timelines = {}
        self.timeline_counter = itertools.count(1)
        self.pickup_cache = {}
        self.assignment_version = 0
        self.family_cache = (None, {})
        self.port_days = {}
        self.port_days_route = {}
        self.queue_delay = {}
        self.error_sum = 0.0
        self.error_count = 0
        self.live_disabled = not _on("live")
        self.trees = {}
        self.activations = 0
        self.private_variants = {}
        self.legs_from = {}
        for leg in context.legs:
            if leg.departure_port is not leg.arrival_port:
                self.legs_from.setdefault(leg.departure_port, []).append(leg)
        self.phase_end = _phase_end()
        self.last_spacing = {}
        self.depart_load = {}

    # -- failure handling ---------------------------------------------------

    def fail(self, component, error):
        self.failed[component] = True
        if component in ("tracking", "routes"):
            # Predictions can no longer be trusted: fall back to averages.
            self.live_disabled = True
            self.trees.clear()
        _log(
            f"fail-{component}",
            f"component '{component}' disabled after error: {error!r}",
        )
        traceback.print_exc()

    # -- helpers ----------------------------------------------------------

    def segs(self, route):
        cached = self._segs_cache.get(route)
        if cached is None or len(cached) != len(route.segments):
            cached = sorted(route.segments, key=lambda segment: segment.sequence_index)
            self._segs_cache[route] = cached
        return cached

    def src_of(self, route):
        source = getattr(route, "_r3_src", None)
        if source is not None:
            return source
        if route in self.info:
            return route
        return None

    def src_pos_in(self, route, pos):
        """Source position of the segment arriving at a call, None if the
        call is an intermediate port of a detour."""
        if route in self.info:
            return pos
        inverse = getattr(route, "_r3_inv_arr", None)
        if inverse is None:
            return None
        return inverse.get(pos)

    def dep_pos_of(self, route, src_pos):
        if route in self.info:
            return src_pos
        return route._r3_dep_map[src_pos]

    def arr_pos_of(self, route, src_pos):
        if route in self.info:
            return src_pos
        return route._r3_arr_map[src_pos]

    def booking_src(self, booking):
        src = getattr(booking, "_r3_src", None)
        if src is not None:
            return src
        route = booking.service_route
        if route in self.info:
            return (booking.departure_segment_index - 1, booking.arrival_segment_index - 1)
        return None

    # -- disruptions ------------------------------------------------------

    def _build_disruptions(self):
        """Mirror DisruptionManager: daily checks, last plan per target wins."""
        self.plan_count = len(self.context.disruption_plans)
        last_leg_plan = {}
        last_berth_plan = {}
        for plan in self.context.disruption_plans:
            if plan.start_offset_days is None or plan.duration_days is None:
                continue
            if plan.target_leg is not None:
                last_leg_plan[plan.target_leg] = plan
            if plan.target_berth is not None and plan.close_berth:
                last_berth_plan[plan.target_berth] = plan
        self.leg_window = {}
        for leg, plan in last_leg_plan.items():
            start = float(plan.start_offset_days)
            end = start + float(plan.duration_days)
            self.leg_window[leg] = (start, end, float(plan.multiplier))
        self.port_closure = {}
        for port in self.context.ports:
            if not port.berths:
                continue
            windows = []
            for berth in port.berths:
                plan = last_berth_plan.get(berth)
                if plan is None:
                    windows = None
                    break
                start = float(plan.start_offset_days)
                windows.append((start, start + float(plan.duration_days)))
            if not windows:
                continue
            start = max(window[0] for window in windows)
            end = min(window[1] for window in windows)
            if start < end:
                self.port_closure[port] = (start, end)

    def mult_at(self, leg, day):
        window = self.leg_window.get(leg)
        if window is None:
            return 1.0
        check = math.floor(day + 1e-9)
        if window[0] <= check < window[1]:
            return window[2]
        return 1.0

    def reopen(self, port, day):
        window = self.port_closure.get(port)
        if window is None:
            return day
        check = math.floor(day + 1e-9)
        if window[0] <= check < window[1]:
            return max(day, float(math.ceil(window[1] - 1e-9)))
        return day

    def _closed_overlaps(self, port, start, end):
        window = self.port_closure.get(port)
        if window is None:
            return False
        return window[0] < end and start < math.ceil(window[1] - 1e-9)

    def _leg_slow_overlaps(self, leg, start, end):
        window = self.leg_window.get(leg)
        if window is None or window[2] <= 1.0:
            return False
        return window[0] < end and start < math.ceil(window[1] - 1e-9)

    # -- detours ------------------------------------------------------------

    def _build_clusters(self):
        lead = _cfg("detour_lead_days")
        min_gain = _cfg("detour_min_gain_days")
        for route, info in self.info.items():
            if info.n < 2:
                continue
            items = []
            for pos, leg in enumerate(info.legs):
                window = self.leg_window.get(leg)
                if window is None or window[2] <= 1.0:
                    continue
                start_eff = float(math.ceil(window[0] - 1e-9))
                end_eff = float(math.ceil(window[1] - 1e-9))
                if end_eff <= start_eff:
                    continue
                path = self._detour_path(leg, start_eff - lead, end_eff)
                if not path or not self._detour_pays(info, pos, path, window[2], min_gain):
                    continue
                items.append([start_eff - lead, end_eff, {pos}])
            if not items:
                continue
            items.sort(key=lambda item: item[0])
            merged = [items[0]]
            for item in items[1:]:
                last = merged[-1]
                if item[0] < last[1]:
                    last[1] = max(last[1], item[1])
                    last[2] |= item[2]
                else:
                    merged.append(item)
            for start, end, positions in merged:
                paths = {}
                for pos in sorted(positions):
                    path = self._detour_path(info.legs[pos], start, end)
                    if path and self._detour_pays(
                        info, pos, path, self.leg_window[info.legs[pos]][2], min_gain
                    ):
                        paths[pos] = path
                if not paths:
                    continue
                cluster = _Cluster(start, end, paths)
                cluster.variant = self._build_variant(route, info, cluster, len(info.clusters) + 1)
                info.clusters.append(cluster)
                self.family[route].append(cluster.variant)

    def _detour_pays(self, info, pos, path, multiplier, min_gain):
        congested = info.nominal[pos] * multiplier
        detour = sum(leg.sailing_distance for leg in path) / (info.speed * 24.0)
        detour += (len(path) - 1) * self.berth_call
        return detour + min_gain < congested

    def _detour_path(self, blocked_leg, start, end):
        origin = blocked_leg.departure_port
        destination = blocked_leg.arrival_port
        max_legs = int(_cfg("detour_max_legs"))
        outgoing = {}
        for leg in self.context.legs:
            if leg is blocked_leg or leg.departure_port is leg.arrival_port:
                continue
            if self._leg_slow_overlaps(leg, start, end):
                continue
            blocked_port = False
            for port in (leg.departure_port, leg.arrival_port):
                if port is origin or port is destination:
                    continue
                if self._closed_overlaps(port, start, end):
                    blocked_port = True
            if blocked_port:
                continue
            outgoing.setdefault(leg.departure_port, []).append(leg)
        distances = {origin: 0.0}
        hops = {origin: 0}
        previous = {}
        order = itertools.count()
        queue = [(0.0, next(order), origin)]
        while queue:
            distance, _, port = heapq.heappop(queue)
            if distance > distances.get(port, _INF):
                continue
            if port is destination:
                break
            for leg in outgoing.get(port, ()):
                candidate = distance + leg.sailing_distance
                if hops[port] + 1 > max_legs:
                    continue
                if candidate < distances.get(leg.arrival_port, _INF) - 1e-9:
                    distances[leg.arrival_port] = candidate
                    hops[leg.arrival_port] = hops[port] + 1
                    previous[leg.arrival_port] = leg
                    heapq.heappush(queue, (candidate, next(order), leg.arrival_port))
        if destination not in previous:
            return None
        path = []
        cursor = destination
        while cursor is not origin:
            leg = previous[cursor]
            path.append(leg)
            cursor = leg.departure_port
            if len(path) > max_legs:
                return None
        path.reverse()
        return path

    def _build_variant(self, route, info, cluster, number):
        return self._make_variant(route, info, cluster.paths, number, "D", cluster)

    def _make_variant(self, route, info, paths, number, tag, cluster=None):
        existing = {item.id.casefold() for item in self.context.service_routes}
        route_id = f"{route.id}-R3{tag}{number}"
        while route_id.casefold() in existing:
            number += 100
            route_id = f"{route.id}-R3{tag}{number}"
        variant = ServiceRoute(
            id=route_id,
            name=f"{route.name} R3 {'detour' if cluster else 'spacing'} {number}",
            start_day_of_week=route.start_day_of_week,
        )
        variant.source_service_route = route
        variant.disruption_key = ("r3-" + tag, route.id, number)
        dep_map = [0] * info.n
        arr_map = [0] * info.n
        inverse = {}
        legs = []
        for pos in range(info.n):
            dep_map[pos] = len(legs)
            legs.extend(paths.get(pos) or [info.legs[pos]])
            arr_map[pos] = len(legs) - 1
            inverse[len(legs) - 1] = pos
        for index, leg in enumerate(legs, start=1):
            variant.segments.append(Segment(index, leg, variant))
        # Validate before registering anything in the context.
        for index, segment in enumerate(variant.segments):
            following = variant.segments[(index + 1) % len(variant.segments)]
            if segment.associated_leg.arrival_port is not following.associated_leg.departure_port:
                raise ValueError(f"detour for {route.id} is not a connected cycle")
        variant._r3_src = route
        variant._r3_dep_map = dep_map
        variant._r3_arr_map = arr_map
        variant._r3_inv_arr = inverse
        variant._r3_cluster = cluster
        for segment in variant.segments:
            segment.associated_leg.segments.append(segment)
            self.context.partial_service_routes.append(segment)
        self.context.service_routes.append(variant)
        if cluster is not None:
            _log(
                f"detour-{route_id}",
                f"detour {route_id}: positions {sorted(cluster.paths)} "
                f"days {cluster.start:.1f}-{cluster.end:.1f}",
            )
        return variant

    def planned_variant(self, route, day):
        info = self.info.get(route)
        if info is None:
            return route
        for cluster in info.clusters:
            if cluster.start <= day < cluster.end:
                return cluster.variant
        return route

    def update(self, now):
        day = _day(now)
        if len(self.context.disruption_plans) != self.plan_count:
            # Plans added during the run only change travel costs.
            self._build_disruptions()
            self.trees.clear()
        if day < self.next_transition:
            return
        for route in self.info:
            desired = self.planned_variant(route, day)
            if desired is not self.active[route]:
                self._activate(route, desired)
        later = [value for value in self.transitions if value > day]
        self.next_transition = later[0] if later else _INF

    def _activate(self, route, target):
        """Move every booking of the service onto the variant now in force.

        A booking that a vessel is carrying right now stays with that vessel
        until the vessel itself switches, so cargo is never left on a route
        its vessel is not sailing.
        """
        carried = {}
        for member in self.family[route]:
            for vessel in member.deployed_vessels:
                for shipment in vessel.carried_shipments:
                    carried[shipment] = member
        for member in self.family[route]:
            if member is target:
                continue
            for booking in list(member.associated_bookings):
                shipment = booking.shipment
                if (
                    carried.get(shipment) is member
                    and shipment.current_booking_index == booking.sequence_index
                ):
                    continue
                self._move_booking(booking, member, target)
        self.active[route] = target
        self.trees.clear()
        self.pickup_cache.clear()
        self.activations += 1
        _log(f"activate-{self.activations}", f"service {route.id} now runs as {target.id}")

    def _move_booking(self, booking, from_route, to_route):
        src = self.booking_src(booking)
        if src is None:
            return False
        dep_pos, arr_pos = src
        booking.departure_segment_index = self.dep_pos_of(to_route, dep_pos) + 1
        booking.arrival_segment_index = self.arr_pos_of(to_route, arr_pos) + 1
        booking._r3_src = src
        while booking in from_route.associated_bookings:
            from_route.associated_bookings.remove(booking)
        booking.service_route = to_route
        if booking not in to_route.associated_bookings:
            to_route.associated_bookings.append(booking)
        return True

    def switch_vessel(self, vessel):
        route = vessel.assigned_service_route
        source = self.src_of(route) if route is not None else None
        if source is None:
            return
        target = self.active[source]
        if route is target:
            return
        self._switch_to(vessel, route, target)

    def _switch_to(self, vessel, route, target):
        current = vessel.current_segment
        if current is None:
            src_pos = None
        else:
            src_pos = self.src_pos_in(route, current.sequence_index - 1)
            if src_pos is None:
                return False  # intermediate detour port: switch at the next call
        moves = []
        for shipment in vessel.carried_shipments:
            try:
                booking = shipment.get_current_booking()
            except ValueError:
                continue
            if booking.service_route is route:
                if self.booking_src(booking) is None:
                    return False  # cannot map this cargo; keep the vessel as it is
                moves.append(booking)
        for booking in moves:
            self._move_booking(booking, route, target)
        if current is not None:
            while vessel in current.current_vessels:
                current.current_vessels.remove(vessel)
        while vessel in route.deployed_vessels:
            route.deployed_vessels.remove(vessel)
        if vessel not in target.deployed_vessels:
            target.deployed_vessels.append(vessel)
        vessel.assigned_service_route = target
        vessel.pending_assigned_service_route = None
        if current is not None:
            new_segment = self.segs(target)[self.arr_pos_of(target, src_pos)]
            vessel.current_segment = new_segment
            if vessel not in new_segment.current_vessels:
                new_segment.current_vessels.append(vessel)
        track = self.track.get(vessel)
        if track is not None:
            track.route = target
            if track.mode == "port":
                track.seg = vessel.current_segment
        self.assignment_version += 1
        self._mark_dirty(vessel)
        self.trees.clear()
        return True

    # -- even spacing of each service's vessels -------------------------------

    def is_departure(self, vessel):
        track = self.track.get(vessel)
        return track is not None and track.mode == "port"

    def space_vessel(self, vessel, now):
        """At a departure, send a vessel that runs too close behind the one
        ahead of it along a slightly longer path of existing legs."""
        if not _on("phase"):
            return
        day = _day(now)
        measuring = day >= self.phase_end
        if measuring and not _on("phase_measure"):
            return
        route = vessel.assigned_service_route
        if route not in self.info or self.active[route] is not route:
            return
        info = self.info[route]
        if info.n < 2 or len(self.family_vessels(route)) < 2:
            return
        cycle = info.headway * info.count
        # A closure or a congested leg on this service bunches its vessels on
        # purpose; fighting that only delays cargo.  Stay out of it.
        if self._route_disturbed(
            info,
            day - _cfg("phase_guard_before_cycles") * cycle,
            day + _cfg("phase_guard_after_cycles") * cycle + 10.0,
        ):
            return
        current = vessel.current_segment
        position = (current.sequence_index % info.n) if current is not None else 0
        strict = measuring and not self._light_vessel(vessel, route, position)
        if strict:
            last = self.last_spacing.get(vessel)
            if last is not None and day - last < _cfg("phase_measure_cooldown_cycles") * cycle:
                return
        delays = self._phase_delays(route, info, day)
        if not delays:
            return
        need = delays.get(vessel, 0.0)
        minimum = _cfg("phase_measure_min_delay_days") if strict else _cfg("phase_min_delay_days")
        if need < minimum:
            return
        if strict:
            need = min(need, _cfg("phase_measure_max_extra_days"))
        choice = self._delay_path(info, position, need, vessel, day)
        if choice is None:
            return
        if strict and choice[0] > _cfg("phase_measure_max_extra_days"):
            return
        extra, legs = choice
        key = (route, position, tuple(id(leg) for leg in legs))
        variant = self.private_variants.get(key)
        if variant is None:
            variant = self._make_variant(
                route, info, {position: legs}, len(self.private_variants) + 1, "S"
            )
            self.private_variants[key] = variant
            self.family[route].append(variant)
        if self._switch_to(vessel, route, variant):
            self.last_spacing[vessel] = day
            _log(
                f"spacing-{route.id}",
                f"spacing {route.id}: first adjustment on day {day:.1f} "
                f"(+{extra:.1f} days for one vessel)",
            )

    def _light_vessel(self, vessel, route, position):
        ratio = _cfg("phase_light_ratio")
        if ratio <= 0.0:
            return False
        usual = self.depart_load.get((route, position))
        if not usual:
            return False
        return _load(vessel) <= ratio * usual

    def _route_disturbed(self, info, start, end):
        if any(c.start < end and start < c.end for c in info.clusters):
            return True
        for leg in info.legs:
            window = self.leg_window.get(leg)
            if window is not None and window[2] != 1.0 and window[0] < end and start < window[1]:
                return True
        for port in info.dep_ports:
            if self._closed_overlaps(port, start, end):
                return True
        return False

    def _phase_delays(self, source, info, day):
        """Delay (days) that each vessel needs so that the service's calls at
        its first port become evenly spaced, moving vessels only later."""
        vessels = self.family_vessels(source)
        count = len(vessels)
        reference = info.n - 1
        phases = {}
        cycles = []
        for vessel in vessels:
            timeline = self.timeline(vessel, day)
            if timeline is None:
                return None
            times = [call[4] for call in timeline.calls if call[2] == reference]
            if len(times) < 2:
                return None
            phases[vessel] = times[0]
            cycles.append(times[1] - times[0])
        cycles.sort()
        cycle = cycles[len(cycles) // 2]
        if cycle <= 0.0:
            return None
        spacing = cycle / count
        order = sorted(vessels, key=lambda item: phases[item])
        unwrapped = [phases[vessel] for vessel in order]
        best = None
        for rotation in range(count):
            sequence = [
                unwrapped[(rotation + i) % count] + (cycle if rotation + i >= count else 0.0)
                for i in range(count)
            ]
            shift = max(sequence[i] - i * spacing for i in range(count))
            total = sum(shift + i * spacing - sequence[i] for i in range(count))
            if best is None or total < best[0] - 1e-9:
                best = (total, rotation, shift, sequence)
        _, rotation, shift, sequence = best
        return {
            order[(rotation + i) % count]: shift + i * spacing - sequence[i]
            for i in range(count)
        }

    def _delay_path(self, info, position, need, vessel, day):
        """Longer path (up to 4 existing legs) that replaces the next segment
        and adds as close to ``need`` days as possible."""
        leg = info.legs[position]
        origin = leg.departure_port
        destination = leg.arrival_port
        speed = _vessel_speed(vessel, self)
        call = self.berth_call
        base = leg.sailing_distance / (speed * 24.0) * self.mult_at(leg, day)
        found = []

        def explore(port, moment, path):
            if path and port is destination:
                found.append((moment - day - base, list(path)))
                return
            if len(path) == 4:
                return
            for candidate in self.legs_from.get(port, ()):
                if not path and candidate is leg:
                    continue
                if self.mult_at(candidate, moment) > 1.0:
                    continue
                arrival = moment + candidate.sailing_distance / (speed * 24.0)
                following = candidate.arrival_port
                if following is not destination:
                    if self.reopen(following, arrival) > arrival:
                        continue
                    arrival += call
                path.append(candidate)
                explore(following, arrival, path)
                path.pop()

        explore(origin, day, [])
        ceiling = need + max(_cfg("phase_tolerance_days"), 0.25 * need)
        floor = 0.5 * _cfg("phase_min_delay_days")
        usable = [item for item in found if floor <= item[0] <= ceiling]
        if not usable:
            return None
        # Closest to the need; a slight overshoot is corrected by the others.
        return min(usable, key=lambda item: (abs(item[0] - need), len(item[1])))

    # -- vessel tracking ----------------------------------------------------

    def _mark_dirty(self, vessel):
        timeline = self.timelines.get(vessel)
        if timeline is not None:
            timeline.dirty = True

    def observe_route_call(self, vessel, now):
        """Called from create_alternative_service_routes.  The call that comes
        right after an arrival (recorded in adjust_bookings...) is the
        departure: cargo handling has finished and the next leg is set."""
        track = self.track.get(vessel)
        if track is None or track.mode != "port":
            return
        day = _day(now)
        source = self.src_of(vessel.assigned_service_route)
        if source is not None and track.seg is not None:
            src_pos = self.src_pos_in(vessel.assigned_service_route, track.seg.sequence_index - 1)
            duration = max(0.0, day - track.t)
            if duration < 10.0:
                key = (source, src_pos)
                self.port_days[key] = _ema(self.port_days.get(key), duration, 0.2)
                self.port_days_route[source] = _ema(self.port_days_route.get(source), duration, 0.05)
            if src_pos is not None:
                key = (source, (src_pos + 1) % self.info[source].n)
                self.depart_load[key] = _ema(self.depart_load.get(key), _load(vessel), 0.1)
        next_segment = vessel.get_next_segment()
        leg = next_segment.associated_leg
        multiplier = getattr(leg, "sailing_time_multiplier", 1.0) or 1.0
        speed = _vessel_speed(vessel, self)
        anchorage = day + leg.sailing_distance / (speed * 24.0) * multiplier
        predicted = self.reopen(leg.arrival_port, anchorage)
        self.track[vessel] = _Track(
            "sea", next_segment, day, multiplier, vessel.assigned_service_route, predicted
        )
        self._mark_dirty(vessel)

    def record_arrival(self, vessel, now):
        day = _day(now)
        track = self.track.get(vessel)
        port = _arrival_port(vessel)
        if track is not None and track.mode == "sea" and track.pred is not None and port is not None:
            sample = day - track.pred
            delay = self.queue_delay.get(port, 0.0)
            error = abs(sample - delay)
            self.queue_delay[port] = min(3.0, max(-0.5, _ema(self.queue_delay.get(port), sample, 0.05)))
            self.error_sum += min(error, 10.0)
            self.error_count += 1
            if (
                not self.live_disabled
                and self.error_count >= 60
                and self.error_sum / self.error_count > _cfg("live_max_error_days")
            ):
                self.live_disabled = True
                self.trees.clear()
                _log(
                    "live-off",
                    "live predictions disabled: mean one-leg error "
                    f"{self.error_sum / self.error_count:.2f} days",
                )
        self.track[vessel] = _Track("port", vessel.current_segment, day, 1.0, vessel.assigned_service_route)
        self._mark_dirty(vessel)
        if port is not None:
            self.trees.pop(port, None)

    def pdays(self, source, src_pos):
        value = self.port_days.get((source, src_pos))
        if value is None:
            value = self.port_days_route.get(source)
        if value is None:
            value = 0.25
        return value

    def timeline(self, vessel, day):
        timeline = self.timelines.get(vessel)
        if timeline is not None and not timeline.dirty and day <= timeline.first_event + 0.02:
            return timeline
        timeline = self._generate(vessel, day)
        if timeline is None:
            self.timelines.pop(vessel, None)
            return None
        self.timelines[vessel] = timeline
        return timeline

    def _generate(self, vessel, day):
        track = self.track.get(vessel)
        route = vessel.assigned_service_route
        if track is None or route is None:
            return None
        source = self.src_of(route)
        if source is None:
            return None
        speed = _vessel_speed(vessel, self)
        segments = self.segs(route)
        count = len(segments)
        if count == 0:
            return None
        if track.mode == "sea":
            segment = track.seg
            if segment is None or segment.associated_service_route is not route:
                return None
            pos = segment.sequence_index - 1
            leg = segment.associated_leg
            port = leg.arrival_port
            anchorage = track.t + leg.sailing_distance / (speed * 24.0) * track.mult
            service = self.reopen(port, anchorage) + self.queue_delay.get(port, 0.0)
            service = max(service, day + 0.01)
        else:
            segment = track.seg
            if segment is None:
                pos = count - 1
                port = segments[0].associated_leg.departure_port
            else:
                if segment.associated_service_route is not route:
                    return None
                pos = segment.sequence_index - 1
                port = segment.associated_leg.arrival_port
            service = track.t
        route, pos, segments, count = self._model_switch(
            source, route, pos, max(service, day), segments
        )
        src_pos = self.src_pos_in(route, pos)
        depart = service + self.pdays(source, src_pos)
        if track.mode == "port":
            depart = max(depart, day + 0.01)
        calls = [(route, pos, src_pos, port, service, depart)]
        first_event = service if track.mode == "sea" else depart
        horizon = day + _cfg("horizon_days")
        while depart <= horizon and len(calls) < 600:
            pos = (pos + 1) % count
            leg = segments[pos].associated_leg
            anchorage = depart + leg.sailing_distance / (speed * 24.0) * self.mult_at(leg, depart)
            port = leg.arrival_port
            service = self.reopen(port, anchorage) + self.queue_delay.get(port, 0.0)
            route, pos, segments, count = self._model_switch(source, route, pos, service, segments)
            src_pos = self.src_pos_in(route, pos)
            depart = service + self.pdays(source, src_pos)
            calls.append((route, pos, src_pos, port, service, depart))
        return _Timeline(calls, first_event, next(self.timeline_counter))

    def _model_switch(self, source, route, pos, day, segments):
        if not self.info[source].clusters:
            return route, pos, segments, len(segments)
        target = self.planned_variant(source, day)
        if target is route:
            return route, pos, segments, len(segments)
        src_pos = self.src_pos_in(route, pos)
        if src_pos is None:
            return route, pos, segments, len(segments)
        new_segments = self.segs(target)
        return target, self.arr_pos_of(target, src_pos), new_segments, len(new_segments)

    def family_vessels(self, source):
        version, mapping = self.family_cache
        if version != self.assignment_version:
            mapping = {}
            for vessel in self.context.vessels:
                route = vessel.assigned_service_route
                owner = self.src_of(route) if route is not None else None
                if owner is not None:
                    mapping.setdefault(owner, []).append(vessel)
            self.family_cache = (self.assignment_version, mapping)
        return mapping.get(source, ())

    def pickups(self, source, day):
        """Departure position -> sorted predicted pickup days, or None when the
        service cannot be predicted (live mode off or a vessel not tracked)."""
        if self.live_disabled:
            return None
        vessels = self.family_vessels(source)
        if not vessels:
            return None
        timelines = []
        for vessel in vessels:
            timeline = self.timeline(vessel, day)
            if timeline is None:
                return None
            timelines.append(timeline)
        key = tuple(timeline.version for timeline in timelines)
        cached = self.pickup_cache.get(source)
        if cached is not None and cached.key == key:
            return cached
        info = self.info[source]
        count = info.n
        entries = [[] for _ in range(count)]
        for timeline in timelines:
            for index, call in enumerate(timeline.calls):
                src_pos = call[2]
                if src_pos is None:
                    continue
                entries[(src_pos + 1) % count].append((call[4], index, timeline))
        days = []
        refs = []
        for bucket in entries:
            bucket.sort(key=lambda item: (item[0], item[1]))
            days.append([item[0] for item in bucket])
            refs.append([(item[2], item[1]) for item in bucket])
        cached = _Pickups(key, days, refs)
        self.pickup_cache[source] = cached
        return cached

    # -- travel time models ---------------------------------------------------

    def seg_days(self, source, info, pos, day):
        """Sailing days of one source segment departing at ``day``; uses the
        detour path when the service will be on a detour by then."""
        if info.clusters:
            variant = self.planned_variant(source, day)
            if variant is not source:
                path = variant._r3_cluster.paths.get(pos)
                if path:
                    moment = day
                    for index, leg in enumerate(path):
                        moment += leg.sailing_distance / (info.speed * 24.0) * self.mult_at(leg, moment)
                        if index < len(path) - 1:
                            moment = self.reopen(leg.arrival_port, moment) + self.berth_call
                    return moment - day
        return info.nominal[pos] * self.mult_at(info.legs[pos], day)

    def avg_ride(self, source, info, dep_pos, ready, target=None):
        """Average model: wait a fraction of the headway, then sail."""
        moment = ready + _cfg("wait_fraction") * info.headway
        moment = self.reopen(info.dep_ports[dep_pos], moment)
        arrivals = []
        boarding_port = info.dep_ports[dep_pos]
        for step in range(info.n - 1 if info.n > 1 else 0):
            pos = (dep_pos + step) % info.n
            moment += self.seg_days(source, info, pos, moment)
            port = info.arr_ports[pos]
            moment = self.reopen(port, moment) + self.berth_call
            if target is not None:
                if pos == target:
                    return moment
                continue
            if port is not boarding_port:
                arrivals.append((port, moment, pos))
        return None if target is not None else arrivals

    def live_ride(self, info, pickups, dep_pos, earliest, target=None):
        """Board the first vessel that starts service at or after
        ``earliest``.  Returns (pickup_day, arrivals) or (None, None)."""
        days = pickups.days[dep_pos]
        index = bisect.bisect_left(days, earliest)
        if index >= len(days):
            return None, None
        timeline, call_index = pickups.refs[dep_pos][index]
        boarding_port = info.dep_ports[dep_pos]
        expected = dep_pos
        steps = 0
        arrivals = []
        calls = timeline.calls
        for position in range(call_index + 1, len(calls)):
            call = calls[position]
            src_pos = call[2]
            if src_pos is None:
                continue
            if src_pos != expected % info.n:
                break
            expected += 1
            steps += 1
            if steps > info.n - 1:
                break
            if target is not None:
                if src_pos == target:
                    return days[index], call[5]
                continue
            if call[3] is not boarding_port:
                arrivals.append((call[3], call[5], src_pos))
        if target is not None:
            return None, None
        return days[index], arrivals

    def connect_buffer(self, arrival, now_day):
        return _cfg("connect_buffer_days") + _cfg("connect_buffer_per_day") * max(0.0, arrival - now_day)

    def ride_time(self, source, dep_pos, arr_pos, ready, buffer, now_day):
        info = self.info.get(source)
        if info is None:
            return None
        pickups = self.pickups(source, now_day)
        if pickups is not None:
            _, arrival = self.live_ride(info, pickups, dep_pos, ready + buffer, target=arr_pos)
            if arrival is not None:
                return arrival
        return self.avg_ride(source, info, dep_pos, ready, target=arr_pos)

    # -- shortest arrival tree ----------------------------------------------

    def build_tree(self, origin, ready, now_day, origin_buffer, ride=None):
        """Earliest-arrival labels from ``origin`` to every port.

        ``ride`` = (timeline, call index, source, source position of the call):
        lets cargo stay on (or board) that vessel from its current call."""
        labels = {origin: ready}
        pred = {}
        order = itertools.count()
        heap = [(ready, next(order), origin)]
        limit = ready + 500.0
        first_pickup = _INF
        pickup_tables = {}

        def relax(from_port, source, dep_pos, arrivals, kind):
            for port, moment, arr_pos in arrivals:
                if moment > limit:
                    continue
                if moment < labels.get(port, _INF) - 1e-9:
                    labels[port] = moment
                    pred[port] = (from_port, source, dep_pos, arr_pos, kind)
                    heapq.heappush(heap, (moment, next(order), port))

        if ride is not None:
            timeline, call_index, source, src_pos = ride
            info = self.info[source]
            arrivals = []
            expected = (src_pos + 1) % info.n
            steps = 0
            for position in range(call_index + 1, len(timeline.calls)):
                call = timeline.calls[position]
                if call[2] is None:
                    continue
                if call[2] != expected % info.n:
                    break
                expected += 1
                steps += 1
                if steps > info.n - 1:
                    break
                if call[3] is not origin:
                    arrivals.append((call[3], call[5], call[2]))
            relax(origin, source, (src_pos + 1) % info.n, arrivals, _RIDE)

        while heap:
            moment, _, port = heapq.heappop(heap)
            if moment > labels.get(port, _INF) + 1e-9:
                continue
            for source, dep_pos in self.departures.get(port, ()):
                info = self.info[source]
                arrivals = None
                if source in pickup_tables:
                    pickups = pickup_tables[source]
                else:
                    pickups = pickup_tables[source] = self.pickups(source, now_day)
                if pickups is not None:
                    buffer = origin_buffer if port is origin else self.connect_buffer(moment, now_day)
                    pickup, arrivals = self.live_ride(info, pickups, dep_pos, moment + buffer)
                    if pickup is not None and port is origin:
                        first_pickup = min(first_pickup, pickup)
                if arrivals is None:
                    arrivals = self.avg_ride(source, info, dep_pos, moment)
                relax(port, source, dep_pos, arrivals, _NORMAL)
        tree = _Tree(origin, labels, pred)
        tree.valid_until = first_pickup
        return tree

    def origin_tree(self, origin, day):
        tree = self.trees.get(origin)
        if tree is not None and day < tree.valid_until and day >= tree.labels[origin]:
            return tree
        buffer = _cfg("origin_buffer_days")
        tree = self.build_tree(origin, day, day, buffer)
        valid = min(
            math.floor(day + 1e-9) + 1.0,
            day + _cfg("tree_max_age_days"),
            tree.valid_until - buffer,
        )
        tree.valid_until = max(valid, day + 1e-6)
        self.trees[origin] = tree
        return tree

    # -- bookings ---------------------------------------------------------

    def make_booking(self, shipment, sequence, source, dep_pos, arr_pos):
        route = self.active[source]
        booking = Booking(
            sequence_index=sequence,
            shipment=shipment,
            service_route=route,
            departure_segment_index=self.dep_pos_of(route, dep_pos) + 1,
            arrival_segment_index=self.arr_pos_of(route, arr_pos) + 1,
        )
        booking._r3_src = (dep_pos, arr_pos)
        route.associated_bookings.append(booking)
        return booking

    def assign(self, now, shipment):
        self.update(now)
        day = _day(now)
        _remove_references(shipment.associated_bookings)
        shipment.associated_bookings = []
        shipment.current_booking_index = None
        origin = shipment.demand.origin_port
        destination = shipment.demand.destination_port
        if origin is destination:
            return True
        edges = self.origin_tree(origin, day).path(destination)
        if not edges:
            return False
        shipment.associated_bookings = [
            self.make_booking(shipment, sequence, edge[1], edge[2], edge[3])
            for sequence, edge in enumerate(edges, start=1)
        ]
        shipment.current_booking_index = 1
        return True

    # -- replanning at arrival --------------------------------------------

    def replan_at_arrival(self, now, vessel):
        if not _on("replan"):
            return
        self.update(now)
        day = _day(now)
        route = vessel.assigned_service_route
        source = self.src_of(route) if route is not None else None
        if source is None or self.active[source] is not route:
            return
        info = self.info[source]
        port = _arrival_port(vessel)
        current = vessel.current_segment
        if current is None:
            src_pos = info.n - 1
        else:
            src_pos = self.src_pos_in(route, current.sequence_index - 1)
        if src_pos is None or port is None:
            return
        timeline = self.timeline(vessel, day)
        if timeline is None or not timeline.calls or timeline.calls[0][3] is not port:
            return
        live = self.pickups(source, day) is not None
        threshold = _cfg("replan_threshold_days") if live else _cfg("replan_threshold_avg_days")
        ride = (timeline, 0, source, src_pos)

        carried = [
            shipment
            for shipment in vessel.carried_shipments
            if shipment.demand.destination_port is not port
        ]
        if carried:
            discharge = timeline.calls[0][5]
            tree = None
            keep_cache = {}
            for shipment in carried:
                try:
                    booking = shipment.get_current_booking()
                except ValueError:
                    continue
                if booking.service_route is not route:
                    continue
                later = _later_bookings(shipment, booking)
                signature = self._signature([booking] + later)
                if signature is None:
                    continue
                if signature in keep_cache:
                    keep = keep_cache[signature]
                else:
                    keep = self._eval_carried(booking, later, timeline, src_pos, info, day)
                    keep_cache[signature] = keep
                if keep is None:
                    continue
                if tree is None:
                    tree = self.build_tree(
                        port, discharge, day, self.connect_buffer(discharge, day), ride
                    )
                best = tree.labels.get(shipment.demand.destination_port)
                if best is None or best >= keep - threshold:
                    continue
                edges = tree.path(shipment.demand.destination_port)
                if edges:
                    self._replace_carried(shipment, booking, later, edges, route, src_pos)

        if not (live and _on("rebook_waiting")) or self.live_disabled:
            return
        waiting = [
            shipment
            for shipment in list(port.shipments_in_storage)
            if shipment.carrying_vessel is None
            and shipment.current_booking_index is not None
            and shipment.demand.destination_port is not port
        ]
        if not waiting:
            return
        tree = None
        keep_cache = {}
        for shipment in waiting:
            try:
                booking = shipment.get_current_booking()
            except ValueError:
                continue
            later = _later_bookings(shipment, booking)
            signature = self._signature([booking] + later)
            if signature is None:
                continue
            if signature in keep_cache:
                keep = keep_cache[signature]
            else:
                keep = self._eval_waiting(booking, later, port, day)
                keep_cache[signature] = keep
            if keep is None:
                continue
            if tree is None:
                tree = self.build_tree(port, day, day, -1e-6, ride)
            best = tree.labels.get(shipment.demand.destination_port)
            if best is None or best >= keep - threshold:
                continue
            edges = tree.path(shipment.demand.destination_port)
            if edges:
                self._replace_waiting(shipment, booking, edges)

    def _signature(self, bookings):
        parts = []
        for booking in bookings:
            source = self.src_of(booking.service_route) if booking.service_route is not None else None
            src = self.booking_src(booking)
            if source is None or src is None:
                return None
            parts.append((id(source), src[0], src[1]))
        return tuple(parts)

    def _eval_carried(self, booking, later, timeline, src_pos, info, day):
        _, arr_pos = self.booking_src(booking)
        if arr_pos == src_pos:
            moment = timeline.calls[0][5]
        else:
            moment = None
            expected = (src_pos + 1) % info.n
            steps = 0
            for call in timeline.calls[1:]:
                if call[2] is None:
                    continue
                if call[2] != expected % info.n:
                    break
                expected += 1
                steps += 1
                if steps > info.n - 1:
                    break
                if call[2] == arr_pos:
                    moment = call[5]
                    break
            if moment is None:
                return None
        return self._eval_rest(later, moment, day)

    def _eval_waiting(self, booking, later, port, day):
        source = self.src_of(booking.service_route)
        dep_pos, arr_pos = self.booking_src(booking)
        info = self.info.get(source)
        if info is None or info.dep_ports[dep_pos] is not port:
            return None
        moment = self.ride_time(source, dep_pos, arr_pos, day, -1e-6, day)
        if moment is None:
            return None
        return self._eval_rest(later, moment, day)

    def _eval_rest(self, later, moment, day):
        for booking in later:
            source = self.src_of(booking.service_route)
            dep_pos, arr_pos = self.booking_src(booking)
            moment = self.ride_time(
                source, dep_pos, arr_pos, moment, self.connect_buffer(moment, day), day
            )
            if moment is None:
                return None
        return moment

    def _replace_carried(self, shipment, booking, later, edges, route, src_pos):
        dep_pos, _ = self.booking_src(booking)
        if edges[0][4] == _RIDE:
            new_arrival = edges[0][3]
            rest = edges[1:]
        else:
            new_arrival = src_pos
            rest = edges
        kept = [item for item in shipment.associated_bookings if item.sequence_index < booking.sequence_index]
        _remove_references(later)
        booking.arrival_segment_index = self.arr_pos_of(route, new_arrival) + 1
        booking._r3_src = (dep_pos, new_arrival)
        new = [
            self.make_booking(shipment, booking.sequence_index + offset, edge[1], edge[2], edge[3])
            for offset, edge in enumerate(rest, start=1)
        ]
        shipment.associated_bookings = kept + [booking] + new
        shipment.current_booking_index = booking.sequence_index

    def _replace_waiting(self, shipment, booking, edges):
        kept = [item for item in shipment.associated_bookings if item.sequence_index < booking.sequence_index]
        replaced = [item for item in shipment.associated_bookings if item.sequence_index >= booking.sequence_index]
        _remove_references(replaced)
        new = [
            self.make_booking(shipment, booking.sequence_index + offset, edge[1], edge[2], edge[3])
            for offset, edge in enumerate(edges)
        ]
        shipment.associated_bookings = kept + new
        shipment.current_booking_index = booking.sequence_index


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _phase_end():
    value = _cfg("phase_until_day")
    if value >= 0.0:
        return value
    try:
        from config.simulation_config import WARM_UP_DAYS

        return float(WARM_UP_DAYS) - 25.0
    except Exception:  # noqa: BLE001
        return 115.0


def _ema(previous, sample, alpha):
    if previous is None:
        return sample
    return previous + alpha * (sample - previous)


def _vessel_speed(vessel, state):
    vessel_class = vessel.vessel_class
    if vessel_class is not None and vessel_class.sailing_speed > 0:
        return vessel_class.sailing_speed
    source = state.src_of(vessel.assigned_service_route)
    info = state.info.get(source)
    return info.speed if info is not None else 20.0


def _load(vessel):
    return sum(shipment.teu_size or 0 for shipment in vessel.carried_shipments)


def _arrival_port(vessel):
    segment = vessel.current_segment
    if segment is not None and segment.associated_leg is not None:
        return segment.associated_leg.arrival_port
    route = vessel.assigned_service_route
    if route is None or not route.segments:
        return None
    first = min(route.segments, key=lambda item: item.sequence_index)
    return first.associated_leg.departure_port


def _later_bookings(shipment, booking):
    return sorted(
        (item for item in shipment.associated_bookings if item.sequence_index > booking.sequence_index),
        key=lambda item: item.sequence_index,
    )


def _remove_references(bookings):
    for booking in bookings:
        route = booking.service_route
        if route is None:
            continue
        while booking in route.associated_bookings:
            route.associated_bookings.remove(booking)
