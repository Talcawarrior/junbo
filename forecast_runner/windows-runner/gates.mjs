// gates.mjs -- slot based source gates (2026-11-18).
//
// The old gates were `utcHour % N === 0` with utcHour fixed at CYCLE START. Cycles run 80-140 min
// and their start minute drifts (e.g. 23:59 -> 02:59), so a cycle starting exactly in hour 00/12
// often never happened: WeatherNext was not fetched for 3+ days after 2026-11-15 01:00Z (Open-Meteo
// keeps ensemble members only ~3-4 days -> permanent loss); Open-Meteo (6 h) and IEM actuals (24 h)
// were skipped the same way. Now each gated step runs in the FIRST cycle of its UTC slot
// (epoch-aligned: 6 h -> 00/06/12/18, 12 h -> 00/12, 24 h -> 00) and is marked done only after a
// success; a failed slot is retried by the next cycle. The state file survives runner restarts,
// so a restart does not re-fetch (shared-IP Open-Meteo quota).
import fs from 'node:fs';
import path from 'node:path';

export function slotOf(ms, hours) {
  return Math.floor(ms / (hours * 3600 * 1000));
}

export function isDue(state, key, nowMs, hours) {
  return state[key] !== slotOf(nowMs, hours);
}

export function markDone(state, key, nowMs, hours) {
  state[key] = slotOf(nowMs, hours);
  return state;
}

export function readGateState(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function writeGateState(file, state) {
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(state), 'utf8');
    return true;
  } catch {
    return false;
  }
}
