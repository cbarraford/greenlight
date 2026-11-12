# Harness only: shifts Python's wall clock so "now" starts at $GREENLIGHT_NOW (epoch seconds) and
# ticks from there. The Rust binary honors the same variable. Monotonic time and sleeps are untouched.
# ponytail: file mtimes stay real; the harness stamps fixture files with GREENLIGHT_NOW instead.
import os
if os.environ.get("GREENLIGHT_NOW"):
    import datetime as _dt, time as _t
    _off = float(os.environ["GREENLIGHT_NOW"]) - _t.time()
    _time = _t.time
    _t.time = lambda: _time() + _off
    _t.time_ns = lambda: int(_t.time() * 1e9)
    for _n in ("localtime", "gmtime", "ctime"):
        _f = getattr(_t, _n)
        setattr(_t, _n, lambda s=None, _f=_f: _f(_t.time() if s is None else s))
    _strftime = _t.strftime
    _t.strftime = lambda fmt, tt=None: _strftime(fmt, _t.localtime() if tt is None else tt)

    class datetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(_t.time(), tz)
        @classmethod
        def today(cls):
            return cls.now()
        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(_t.time(), _dt.timezone.utc).replace(tzinfo=None)

    class date(_dt.date):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(_t.time())
    _dt.datetime, _dt.date = datetime, date
