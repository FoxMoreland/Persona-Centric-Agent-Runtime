"""Bindings supplied by the in-process Android application host."""

import sys


IS_ANDROID = sys.platform == "android" or hasattr(sys, "getandroidapilevel")
_host = None


def bind(host):
    global _host
    if not IS_ANDROID or (_host is not None and _host != host):
        raise RuntimeError("Android runtime host cannot be rebound")
    _host = host


def host():
    if _host is None:
        raise RuntimeError("Android runtime host is unavailable")
    return _host
