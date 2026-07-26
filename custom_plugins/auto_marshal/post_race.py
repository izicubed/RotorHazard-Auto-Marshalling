'''
Post-race controller for the Auto Marshalling plugin.

Fully local, deterministic — no API keys, no internet:

  * Trigger on Evt.LAPS_SAVE with a cancellable 5-second countdown before the
    automatic run; re-processing guards; LAPS_RESAVE guard.
  * Per pilot: validate RSSI, recompute laps with the pilot's STORED
    EnterAt/ExitAt, and only when self-check/diagnostics show a bad calibration
    re-tune thresholds against the seat's other rounds and the trace itself.
    Threshold changes stay in the saved race; live node thresholds are never
    touched.
  * Hard Minimum-Lap-Time invariant (cluster resolution by highest peak),
    Minimum-First-Crossing, late-lap rules, protected manual/API laps.
  * Historical baseline (median / MAD) used for warnings only.
  * Per-pilot + per-race self-check, dry-run and safe-mode, blockers/warnings,
    JSON report (logged + broadcast).
  * A snapshot-driven panel shown on Run (in the plugin dock) and Marshal
    (above the RSSI graph), with cancel + manual (race / per-pilot) triggers.
'''

import json
import logging
from time import monotonic

import gevent
from flask import Blueprint

from eventmanager import Evt
from RHUI import UIField, UIFieldType, UIFieldSelectOption

logger = logging.getLogger(__name__)

PLUGIN_ID = 'auto_marshal'

# LapSource (mirror Database.LapSource)
SRC_REALTIME, SRC_MANUAL, SRC_RECALC, SRC_AUTOMATIC, SRC_API = 0, 1, 2, 3, 4
TIMER_SOURCES = (SRC_REALTIME, SRC_RECALC, SRC_AUTOMATIC)
PROTECTED_SOURCES = (SRC_MANUAL, SRC_API)
MARSHAL_RSSI = 0

# Options (the historical `cm_` prefix is kept on purpose: settings and the
# learned per-pilot sensitivity factors survive the upgrade from the plugin's
# claude_marshal era)
OPT_ENABLED = 'cm_enabled'
OPT_DRY_RUN = 'cm_dry_run'
OPT_SAFE = 'cm_safe_mode'
OPT_COUNTDOWN = 'cm_countdown'
OPT_STRICT_MIN_LAP = 'cm_strict_min_lap'
OPT_HISTORY_MIN = 'cm_history_min_laps'
OPT_DEL_MANUAL = 'cm_allow_delete_manual'
OPT_DEL_API = 'cm_allow_delete_api'
OPT_REPORT_ATTR = 'cm_report_attr'
OPT_THEME = 'cm_theme'

# Socket events (server <-> browser)
STATE_EVENT = 'auto_marshal_state'
EV_GET_STATE = 'auto_marshal_get_state'
EV_CANCEL = 'auto_marshal_cancel'
EV_RUN_RACE = 'auto_marshal_run_race'
EV_RUN_PILOT = 'auto_marshal_run_pilot'
EV_APPLY = 'auto_marshal_apply'
EV_CONTEXT = 'auto_marshal_context'
EV_SET_ENABLED = 'auto_marshal_set_enabled'

# History thresholds (spec §9.5)
HIST_FAST_WARN = 0.75
HIST_FAST_BLOCK = 0.55
HIST_SLOW_WARN = 1.75
HIST_Z_WARN = 3.0


class MarshalController:
    def __init__(self, rhapi):
        self._rhapi = rhapi
        self._t0 = 0.0
        self._state = {'phase': 'idle', 'pilots': []}
        self._pending = {}          # race_id -> {'cancelled': bool}
        self._processing_races = set()
        self._processed_races = set()
        self._processing_pilots = set()
        self._pending_apply = None   # computed-but-not-yet-saved results
        self._auto_race_id = None    # the heat just saved from /run (auto flow)
        self._cancel_races = set()   # race_ids the user asked to stop mid-run

    # ------------------------------------------------------------------ setup

    def register_blueprint(self):
        bp = Blueprint(PLUGIN_ID, __name__, static_folder='static',
                       static_url_path='/auto_marshal/static')
        self._rhapi.ui.blueprint_add(bp)

    def on_startup(self, _args=None):
        self._register_ui()

    def _register_ui(self):
        ui = self._rhapi.ui
        fields = self._rhapi.fields
        ui.register_panel(PLUGIN_ID, 'Auto Marshalling', 'settings', order=0)

        def opt(name, label, ftype, value, desc, options=None):
            kw = dict(name=name, label=label, field_type=ftype, value=value, desc=desc)
            if options is not None:
                kw['options'] = options
            fields.register_option(UIField(**kw), PLUGIN_ID)

        opt(OPT_ENABLED, 'Auto-marshal after each race is saved',
            UIFieldType.CHECKBOX, True,
            'Master switch for the automatic run (with countdown). Manual runs '
            'from the panel work regardless.')
        opt(OPT_DRY_RUN, 'Dry-run (preview only, do not save)',
            UIFieldType.CHECKBOX, False,
            'When on, computes and reports results without writing them. Turn on '
            'if you only want a preview.')
        opt(OPT_SAFE, 'Safe mode (do not save when a blocker is raised)',
            UIFieldType.CHECKBOX, True,
            'If any pilot fails validation/self-check, the race is left '
            'untouched and flagged for manual review.')
        opt(OPT_COUNTDOWN, 'Auto-run countdown (seconds)',
            UIFieldType.BASIC_INT, 5,
            'Cancellable countdown shown before the automatic run starts.')
        opt(OPT_STRICT_MIN_LAP, 'Strict Minimum Lap Time',
            UIFieldType.CHECKBOX, True,
            'Guarantee no active lap is shorter than the Minimum Lap Time.')
        opt(OPT_HISTORY_MIN, 'History minimum laps for baseline',
            UIFieldType.BASIC_INT, 8,
            'Fewer valid historical laps than this → history is warning-only.')
        opt(OPT_DEL_MANUAL, 'Allow auto-deleting manual laps',
            UIFieldType.CHECKBOX, False,
            'Off by default so hand-judged laps are never removed silently.')
        opt(OPT_DEL_API, 'Allow auto-deleting API laps',
            UIFieldType.CHECKBOX, False, 'Off by default.')
        opt(OPT_REPORT_ATTR, 'Store report on the saved race',
            UIFieldType.CHECKBOX, True,
            'Best-effort: attach the run report to the race as an attribute.')
        opt(OPT_THEME, 'Panel theme', UIFieldType.SELECT, 'dark',
            'Colour scheme of the marshalling panels on the Run and Marshal '
            'pages. Auto follows each viewer\'s browser/OS light-dark '
            'preference. Applies live, no reload needed.', options=[
                UIFieldSelectOption('dark', 'Dark'),
                UIFieldSelectOption('light', 'Light'),
                UIFieldSelectOption('auto', 'Auto (follow browser/OS)')])

        self._register_loader(ui)

    def _register_loader(self, ui):
        # Inject the panel front-end only on the pages the spec calls for:
        # Run (in the plugin dock) and Marshal (above the RSSI graph).
        fields = self._rhapi.fields
        loader = '<script src="/auto_marshal/static/auto_marshal.js"></script>'
        for page in ('run', 'marshal'):
            panel = 'auto_marshal_load_' + page
            ui.register_panel(panel, 'Auto Marshalling', page, order=0)
            ui.register_markdown(panel, 'auto_marshal_boot_' + page, loader)
            fields.register_option(UIField(
                name='_auto_marshal_boot_' + page, label='', value='',
                field_type=UIFieldType.TEXT, private=True, desc=loader), panel)

    # -------------------------------------------------------------- option io

    def _opt(self, name, default=None):
        try:
            val = self._rhapi.db.option(name)
        except Exception:
            return default
        return default if val is None or val == '' else val

    def _opt_bool(self, name, default=False):
        return self._opt(name, default) in (True, 1, '1', 'true', 'True')

    def _opt_int(self, name, default):
        try:
            return int(float(self._opt(name, default)))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------- rh access

    @property
    def _rhdata(self):
        return self._rhapi.db._racecontext.rhdata

    def _time_format(self):
        try:
            return self._rhapi.db._racecontext.serverconfig.get_item('UI', 'timeFormat') \
                or '{m}:{s}.{d}'
        except Exception:
            return '{m}:{s}.{d}'

    def _notify(self, message):
        try:
            self._rhapi.ui.message_notify(message)
        except Exception:
            pass

    # --------------------------------------------------------- state / snapshot

    def _push(self):
        if self._state.get('phase') == 'running':
            self._state['elapsed'] = round(monotonic() - self._t0, 1)
        self._state['theme'] = self._opt(OPT_THEME, 'dark')
        self._state['enabled'] = self._opt_bool(OPT_ENABLED, True)
        try:
            self._rhapi.ui.socket_broadcast(STATE_EVENT, self._state)
        except Exception:
            logger.exception('auto_marshal state broadcast failed')

    def on_get_state(self, _data=None):
        self._state['theme'] = self._opt(OPT_THEME, 'dark')
        self._state['enabled'] = self._opt_bool(OPT_ENABLED, True)
        try:
            self._rhapi.ui.socket_send(STATE_EVENT, self._state)
        except Exception:
            logger.exception('auto_marshal state send failed')

    def on_set_enabled(self, data):
        '''Panel Enabled/Disabled toggle: master switch for all automatic
        marshalling (the post-race flow AND the real-time in-race guard).
        Manual runs from the Marshal page keep working either way.'''
        val = (data or {}).get('enabled')
        if val is None:
            return
        try:
            self._rhapi.db.option_set(OPT_ENABLED, '1' if val else '0')
        except Exception:
            logger.exception('auto_marshal enable toggle failed')
            return
        self._notify('Auto Marshalling {}'.format(
            'enabled' if val else 'disabled — no automatic runs or in-race '
            'corrections until re-enabled'))
        logger.info('auto_marshal %s via panel toggle',
                    'enabled' if val else 'disabled')
        self._push()

    def on_option_set(self, args):
        '''Re-broadcast the panel state when the theme option changes so all
        open pages restyle immediately.'''
        if (args or {}).get('option') == OPT_THEME:
            self._push()

    def _seat_entry(self, seat):
        for p in self._state.get('pilots', []):
            if p['seat'] == seat:
                return p
        return None

    def _mode(self):
        # Fallbacks match the field defaults: auto-save on, safe-mode on.
        # (RotorHazard's option() returns None for a field defaulting to False,
        # so the fallback here is what actually applies when unset.)
        return {'dry_run': self._opt_bool(OPT_DRY_RUN, False),
                'safe_mode': self._opt_bool(OPT_SAFE, True)}

    # ----------------------------------------------------------- event hooks

    def on_laps_save(self, args):
        if not self._opt_bool(OPT_ENABLED, True):
            return
        race_id = (args or {}).get('race_id')
        if race_id is None:
            return
        if race_id in self._processing_races or race_id in self._pending:
            return
        # This is the current heat just saved from /run — mark it so the /run
        # panel shows only this (auto) flow, never a previously selected race.
        self._auto_race_id = race_id
        gevent.spawn(self._countdown_then_run, race_id)

    def on_laps_resave(self, args):
        # Guard only: our own save triggers this; never recurse.
        return

    def on_cancel(self, data):
        '''Stop a run — during the countdown or while it's processing pilots.'''
        race_id = (data or {}).get('race_id')
        if race_id is None:
            # generic stop: cancel every pending countdown and running race
            for st in self._pending.values():
                st['cancelled'] = True
            self._cancel_races.update(self._processing_races)
            return
        if race_id in self._pending:
            self._pending[race_id]['cancelled'] = True
        self._cancel_races.add(race_id)

    def _origin(self, race_id):
        # 'auto' only for the heat just saved from /run (or manual re-runs of it);
        # everything else is a marshal-page action and hidden on /run.
        return 'auto' if race_id == self._auto_race_id else 'manual'

    def on_run_race(self, data):
        race_id = (data or {}).get('race_id')
        if race_id is None:
            meta = self._latest_race()
            race_id = meta.id if meta else None
        if race_id is None:
            return
        if race_id in self._processing_races:
            return
        gevent.spawn(self._run_race_job, race_id, 'manual')

    def on_run_pilot(self, data):
        data = data or {}
        race_id = data.get('race_id')
        pilotrace_id = data.get('pilotrace_id')
        if race_id is None or pilotrace_id is None:
            return
        if pilotrace_id in self._processing_pilots or race_id in self._processing_races:
            return
        gevent.spawn(self._run_race_job, race_id, 'manual', pilotrace_id)

    def on_context(self, data):
        '''Marshal page asks which race it is viewing so the panel can list its
        pilots for individual runs.'''
        data = data or {}
        heat_id = data.get('heat_id')
        round_no = data.get('round')
        meta = self._resolve_race(heat_id, round_no)
        if not meta or self._state.get('phase') in ('running', 'waiting_countdown'):
            return
        # Already showing this race — keep it (so a finished result and its Apply
        # button survive switching pilots within the same heat/round).
        if self._state.get('race_id') == meta.id:
            return
        # A different heat/round was selected: drop any un-applied preview and
        # show the newly selected race's pilots so the panel tracks the menu.
        self._pending_apply = None
        self._state = self._context_snapshot(meta)
        self.on_get_state()

    # ------------------------------------------------------------- countdown

    def _countdown_then_run(self, race_id):
        seconds = max(0, self._opt_int(OPT_COUNTDOWN, 5))
        self._pending[race_id] = {'cancelled': False}
        meta = self._safe(lambda: self._rhdata.get_savedRaceMeta(race_id))
        heat_name, rnd = self._race_labels(meta)
        try:
            for remaining in range(seconds, 0, -1):
                if self._pending[race_id]['cancelled']:
                    break
                self._state = {'phase': 'waiting_countdown', 'race_id': race_id,
                               'heat': heat_name, 'round': rnd, 'countdown': remaining,
                               'countdown_total': seconds, 'cancellable': True,
                               'origin': 'auto', 'mode': self._mode(), 'pilots': []}
                self._push()
                gevent.sleep(1)
            if self._pending[race_id]['cancelled']:
                self._state = {'phase': 'cancelled', 'race_id': race_id,
                               'heat': heat_name, 'round': rnd, 'pilots': [],
                               'origin': 'auto',
                               'message': 'Marshalling cancelled (CANCELLED_BY_USER)'}
                self._push()
                logger.info('auto_marshal race %s CANCELLED_BY_USER', race_id)
                return
        finally:
            self._pending.pop(race_id, None)
        self._run_race_job(race_id, 'auto')

    # --------------------------------------------------------------- pipeline

    def _run_race_job(self, race_id, source, only_pilotrace=None):
        self._processing_races.add(race_id)
        self._cancel_races.discard(race_id)
        if only_pilotrace is not None:
            self._processing_pilots.add(only_pilotrace)
        self._t0 = monotonic()
        mode = self._mode()
        origin = self._origin(race_id)
        try:
            meta = self._rhdata.get_savedRaceMeta(race_id)
            if not meta:
                return
            start_time = meta.start_time
            heat = self._rhdata.get_heat(meta.heat_id) if meta.heat_id else None
            heat_name, rnd = self._race_labels(meta)
            fmt = self._safe(lambda: self._rhdata.get_raceFormat(meta.format_id))
            timefmt = self._time_format()
            opts = {
                'min_lap_ms': self._opt_int('MinLapSec', 0) * 1000,
                'min_lap_behavior': self._opt_int('MinLapBehavior', 0),
                'min_first_crossing_ms': self._opt_int('MinFirstCrossingSec', 0) * 1000,
                'strict_min_lap': self._opt_bool(OPT_STRICT_MIN_LAP, True),
                'del_manual': self._opt_bool(OPT_DEL_MANUAL, False),
                'del_api': self._opt_bool(OPT_DEL_API, False),
                'history_min': self._opt_int(OPT_HISTORY_MIN, 8),
            }

            runs = self._rhdata.get_savedPilotRaces_by_savedRaceMeta(race_id) or []
            if only_pilotrace is not None:
                runs = [r for r in runs if r.id == only_pilotrace]
            siblings = self._sibling_info(meta.heat_id, race_id)

            self._state = {
                'phase': 'running', 'race_id': race_id, 'heat': heat_name,
                'round': rnd, 'origin': origin, 'cancellable': True,
                'mode': mode, 'total': len(runs), 'done': 0, 'elapsed': 0.0,
                'pilots': [{'seat': r.node_index, 'pilotrace_id': r.id,
                            'callsign': self._callsign(r.pilot_id), 'status': 'wait',
                            'warnings': [], 'blockers': []} for r in runs],
            }
            self._push()

            reports = []
            cancelled = False
            for r in runs:
                if race_id in self._cancel_races:   # user pressed Stop
                    cancelled = True
                    break
                entry = self._seat_entry(r.node_index)
                if entry:
                    entry['status'] = 'run'
                self._push()
                pt = monotonic()
                rep = self._process_pilot(r, meta, start_time, fmt, opts,
                                          siblings, timefmt)
                rep['seconds'] = round(monotonic() - pt, 1)
                reports.append(rep)
                if entry:
                    entry.update(status=('err' if rep['blockers'] else
                                         ('warn' if rep['warnings'] else 'ok')),
                                 enter_at=rep.get('enter_at'), exit_at=rep.get('exit_at'),
                                 laps=rep.get('active_laps'), changed=rep.get('changed'),
                                 warnings=rep['warnings'], blockers=rep['blockers'],
                                 reasoning=rep.get('reasoning'), seconds=rep['seconds'])
                self._state['done'] += 1
                self._push()

            if cancelled:
                # Stopped mid-run: discard the partial preview, don't offer Apply.
                self._pending_apply = None
                self._state['phase'] = 'cancelled'
                self._state['origin'] = origin
                self._state['can_apply'] = False
                self._state['message'] = 'Marshalling stopped'
                self._push()
                logger.info('auto_marshal race %s stopped by user', race_id)
                return

            # Compute-and-preview: never write during the run. Buffer the
            # non-blocked results so the operator can review, then Apply.
            applicable = [{'pilotrace_id': r['_run'].id,
                           'node_index': r['_run'].node_index,
                           'pilot_id': r['_run'].pilot_id,
                           'laps': r['_laps'], 'enter_at': r['enter_at'],
                           'exit_at': r['exit_at']}
                          for r in reports if r.get('_laps') is not None]
            self._pending_apply = ({'race_id': race_id, 'items': applicable}
                                   if applicable else None)
            phase = 'complete'
            self._finish(reports, phase, saved=False,
                         elapsed=round(monotonic() - self._t0, 1),
                         can_apply=bool(applicable))
            self._state['origin'] = origin
            self._push()
            self._write_report(meta, reports, phase, False)
        except Exception as ex:
            logger.exception('auto_marshal run failed')
            self._state = {'phase': 'error', 'origin': origin, 'pilots': [],
                           'message': str(ex)}
            self._push()
        finally:
            self._processing_races.discard(race_id)
            self._cancel_races.discard(race_id)
            if only_pilotrace is not None:
                self._processing_pilots.discard(only_pilotrace)

    def _finish(self, reports, phase, saved, elapsed, can_apply=False):
        deleted = sum(r.get('deleted_count', 0) for r in reports)
        changed = sum(1 for r in reports if r.get('changed'))
        self._state['phase'] = phase
        self._state['elapsed'] = elapsed
        self._state['saved'] = saved
        self._state['can_apply'] = can_apply
        self._state['summary'] = {
            'pilots_total': len(reports), 'pilots_changed': changed,
            'laps_deleted': deleted,
            'warnings': sum(len(r['warnings']) for r in reports),
            'blockers': sum(len(r['blockers']) for r in reports),
        }
        self._push()

    # ----------------------------------------------------------- per pilot

    def _process_pilot(self, run, meta, start_time, fmt, opts, siblings,
                       timefmt):
        rep = {'pilotrace_id': run.id, 'seat': run.node_index,
               'callsign': self._callsign(run.pilot_id),
               'warnings': [], 'blockers': [], 'changed': False,
               'deleted_count': 0, '_run': run, '_laps': None,
               'enter_at': run.enter_at, 'exit_at': run.exit_at}

        # 1) validate RSSI history (thresholds are NOT a blocker — invalid stored
        #    thresholds are exactly what the re-tune is for)
        vals, times, verr = self._parse_history(run)
        if verr:
            rep['blockers'].append(verr)
            return rep

        expected = siblings.get(run.node_index, {}).get('laps')
        sib_thr = siblings.get(run.node_index, {}).get('thresholds')
        baseline = self._history_baseline(run.pilot_id, meta.id, opts)
        rmin, rmax = min(vals), max(vals)

        # 2) recompute with the stored thresholds when they are usable. Invalid
        #    stored thresholds (enter <= exit, or missing) are themselves a
        #    reason to recalibrate — treat as bad and skip the stored recompute.
        stored_ok = (run.enter_at is not None and run.exit_at is not None
                     and run.enter_at > run.exit_at)
        if stored_ok:
            enter, exit_at = run.enter_at, run.exit_at
            laps = self._finalize(self._recalc(vals, times, start_time, enter, exit_at),
                                  run, opts, fmt, timefmt, rep)
            bad = self._calibration_bad(laps, baseline, expected, exit_at, enter,
                                        rmin, rmax, sib_thr)
        else:
            enter, exit_at = run.enter_at, run.exit_at   # possibly invalid / None
            laps = []
            bad = True

        # 3) broken calibration: local re-tune against this seat's other rounds
        #    and the trace itself. Whichever of stored/re-tuned is more
        #    plausible wins, so good data is never regressed and broken/invalid
        #    calibrations get repaired.
        if bad:
            fixed = self._local_retune(vals, times, start_time, run, fmt, opts,
                                       timefmt, expected, baseline, sib_thr,
                                       laps)
            if fixed:
                laps, enter, exit_at = fixed
                rep['changed'] = True
                rep['reasoning'] = ('Re-tuned locally: thresholds matched '
                                    'against this seat\'s other rounds.')
            else:
                rep['blockers'].append('BAD_CALIBRATION_UNRESOLVED')

        rep['enter_at'], rep['exit_at'] = enter, exit_at

        # 4) protected-lap min-lap conflict blocker
        if opts['strict_min_lap']:
            for l in laps:
                if (not l['deleted']) and l['source'] in PROTECTED_SOURCES \
                        and l.get('lap_time') and l['_idx'] > 0 \
                        and l['lap_time'] < opts['min_lap_ms']:
                    rep['blockers'].append('PROTECTED_LAP_UNDER_MIN_LAP')
                    break

        # 5) history warnings
        self._history_check(laps, baseline, opts, rep)

        # 6) self-check
        self._self_check(laps, enter, exit_at, opts, rep)

        # Scored lap count excludes the holeshot (first crossing = lap 0), which
        # RotorHazard does not count unless the format's start behavior is
        # "First lap counts" (StartBehavior.FIRST_LAP == 1).
        crossings = sum(1 for l in laps if not l['deleted'])
        sb = getattr(fmt, 'start_behavior', 0) if fmt else 0
        has_holeshot = (sb != 1) and crossings >= 1
        rep['crossings'] = crossings
        rep['active_laps'] = crossings - (1 if has_holeshot else 0)
        rep['holeshot'] = has_holeshot
        if not rep['blockers']:
            rep['_laps'] = laps
        else:
            # keep original laps untouched on blocker
            rep['_laps'] = None
        # count deletions we introduced
        rep['deleted_count'] = max(rep['deleted_count'],
                                   sum(1 for l in laps if l['deleted'] and l['source'] == SRC_RECALC))
        return rep

    # --------------------------------------------------------- lap algorithm

    def _recalc(self, vals, times, start_time, enter_at, exit_at):
        '''RSSI crossing detection; timestamp = midpoint of the peak.'''
        laps = []
        crossing = False
        peak = 0
        pf = pl = 0
        for rssi, t in zip(vals, times):
            if (not crossing) and rssi >= enter_at:
                crossing = True
                peak, pf, pl = rssi, t, t
                continue
            if crossing:
                if rssi > peak:
                    peak, pf, pl = rssi, t, t
                elif rssi == peak:
                    pl = t
                if rssi < exit_at:
                    ts = (((pf + pl) / 2) - start_time) * 1000
                    if ts > 0:
                        laps.append({'lap_time_stamp': ts, 'source': SRC_RECALC,
                                     'peak_rssi': peak, 'deleted': False})
                    crossing = False
                    peak = 0
        if crossing:
            ts = (((pf + pl) / 2) - start_time) * 1000
            if ts > 0:
                laps.append({'lap_time_stamp': ts, 'source': SRC_RECALC,
                             'peak_rssi': peak, 'deleted': False})
        return laps

    def _finalize(self, laps, run, opts, fmt, timefmt, rep):
        import RHUtils
        min_first = opts['min_first_crossing_ms']
        min_lap = opts['min_lap_ms']

        # min first crossing
        for l in laps:
            if l['lap_time_stamp'] < min_first:
                l['deleted'] = True

        # protected manual/API laps
        for lap in (self._rhdata.get_savedRaceLaps_by_savedPilotRace(run.id) or []):
            if lap.source in PROTECTED_SOURCES:
                laps.append({'lap_time_stamp': lap.lap_time_stamp, 'source': lap.source,
                             'peak_rssi': lap.peak_rssi, 'deleted': bool(lap.deleted)})

        laps.sort(key=lambda l: l['lap_time_stamp'])

        # minimum lap time: resolve clusters (keep highest peak)
        if min_lap and opts['strict_min_lap']:
            self._enforce_min_lap(laps, min_lap, opts, rep)

        # late laps (count-down formats)
        self._apply_late(laps, fmt)

        # incremental lap times
        self._update_incremental(laps, timefmt, RHUtils)
        return laps

    def _enforce_min_lap(self, laps, min_lap, opts, rep):
        def protected(l):
            return l['source'] in PROTECTED_SOURCES

        def deletable(l):
            if not protected(l):
                return True
            if l['source'] == SRC_MANUAL:
                return opts['del_manual']
            return opts['del_api']

        for _ in range(len(laps) + 2):
            act = sorted((l for l in laps if not l['deleted']),
                         key=lambda l: l['lap_time_stamp'])
            conflict = None
            for i in range(1, len(act)):
                if act[i]['lap_time_stamp'] - act[i - 1]['lap_time_stamp'] < min_lap:
                    conflict = (act[i - 1], act[i])
                    break
            if not conflict:
                return
            a, b = conflict
            pa = a.get('peak_rssi'); pb = b.get('peak_rssi')
            # keep the higher peak; if unknown, drop the later one (b)
            loser = a if (pa is not None and pb is not None and pa < pb) else b
            if not deletable(loser):
                other = b if loser is a else a
                loser = other if deletable(other) else loser
            if not deletable(loser):
                rep.setdefault('blockers', []).append('SHORT_LAP_REQUIRES_REVIEW')
                return
            loser['deleted'] = True
            loser.setdefault('flags', []).append('SHORT_LAP_DELETED')

    def _apply_late(self, laps, fmt):
        unlimited = getattr(fmt, 'race_mode', 1) == 1 if fmt else True
        limit_ms = (getattr(fmt, 'race_time_sec', 0) or 0) * 1000 if fmt else 0
        if unlimited or not limit_ms:
            return
        finished = False
        for l in sorted(laps, key=lambda l: l['lap_time_stamp']):
            if l['deleted']:
                continue
            if finished:
                l['deleted'] = True
                l.setdefault('flags', []).append('LATE_AFTER_FINISH')
            elif l['lap_time_stamp'] > limit_ms:
                finished = True

    def _update_incremental(self, laps, timefmt, RHUtils):
        laps.sort(key=lambda l: l['lap_time_stamp'])
        idx = 0
        last = None
        for l in laps:
            if l['deleted']:
                l['lap_time'] = 0
                l['lap_time_formatted'] = '-'
                l['_idx'] = -1
                continue
            lt = l['lap_time_stamp'] if last is None else l['lap_time_stamp'] - last
            l['lap_time'] = lt
            l['lap_time_formatted'] = RHUtils.format_time_to_str(lt, timefmt)
            l['_idx'] = idx
            idx += 1
            last = l['lap_time_stamp']

    # --------------------------------------------------------- diagnostics

    def _calibration_bad(self, laps, baseline, expected, exit_at, enter_at,
                         rmin, rmax, sib_thresholds=None):
        active = [l for l in laps if not l['deleted']]
        n = len(active)
        if n == 0:
            return True
        # ExitAt below the noise floor -> a crossing never returns to Clear, so
        # passes merge into one giant lap (e.g. an uncalibrated exit_at of 0).
        if exit_at <= rmin:
            return True
        # EnterAt above every sample -> no real crossing can trigger.
        if enter_at > rmax:
            return True
        # Recalc had to delete a pile of sub-Minimum-Lap crossings -> EnterAt
        # sits in the noise band and the trace is full of false passes. (Twins
        # of protected manual/API laps are legitimate duplicates, not noise.)
        prot = [l['lap_time_stamp'] for l in active
                if l['source'] in PROTECTED_SOURCES]
        short_dels = sum(
            1 for l in laps
            if l['deleted'] and 'SHORT_LAP_DELETED' in (l.get('flags') or ())
            and not any(abs(l['lap_time_stamp'] - p) <= 2500 for p in prot))
        if short_dels >= max(3, int(0.25 * (n + short_dels))):
            return True
        # Far below the EnterAt this seat used in its other rounds -> almost
        # certainly a mid-race auto-correction gone wrong, not a real change.
        if sib_thresholds:
            enters = [t['enter_at'] for t in sib_thresholds
                      if t.get('enter_at')]
            if enters and enter_at < _median(enters) - 10:
                return True
        if baseline and baseline.get('median'):
            longest = max((l['lap_time'] for l in active if l.get('lap_time')), default=0)
            if longest > baseline['median'] * 2.5 and (expected or 2) > n:
                return True
        if expected and n < max(1, expected - 1):
            return True
        return False

    def _better(self, new_laps, old_laps, expected):
        na = sum(1 for l in new_laps if not l['deleted'])
        oa = sum(1 for l in old_laps if not l['deleted'])
        if na == 0:
            return False
        if expected:
            return abs(na - expected) <= abs(oa - expected)
        return na >= oa

    def _local_retune(self, vals, times, start_time, run, fmt, opts, timefmt,
                      expected, baseline, sib_thr, old_laps):
        '''Deterministic threshold repair: try the EnterAt/ExitAt this seat
        used in its other rounds plus a probe grid over the trace span; keep
        the candidate whose recomputed laps look most like this pilot's
        history (no sub-Minimum-Lap noise, no merged passes, plausible count).
        Returns (laps, enter, exit) or None when nothing plausible is found.'''
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span < 15:
            return None
        cands = []
        for t in (sib_thr or []):
            e, x = t.get('enter_at'), t.get('exit_at')
            if e and x and e > x:
                cands.append((int(e), int(x)))
        for f in (0.45, 0.55, 0.65, 0.75, 0.85):
            e = int(lo + span * f)
            x = lo + max(3, int(0.25 * (e - lo)))
            cands.append((e, x))
        best = None
        for e, x in dict.fromkeys(cands):
            if not (lo < x < e <= hi):
                continue
            probe_rep = {'warnings': [], 'blockers': [], 'deleted_count': 0}
            laps = self._finalize(self._recalc(vals, times, start_time, e, x),
                                  run, opts, fmt, timefmt, probe_rep)
            if probe_rep['blockers']:
                continue
            if self._calibration_bad(laps, baseline, expected, x, e, lo, hi,
                                     sib_thr):
                continue
            score = self._retune_score(laps, expected, baseline, opts)
            if best is None or score < best[0]:
                best = (score, e, x, laps)
        if not best:
            return None
        score, e, x, laps = best
        if not self._better(laps, old_laps, expected):
            return None
        logger.info('auto_marshal local re-tune seat %s: EnterAt %s ExitAt %s '
                    '(score %.1f)', run.node_index + 1, e, x, score)
        return laps, e, x

    def _retune_score(self, laps, expected, baseline, opts):
        '''Lower is better. Penalizes noise crossings that had to be deleted
        as sub-Minimum-Lap, laps far off the pilot's historical pace, and
        merged passes (a lap spanning ~two typical laps).'''
        act = [l for l in laps if not l['deleted']]
        score = 0.0
        score += 5.0 * sum(
            1 for l in laps
            if l['deleted'] and 'SHORT_LAP_DELETED' in (l.get('flags') or ()))
        if expected:
            score += 3.0 * abs(len(act) - expected)
        med = (baseline or {}).get('median')
        for l in act:
            if l.get('_idx', 0) <= 0 or not l.get('lap_time'):
                continue
            if med:
                if l['lap_time'] > med * 1.6:
                    score += 8.0          # likely a merged/missed pass
                elif l['lap_time'] < med * HIST_FAST_WARN:
                    score += 5.0          # likely a false pass
            elif opts['min_lap_ms'] and l['lap_time'] < opts['min_lap_ms'] * 1.2:
                score += 5.0
        return score

    def _history_check(self, laps, baseline, opts, rep):
        if not baseline or not baseline.get('median'):
            if baseline is not None and baseline.get('count', 0) < opts['history_min']:
                rep['warnings'].append('INSUFFICIENT_HISTORY')
            return
        med = baseline['median']; sig = baseline['sigma'] or 1
        min_lap = opts['min_lap_ms']
        for l in laps:
            if l['deleted'] or l['_idx'] == 0 or not l.get('lap_time'):
                continue
            lt = l['lap_time']
            if lt < med * HIST_FAST_BLOCK and lt < min_lap * 1.2:
                rep['blockers'].append('HIGH_CONFIDENCE_SHORT_FALSE_PASS')
            elif lt < med * HIST_FAST_WARN:
                rep['warnings'].append('FAST_VS_HISTORY')
            elif lt > med * HIST_SLOW_WARN:
                rep['warnings'].append('SLOW_VS_HISTORY')

    def _self_check(self, laps, enter, exit_at, opts, rep):
        if enter <= exit_at:
            rep['blockers'].append('SELF_CHECK_FAILED:THRESHOLDS')
        act = [l for l in laps if not l['deleted']]
        stamps = [l['lap_time_stamp'] for l in act]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            rep['blockers'].append('SELF_CHECK_FAILED:NON_MONOTONIC')
        if len(set(stamps)) != len(stamps):
            rep['blockers'].append('SELF_CHECK_FAILED:DUPLICATE_STAMP')
        if any(l['lap_time'] <= 0 for l in act):
            rep['blockers'].append('SELF_CHECK_FAILED:NONPOSITIVE_LAP')
        if opts['min_first_crossing_ms'] and act and act[0]['lap_time_stamp'] < opts['min_first_crossing_ms']:
            rep['blockers'].append('SELF_CHECK_FAILED:UNDER_MIN_FIRST')
        if opts['strict_min_lap'] and opts['min_lap_ms']:
            for l in act:
                if l['_idx'] > 0 and l['lap_time'] < opts['min_lap_ms']:
                    rep['blockers'].append('SELF_CHECK_FAILED:SHORT_LAP')
                    break
        # dedupe blockers
        rep['blockers'] = list(dict.fromkeys(rep['blockers']))

    # --------------------------------------------------------------- history

    def _history_baseline(self, pilot_id, race_id, opts):
        laps = []
        for lap in (self._safe(lambda: self._rhdata.get_savedRaceLaps()) or []):
            if getattr(lap, 'pilot_id', None) != pilot_id:
                continue
            if lap.race_id == race_id or lap.deleted:
                continue
            if lap.source not in TIMER_SOURCES:
                continue
            lt = lap.lap_time
            if lt and lt >= opts['min_lap_ms'] and lt > 0:
                laps.append(lt)
        if not laps:
            return {'count': 0}
        med = _median(laps)
        mad = _median([abs(x - med) for x in laps])
        sigma = max(1.4826 * mad, med * 0.05)
        return {'count': len(laps), 'median': round(med), 'sigma': round(sigma)}

    def _sibling_info(self, heat_id, race_id):
        '''Per-seat prior thresholds and typical active lap counts from the
        other rounds of the same heat.'''
        out = {}
        if not heat_id:
            return out
        for m in (self._safe(lambda: self._rhdata.get_savedRaceMetas_by_heat(heat_id)) or []):
            if m.id == race_id:
                continue
            for pr in (self._safe(lambda: self._rhdata.get_savedPilotRaces_by_savedRaceMeta(m.id)) or []):
                d = out.setdefault(pr.node_index, {'thresholds': [], 'counts': []})
                if pr.enter_at and pr.exit_at:
                    d['thresholds'].append({'enter_at': pr.enter_at, 'exit_at': pr.exit_at})
                active = sum(1 for l in (self._safe(
                    lambda: self._rhdata.get_savedRaceLaps_by_savedPilotRace(pr.id)) or [])
                    if not l.deleted)
                if active:
                    d['counts'].append(active)
        for seat, d in out.items():
            d['laps'] = int(round(_median(d['counts']))) if d['counts'] else None
        return out

    # ------------------------------------------------------------ apply / save

    def on_apply(self, data):
        '''Write the previewed results to the database (the Apply button).'''
        pa = self._pending_apply
        if not pa:
            return
        race_id = (data or {}).get('race_id')
        if race_id is not None and race_id != pa['race_id']:
            return
        if pa['race_id'] in self._processing_races:
            return
        meta = self._safe(lambda: self._rhdata.get_savedRaceMeta(pa['race_id']))
        if not meta:
            return
        heat = self._safe(lambda: self._rhdata.get_heat(meta.heat_id)) if meta.heat_id else None
        n = 0
        for it in pa['items']:
            try:
                self._save_item(meta.id, it)
                n += 1
            except Exception:
                logger.exception('auto_marshal apply failed for seat %s',
                                 it.get('node_index'))
        self._rebuild_caches(pa['race_id'], heat)
        self._processed_races.add(pa['race_id'])
        self._pending_apply = None
        self._state['phase'] = 'applied'
        self._state['can_apply'] = False
        self._state['applied_count'] = n
        self._push()
        self._notify('Auto marshalling applied to {} pilot(s) (race {})'
                     .format(n, pa['race_id']))
        try:
            self._rhapi.ui.broadcast_ui('marshal')
        except Exception:
            pass

    def _save_item(self, race_id, it):
        self._rhdata.alter_savedPilotRace({
            'pilotrace_id': it['pilotrace_id'],
            'enter_at': it['enter_at'], 'exit_at': it['exit_at']})
        self._rhdata.replace_savedRaceLaps({
            'race_id': race_id, 'pilotrace_id': it['pilotrace_id'],
            'node_index': it['node_index'], 'pilot_id': it['pilot_id'],
            'laps': [{'lap_time_stamp': l['lap_time_stamp'], 'lap_time': l['lap_time'],
                      'lap_time_formatted': l['lap_time_formatted'],
                      'peak_rssi': l['peak_rssi'], 'source': l['source'],
                      'deleted': l['deleted']} for l in it['laps']]})

    def _rebuild_caches(self, race_id, heat):
        self._safe(lambda: self._rhdata.clear_results_savedRaceMeta(race_id))
        if heat:
            self._safe(lambda: self._rhdata.clear_results_heat(heat))
            if heat.class_id:
                self._safe(lambda: self._rhdata.clear_results_raceClass(heat.class_id))

    def _write_report(self, meta, reports, phase, saved):
        summary = self._state.get('summary', {})
        report = {'plugin': 'auto_marshal', 'race_id': meta.id, 'phase': phase,
                  'saved': saved, 'summary': summary,
                  'pilots': [{k: r[k] for k in ('seat', 'callsign', 'enter_at',
                              'exit_at', 'active_laps', 'changed', 'warnings',
                              'blockers') if k in r} for r in reports]}
        logger.info('auto_marshal report: %s', json.dumps(report, ensure_ascii=False))
        if self._opt_bool(OPT_REPORT_ATTR, True):
            try:
                self._rhdata.alter_savedRaceMeta(meta.id, {
                    'race_attr': 'auto_marshal_report',
                    'value': json.dumps(report, ensure_ascii=False)})
            except Exception:
                pass  # attribute storage is best-effort / version-sensitive

    # ---------------------------------------------------------------- helpers

    def _parse_history(self, run):
        try:
            vals = json.loads(run.history_values)
            times = json.loads(run.history_times)
        except Exception:
            return None, None, 'NO_RSSI_HISTORY'
        if getattr(run, 'marshal_type', 0) not in (MARSHAL_RSSI, None):
            return None, None, 'UNSUPPORTED_MARSHAL_TYPE'
        if not vals or not times:
            return None, None, 'NO_RSSI_HISTORY'
        if len(vals) != len(times):
            return None, None, 'HISTORY_ARRAY_LENGTH_MISMATCH'
        # RotorHazard RSSI timestamps carry sub-second backward jitter, which is
        # harmless for the order-based crossing scan. Only flag genuine
        # corruption: an overall-decreasing series or a large backward jump.
        if times[-1] < times[0] or any((a - b) > 2.0 for a, b in zip(times, times[1:])):
            return None, None, 'NON_MONOTONIC_HISTORY_TIMES'
        if not all(isinstance(v, (int, float)) for v in vals):
            return None, None, 'NON_NUMERIC_RSSI'
        return vals, times, None

    def _callsign(self, pilot_id):
        p = self._safe(lambda: self._rhdata.get_pilot(pilot_id))
        return getattr(p, 'callsign', None) or 'Seat'

    def _race_labels(self, meta):
        if not meta:
            return '?', None
        heat = self._safe(lambda: self._rhdata.get_heat(meta.heat_id)) if meta.heat_id else None
        name = getattr(heat, 'display_name', None) or getattr(heat, 'note', None) \
            or 'Heat {}'.format(meta.heat_id)
        return name, meta.round_id

    def _latest_race(self):
        metas = self._safe(lambda: self._rhdata.get_savedRaceMetas()) or []
        return metas[-1] if metas else None

    def _resolve_race(self, heat_id, round_no):
        if not heat_id:
            return None
        metas = self._safe(lambda: self._rhdata.get_savedRaceMetas_by_heat(heat_id)) or []
        if not metas:
            return None
        for m in metas:
            if m.round_id == round_no:
                return m
        try:
            return sorted(metas, key=lambda m: m.round_id)[int(round_no)]
        except Exception:
            return metas[-1]

    def _context_snapshot(self, meta):
        heat_name, rnd = self._race_labels(meta)
        runs = self._safe(lambda: self._rhdata.get_savedPilotRaces_by_savedRaceMeta(meta.id)) or []
        return {'phase': 'idle', 'race_id': meta.id, 'heat': heat_name, 'round': rnd,
                'origin': 'context', 'mode': self._mode(),
                'pilots': [{'seat': r.node_index, 'pilotrace_id': r.id,
                            'callsign': self._callsign(r.pilot_id), 'status': 'idle',
                            'warnings': [], 'blockers': []} for r in runs]}

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0
