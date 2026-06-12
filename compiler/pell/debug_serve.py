"""pell debug-serve — SQL-transport debugging (the "outbound" engine).

Drives Oracle's classic Probe API (DBMS_DEBUG) over two ordinary
OUTBOUND SQL connections — nothing connects back to the client, so it
works through SSH tunnels, NATs, and instances with no network ACL:

    target session   initialize / debug_on, then runs the block
                     (suspends at startup waiting for the debugger)
    control session  attach / synchronize / set_breakpoint / continue /
                     get_value / print_backtrace — all PL/SQL calls

The IDE talks to this process over stdin/stdout with JSON lines —
a full-duplex channel (websocket-style, minus the socket):

    -> {"cmd": "set_breakpoint", "owner": "PELL_TEST", "name": "HELLO",
        "namespace": 2, "line": 5, "id": "bp1"}
    -> {"cmd": "run"}
    <- {"event": "suspended", "reason": "breakpoint", "owner": ...,
        "name": ..., "line": 5, "stack": [...]}
    -> {"cmd": "locals"}
    <- {"event": "locals", "values": [{"name": "P_NAME", "value": "w1"}]}
    -> {"cmd": "step_over"}
    ...
    <- {"event": "terminated", "output": ["..."], "error": null}

Requires DEBUG CONNECT SESSION (same as JDWP) but NOT the network ACE.
Protocol facts pinned live on 23ai: the target suspends at
interpreter_starting (a built-in rendezvous — no go-barrier needed);
runtime_info.terminated is unreliable (NULL unless requested) so
termination is reason exit/knl_exit or a dead target thread;
breakpoints must be deleted before the final continue or detach hangs.
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading

# DBMS_DEBUG constants (from the 23ai package spec).
NS_CURSOR = 0          # anonymous blocks
NS_PKG_SPEC = 1
NS_PKG_BODY = 2
NS_TRIGGER = 3
BREAK_ANY_CALL = 12    # step into
BREAK_RETURN = 16      # step out (of current frame)
BREAK_NEXT_LINE = 32   # step over
ABORT_EXECUTION = 8192
REASONS = {
    0: "none", 2: "starting", 3: "breakpoint", 4: "sql", 6: "enter",
    7: "return", 8: "finish", 9: "line", 11: "exception", 15: "exit",
    17: "timeout", 20: "instantiate", 25: "knl_exit",
}
TERMINAL_REASONS = {"exit", "knl_exit"}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class ProbeSession:
    """The control half — every method is a PL/SQL call on the
    control connection. Probe records can't cross the client API, so
    each call wraps an anonymous block with scalar binds."""

    def __init__(self, control_conn):
        self.cur = control_conn.raw.cursor()

    def attach(self, sid: str) -> None:
        self.cur.callproc("dbms_debug.attach_session", [sid])

    def synchronize(self, timeout_s: int = 3600) -> dict:
        b = self._vars(st=int, reason=int)
        self.cur.execute(
            """declare ri dbms_debug.runtime_info; s binary_integer;
            begin
              s := dbms_debug.set_timeout(:t);
              s := dbms_debug.synchronize(ri, 0);
              :st := s; :reason := ri.reason;
            end;""",
            {"t": timeout_s, **b})
        return {"status": b["st"].getvalue(),
                "reason": REASONS.get(b["reason"].getvalue(), "?")}

    def set_breakpoint(self, owner: str | None, name: str | None,
                       namespace: int, line: int) -> tuple[int, int | None]:
        b = self._vars(st=int, bp=int)
        self.cur.execute(
            """declare pi dbms_debug.program_info; s binary_integer; bp binary_integer;
            begin
              pi.namespace := :ns; pi.name := :n; pi.owner := :o; pi.dblink := null;
              s := dbms_debug.set_breakpoint(pi, :l, bp);
              :st := s; :bp := bp;
            end;""",
            {"ns": namespace, "n": name, "o": owner, "l": line, **b})
        return b["st"].getvalue(), b["bp"].getvalue()

    def delete_all_breakpoints(self) -> None:
        self.cur.execute(
            """declare
              t dbms_debug.breakpoint_table; s binary_integer;
            begin
              dbms_debug.show_breakpoints(t);
              if t.count > 0 then
                for i in t.first..t.last loop
                  s := dbms_debug.delete_breakpoint(i);
                end loop;
              end if;
            end;""")

    def cont(self, flags: int) -> dict:
        b = self._vars(st=int, reason=int, name=str, owner=str, line=int, depth=int)
        self.cur.execute(
            """declare ri dbms_debug.runtime_info; s binary_integer;
            begin
              s := dbms_debug.continue(ri, :flags,
                     dbms_debug.info_getlineinfo + dbms_debug.info_getstackdepth);
              :st := s; :reason := ri.reason; :name := ri.program.name;
              :owner := ri.program.owner; :line := ri.line#; :depth := ri.stackdepth;
            end;""",
            {"flags": flags, **b})
        return {
            "status": b["st"].getvalue(),
            "reason": REASONS.get(b["reason"].getvalue(), str(b["reason"].getvalue())),
            "name": b["name"].getvalue(),
            "owner": b["owner"].getvalue(),
            "line": b["line"].getvalue(),
            "depth": b["depth"].getvalue(),
        }

    def backtrace(self) -> list[dict]:
        b = {"s": self.cur.var(str)}
        self.cur.execute(
            """declare t dbms_debug.backtrace_table; s varchar2(32000) := '';
            begin
              dbms_debug.print_backtrace(t);
              if t.count > 0 then
                for i in t.first..t.last loop
                  s := s || nvl(t(i).owner,'') || chr(9) || nvl(t(i).name,'') ||
                       chr(9) || t(i).line# || chr(10);
                end loop;
              end if;
              :s := s;
            end;""", b)
        frames = []
        for row in (b["s"].getvalue() or "").splitlines():
            owner, name, line = row.split("\t")
            frames.append({"owner": owner or None, "name": name or None,
                           "line": int(line)})
        frames.reverse()  # probe lists outermost first; IDE wants top first
        return frames

    def get_value(self, var_name: str, frame: int = 0) -> tuple[int, str | None]:
        b = self._vars(st=int, v=str)
        self.cur.execute(
            """declare s binary_integer; v varchar2(4000);
            begin
              s := dbms_debug.get_value(:n, :f, v, null);
              :st := s; :v := v;
            end;""",
            {"n": var_name, "f": frame, **b})
        return b["st"].getvalue(), b["v"].getvalue()

    def detach(self) -> None:
        self.cur.callproc("dbms_debug.detach_session", [])

    def _vars(self, **kinds):
        return {k: self.cur.var(t) for k, t in kinds.items()}


# --------------------------------------------------------------------------
# locals discovery: Probe has no "list variables" call — names come from
# the unit's source (params + DECLARE block of the enclosing subprogram).
# --------------------------------------------------------------------------

_DECL_RE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+(?:CONSTANT\s+)?([a-z_][a-z0-9_$.%() ]*?)\s*(?::=|;)", re.I)
_PARAM_RE = re.compile(r"[(,]\s*([a-z_][a-z0-9_]*)\s+(IN\s+OUT|IN|OUT)\s+([a-z_][a-z0-9_$.%]*)", re.I)
_SUB_RE = re.compile(r"^\s*(?:FUNCTION|PROCEDURE)\s+([a-z_][a-z0-9_]*)", re.I)
_KEYWORDS = {"begin", "end", "exception", "pragma", "type", "cursor", "is", "as"}

# Compiler-emitted scaffolding the user never wrote — hidden from the
# variables view. `l_pell_*` is the emitter's private namespace (JSON
# DOM temporaries l_pell_jobj_N, sql cursor temporaries, etc.).
_INTERNAL_PREFIXES = ("l_pell_", "pell_")


def _is_opaque_type(base_type: str) -> bool:
    """True when GET_VALUE can't read this type's value (anything that
    isn't a PL/SQL scalar)."""
    return bool(base_type) and base_type.upper() not in _SCALAR_TYPES

# Scalar PL/SQL types GET_VALUE can read. Anything else (JSON,
# JSON_OBJECT_T, collections, %ROWTYPE records, ADTs, REF CURSOR) is
# opaque to BOTH Oracle debug APIs — GET_VALUE returns only scalars,
# and DBMS_DEBUG.EXECUTE can only run at global scope (frame -1), where
# frame-locals aren't visible — so such locals are labeled by type
# rather than given a fake value.
_SCALAR_TYPES = {
    "VARCHAR2", "VARCHAR", "NVARCHAR2", "CHAR", "NCHAR", "STRING",
    "NUMBER", "INTEGER", "INT", "PLS_INTEGER", "BINARY_INTEGER",
    "SIMPLE_INTEGER", "NATURAL", "POSITIVE", "FLOAT", "REAL",
    "BINARY_FLOAT", "BINARY_DOUBLE", "DEC", "DECIMAL", "NUMERIC",
    "DATE", "TIMESTAMP", "BOOLEAN", "RAW", "ROWID", "UROWID",
}


def _is_internal(name: str) -> bool:
    return name.lower().startswith(_INTERNAL_PREFIXES)


def candidate_locals(source: str, line: int, max_names: int = 40) -> list[dict]:
    """Variables worth showing for a frame at `line` of `source`:
    the enclosing subprogram's params + declarations, each as
    {name, type, opaque}. Compiler temporaries (l_pell_*) are skipped.
    `opaque` marks types GET_VALUE can't read (objects/collections)."""
    lines = source.splitlines()
    start = 0
    for i in range(min(line, len(lines)) - 1, -1, -1):
        if _SUB_RE.match(lines[i]) or lines[i].strip().lower().startswith("declare"):
            start = i
            break
    out: list[dict] = []
    seen = set()
    in_body = False

    def add(name: str, type_text: str):
        nm = name.lower()
        if nm in seen or nm in _KEYWORDS or _is_internal(nm):
            return
        seen.add(nm)
        base_type = type_text.strip().split("(")[0].split()[0].upper() if type_text.strip() else ""
        out.append({
            "name": name.upper(),
            "type": type_text.strip() or None,
            "opaque": _is_opaque_type(base_type),
        })

    for i in range(start, min(line + 1, len(lines))):
        text = lines[i]
        for m in _PARAM_RE.finditer(text):
            add(m.group(1), m.group(3))
        stripped = text.strip().lower()
        if stripped == "begin" or stripped.startswith("begin "):
            in_body = True
        if not in_body:
            m = _DECL_RE.match(text)
            if m:
                add(m.group(1), m.group(2))
        if len(out) >= max_names:
            break
    return out


# --------------------------------------------------------------------------
# the serve loop
# --------------------------------------------------------------------------

def serve(block: str, connect_str: str | None) -> int:
    import os
    _dbg = os.environ.get("PELL_SERVE_DEBUG")
    def _trace(msg):
        if _dbg:
            print(f"serve: {msg}", file=sys.stderr, flush=True)
    from . import driver
    from .driver import _drain_dbms_output, _strip_terminator

    block = _strip_terminator(block)
    # The block's LAST act is turning the debugger off — after the
    # control session detaches, any further PL/SQL on the target
    # (including debug_off or an output drain) suspends waiting for a
    # debugger that's gone. Inside the block it's still attached.
    end_at = block.rstrip().rfind("END;")
    if end_at >= 0:
        block = (block[:end_at]
                 + "  dbms_debug.debug_off;\n"
                 + block[end_at:])

    target = driver.connect(connect_str)
    control = driver.connect(connect_str)
    _trace("connected")
    tc = target.raw.cursor()
    tc.execute("ALTER SESSION SET PLSQL_DEBUG = TRUE")
    tc.execute("ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 1")
    tc.callproc("dbms_output.enable", [None])
    _trace("session prepped")
    # Must be set in the TARGET session: when the target's wait for
    # the next debugger command times out, the default behaviour
    # (continue_on_timeout) lets it RESUME ON ITS OWN and blow past
    # breakpoints. retry = wait indefinitely.
    tc.callproc("dbms_debug.set_timeout_behaviour", [0])  # retry_on_timeout
    sid = tc.callfunc("dbms_debug.initialize", str, [])
    _trace("initialized")
    tc.callproc("dbms_debug.debug_on", [True, False])
    _trace("debug_on")

    probe = ProbeSession(control)
    probe.attach(sid)
    _trace("attached")

    target_done = threading.Event()
    target_error: list[str] = []

    def run_target():
        try:
            tc.execute(block)
        except Exception as e:
            target_error.append(str(e).splitlines()[0])
        finally:
            target_done.set()

    threading.Thread(target=run_target, daemon=True).start()
    _trace("target thread started")

    # Short-timeout poll: if the target died before producing any
    # debuggable activity (compile error in the block, connection
    # trouble), a plain synchronize would hang forever.
    while True:
        sync = probe.synchronize(timeout_s=3)
        _trace(f"synchronize -> {sync}")
        if sync["status"] == 0 and sync["reason"] != "timeout":
            # Startup poll done — restore a long probe timeout and pin
            # the TARGET's timeout behaviour to retry: with the default
            # (continue_on_timeout) the target resumes BY ITSELF when
            # the debugger takes longer than the timeout between
            # commands, blowing straight past breakpoints.
            probe.cur.execute(
                """declare s binary_integer;
                begin s := dbms_debug.set_timeout(3600); end;""")
            break
        if target_done.is_set():
            _emit({"event": "terminated", "output": [],
                   "error": target_error[0] if target_error else
                            f"target exited before debugging began ({sync})"})
            return 1
    # Target is suspended at interpreter_starting — breakpoints can be
    # set race-free before any code runs.
    _emit({"event": "ready", "debug_session_id": sid})

    cmds: "queue.Queue[dict]" = queue.Queue()

    def read_stdin():
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmds.put(json.loads(raw))
            except json.JSONDecodeError as e:
                _emit({"event": "error", "message": f"bad command: {e}"})
        cmds.put({"cmd": "stop"})  # EOF = the IDE went away

    threading.Thread(target=read_stdin, daemon=True).start()

    # NULL owner does NOT default to the connected schema in Probe
    # (set_breakpoint returns 16 = unit not found) — resolve it here.
    connected_user = str(list(control.run_query(
        "select user as u from dual")[0].values())[0])

    source_cache: dict[tuple, str] = {}

    def unit_source(owner: str | None, name: str | None) -> str:
        if not name:
            return block  # the anon block itself
        key = (owner, name)
        if key not in source_cache:
            rows = control.run_query(
                "select text from all_source where owner = nvl(:o, user) "
                "and name = :n and type = 'PACKAGE BODY' order by line",
                {"o": owner, "n": name})
            source_cache[key] = "".join(str(list(r.values())[0]) for r in rows)
        return source_cache[key]

    def finish(error: str | None) -> int:
        # Order matters: the target's execute() doesn't return until
        # the debugger DETACHES, and the output drain needs the target
        # connection idle — so detach, wait, drain, then report.
        _trace("finish: detaching")
        try:
            probe.detach()
        except Exception as e:
            _trace(f"finish: detach failed {e}")
        _trace("finish: waiting for target")
        target_done.wait(15)
        _trace(f"finish: target done={target_done.is_set()}")
        out = []
        # Drain only on the success path: when the block raised, its
        # final debug_off never ran, and the drain (PL/SQL) would
        # suspend on the still-debug-on session.
        if target_done.is_set() and not target_error and error is None:
            try:
                out = _drain_dbms_output(tc)
            except Exception as e:
                _trace(f"finish: drain failed {e}")
        _emit({"event": "terminated",
               "output": out,
               "error": error or (target_error[0] if target_error else None)})
        try:
            target.close(); control.close()
        except Exception:
            pass
        return 0

    last = {"owner": None, "name": None, "line": None}

    def do_continue(flags: int) -> bool:
        """Returns True when the session is over."""
        r = probe.cont(flags)
        _trace(f"cont({flags}) -> {r} target_done={target_done.is_set()}")
        if r["reason"] in TERMINAL_REASONS or r["status"] != 0 or target_done.is_set():
            return True
        last.update({"owner": r["owner"], "name": r["name"], "line": r["line"]})
        stack = []
        try:
            stack = probe.backtrace()
        except Exception:
            pass
        _emit({"event": "suspended", "reason": r["reason"], "owner": r["owner"],
               "name": r["name"], "line": r["line"], "stack": stack})
        return False

    while True:
        c = cmds.get()
        cmd = c.get("cmd")
        try:
            if cmd == "set_breakpoint":
                st, bp = probe.set_breakpoint(
                    (c.get("owner") or connected_user).upper(),
                    c.get("name"),
                    int(c.get("namespace", NS_PKG_BODY)), int(c["line"]))
                _emit({"event": "bp_set", "id": c.get("id"), "status": st, "bp": bp})
            elif cmd in ("run", "continue"):
                if do_continue(0):
                    return finish(None)
            elif cmd == "step_over":
                if do_continue(BREAK_NEXT_LINE):
                    return finish(None)
            elif cmd == "step_into":
                if do_continue(BREAK_ANY_CALL):
                    return finish(None)
            elif cmd == "step_out":
                if do_continue(BREAK_RETURN):
                    return finish(None)
            elif cmd == "locals":
                frame = int(c.get("frame", 0))
                src = unit_source(last["owner"], last["name"])
                values = []
                for var in candidate_locals(src, last["line"] or 1):
                    nm = var["name"]
                    # Opaque object types (JSON, JSON_OBJECT_T, collections,
                    # records, ADTs) have NO readable value through either
                    # Oracle debug API: GET_VALUE handles only scalars, and
                    # DBMS_DEBUG.EXECUTE can't reach frame-locals (runs at
                    # global scope only). Show the declared type so the var
                    # is at least identified, rather than a bare <OPAQUE>.
                    if var["opaque"]:
                        values.append({"name": nm, "value": f"<{var['type']}>",
                                       "type": var["type"], "opaque": True})
                        continue
                    try:
                        st, v = probe.get_value(nm, frame)
                        if st == 0:
                            values.append({"name": nm, "value": v, "type": var["type"]})
                        elif var["type"]:
                            values.append({"name": nm, "value": f"<{var['type']}>",
                                           "type": var["type"], "opaque": True})
                    except Exception:
                        pass
                _emit({"event": "locals", "frame": frame, "values": values})
            elif cmd == "stop":
                probe.delete_all_breakpoints()
                try:
                    probe.cont(ABORT_EXECUTION)
                except Exception:
                    pass
                return finish("aborted")
            else:
                _emit({"event": "error", "message": f"unknown cmd {cmd!r}"})
        except Exception as e:
            _emit({"event": "error", "message": str(e).splitlines()[0]})
            if target_done.is_set():
                return finish(None)
    # unreachable
