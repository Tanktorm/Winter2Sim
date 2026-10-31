"""Strategy functions that contestants may modify.

Each function is called by the simulation at a specific decision point. The
logic lives in ``r3_strategy.py``: a general strategy with no port names, route
ids or dates, so it adapts to any network.  Any internal error makes a function
return ``None``, which hands that decision to ``DefaultStrategy``.
"""

from . import r3_strategy


class UserStrategy:
    @staticmethod
    def select_vessel_for_berth(
        maritime_data_context,
        port,
        waiting_vessels,
        available_berths,
        current_time,
        waiting_since_by_vessel=None,
    ):
        """PortResponseStrategy.

        Select the next vessel to receive a berth at a congested port.
        Return exactly one vessel contained in ``waiting_vessels`` or ``None``
        to use the default policy.
        """
        return r3_strategy.select_vessel_for_berth(
            maritime_data_context,
            port,
            waiting_vessels,
            current_time,
            waiting_since_by_vessel,
        )

    @staticmethod
    def create_alternative_service_routes(context, now, vessel=None):
        """ShippingLineResponseStrategy.

        Builds full-fleet detours around congested legs (existing legs and
        vessels only) and moves vessels onto them at their port calls.
        Returning ``None`` uses the default implementation.
        """
        return r3_strategy.create_alternative_service_routes(context, now, vessel)

    @staticmethod
    def assign_associated_bookings(context, now, shipment):
        """CargoOwnerResponseStrategy.

        Assign the booking chain that arrives earliest.  Return ``True`` when a
        chain was assigned, ``False`` when none exists, ``None`` for default.
        """
        return r3_strategy.assign_associated_bookings(context, now, shipment)

    @staticmethod
    def adjust_bookings_before_cargo_handling(context, now, vessel):
        """CargoOwnerResponseStrategy.

        Rebook carried (and waiting) cargo only when a plan that arrives
        clearly earlier exists.  Return ``True`` after updating the chains.
        """
        return r3_strategy.adjust_bookings_before_cargo_handling(context, now, vessel)
