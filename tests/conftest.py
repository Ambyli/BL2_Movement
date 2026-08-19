"""Test scaffolding.

BL2 SDK mods live inside pyunrealsdk's embedded interpreter and rely on symbols provided by that
runtime (`uemath`, `mods_base`, `unrealsdk`, `networking`, `tweens`). None of those live on PyPI, so
importing the mod's modules in a plain venv would explode. This file registers stand-in modules in
`sys.modules` before any test does an import, and adds the repo's parent to `sys.path` so the mod
can be imported as a package (its production code uses relative imports).

Only the surface actually touched by the code under test is faked — mostly enough to satisfy the
imports and let pure-math paths run. Vector is the one exception: the slide code does real vector
arithmetic, so it gets a working implementation rather than a mock.
"""

from __future__ import annotations

import math
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# --- import path -----------------------------------------------------------------------------------
# The mod uses relative imports (`from .config import ...`), so it has to be imported as a package
# rather than as loose modules. The tests spell that package `BL2_Movement`, after the GitHub repo -
# but the same code is checked out as `sliding` inside the game's `sdk_mods`, and a GitHub zip
# extracts as `BL2_Movement-main`. Depending on the directory name meant the suite simply refused to
# collect in the game folder, which is the copy actually being played and therefore the copy whose
# regressions matter. This is the same lesson as PROTOCOL_PREFIX in lifecycle.py: pin the name to
# the mod, not to wherever somebody happened to put it.
#
# Binding `__path__` rather than importing the real package is deliberate - it makes submodules
# resolve out of this directory without executing `__init__.py`, which would build the mod and wire
# up hooks as an import side effect.

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO.parent) not in sys.path:
    sys.path.insert(0, str(_REPO.parent))

if "BL2_Movement" not in sys.modules:
    _package = ModuleType("BL2_Movement")
    _package.__path__ = [str(_REPO)]  # type: ignore[attr-defined]
    sys.modules["BL2_Movement"] = _package


# --- uemath.Vector — real math, not a mock ---------------------------------------------------------


class _Vector:
    """Stand-in for uemath.Vector covering the operations the slide code performs on it.

    Mutates on `normalize()` and returns self — the production code both reads `.x/.y` immediately
    after normalizing an existing vector AND chains `.lerp(v.normalize(), a).normalize()` on
    temporaries, and both rely on the same return semantics.
    """

    __slots__ = ("x", "y", "z")

    def __init__(self, arg=None):
        if arg is None:
            self.x = self.y = self.z = 0.0
        elif hasattr(arg, "X") and hasattr(arg, "Y") and hasattr(arg, "Z"):
            # Unreal FVector style (uppercase)
            self.x, self.y, self.z = float(arg.X), float(arg.Y), float(arg.Z)
        elif hasattr(arg, "x") and hasattr(arg, "y") and hasattr(arg, "z"):
            self.x, self.y, self.z = float(arg.x), float(arg.y), float(arg.z)
        else:
            self.x, self.y, self.z = (float(c) for c in arg)

    @property
    def magnitude(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalize(self) -> _Vector:
        m = self.magnitude
        if m > 0:
            self.x /= m
            self.y /= m
            self.z /= m
        return self

    def dot(self, other: _Vector) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def lerp(self, other: _Vector, alpha: float) -> _Vector:
        return _Vector((
            self.x + (other.x - self.x) * alpha,
            self.y + (other.y - self.y) * alpha,
            self.z + (other.z - self.z) * alpha,
        ))

    def __add__(self, other: _Vector) -> _Vector:
        return _Vector((self.x + other.x, self.y + other.y, self.z + other.z))

    def __sub__(self, other: _Vector) -> _Vector:
        return _Vector((self.x - other.x, self.y - other.y, self.z - other.z))

    def __mul__(self, scalar: float) -> _Vector:
        return _Vector((self.x * scalar, self.y * scalar, self.z * scalar))


# --- module registry helpers -----------------------------------------------------------------------


def _install(name: str, **attrs) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# --- uemath ----------------------------------------------------------------------------------------

_install("uemath", Vector=_Vector)


# --- mods_base -------------------------------------------------------------------------------------


class _SliderOption:
    """Live-tunable option. Tests can mutate `.value` at will to swap tuning at runtime."""

    def __init__(self, name, value, minval, maxval, step, is_integer=False, description=""):
        self.name = name
        self.value = value
        self.min_value = minval
        self.max_value = maxval
        self.step = step
        self.is_integer = is_integer
        self.description = description


class _GroupedOption:
    def __init__(self, name, options):
        self.name = name
        self.options = list(options)


class _BoolOption:
    def __init__(self, name, value, description=""):
        self.name = name
        self.value = value
        self.description = description


def _hook_decorator(*_args, **_kwargs):
    """Fake `mods_base.hook` — accepts a pattern and optionally a Type, returns identity decorator."""

    def _decorate(func):
        return func

    return _decorate


_install(
    "mods_base",
    SliderOption=_SliderOption,
    GroupedOption=_GroupedOption,
    BoolOption=_BoolOption,
    ENGINE=MagicMock(name="ENGINE"),
    hook=_hook_decorator,
    build_mod=lambda **_kwargs: MagicMock(name="Mod"),
    get_pc=lambda: MagicMock(name="PlayerController"),
)


# --- unrealsdk -------------------------------------------------------------------------------------


class _EnetMode:
    NM_Standalone = "NM_Standalone"
    NM_DedicatedServer = "NM_DedicatedServer"
    NM_ListenServer = "NM_ListenServer"
    NM_Client = "NM_Client"


class _WeakPointer:
    """Poor man's WeakPointer. Holds a strong ref; callable to fetch it back."""

    def __init__(self, obj=None):
        self._obj = obj

    def __call__(self):
        return self._obj

    def __class_getitem__(cls, _item):
        return cls


@contextmanager
def _prevent_hooking_direct_calls():
    """Fake `unrealsdk.hooks.prevent_hooking_direct_calls` - context manager, no-op in tests."""
    yield


_unrealsdk = _install(
    "unrealsdk",
    find_enum=lambda _name: _EnetMode(),
    construct_object=lambda *_a, **_kw: MagicMock(name="ConstructedObject"),
    find_all=lambda *_a, **_kw: [],
)
_unrealsdk_hooks = _install(
    "unrealsdk.hooks",
    Type=SimpleNamespace(PRE="pre", POST="post"),
    Block=object(),  # sentinel returned from hooks to block the underlying call
    add_hook=lambda *_a, **_kw: True,
    remove_hook=lambda *_a, **_kw: True,
    prevent_hooking_direct_calls=_prevent_hooking_direct_calls,
)
_unrealsdk_unreal = _install(
    "unrealsdk.unreal",
    WeakPointer=_WeakPointer,
    UObject=MagicMock,
    WrappedStruct=MagicMock,
)
# Expose the submodules on the parent so `from unrealsdk import unreal, hooks` resolves.
_unrealsdk.unreal = _unrealsdk_unreal
_unrealsdk.hooks = _unrealsdk_hooks


# --- networking ------------------------------------------------------------------------------------


class _MessageNamespace:
    """Fakes `host` / `targeted` namespaces from `networking.decorators`.

    Real ones ship messages over the wire when the wrapped function is called. Here we replace the
    decorated body with a no-op wrapper that swallows any arguments the networking layer would have
    injected, so tests can trigger paths that end in a message send without exploding.
    """

    def _wrap(self, func):
        def _sink(*_args, **_kwargs):
            return None

        _sink.sender = SimpleNamespace(Owner=None)
        _sink.__name__ = func.__name__
        _sink.__wrapped__ = func
        return _sink

    def message(self, func):
        return self._wrap(func)

    def json_message(self, func):
        return self._wrap(func)


_networking = _install("networking", add_network_functions=lambda *_a, **_kw: None)
_networking_decorators = _install(
    "networking.decorators",
    # `host` was renamed to `broadcast` at some point; keep both so older or newer callers work.
    host=_MessageNamespace(),
    broadcast=_MessageNamespace(),
    targeted=_MessageNamespace(),
)
_networking.decorators = _networking_decorators


# --- tweens ----------------------------------------------------------------------------------------
# Only the viewmodel module uses these; kept minimal so `__init__` can execute at import time.


class _TweenChain:
    def from_current(self):
        return self

    def transition(self, _transition):
        return self


class _Tween:
    def is_running(self):
        return False

    def kill(self):
        pass

    def tween_property(self, *_args, **_kwargs):
        return _TweenChain()

    def set_parallel(self, _parallel):
        pass

    def start(self):
        pass


_install(
    "tweens",
    Tween=_Tween,
    circ_out=lambda *_a, **_kw: None,
    cubic_in_out=lambda *_a, **_kw: None,
    cubic_out=lambda *_a, **_kw: None,
    elastic_out=lambda *_a, **_kw: None,
    quad_out=lambda *_a, **_kw: None,
)


# --- coroutines ------------------------------------------------------------------------------------
# Declared as a runtime dep but not actually imported by the code paths under test.

_install("coroutines")


# --- debug log redirect ------------------------------------------------------------------------------
# `debug.dbg` appends to a file in the user's home directory, and several code paths under test call
# it. Left alone, running the suite interleaves test noise into the *live game's* diagnostic log -
# the one artifact we rely on to tell what the mod did in a real session, and which has a 4000 line
# budget that test runs would quietly spend. Point it somewhere disposable instead.

import tempfile  # noqa: E402

from BL2_Movement import debug as _debug  # noqa: E402

_debug.LOG_PATH = Path(tempfile.gettempdir()) / "bl2_slide_debug_tests.log"
