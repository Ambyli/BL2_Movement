"""Phase 0 probe for the third-person slide animation (docs/third-person-slide-animation.md).

Run it from the in-game pyunrealsdk console, on foot and standing normally, in a level (not the
main menu):

    pyexec sdk_mods/sliding/spikes/phase0_probe.py

Wait ~1 second after running, then send me ~/bl2_phase0_probe.txt. It answers three questions:

  1. Does the body mesh expose a FullBodyAnimSlot (or similarly named slot), and what AnimSequences
     are already loaded that could stand in for a slide (crouch / crawl / injured)?
  2. Is the body mesh's *component* transform (Mesh.Rotation / Mesh.Translation) readable, and does
     it belong to the component rather than the shared SkeletalMesh asset (so leaning it won't warp
     every other pawn using that mesh)?
  3. Does a component-transform lean *persist* under the running AnimTree, or is it stomped each
     frame? A short coroutine applies a lean, waits half a second, reads it back, logs the verdict,
     and resets - so you don't have to eyeball it. This appends to the same file ~0.5s after the
     rest, which is why you wait a beat before sending.

Everything is wrapped so one failed probe still leaves a usable report.

Question 3 answers whether the lean is *technically* viable. Whether it *looks* right, and is
visible to other players, still wants a human eyeball - best from a second player's screen, since
your own camera is first-person and won't show your own body. Apply/reset the lean by hand with:

    py from mods_base import get_pc; m=get_pc().Pawn.Mesh; m.Rotation.Roll=8000; m.Translation.Z=-40
    py from mods_base import get_pc; m=get_pc().Pawn.Mesh; m.Rotation.Roll=0; m.Translation.Z=0

Roll 8000 is ~44 degrees.
"""

from __future__ import annotations

from pathlib import Path

from coroutines import Time, WaitWhile, start_coroutine_tick
from mods_base import get_pc

OUT = Path.home() / "bl2_phase0_probe.txt"
_lines: list[str] = []


def w(s: object = "") -> None:
    _lines.append(str(s))


def probe(label: str, fn) -> None:
    try:
        fn()
    except Exception as ex:  # noqa: BLE001 - a failed probe must not abort the report
        w(f"[{label}] ERROR {type(ex).__name__}: {ex}")


pc = get_pc()
pawn = getattr(pc, "Pawn", None)
w(f"pc={pc}")
w(f"pawn={pawn} class={pawn.Class.Name if pawn is not None else None}")

mesh = getattr(pawn, "Mesh", None)
w(f"mesh(body)={mesh} class={mesh.Class.Name if mesh is not None else None}")
w(f"arms={getattr(pawn, 'Arms', None)}  (first-person, for contrast)")


def _transform() -> None:
    w("\n== body component transform (the asset-free lean lever) ==")
    t, r = mesh.Translation, mesh.Rotation
    w(f"Mesh.Translation=({t.X:.2f},{t.Y:.2f},{t.Z:.2f})")
    w(f"Mesh.Rotation=(Pitch={r.Pitch},Yaw={r.Yaw},Roll={r.Roll})")
    w(f"Mesh.Scale={mesh.Scale} Scale3D={getattr(mesh, 'Scale3D', None)}")
    sk = getattr(mesh, "SkeletalMesh", None)
    w(f"Mesh.SkeletalMesh(asset, SHARED - do NOT lean this)={sk}")


probe("transform", _transform)


def _slots() -> None:
    w("\n== AnimNodeSlot probe (guessed names) ==")
    for name in ("FullBodyAnimSlot", "CustomAnimSlot", "UpperBodyAnimSlot", "FullBody", "AnimSlot"):
        try:
            node = mesh.FindAnimNode(name)
        except Exception as ex:  # noqa: BLE001
            node = f"<err {type(ex).__name__}: {ex}>"
        w(f"FindAnimNode({name!r}) -> {node}")
    w(f"Mesh.AnimTreeTemplate={getattr(mesh, 'AnimTreeTemplate', None)}")


probe("slots", _slots)


def _walk_tree() -> None:
    w("\n== full AnimTree walk (find the real slot names) ==")
    root = getattr(mesh, "Animations", None)
    w(f"Mesh.Animations(root)={root}")
    seen: set[str] = set()
    stack: list[tuple[object, int]] = [(root, 0)]
    count = 0
    while stack and count < 400:
        node, depth = stack.pop()
        if node is None:
            continue
        key = str(node)
        if key in seen or depth > 14:
            continue
        seen.add(key)
        count += 1
        cls = node.Class.Name
        node_name = getattr(node, "NodeName", None)
        marker = "  <-- SLOT" if "Slot" in str(cls) else ""
        w(f"{'  ' * depth}{cls} NodeName={node_name}{marker}")
        kids = getattr(node, "Children", None) or []
        for ch in kids:
            stack.append((getattr(ch, "Anim", None), depth + 1))


probe("walk_tree", _walk_tree)


def _anim_sets() -> None:
    w("\n== AnimSets / sequences (stand-in slide clips) ==")
    keys = ("slide", "crouch", "duck", "crawl", "injur", "revive", "death", "cover", "sprint", "run", "dodge")
    sets = list(getattr(mesh, "AnimSets", None) or [])
    w(f"{len(sets)} AnimSet(s) on the body mesh")
    for aset in sets:
        try:
            seqs = list(aset.Sequences)
            names = [str(s.SequenceName) for s in seqs]
        except Exception as ex:  # noqa: BLE001
            w(f"  AnimSet {getattr(aset, 'Name', aset)}: <seq err {ex}>")
            continue
        w(f"\nAnimSet {aset.Name}: {len(names)} sequences")
        hits = [n for n in names if any(k in n.lower() for k in keys)]
        if hits:
            w(f"  keyword hits: {hits}")
        w(f"  first 40: {names[:40]}")


probe("anim_sets", _anim_sets)

OUT.write_text("\n".join(_lines), encoding="utf-8")
print(f"[phase0] wrote {OUT} ({len(_lines)} lines)")


def _append(s: str) -> None:
    with OUT.open("a", encoding="utf-8") as f:
        f.write(s + "\n")


# Automated persistence check: apply a lean to the body component, wait half a second of game time,
# read it back. If the value survived, the AnimTree does NOT overwrite component transforms and the
# asset-free lean lever works as-is; if it was reset, Phase 1 needs a per-frame reassert (drive it
# from the slide coroutine) or a SkelControl instead. Runs as a coroutine because we have to let real
# frames elapse between applying and reading. Everything the report already wrote is safe on disk
# before this starts, so a failure here only costs the persistence line.
_acc = {"t": 0.0}


def _still_waiting() -> bool:
    _acc["t"] += Time.delta_time
    return _acc["t"] < 0.5


def _persistence_check():
    _append("\n== body component transform persistence (auto) ==")
    m = get_pc().Pawn.Mesh
    roll0, z0 = m.Rotation.Roll, m.Translation.Z
    m.Rotation.Roll = roll0 + 8000
    m.Translation.Z = z0 - 40.0
    applied_roll, applied_z = m.Rotation.Roll, m.Translation.Z
    yield WaitWhile(_still_waiting)
    after_roll, after_z = m.Rotation.Roll, m.Translation.Z
    persisted = abs(after_roll - applied_roll) < 100 and abs(after_z - applied_z) < 2.0
    _append(f"applied:     Roll={applied_roll} Z={applied_z:.2f}")
    _append(f"after 0.5s:  Roll={after_roll} Z={after_z:.2f}")
    _append(
        f"persisted={persisted}  "
        "(True = AnimTree did NOT stomp the component transform: lean lever works as-is. "
        "False = need a per-frame reassert from the slide driver, or a SkelControl.)",
    )
    m.Rotation.Roll = roll0
    m.Translation.Z = z0
    _append(f"reset to:    Roll={roll0} Z={z0:.2f}")


try:
    start_coroutine_tick(_persistence_check())
    print("[phase0] persistence check running - wait ~1s, then send me the file")
except Exception as ex:  # noqa: BLE001 - spike; report and move on
    _append(f"[persistence] ERROR {type(ex).__name__}: {ex}")
    print(f"[phase0] persistence check skipped: {type(ex).__name__}: {ex}")
