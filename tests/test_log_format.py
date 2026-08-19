"""The log line prefix, and the fact that both writers produce the same one.

Worth pinning because the value of the timestamp is entirely comparative: a host's log is read
against a client's copy of the same run, and each file is read against the `TimeStamp=` values in
the packets it records. A change that reformats one writer and not the other costs exactly that,
and would do it silently - the logs would still look fine in isolation.
"""

from __future__ import annotations

import BL2_Movement.state as st
from BL2_Movement import debug as d
from BL2_Movement import discovery as disc


def _boom():
    raise RuntimeError("no world info during load")


def _prefix(line: str) -> str:
    return line.split("] ", 1)[0] + "]"


def _body(line: str) -> str:
    return line.split("] ", 1)[1]


def test_prefix_carries_both_clocks(tmp_path, monkeypatch):
    """Wall clock to milliseconds, plus world time at the netcode's own precision."""
    monkeypatch.setattr(d, "LOG_PATH", tmp_path / "dbg.log")
    monkeypatch.setattr(d, "_count", 0)
    monkeypatch.setattr(st, "world_time", lambda: 1234.5678)

    d.dbg("ENTER_OWN client=True speed=970")
    line = (tmp_path / "dbg.log").read_text().splitlines()[0]

    assert line.startswith("[")
    assert "w=1234.568]" in line
    wall = _prefix(line).split(" ")[0].lstrip("[")
    assert len(wall) == len("00:00:00.000")


def test_no_world_still_logs(tmp_path, monkeypatch):
    """`world_time` raises mid-load and mid-transition. Losing the line there would lose exactly
    the startup and level-change diagnostics we most need."""
    monkeypatch.setattr(d, "LOG_PATH", tmp_path / "dbg.log")
    monkeypatch.setattr(d, "_count", 0)
    monkeypatch.setattr(st, "world_time", _boom)

    d.dbg("PHYS SLIDING hook on Engine.Pawn:PhysWalking: added")
    line = (tmp_path / "dbg.log").read_text().splitlines()[0]

    assert "w=?]" in line
    assert _body(line) == "0001 PHYS SLIDING hook on Engine.Pawn:PhysWalking: added"


def test_session_counter_still_leads_the_message(tmp_path, monkeypatch):
    """Session boundaries are found by the counter restarting at 0001, so it must stay immediately
    in front of the message with the stamp ahead of it, not interleaved."""
    monkeypatch.setattr(d, "LOG_PATH", tmp_path / "dbg.log")
    monkeypatch.setattr(d, "_count", 0)
    monkeypatch.setattr(st, "world_time", lambda: 10.0)

    d.dbg("first")
    d.dbg("second")
    lines = (tmp_path / "dbg.log").read_text().splitlines()

    assert _body(lines[0]) == "0001 first"
    assert _body(lines[1]) == "0002 second"


def test_both_writers_share_the_prefix(tmp_path, monkeypatch):
    """The whole point: one prefix shape, so the two files can be merged into one timeline."""
    monkeypatch.setattr(d, "LOG_PATH", tmp_path / "dbg.log")
    monkeypatch.setattr(d, "_count", 0)
    monkeypatch.setattr(disc, "DISCOVERY_LOG", tmp_path / "disc.log")
    monkeypatch.setattr(disc._Progress, "notes", 0)
    monkeypatch.setattr(st, "world_time", lambda: 77.25)

    d.dbg("SLIDE_ON via=message who=Ambyli n=2")
    disc.note("PATH close moved=880 went=(0.31,-0.95)")

    dbg_line = (tmp_path / "dbg.log").read_text().splitlines()[0]
    disc_line = (tmp_path / "disc.log").read_text().splitlines()[0]

    assert "w=77.250]" in dbg_line
    assert "w=77.250]" in disc_line
    assert len(_prefix(dbg_line)) == len(_prefix(disc_line))
    # discovery keeps its tag first in the body; only the shared prefix precedes it.
    assert _body(disc_line).startswith("PATH close ")
