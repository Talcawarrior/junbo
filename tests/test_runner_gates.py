"""forecast_runner/windows-runner/gates.mjs -- slot tabanli kaynak kapilari (2026-11-18).

Olay: WeatherNext 2026-11-15 01:00Z'den sonra 3+ gun cekilmedi. Kapi
`utcHour % 12 === 0` idi ve utcHour TUR BASINDA sabitti; turlar 80-140 dk surup
kaydigi icin (23:59 -> 02:59 ...) 00/12'de baslayan tur hic olmadi. Open-Meteo
(6 sa) ve IEM (24 sa) de ayni sebeple atlaniyordu.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

GATES = Path(__file__).resolve().parent.parent / "forecast_runner" / "windows-runner" / "gates.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node yok")


def _run(js: str):
    code = f"import * as g from {json.dumps(GATES.as_uri())};\n{js}"
    out = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_slots_are_utc_epoch_aligned():
    r = _run(
        """
        const d = (s) => Date.parse(s);
        const hours = [0, 5, 6, 11, 12, 17, 18, 23];
        console.log(JSON.stringify({
          sameWn: g.slotOf(d('2026-11-18T00:05:00Z'), 12) === g.slotOf(d('2026-11-18T11:59:00Z'), 12),
          nextWn: g.slotOf(d('2026-11-18T11:59:00Z'), 12) !== g.slotOf(d('2026-11-18T12:00:00Z'), 12),
          om6: hours.map((h) => g.slotOf(d(`2026-11-18T${String(h).padStart(2, '0')}:30:00Z`), 6)),
        }));
        """
    )
    assert r["sameWn"] and r["nextWn"]
    om = r["om6"]
    assert om[0] == om[1] and om[2] == om[3] and om[4] == om[5] and om[6] == om[7]
    assert len(set(om)) == 4


def test_drifted_cycle_starts_still_fetch_every_slot():
    # 2026-11-17/18 gercek tur baslangiclari. Eski kapi bunlarin HICBIRINDE WN2 cekmedi.
    r = _run(
        """
        const st = {};
        const starts = ['2026-11-17T23:59:42Z', '2026-11-18T02:59:42Z', '2026-11-18T04:59:42Z',
                        '2026-11-18T11:56:45Z', '2026-11-18T14:33:24Z'];
        const out = starts.map((t) => {
          const ms = Date.parse(t);
          const due = g.isDue(st, 'weatherNext', ms, 12);
          if (due) g.markDone(st, 'weatherNext', ms, 12);
          return due;
        });
        console.log(JSON.stringify(out));
        """
    )
    # 23:59 -> 11-17 12Z slotu; 02:59 -> 11-18 00Z slotu (yeni); 04:59 ve 11:56 ayni slot; 14:33 -> 12Z slotu
    assert r == [True, True, False, False, True]


def test_failed_slot_is_retried_and_success_persists(tmp_path):
    state_file = tmp_path / "state" / "gated_slots_utc.json"
    js = """
        const file = __FILE__;
        const ms = Date.parse('2026-11-18T03:00:00Z');
        const st = g.readGateState(file);
        const dueBefore = g.isDue(st, 'iemActuals', ms, 24);
        const dueAfterFail = g.isDue(g.readGateState(file), 'iemActuals', ms + 3600e3, 24);
        g.markDone(st, 'iemActuals', ms, 24);
        const wrote = g.writeGateState(file, st);
        const dueAfterOk = g.isDue(g.readGateState(file), 'iemActuals', ms + 3600e3, 24);
        const dueNextDay = g.isDue(g.readGateState(file), 'iemActuals', ms + 24 * 3600e3, 24);
        console.log(JSON.stringify({ dueBefore, dueAfterFail, wrote, dueAfterOk, dueNextDay }));
    """.replace("__FILE__", json.dumps(str(state_file)))
    r = _run(js)
    assert r == {"dueBefore": True, "dueAfterFail": True, "wrote": True, "dueAfterOk": False, "dueNextDay": True}


def test_index_uses_slot_gates_not_hour_modulo():
    src = (GATES.parent / "index.mjs").read_text(encoding="utf-8")
    assert "from './gates.mjs'" in src
    assert "utcHour % 12" not in src and "utcHour % 6" not in src and "utcHour === 0" not in src
