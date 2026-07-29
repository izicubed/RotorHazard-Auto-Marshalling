'''
Auto Marshalling plugin for RotorHazard.

Fully local, deterministic marshalling — no API keys, no internet:

  * Real-time guard: during each race it watches the live RSSI traces, injects
    gate passes the node missed (e.g. an unregistered holeshot), re-tunes
    EnterAt/ExitAt live, fixes stuck crossings, and learns per-pilot
    thresholds from saved races and from your manual marshalling.
  * Post-race check: after a race is saved it recomputes each pilot's laps
    from the stored RSSI trace and, when a calibration looks broken, re-tunes
    thresholds against the pilot's other rounds — preview first, nothing is
    written until Apply. Panel on Run and Marshal (native graph integration).
'''

from eventmanager import Evt
from .post_race import (
    MarshalController, EV_GET_STATE, EV_CANCEL, EV_RUN_RACE, EV_RUN_PILOT,
    EV_APPLY, EV_CONTEXT, EV_SET_ENABLED, EV_SET_MODE,
)
from .realtime_guard import RealtimeGuard, EV_RT_GET


def initialize(rhapi):
    controller = MarshalController(rhapi)
    controller.register_blueprint()

    rhapi.events.on(Evt.STARTUP, controller.on_startup)
    rhapi.events.on(Evt.LAPS_SAVE, controller.on_laps_save)
    rhapi.events.on(Evt.LAPS_RESAVE, controller.on_laps_resave)

    rhapi.ui.socket_listen(EV_GET_STATE, controller.on_get_state)
    rhapi.ui.socket_listen(EV_CANCEL, controller.on_cancel)
    rhapi.ui.socket_listen(EV_RUN_RACE, controller.on_run_race)
    rhapi.ui.socket_listen(EV_RUN_PILOT, controller.on_run_pilot)
    rhapi.ui.socket_listen(EV_APPLY, controller.on_apply)
    rhapi.ui.socket_listen(EV_CONTEXT, controller.on_context)
    # panel Enabled/Disabled toggle (master switch: auto flow + realtime guard)
    rhapi.ui.socket_listen(EV_SET_ENABLED, controller.on_set_enabled)
    # panel Classic/YGR switch (which tuning school a race is judged by)
    rhapi.ui.socket_listen(EV_SET_MODE, controller.on_set_mode)

    # Real-time (fully local) in-race marshalling guard:
    # catches missed passes (holeshot) and re-tunes EnterAt/ExitAt live.
    guard = RealtimeGuard(rhapi)
    rhapi.events.on(Evt.STARTUP, lambda _a=None: guard.register_ui(),
                    name='auto_marshal_rt_ui')
    rhapi.events.on(Evt.RACE_START, guard.on_race_start,
                    name='auto_marshal_rt_start')
    rhapi.events.on(Evt.RACE_STOP, guard.on_race_stop,
                    name='auto_marshal_rt_stop')
    # learning loop: stash the decision log with the saved race, then compare
    # it with the operator's manual marshalling to tune per-pilot sensitivity
    rhapi.events.on(Evt.LAPS_SAVE, guard.on_laps_save,
                    name='auto_marshal_rt_save')
    rhapi.events.on(Evt.LAPS_RESAVE, guard.on_laps_resave,
                    name='auto_marshal_rt_resave')
    rhapi.ui.socket_listen(EV_RT_GET, guard.on_rt_get)
    # Theme option (cm_theme) changed: restyle both panels live.
    rhapi.events.on(Evt.OPTION_SET, controller.on_option_set,
                    name='auto_marshal_theme')
    rhapi.events.on(Evt.OPTION_SET, guard.on_option_set,
                    name='auto_marshal_rt_theme')
