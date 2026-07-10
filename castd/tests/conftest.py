"""Stub the hardware-only D-Bus/GLib bindings so that modules importing them
(castd.p2p.dbus_go, and transitively castd.main) can at least be *imported*
in a plain pytest run on a machine without python3-dbus/python3-gi
installed (this dev box).

This is a real, meaningful check beyond py_compile: py_compile only
compiles syntax and never executes an import, so it would not catch a typo
in a cross-module name (e.g. importing an Action member that does not
exist, or a function that got renamed). Actually importing castd.main here
means every name reference across the whole package is resolved for real.

What these stubs do NOT do: they do not simulate wpa_supplicant's actual
D-Bus behavior (no fake GroupStarted signals, no fake property semantics).
Anything that depends on that behavior is out of reach until Phase 0's
real-hardware verification.
"""
from __future__ import annotations

import sys
import types


def _install_module(name: str, **attrs) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _install_dbus_stub() -> None:
    if "dbus" in sys.modules:
        return

    dbus_mod = _install_module(
        "dbus",
        DBusException=type("DBusException", (Exception,), {}),
        PROPERTIES_IFACE="org.freedesktop.DBus.Properties",
        Byte=int,
        Array=lambda items, signature=None: list(items),
        Dictionary=lambda d, signature=None: dict(d),
        String=str,
        Int32=int,
        Signature=lambda s: s,
        Interface=lambda obj, dbus_interface=None: types.SimpleNamespace(),
        SystemBus=lambda: types.SimpleNamespace(
            get_object=lambda *a, **k: types.SimpleNamespace(),
            add_signal_receiver=lambda *a, **k: None,
        ),
    )
    mainloop_mod = _install_module("dbus.mainloop")
    glib_mod = _install_module("dbus.mainloop.glib", DBusGMainLoop=lambda *a, **k: None)
    dbus_mod.mainloop = mainloop_mod
    mainloop_mod.glib = glib_mod


def _install_gi_stub() -> None:
    if "gi" in sys.modules:
        return

    gi_mod = _install_module("gi")
    gobject_ns = types.SimpleNamespace(
        threads_init=lambda: None,
        MainLoop=lambda: types.SimpleNamespace(
            run=lambda: None,
            get_context=lambda: types.SimpleNamespace(iteration=lambda block: None),
        ),
    )
    gi_repo_mod = _install_module("gi.repository", GObject=gobject_ns)
    gi_mod.repository = gi_repo_mod


_install_dbus_stub()
_install_gi_stub()
