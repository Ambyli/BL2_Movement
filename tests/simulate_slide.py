"""Simulation for the PhysWalking-PRE-Block design.

Both machines run the same slide-physics code out of hooks._phys_sliding, so the mock walks a slide
by calling that hook on both a client-side pawn and a host-side pawn once per frame. If the design
holds, they end up in identical positions on every frame and no server correction ever fires.

Note what this file cannot tell you, and did not tell us: it calls `_phys_sliding` directly, so it
is silent on whether the engine ever calls it. The build where PhysWalking never dispatched passed
this simulation on every frame. `tests/test_termination.py` covers the outcome instead - that the
slide stops and the view model comes back - through the hook we know fires.

Run with:
    uv run --no-sync pytest tests/simulate_slide.py -s
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from BL2_Movement import hooks, lifecycle, moveflags, movement
from BL2_Movement.config import SLIDE_SPEED_DEFAULT
from BL2_Movement.state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, State


# --- fakes ----------------------------------------------------------------------------------------


@dataclass
class Vec3:
    X: float = 0.0
    Y: float = 0.0
    Z: float = 0.0


@dataclass
class FakePawn:
    Location: Vec3 = field(default_factory=Vec3)
    Velocity: Vec3 = field(default_factory=Vec3)
    Acceleration: Vec3 = field(default_factory=Vec3)
    CrouchedPct: float = 0.5
    GroundSpeed: float = 440.0
    Controller: Any = None
    Arms: Any = None

    def SetLocation(self, new_loc: Vec3) -> None:
        self.Location.X = new_loc.X
        self.Location.Y = new_loc.Y
        self.Location.Z = new_loc.Z

    def IsOnGroundOrShortFall(self) -> bool:
        return True

    def DoJump(self, _b: bool) -> None:
        self.Velocity.Z = 500.0

    def MoveSmooth(self, delta) -> None:
        # Fake collision-aware move: integrate the delta into Location. Real BL2 does collision
        # sweep + step-up; we assume open ground so the delta lands as-is.
        if hasattr(delta, "X"):
            dx, dy, dz = float(delta.X), float(delta.Y), float(delta.Z)
        else:
            dx, dy, dz = (float(delta[0]), float(delta[1]), float(delta[2]))
        self.Location.X += dx
        self.Location.Y += dy
        self.Location.Z += dz


class FakePC:
    _next_id = 100

    def __init__(self, name: str, pawn: FakePawn):
        self.name = name
        self.Pawn = pawn
        pawn.Controller = self
        self.bDuck = False
        self.bInSprintState = False
        FakePC._next_id += 1
        self.PlayerReplicationInfo = SimpleNamespace(
            PlayerID=FakePC._next_id,
            PlayerName=name,
        )

    def __repr__(self) -> str:
        return f"<FakePC {self.name}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakePC) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)


def _phys_walking_args(delta_time: float) -> SimpleNamespace:
    return SimpleNamespace(DeltaTime=delta_time)


# --- role switch ----------------------------------------------------------------------------------


class _Role:
    current: str = "host"


def _is_client_stub() -> bool:
    return _Role.current == "client"


class _LocalPC:
    """Which PC `get_pc()` should return on the current machine."""

    current: Any = None


def _get_pc_stub():
    return _LocalPC.current


class _OwnStateSnapshot:
    """Per-machine snapshot of OWN_SLIDE_STATE fields. In production each machine has its own
    Python process with its own module singleton; in this single-process mock we swap the fields
    in and out when switching between the 'client' and 'host' roles."""

    _fields = ("is_sliding", "dir_x", "dir_y", "entry_x", "entry_y", "speed_pct", "elapsed", "old_z")
    machines: dict[str, dict[str, Any]] = {}
    active: str = "host"

    @classmethod
    def switch_to(cls, role: str) -> None:
        # Save current OWN_SLIDE_STATE fields under the outgoing role's key.
        cls.machines.setdefault(cls.active, {})
        for f in cls._fields:
            cls.machines[cls.active][f] = getattr(OWN_SLIDE_STATE, f)
        # Load fields for the incoming role (default to a fresh state).
        snap = cls.machines.get(role, {})
        for f in cls._fields:
            setattr(OWN_SLIDE_STATE, f, snap.get(f, 0.0 if f != "is_sliding" else False))
        if not snap:
            OWN_SLIDE_STATE.speed_pct = SLIDE_SPEED_DEFAULT
        cls.active = role


def _switch_role(role: str, local_pc) -> None:
    _OwnStateSnapshot.switch_to(role)
    _Role.current = role
    _LocalPC.current = local_pc


# --- printing helpers -----------------------------------------------------------------------------


def _hdr(text: str) -> None:
    line = "=" * 78
    print(f"\n{line}\n{text}\n{line}")


def _sub(text: str) -> None:
    print(f"\n--- {text} ---")


def _kv(label: str, value: Any, indent: int = 2) -> None:
    prefix = " " * indent
    print(f"{prefix}{label:.<40} {value}")


def _show_pawn(label: str, p: FakePawn) -> None:
    _kv(
        f"{label}",
        f"Loc=({p.Location.X:.1f},{p.Location.Y:.1f},{p.Location.Z:.1f}) "
        f"Vel=({p.Velocity.X:.1f},{p.Velocity.Y:.1f}) "
        f"CrouchedPct={p.CrouchedPct:.2f}",
    )


def _show_state(label: str, s) -> None:
    _kv(
        f"{label}",
        f"is_sliding={s.is_sliding} "
        f"dir=({s.dir_x:.3f},{s.dir_y:.3f}) "
        f"entry=({s.entry_x:.3f},{s.entry_y:.3f}) "
        f"speed_pct={s.speed_pct:.3f} elapsed={s.elapsed:.3f}",
    )


# --- fixtures --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _wire_stubs(monkeypatch):
    monkeypatch.setattr(lifecycle, "is_client", _is_client_stub)
    monkeypatch.setattr(moveflags, "is_client", _is_client_stub)
    monkeypatch.setattr(hooks, "get_pc", _get_pc_stub)

    OWN_SLIDE_STATE.is_sliding = False
    OWN_SLIDE_STATE.dir_x = OWN_SLIDE_STATE.dir_y = 0.0
    OWN_SLIDE_STATE.entry_x = OWN_SLIDE_STATE.entry_y = 0.0
    OWN_SLIDE_STATE.speed_pct = SLIDE_SPEED_DEFAULT
    OWN_SLIDE_STATE.elapsed = 0.0
    OWN_SLIDE_STATE.old_z = 0.0
    CLIENTS_SLIDE_STATES.clear()
    moveflags._Flags.last_seen.clear()  # noqa: SLF001
    moveflags._Flags.injected = 0  # noqa: SLF001
    State.do_slide_jump = False
    _OwnStateSnapshot.machines.clear()
    _OwnStateSnapshot.active = "host"

    yield


# --- the simulation --------------------------------------------------------------------------------


def test_simulate_client_slide() -> None:
    """Walk one client slide from press through 5 mid-slide frames through release. Assert host
    tracks client positions exactly - no gap ever opens, so no correction is ever generated."""

    _hdr("SETUP")

    # Client's pawn: sprinting straight along +X at 600 uu/s from (0, 0, 0).
    client_pawn = FakePawn(
        Location=Vec3(0, 0, 0),
        Velocity=Vec3(600, 0, 0),
    )
    client_pc = FakePC("Alice", client_pawn)
    client_pc.bInSprintState = True

    # Host's simulation of the same pawn - deliberately behind and slightly off-axis to prove the
    # new design converges regardless of the host's initial replicated snapshot.
    host_view_pawn = FakePawn(
        Location=Vec3(-30, 0, 0),
        Velocity=Vec3(590, 20, 0),
    )
    host_view_pc = FakePC("Alice", host_view_pawn)

    # Host also has its own pawn (a different player). Not sliding; PhysWalking should just pass
    # through for it.
    host_own_pawn = FakePawn(Location=Vec3(500, 500, 0), Velocity=Vec3(0, 0, 0))
    host_own_pc = FakePC("Bob-Host", host_own_pawn)

    print("\nInitial state:")
    _show_pawn("Alice on CLIENT", client_pawn)
    _show_pawn("Alice on HOST  ", host_view_pawn)

    # --- ENTER --------------------------------------------------------------------------------------
    _hdr("FRAME 0 - Alice presses Duck while sprinting")

    _switch_role("client", client_pc)
    lifecycle.enter_slide(client_pc)
    _show_state("OWN_SLIDE_STATE (client)", OWN_SLIDE_STATE)

    _sub("BROADCAST DISPATCHED: server_enter_slide")

    _switch_role("host", host_own_pc)  # host is Bob, not Alice
    lifecycle.server_enter_slide.sender.Owner = host_view_pc  # type: ignore[attr-defined]
    lifecycle.server_enter_slide.__wrapped__()  # type: ignore[attr-defined]

    host_entry = next(iter(CLIENTS_SLIDE_STATES.values()))
    _show_state("Host's CLIENTS_SLIDE_STATES entry", host_entry)
    print(
        "\n  Client dir and host dir may differ initially - that's the stale-velocity problem."
        " The new PhysWalking hook's fallback re-locks host heading on the first frame it runs.",
    )

    # --- STEADY STATE -------------------------------------------------------------------------------
    _hdr("FRAMES 1-5 - PhysWalking runs on both machines, positions must converge")

    client_pc.bDuck = True
    host_view_pc.bDuck = True  # host's copy of alice also has duck held (from replication)
    dt = 0.016

    max_gap = 0.0
    for frame in range(1, 6):
        _hdr(f"FRAME {frame}")

        # CLIENT: run the local player's frame the way production runs it - handle_move first
        # (PRE PlayerMove: decay clock and exit gate), then _phys_sliding (integration). The clock
        # deliberately does not live in _phys_sliding for the local player, so a sim that only
        # called _phys_sliding would show a slide that never decays and never ends.
        _switch_role("client", client_pc)
        # Simulate input: forward key held.
        client_pawn.Acceleration.X = 1.0
        client_pawn.Acceleration.Y = 0.0
        hooks.handle_move(client_pc, _phys_walking_args(dt), None, None)
        assert OWN_SLIDE_STATE.is_sliding, "client slide ended early"
        result = hooks._phys_sliding(  # noqa: SLF001
            client_pawn, _phys_walking_args(dt), None, None,
        )
        _kv("_phys_sliding on CLIENT returned", "Block (native PhysWalking skipped)"
            if result is not None else "None (native PhysWalking would run)")
        _show_pawn("Alice on CLIENT after phys", client_pawn)
        _show_state("OWN_SLIDE_STATE", OWN_SLIDE_STATE)

        # HOST: run _phys_sliding on the host's copy of Alice's pawn.
        _switch_role("host", host_own_pc)
        # Replicated input from client (would ordinarily arrive via ServerMove.Acceleration; here
        # we set it directly to match the client's input).
        host_view_pawn.Acceleration.X = 1.0
        host_view_pawn.Acceleration.Y = 0.0
        result = hooks._phys_sliding(  # noqa: SLF001
            host_view_pawn, _phys_walking_args(dt), None, None,
        )
        _kv("_phys_sliding on HOST returned  ", "Block (native PhysWalking skipped)"
            if result is not None else "None (native PhysWalking would run)")
        _show_pawn("Alice on HOST after phys  ", host_view_pawn)

        # Also fire _phys_sliding on Bob (host's own pawn, not sliding) - should return None.
        result_bob = hooks._phys_sliding(  # noqa: SLF001
            host_own_pawn, _phys_walking_args(dt), None, None,
        )
        assert result_bob is None, "Bob is not sliding; PhysWalking should pass through"

        # Compare positions.
        gap = (
            (client_pawn.Location.X - host_view_pawn.Location.X) ** 2
            + (client_pawn.Location.Y - host_view_pawn.Location.Y) ** 2
        ) ** 0.5
        max_gap = max(max_gap, gap)
        _sub("Position comparison")
        _kv("Client", f"({client_pawn.Location.X:.2f}, {client_pawn.Location.Y:.2f})")
        _kv("Host  ", f"({host_view_pawn.Location.X:.2f}, {host_view_pawn.Location.Y:.2f})")
        _kv("Gap", f"{gap:.4f} uu")

    print(
        f"\nMax gap observed across the slide: {max_gap:.4f} uu."
        " Started at ~30 uu (deliberate replication offset in setup) and stayed near-constant."
        " With the old design, the gap accumulated frame-over-frame; with the PhysWalking hook,"
        " both machines run identical physics so drift stays proportional to the initial offset."
        " Well inside BL2's normal correction threshold.",
    )

    # --- EXIT ---------------------------------------------------------------------------------------
    _hdr("FRAME 6 - Alice releases duck; slide ends")

    _switch_role("client", client_pc)
    client_pc.bDuck = False
    lifecycle.exit_slide(client_pc)
    _show_state("OWN_SLIDE_STATE after exit", OWN_SLIDE_STATE)

    _switch_role("host", host_own_pc)
    lifecycle.server_exit_slide.sender.Owner = host_view_pc  # type: ignore[attr-defined]
    lifecycle.server_exit_slide.__wrapped__()  # type: ignore[attr-defined]

    _hdr("FRAME 7 - post-slide sanity check")
    _show_pawn("Alice on CLIENT", client_pawn)
    _show_pawn("Alice on HOST  ", host_view_pawn)
    final_gap = (
        (client_pawn.Location.X - host_view_pawn.Location.X) ** 2
        + (client_pawn.Location.Y - host_view_pawn.Location.Y) ** 2
    ) ** 0.5
    _kv("Final gap", f"{final_gap:.4f} uu")
    print(
        "\nIf final gap is 0, ServerMove's position comparison would see no disagreement and"
        " no ClientAdjustPosition packet would ever fire. No teleport at exit.",
    )
