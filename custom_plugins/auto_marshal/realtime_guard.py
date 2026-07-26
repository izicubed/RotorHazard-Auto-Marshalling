'''
Real-time marshalling guard for the Auto Marshalling plugin.

Runs entirely on the timer (Raspberry Pi) with NO external/AI calls. While a
race is underway it watches each seat's live RSSI trace (node.history_values /
history_times — the same trace RotorHazard stores for post-race marshalling)
and fixes, in real time, the classic "missed HoleShot" failure: the craft
clearly passed the gate (a strong RSSI peak) but no crossing was registered
because EnterAt was too high (or ExitAt was bad), so every later lap count is
off by one.

When a completed peak is found that matches no recorded lap:
  * EnterAt/ExitAt for that seat are re-tuned live (below the observed peak,
    above the noise floor) via RotorHazard's own calibration path, so the
    node detects the following passes itself;
  * the missed pass is injected into the current race at the peak's timestamp
    (race.add_lap → it becomes lap 0 / the holeshot when it is the first
    pass) — or, if a later lap was already recorded before we could react,
    the seat's lap list is rebuilt with race.replace_laps so the pass lands
    in the right position and laps renumber.

A "stuck crossing" (ExitAt at/below the noise floor so the crossing never
ends and passes merge) is also detected: ExitAt is raised and the crossing is
force-ended so the node reports the pass itself.
'''

import json
import logging
from time import monotonic

import gevent

from eventmanager import Evt
from Database import LapSource
from RHRace import RaceStatus
from RHUI import UIField, UIFieldType, UIFieldSelectOption

logger = logging.getLogger(__name__)

OPT_RT_ENABLED = 'cm_rt_enabled'
OPT_RT_SCOPE = 'cm_rt_scope'
OPT_RT_SENS = 'cm_rt_sensitivity'
OPT_RT_TUNE = 'cm_rt_tune'
OPT_RT_STUCK = 'cm_rt_stuck'
OPT_RT_MAX_FIX = 'cm_rt_max_fixes'
OPT_RT_LEARN = 'cm_rt_learn'
OPT_RT_BIAS = 'cm_rt_bias'              # learned per-pilot sensitivity factors
RT_LOG_ATTR = 'auto_marshal_rt_log'   # per-race decision log attribute

RT_EVENT = 'auto_marshal_rt'          # server -> browser live feed
EV_RT_GET = 'auto_marshal_rt_get'     # browser asks for current feed

POLL_SECS = 0.3
CONFIRM_SECS = 1.2        # peak must be over for this long before acting
LAP_MATCH_SECS = 2.5      # peak within this window of a recorded lap = counted
STUCK_SECS = 8.0          # crossing open this long = ExitAt below noise floor
COOLDOWN_SECS = 4.0
MIN_SAMPLES = 6

# minimum peak rise above the race-trace noise floor to call it a gate pass
SENS_MIN_RISE = {'low': 45, 'normal': 30, 'high': 18}
# scale applied to a history-derived per-pilot rise, keyed by sensitivity
SENS_PRIOR_SCALE = {'low': 1.3, 'normal': 1.0, 'high': 0.75}
PRIOR_RISE_FRACTION = 0.4  # required rise = this share of the pilot's typical rise
# start-line / parked-quad ripples reach ~15 over the floor, so the learned
# per-pilot threshold must never drop into that band
RISE_CLAMP = (16, 80)
FAST_LAP_FRACTION = 0.6    # candidate lap < this share of pilot's median = false pass
BIAS_STEP = 0.15           # feedback adjustment per confirmed manual correction
BIAS_CLAMP = (0.6, 1.8)
HOLESHOT_MIN_MS = 1000     # earliest believable first gate pass after the tone
HOLESHOT_RISE_FACTOR = 1.3  # an early first pass needs a stronger peak
HOLESHOT_STRICT_MS = 8000  # start-line ripple window: strong rise required inside
TUNE_STRONG_FRACTION = 0.7  # rise share of the pilot's prior to re-tune on 1st add
TUNE_CAP_FRACTION = 0.55   # EnterAt never re-tuned below floor + this x prior rise
ADD_MATCH_SECS = 1.2       # feedback: an added lap survives if a final lap is this close
HOLD_SECS = 4.5            # approach-ripple guard: wait this long before injecting
PRIOR_TRACES = 2           # prior pilotraces parsed per seat for peak estimation
TIMER_SOURCES = (0, 2, 3)  # LapSource REALTIME / RECALC / AUTOMATIC
PRIOR_LAP_SOURCES = (0, 2, 3, 4)   # + API: kept guard adds are real laps too
TUNE_SAFE_GAP = 4          # noise ceiling must sit this far under the weakest pass


class RealtimeGuard:
    def __init__(self, rhapi):
        self._rhapi = rhapi
        self._gen = 0                # monitor generation token
        self._stop = True
        self._seats = {}             # node_index -> per-race state
        self._events = []            # feed shown in the panel
        self._active = False
        self._priors = {}            # node_index -> {'rise','lap_med'} from history
        self._decisions = []         # tunable decisions made during current race
        self._race_log = {}          # saved race_id -> decisions (for feedback)
        self._bias = {}              # pilot_id(str) -> learned sensitivity factor

    # ------------------------------------------------------------------ setup

    def register_ui(self):
        fields = self._rhapi.fields
        panel = 'auto_marshal'     # same settings panel as the main plugin

        def opt(name, label, ftype, value, desc, options=None):
            kw = dict(name=name, label=label, field_type=ftype, value=value, desc=desc)
            if options is not None:
                kw['options'] = options
            fields.register_option(UIField(**kw), panel)

        opt(OPT_RT_ENABLED, 'Real-time marshalling during races',
            UIFieldType.CHECKBOX, True,
            'Fully local (no AI/API): watch live RSSI during a race, fix missed '
            'passes (e.g. the holeshot) and re-tune EnterAt/ExitAt in real time.')
        opt(OPT_RT_SCOPE, 'Real-time: which missed passes to add',
            UIFieldType.SELECT, 'all',
            'Holeshot-only limits corrections to the first gate pass.', options=[
                UIFieldSelectOption('all', 'Any missed pass'),
                UIFieldSelectOption('holeshot', 'Holeshot (first pass) only')])
        opt(OPT_RT_SENS, 'Real-time: sensitivity',
            UIFieldType.SELECT, 'normal',
            'How strong an unregistered RSSI peak must be (vs the noise floor) '
            'to count as a missed gate pass.', options=[
                UIFieldSelectOption('low', 'Low (only very clear peaks)'),
                UIFieldSelectOption('normal', 'Normal'),
                UIFieldSelectOption('high', 'High (weaker peaks too)')])
        opt(OPT_RT_TUNE, 'Real-time: re-tune EnterAt/ExitAt live',
            UIFieldType.CHECKBOX, True,
            'When a missed pass is found, lower EnterAt below the observed peak '
            '(and fix ExitAt) so the node catches the next passes itself.')
        opt(OPT_RT_STUCK, 'Real-time: fix stuck crossings',
            UIFieldType.CHECKBOX, True,
            'If a crossing never ends (ExitAt at/below the noise floor), raise '
            'ExitAt and force the crossing to end so the pass is recorded.')
        opt(OPT_RT_MAX_FIX, 'Real-time: max corrections per pilot per race',
            UIFieldType.BASIC_INT, 3, 'Safety cap for automatic corrections.')
        opt(OPT_RT_LEARN, 'Real-time: learn from manual marshalling',
            UIFieldType.CHECKBOX, True,
            'Use each pilot\'s saved races for per-pilot detection thresholds, '
            'and adjust them when manual marshalling confirms or reverts the '
            'automatic real-time corrections.')

    # ------------------------------------------------------------- rh access

    @property
    def _ctx(self):
        return self._rhapi.db._racecontext

    def _opt_bool(self, name, default=False):
        try:
            val = self._rhapi.db.option(name)
        except Exception:
            return default
        if val is None or val == '':
            return default
        return val in (True, 1, '1', 'true', 'True')

    def _opt(self, name, default=None):
        try:
            val = self._rhapi.db.option(name)
        except Exception:
            return default
        return default if val is None or val == '' else val

    def _opt_int(self, name, default):
        try:
            return int(float(self._opt(name, default)))
        except (TypeError, ValueError):
            return default

    # ----------------------------------------------------------- event hooks

    def on_race_start(self, _args=None):
        # cm_enabled is the plugin-wide master switch (panel toggle);
        # cm_rt_enabled additionally scopes just the real-time guard.
        if not self._opt_bool('cm_enabled', True) \
                or not self._opt_bool(OPT_RT_ENABLED, True):
            return
        self._gen += 1
        self._stop = False
        self._seats = {}
        self._events = []
        self._priors = {}
        self._decisions = []
        self._bias = self._load_bias()
        self._active = True
        self._push()
        gevent.spawn(self._monitor, self._gen)
        logger.info('auto_marshal realtime guard armed (race start)')

    def on_race_stop(self, _args=None):
        # operator ended the race; RACE_FINISH (time expired) keeps us running
        self._stop = True
        self._active = False
        self._push()

    def on_laps_save(self, args):
        '''Race saved: keep this race's decision log for later comparison with
        manual marshalling (memory + best-effort race attribute).'''
        race_id = (args or {}).get('race_id')
        if race_id is None or not self._decisions:
            return
        self._race_log[race_id] = self._prune_confirmed_skips(
            race_id, self._decisions)
        self._decisions = []
        while len(self._race_log) > 10:
            self._race_log.pop(next(iter(self._race_log)))
        if self._opt_bool(OPT_RT_LEARN, True):
            try:
                self._ctx.rhdata.alter_savedRaceMeta(race_id, {
                    'race_attr': RT_LOG_ATTR,
                    'value': json.dumps(self._race_log[race_id])})
            except Exception:
                pass  # attribute storage is best-effort / version-sensitive

    def _prune_confirmed_skips(self, race_id, decisions):
        '''Drop 'skipped' decisions that landed near a lap the timer recorded
        anyway — or near a crossing the node started itself (even one RH then
        discarded under Min Lap Time): those were correct duplicate
        suppressions, NOT false negatives. Feeding them to the sensitivity
        learning is what used to crank per-pilot thresholds down until
        start-line ripples became "missed passes".'''
        try:
            rhdata = self._ctx.rhdata
            stamps = []
            for run in (rhdata.get_savedPilotRaces_by_savedRaceMeta(race_id)
                        or []):
                for l in (rhdata.get_savedRaceLaps_by_savedPilotRace(run.id)
                          or []):
                    if not l.deleted:
                        stamps.append((run.pilot_id, l.lap_time_stamp))
        except Exception:
            return list(decisions)
        cross_ms = {}                     # seat -> [crossing starts, race ms]
        start = getattr(self, '_start_mono', None)
        if start is not None:
            for seat, st in self._seats.items():
                cross_ms[seat] = [(cs - start) * 1000
                                  for cs in st.get('cross_starts', ())]
        out = []
        for d in decisions:
            if d.get('outcome') == 'skipped':
                near_lap = any(p == d.get('pilot')
                               and abs(s - d['t_ms']) <= LAP_MATCH_SECS * 1000
                               for p, s in stamps)
                near_cross = any(
                    abs(c - d['t_ms']) <= LAP_MATCH_SECS * 1000
                    for c in cross_ms.get(d.get('seat'), ()))
                if near_lap or near_cross:
                    continue
            out.append(d)
        return out

    def on_laps_resave(self, args):
        '''Manual marshalling saved for one pilot: compare the operator's final
        laps with our real-time decisions and tune this pilot's sensitivity.'''
        if not self._opt_bool(OPT_RT_LEARN, True):
            return
        args = args or {}
        race_id = args.get('race_id')
        pilot_id = args.get('pilot_id')
        if race_id is None or pilot_id is None:
            return
        try:
            self._apply_feedback(race_id, pilot_id)
        except Exception:
            logger.exception('auto_marshal rt feedback failed')

    def on_rt_get(self, _data=None):
        try:
            self._rhapi.ui.socket_send(RT_EVENT, self._snapshot())
        except Exception:
            pass

    # ------------------------------------------------------------- feed push

    def _snapshot(self):
        return {'active': self._active, 'events': self._events[-12:],
                'theme': self._opt('cm_theme', 'dark')}

    def on_option_set(self, args):
        '''Restyle the live feed immediately when the theme option changes.'''
        if (args or {}).get('option') == 'cm_theme':
            self._push()

    def _push(self):
        try:
            self._rhapi.ui.socket_broadcast(RT_EVENT, self._snapshot())
        except Exception:
            logger.exception('auto_marshal rt broadcast failed')

    def _record(self, seat, callsign, action, detail, race_ms):
        self._events.append({
            'seat': seat, 'callsign': callsign, 'action': action,
            'detail': detail, 't_ms': int(race_ms)})
        self._push()

    # --------------------------------------------------------------- monitor

    def _monitor(self, gen):
        ctx = self._ctx
        try:
            if self._opt_bool(OPT_RT_LEARN, True):
                try:
                    self._compute_priors(ctx)
                except Exception:
                    logger.exception('auto_marshal rt priors failed')
            while gen == self._gen and not self._stop:
                race = ctx.race
                status = race.race_status
                if status == RaceStatus.READY:      # race discarded/reset
                    break
                # keep watching through DONE (time expired, grace laps still
                # count) until the operator stops the race
                if status in (RaceStatus.RACING, RaceStatus.DONE):
                    try:
                        self._scan_all(ctx, race)
                    except Exception:
                        logger.exception('auto_marshal rt scan failed')
                gevent.sleep(POLL_SECS)
        finally:
            if gen == self._gen:
                self._active = False
                self._push()
            logger.info('auto_marshal realtime guard stopped')

    def _scan_all(self, ctx, race):
        import RHUtils
        self._start_mono = race.start_time_monotonic
        sens = self._opt(OPT_RT_SENS, 'normal')
        opts = {
            'scope': self._opt(OPT_RT_SCOPE, 'all'),
            'sens': sens,
            'min_rise': SENS_MIN_RISE.get(sens, 30),
            'tune': self._opt_bool(OPT_RT_TUNE, True),
            'stuck': self._opt_bool(OPT_RT_STUCK, True),
            'max_fix': max(1, self._opt_int(OPT_RT_MAX_FIX, 3)),
            'min_lap_ms': self._opt_int('MinLapSec', 0) * 1000,
            'min_first_ms': self._opt_int('MinFirstCrossingSec', 0) * 1000,
        }
        for node in ctx.interface.nodes:
            idx = node.index
            pilot_id = (race.node_pilots or {}).get(idx, RHUtils.PILOT_ID_NONE)
            if not node.frequency or pilot_id == RHUtils.PILOT_ID_NONE:
                continue
            st = self._seats.setdefault(idx, {
                'consumed': 0, 'fixes': 0, 'adds': 0, 'last_fix': 0.0,
                'stuck_fixed': False})
            if st['fixes'] >= opts['max_fix']:
                continue
            if monotonic() - st['last_fix'] < COOLDOWN_SECS:
                continue
            seat_opts = dict(opts)
            seat_opts['pilot_id'] = pilot_id
            seat_opts['min_rise'] = self._seat_min_rise(idx, pilot_id, sens)
            seat_opts['lap_med'] = (self._priors.get(idx) or {}).get('lap_med')
            self._scan_seat(ctx, race, node, pilot_id, st, seat_opts)

    # -------------------------------------------------- history priors (lvl 1)

    def _compute_priors(self, ctx):
        '''Per-seat detection priors from this pilot's saved races: typical
        crossing rise above the noise floor (from stored lap peaks, or the
        stored RSSI trace on versions without lap peak_rssi) and the pilot's
        median lap time.'''
        race = ctx.race
        rhdata = ctx.rhdata
        seated = {}          # node_index -> pilot_id
        for node in ctx.interface.nodes:
            pid = (race.node_pilots or {}).get(node.index)
            if pid and node.frequency:
                seated[node.index] = pid
        if not seated:
            return

        min_lap_ms = self._opt_int('MinLapSec', 0) * 1000
        pilots = set(seated.values())
        laps_by_pilot = {}
        for lap in (rhdata.get_savedRaceLaps() or []):
            pid = getattr(lap, 'pilot_id', None)
            if pid not in pilots or lap.deleted:
                continue
            if lap.source not in PRIOR_LAP_SOURCES:
                continue
            lt = lap.lap_time
            if lt and lt > 0 and lt >= min_lap_ms:
                laps_by_pilot.setdefault(pid, []).append(lt)

        pilotraces = sorted(rhdata.get_savedPilotRaces() or [],
                            key=lambda p: p.id, reverse=True)
        for idx, pid in seated.items():
            node = ctx.interface.nodes[idx]
            cand = [pr for pr in pilotraces if pr.pilot_id == pid
                    and pr.node_index == idx
                    and pr.frequency == node.frequency][:PRIOR_TRACES]
            if not cand:
                cand = [pr for pr in pilotraces
                        if pr.pilot_id == pid][:PRIOR_TRACES]
            rises = []
            weak_rise = None
            tune_ok = True
            for pr in cand:
                try:
                    vals = json.loads(pr.history_values)
                    times = json.loads(pr.history_times)
                except Exception:
                    continue
                if not vals or len(vals) < 10:
                    continue
                floor_h = min(vals)
                laps = [l for l in
                        (rhdata.get_savedRaceLaps_by_savedPilotRace(pr.id) or [])
                        if not l.deleted]
                peaks = [l.peak_rssi for l in laps
                         if getattr(l, 'peak_rssi', None)]
                top = _median(peaks) if peaks else max(vals)
                if top and top > floor_h:
                    rises.append(top - floor_h)
                if peaks and min(peaks) > floor_h:
                    weak = min(peaks) - floor_h
                    if weak_rise is None or weak < weak_rise:
                        weak_rise = weak
                # noise ceiling vs weakest pass: when this pilot's non-pass
                # ripples reach as high as their weakest real pass, there is
                # no safe band to move EnterAt into — live re-tuning down
                # would only make the node chase noise
                meta = None
                try:
                    meta = rhdata.get_savedRaceMeta(pr.race_id)
                except Exception:
                    pass
                if peaks and laps and meta and len(times) == len(vals) \
                        and times[-1] - times[0] > 30:
                    start_s = meta.start_time
                    stamps_s = [start_s + l.lap_time_stamp / 1000.0
                                for l in laps]
                    ceiling = floor_h
                    for v, t in zip(vals, times):
                        if all(abs(t - s) > LAP_MATCH_SECS
                               for s in stamps_s):
                            ceiling = max(ceiling, v)
                    if ceiling >= min(peaks) - TUNE_SAFE_GAP:
                        tune_ok = False
            prior = {}
            if rises:
                prior['rise'] = int(sum(rises) / len(rises))
            if weak_rise is not None:
                # the pilot's weakest KEPT pass: the required rise must stay
                # below it or real passes start getting rejected whenever the
                # feedback bias inches up
                prior['weak_rise'] = int(weak_rise)
            prior['tune_ok'] = tune_ok
            lts = laps_by_pilot.get(pid) or []
            if len(lts) >= 4:
                prior['lap_med'] = int(_median(lts))
            self._priors[idx] = prior
            if prior:
                logger.info('auto_marshal rt priors seat %s: %s (min_rise %s)',
                            idx + 1, prior,
                            self._seat_min_rise(idx, pid,
                                                self._opt(OPT_RT_SENS, 'normal')))

    def _seat_min_rise(self, seat, pilot_id, sens):
        '''Required peak rise for this seat: pilot's history when available,
        the global sensitivity constant otherwise, scaled by the learned
        per-pilot feedback factor — but never above the pilot's weakest kept
        pass, or real passes get rejected as soon as the bias inches up.'''
        prior = self._priors.get(seat) or {}
        base = float(SENS_MIN_RISE.get(sens, 30))
        rise = prior.get('rise')
        if rise:
            base = PRIOR_RISE_FRACTION * rise * SENS_PRIOR_SCALE.get(sens, 1.0)
        base *= self._bias.get(str(pilot_id), 1.0)
        weak = prior.get('weak_rise')
        if weak:
            base = min(base, max(RISE_CLAMP[0], weak - 2))
        return int(max(RISE_CLAMP[0], min(RISE_CLAMP[1], base)))

    # ------------------------------------------------------------- per seat

    def _scan_seat(self, ctx, race, node, pilot_id, st, opts):
        start = race.start_time_monotonic
        now = monotonic()

        # track node crossing starts (feeds the approach-ripple guard below)
        if node.crossing_flag and node.enter_at_timestamp:
            cs = st.setdefault('cross_starts', [])
            if not cs or abs(cs[-1] - node.enter_at_timestamp) > 0.05:
                cs.append(node.enter_at_timestamp)
                del cs[:-12]

        # stuck crossing: entered long ago, never exits -> ExitAt too low
        if opts['stuck'] and not st['stuck_fixed'] and node.crossing_flag \
                and node.enter_at_timestamp \
                and now - node.enter_at_timestamp > STUCK_SECS:
            self._fix_stuck(ctx, node, pilot_id, st, race, start)
            return

        # snapshot the live trace (appended-to during the race, never pruned;
        # the slice itself is the copy)
        n = min(len(node.history_values), len(node.history_times))
        if n < MIN_SAMPLES:
            return
        vals = node.history_values[:n]
        times = node.history_times[:n]

        floor = min(vals)
        laps = self._active_laps(race, node.index)

        if opts['scope'] == 'holeshot' and laps:
            st['consumed'] = n
            return

        i = max(st['consumed'], 0)
        pk = -1
        peak = -1
        while i < n:
            v = vals[i]
            if v > peak:
                peak, pk = v, i
            drop_req = max(10, int(0.35 * max(1, peak - floor)))
            if pk >= 0 and peak - v >= drop_req:
                # peak formed and clearly over -> evaluate it
                verdict = self._handle_peak(ctx, race, node, pilot_id, st, opts,
                                            vals, times, floor, pk, start, laps)
                if verdict == 'wait':
                    return              # too fresh — re-evaluate next poll
                st['consumed'] = i
                if verdict == 'fixed':
                    return              # one correction per poll per seat
                laps = self._active_laps(race, node.index)
                peak, pk = -1, -1       # rescan for the next peak
            i += 1

    def _handle_peak(self, ctx, race, node, pilot_id, st, opts,
                     vals, times, floor, pk, start, laps):
        '''Returns 'fixed' when a correction was made, 'skip' to discard the
        peak, or 'wait' when it is too fresh to judge yet.'''
        peak = vals[pk]
        t_pk = times[pk]
        now = monotonic()

        if now - t_pk < CONFIRM_SECS:
            return 'wait'
        lap_ts_ms = (t_pk - start) * 1000
        if lap_ts_ms <= 0:
            return 'skip'
        # already registered by the node? (checked before anything is logged so
        # the feedback learning never sees correctly-suppressed duplicates)
        for l in laps:
            if abs(l.lap_time_stamp - lap_ts_ms) <= LAP_MATCH_SECS * 1000:
                return 'skip'

        rise = peak - floor
        if rise < opts['min_rise']:
            # tunable rejection: log prominent-enough peaks for feedback
            if rise >= 12:
                self._log_decision(node.index, opts, lap_ts_ms, peak, floor,
                                   'skipped', 'low_rise')
            return 'skip'
        # rise on the way in too (rejects the launch-pad plateau at t=0)
        left_min = min(vals[max(0, st['consumed']):pk + 1])
        if peak - left_min < max(10, int(0.35 * rise)):
            return 'skip'

        start_behavior = getattr(getattr(race, 'format', None),
                                 'start_behavior', 0) or 0
        if not laps:
            # first crossing of the race. In Hole Shot formats it comes
            # seconds after the tone; with First Crossing = First Lap or
            # Staggered Start the craft must fly a whole lap first — gate by
            # what is physically possible for this format/pilot.
            earliest = max(opts['min_first_ms'], HOLESHOT_MIN_MS)
            if start_behavior and opts.get('lap_med'):
                # first pass = full lap: can't be faster than other laps
                earliest = max(earliest,
                               int(FAST_LAP_FRACTION * opts['lap_med']))
            if lap_ts_ms < earliest:
                self._log_decision(node.index, opts, lap_ts_ms, peak, floor,
                                   'skipped', 'too_early')
                return 'skip'
            # powered quads sitting on the start line paint pass-like ripples
            # in the first moments — require a clearly stronger peak there
            # (late first passes are judged like any other pass)
            if lap_ts_ms < HOLESHOT_STRICT_MS \
                    and rise < HOLESHOT_RISE_FACTOR * opts['min_rise']:
                self._log_decision(node.index, opts, lap_ts_ms, peak, floor,
                                   'skipped', 'weak_holeshot')
                return 'skip'
        # node is (or was) crossing around this peak -> it will report it itself
        if node.crossing_flag and node.enter_at_timestamp \
                and t_pk >= node.enter_at_timestamp - 1.0:
            return 'skip'
        # only act on the actual failure mode: the peak stayed below EnterAt
        if node.enter_at_level and peak >= node.enter_at_level:
            return 'skip'
        # keep Min Lap Time from the previous counted pass
        if laps and opts['min_lap_ms'] \
                and lap_ts_ms - laps[-1].lap_time_stamp < opts['min_lap_ms'] \
                and lap_ts_ms > laps[-1].lap_time_stamp:
            return 'skip'
        # plausibility vs the pilot's median lap time (history prior)
        prev_ts = max((l.lap_time_stamp for l in laps
                       if l.lap_time_stamp < lap_ts_ms), default=None)
        if prev_ts is not None and opts.get('lap_med') \
                and lap_ts_ms - prev_ts < FAST_LAP_FRACTION * opts['lap_med']:
            self._log_decision(node.index, opts, lap_ts_ms, peak, floor,
                               'skipped', 'fast_lap')
            return 'skip'

        # approach-ripple guard: a craft nearing the gate paints a small bump
        # a few seconds before the real crossing peak. Hold the candidate; if
        # the node starts a crossing (or a lap lands) right behind it, the
        # candidate was the ripple of that pass — drop it.
        if now - t_pk < HOLD_SECS:
            return 'wait'
        if any(t_pk < cs <= t_pk + HOLD_SECS
               for cs in st.get('cross_starts', ())):
            self._log_decision(node.index, opts, lap_ts_ms, peak, floor,
                               'skipped', 'ripple_before_pass')
            return 'skip'
        for l in laps:
            if lap_ts_ms < l.lap_time_stamp <= lap_ts_ms + HOLD_SECS * 1000:
                return 'skip'   # the real pass right behind it is recorded

        callsign = self._callsign(pilot_id)
        tuned = ''
        prior = self._priors.get(node.index) or {}
        prior_rise = prior.get('rise')
        # Re-tune thresholds only on strong evidence: a peak comparable to the
        # pilot's typical crossing, or a repeat missed pass on a seat where
        # history gives us a safe lower bound for EnterAt. A single weak add
        # then just fills the lap without moving EnterAt into the noise band
        # (where the node starts over-detecting on its own). Seats whose
        # history shows noise ripples overlapping their weakest real passes
        # (tune_ok False) are never re-tuned down — laps are filled one by
        # one and the operator's high EnterAt stays authoritative.
        strong = rise >= (TUNE_STRONG_FRACTION * prior_rise if prior_rise
                          else 1.5 * opts['min_rise'])
        if opts['tune'] and prior.get('tune_ok', True) \
                and (strong or (st.get('adds', 0) >= 1 and prior_rise)):
            tuned = self._tune(ctx, node, floor, peak, prior_rise)

        # in First Lap / Staggered formats the first crossing is a scored lap,
        # not a holeshot — RotorHazard numbers it per the format either way,
        # this only affects how the correction is reported
        holeshot = not laps and start_behavior == 0
        if not laps or lap_ts_ms > laps[-1].lap_time_stamp:
            # fast path: pass is later than everything recorded
            if self._add_lap_takes_peak(race):
                race.add_lap(node, t_pk, LapSource.API, peak=peak)
            else:  # RotorHazard <= 4.3.x: add_lap has no **kwargs
                race.add_lap(node, t_pk, LapSource.API)
            added = len(self._active_laps(race, node.index)) > len(laps)
            action = 'holeshot' if holeshot else 'pass'
            if not added:
                logger.warning('auto_marshal rt: add_lap did not register '
                               '(seat %s, ts %.0fms)', node.index + 1, lap_ts_ms)
                return 'skip'
        else:
            # late path: a newer lap was already recorded -> rebuild the list
            if not self._insert_lap(race, node.index, lap_ts_ms, peak, opts):
                return 'skip'
            action = 'holeshot' if holeshot else 'pass_inserted'

        st['fixes'] += 1
        st['adds'] = st.get('adds', 0) + 1
        st['last_fix'] = now
        self._log_decision(node.index, opts, lap_ts_ms, peak, floor, 'added')
        detail = 'peak {}{}'.format(peak, tuned)
        self._record(node.index, callsign, action, detail, lap_ts_ms)
        self._notify('Auto Marshal: added missed {} for {} ({})'.format(
            'holeshot' if holeshot else 'pass', callsign, detail))
        logger.info('auto_marshal rt: seat %s (%s) missed %s added at %.0fms, %s',
                    node.index + 1, callsign, action, lap_ts_ms, detail)
        return 'fixed'

    # ------------------------------------------------------------ corrections

    def _tune(self, ctx, node, floor, peak, prior_rise=None):
        '''Re-tune the live node thresholds around the observed missed peak.
        Follows doc/Tuning Parameters.md: EnterAt below every true crossing
        peak and above the cruising noise; ExitAt above the noise floor but
        below in-crossing valleys.'''
        span = max(1, peak - floor)
        new_enter = peak - max(8, int(0.25 * span))
        new_enter = max(new_enter, floor + 12)
        if prior_rise:
            # never chase a weak peak into the pilot's noise band: keep EnterAt
            # in the upper half of their typical crossing rise. If the missed
            # peak itself sits below that band, leave the calibration alone —
            # the guard keeps filling such passes lap-by-lap instead.
            new_enter = max(new_enter, floor + int(TUNE_CAP_FRACTION * prior_rise))
        changed = []
        if new_enter < peak and new_enter > floor \
                and new_enter < (node.enter_at_level or 999):
            ctx.calibration.set_enter_at_level(node.index, new_enter)
            changed.append('EnterAt {}'.format(new_enter))
        enter_now = node.enter_at_level or new_enter
        exit_now = node.exit_at_level or 0
        if exit_now >= enter_now or exit_now <= floor:
            new_exit = floor + max(6, int(0.35 * max(1, enter_now - floor)))
            new_exit = min(new_exit, enter_now - 5)
            if new_exit > 0 and new_exit != exit_now:
                ctx.calibration.set_exit_at_level(node.index, new_exit)
                changed.append('ExitAt {}'.format(new_exit))
        if changed:
            try:
                ctx.rhui.emit_enter_and_exit_at_levels()
            except Exception:
                pass
            return ', ' + ', '.join(changed)
        return ''

    def _fix_stuck(self, ctx, node, pilot_id, st, race, start):
        n = min(len(node.history_values), len(node.history_times))
        floor = min(node.history_values[:n]) if n else 0
        enter = node.enter_at_level or 0
        new_exit = floor + max(8, int(0.30 * max(1, enter - floor)))
        new_exit = min(new_exit, enter - 5) if enter > 5 else new_exit
        if new_exit > (node.exit_at_level or 0):
            ctx.calibration.set_exit_at_level(node.index, new_exit)
        try:
            ctx.interface.force_end_crossing(node.index)
        except Exception:
            logger.exception('force_end_crossing failed')
        st['stuck_fixed'] = True
        st['fixes'] += 1
        st['last_fix'] = monotonic()
        callsign = self._callsign(pilot_id)
        race_ms = max(0, (node.enter_at_timestamp - start) * 1000) \
            if node.enter_at_timestamp else 0
        self._record(node.index, callsign, 'stuck',
                     'ExitAt raised to {}'.format(new_exit), race_ms)
        self._notify('Auto Marshal: stuck crossing on {} — ExitAt raised to {}'.format(
            callsign, new_exit))
        logger.info('auto_marshal rt: stuck crossing seat %s fixed (ExitAt %s)',
                    node.index + 1, new_exit)

    def _insert_lap(self, race, node_index, lap_ts_ms, peak, opts):
        '''A later lap beat us to the record: rebuild the seat's lap list with
        the missed pass in its correct position (renumbers + refreshes UI).'''
        existing = [l for l in race.node_laps[node_index]
                    if not getattr(l, 'invalid', False)]
        items = [{'lap_time_stamp': l.lap_time_stamp, 'lap_time': 0,
                  'source': l.source, 'deleted': bool(l.deleted)}
                 for l in existing]
        items.append({'lap_time_stamp': lap_ts_ms, 'lap_time': 0,
                      'source': LapSource.API, 'deleted': False})
        items.sort(key=lambda x: x['lap_time_stamp'])
        # recompute incremental times over the active chain
        last = 0
        for it in items:
            if it['deleted']:
                continue
            it['lap_time'] = it['lap_time_stamp'] - last
            last = it['lap_time_stamp']
        # neighbours must stay >= Min Lap Time
        if opts['min_lap_ms']:
            act = [it for it in items if not it['deleted']]
            for a, b in zip(act, act[1:]):
                if b['lap_time_stamp'] - a['lap_time_stamp'] < opts['min_lap_ms']:
                    return False
        try:
            if hasattr(race, 'replace_laps'):       # RotorHazard 4.4+
                race.replace_laps({'seat': node_index, 'laps': items})
            else:                                   # 4.3.x fallback
                self._replace_laps_compat(race, node_index, items)
        except Exception:
            logger.exception('auto_marshal rt replace_laps failed')
            return False
        try:
            race.check_win_condition()
        except Exception:
            pass
        return True

    def _replace_laps_compat(self, race, node_index, items):
        '''RotorHazard <= 4.3.x has no race.replace_laps — rebuild the seat's
        Crossing list directly, mirroring what 4.4's replace_laps does.'''
        import RHUtils
        from RHRace import Crossing
        try:
            timefmt = self._ctx.serverconfig.get_item('UI', 'timeFormat') \
                or '{m}:{s}.{d}'
        except Exception:
            timefmt = '{m}:{s}.{d}'
        lap_objs = []
        lap_number = 0
        for it in items:
            lap = Crossing()
            lap.node_index = node_index
            lap.lap_number = lap_number
            lap.lap_time_stamp = it['lap_time_stamp']
            lap.lap_time = it['lap_time']
            lap.lap_time_formatted = RHUtils.format_time_to_str(
                it['lap_time'], timefmt)
            lap.source = it['source']
            lap.deleted = it['deleted']
            if not it['deleted']:
                lap_number += 1
            lap_objs.append(lap)
        race.node_laps[node_index] = lap_objs
        try:
            race.clear_lap_results()
        except Exception:
            pass
        race.clear_results()
        try:
            self._ctx.rhui.emit_current_laps()
            self._ctx.rhui.emit_current_leaderboard()
        except Exception:
            pass

    @staticmethod
    def _add_lap_takes_peak(race):
        '''True when race.add_lap accepts the peak kwarg (RotorHazard 4.4+).'''
        cached = getattr(RealtimeGuard, '_addlap_peak_ok', None)
        if cached is None:
            import inspect
            try:
                params = inspect.signature(race.add_lap).parameters.values()
                cached = any(p.kind == p.VAR_KEYWORD for p in params)
            except (TypeError, ValueError):
                cached = False
            RealtimeGuard._addlap_peak_ok = cached
        return cached

    # ---------------------------------------------- feedback learning (lvl 2)

    def _log_decision(self, seat, opts, t_ms, peak, floor, outcome, reason=None):
        d = {'seat': seat, 'pilot': opts.get('pilot_id'), 't_ms': int(t_ms),
             'peak': int(peak), 'floor': int(floor),
             'min_rise': opts.get('min_rise'), 'outcome': outcome}
        if reason:
            d['reason'] = reason
        self._decisions.append(d)
        if len(self._decisions) > 200:
            del self._decisions[:100]

    def _apply_feedback(self, race_id, pilot_id):
        '''Compare the operator's final laps with our in-race decisions and
        adjust this pilot's sensitivity factor: an added pass the operator
        removed -> require stronger peaks; a lap added where we skipped a
        candidate -> allow weaker peaks.'''
        decisions = self._race_log.get(race_id)
        if decisions is None:
            try:  # server restarted since the race: recover from the attribute
                raw = self._rhapi.db.race_attribute_value(race_id, RT_LOG_ATTR) \
                    or self._rhapi.db.race_attribute_value(
                        race_id, 'claude_marshal_rt_log')   # pre-2.0 races
                decisions = json.loads(raw) if raw else []
            except Exception:
                decisions = []
        mine = [d for d in decisions if d.get('pilot') == pilot_id]
        if not mine:
            return
        rhdata = self._ctx.rhdata
        runs = rhdata.get_savedPilotRaces_by_savedRaceMeta(race_id) or []
        run = next((r for r in runs if r.pilot_id == pilot_id), None)
        if not run:
            return
        stamps = [l.lap_time_stamp for l in
                  (rhdata.get_savedRaceLaps_by_savedPilotRace(run.id) or [])
                  if not l.deleted]

        delta = 0
        for d in mine:
            if d['outcome'] == 'added':
                # tight window: a phantom holeshot ~1.8 s before the real one
                # must not pass for "kept" just because the real lap is close
                near = any(abs(s - d['t_ms']) <= ADD_MATCH_SECS * 1000
                           for s in stamps)
                if not near:
                    delta += 1  # false positive: our pass was removed
            elif d['outcome'] == 'skipped':
                near = any(abs(s - d['t_ms']) <= LAP_MATCH_SECS * 1000
                           for s in stamps)
                if near:
                    delta -= 1  # false negative: operator added a lap there
        if not delta:
            return

        key = str(pilot_id)
        old = self._bias.get(key, 1.0)
        new = old + BIAS_STEP * max(-2, min(2, delta))
        new = round(max(BIAS_CLAMP[0], min(BIAS_CLAMP[1], new)), 2)
        if new == old:
            return
        self._bias[key] = new
        self._save_bias()
        callsign = self._callsign(pilot_id)
        direction = 'less' if new > old else 'more'
        self._notify('Auto Marshal: learned from manual marshalling — {} '
                     'sensitive for {} (factor {} → {})'.format(
                         direction, callsign, old, new))
        logger.info('auto_marshal rt feedback: race %s pilot %s delta %+d, '
                    'bias %s -> %s', race_id, pilot_id, delta, old, new)

    def _load_bias(self):
        try:
            data = json.loads(self._opt(OPT_RT_BIAS, '') or '{}')
            return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_bias(self):
        try:
            self._rhapi.db.option_set(OPT_RT_BIAS, json.dumps(self._bias))
        except Exception:
            logger.exception('auto_marshal rt bias save failed')

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _active_laps(race, node_index):
        return [l for l in race.node_laps.get(node_index, [])
                if not l.deleted and not getattr(l, 'invalid', False)]

    def _callsign(self, pilot_id):
        try:
            p = self._ctx.rhdata.get_pilot(pilot_id)
            return getattr(p, 'callsign', None) or 'Seat'
        except Exception:
            return 'Seat'

    def _notify(self, message):
        try:
            self._rhapi.ui.message_notify(message)
        except Exception:
            pass


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2.0
