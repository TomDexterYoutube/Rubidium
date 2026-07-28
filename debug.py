import sys
import os
import re
import argparse
import copy
import shutil
import random
import subprocess

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from lexer import tokenize
from parser import Parser
import rub_ast as ast

# ---------------------------------------------------------------------------
# BUG-6: wide floats (f128 / f256 / f512 / f1024 / f2048)
# ---------------------------------------------------------------------------
# The compiler maps all of these to LLVM `fp128` — IEEE binary128, a 113-bit
# significand — and print() shows the EXACT decimal value of the stored
# number (syntax lines 275-281). Python's float is a 64-bit double, so
# evaluating these as plain floats made the debug run disagree with the
# compiled binary (`3.33333` instead of 3.3333...461728... for `(10/3): f2048`).
#
# _Wide holds the exact rational value of a binary128 number: every operation
# is computed exactly and then rounded once to binary128, the same way the
# hardware/soft-float does, so the debugger reproduces the compiler digit for
# digit. Because a binary128 value is always a dyadic rational, its decimal
# expansion is finite and can be printed exactly.
WIDE_FLOAT_TYPES = ('f128', 'f256', 'f512', 'f1024', 'f2048')
_B128_PREC = 113          # significand bits, including the implicit leading 1


def _round_binary128(value):
    """Round an exact rational (or int/float) to the nearest binary128 value,
    ties-to-even — matching IEEE 754."""
    from fractions import Fraction
    f = Fraction(value)
    if f == 0:
        return Fraction(0)
    neg = f < 0
    if neg:
        f = -f
    # Normalise into [1, 2) and remember the exponent.
    e = 0
    two = Fraction(2)
    while f >= 2:
        f /= two
        e += 1
    while f < 1:
        f *= two
        e -= 1
    shift = 1 << (_B128_PREC - 1)
    scaled = f * shift
    q, r = divmod(scaled.numerator, scaled.denominator)
    # ties-to-even
    if 2 * r > scaled.denominator or (2 * r == scaled.denominator and q % 2):
        q += 1
    out = Fraction(q, shift) * (two ** e)
    return -out if neg else out


class _Wide:
    """A binary128 value. Arithmetic rounds to binary128 after every step."""
    __slots__ = ('v',)

    def __init__(self, value):
        from fractions import Fraction
        self.v = value if isinstance(value, Fraction) else _round_binary128(value)

    @staticmethod
    def _raw(other):
        """The exact rational behind `other`, or None if it isn't numeric."""
        from fractions import Fraction
        if isinstance(other, _Wide):
            return other.v
        if isinstance(other, bool):
            return None
        if isinstance(other, (int, float)):
            return Fraction(other)
        return None

    def _op(self, other, fn):
        o = self._raw(other)
        if o is None:
            return NotImplemented
        return _Wide(_round_binary128(fn(self.v, o)))

    def _rop(self, other, fn):
        o = self._raw(other)
        if o is None:
            return NotImplemented
        return _Wide(_round_binary128(fn(o, self.v)))

    def __add__(self, o):      return self._op(o, lambda a, b: a + b)
    def __radd__(self, o):     return self._rop(o, lambda a, b: a + b)
    def __sub__(self, o):      return self._op(o, lambda a, b: a - b)
    def __rsub__(self, o):     return self._rop(o, lambda a, b: a - b)
    def __mul__(self, o):      return self._op(o, lambda a, b: a * b)
    def __rmul__(self, o):     return self._rop(o, lambda a, b: a * b)
    def __truediv__(self, o):  return self._op(o, lambda a, b: a / b)
    def __rtruediv__(self, o): return self._rop(o, lambda a, b: a / b)
    def __mod__(self, o):      return self._op(o, lambda a, b: a - b * int(a / b))
    def __neg__(self):         return _Wide(-self.v)
    def __abs__(self):         return _Wide(abs(self.v))

    def __pow__(self, o):
        from fractions import Fraction
        e = self._raw(o)
        if e is None:
            return NotImplemented
        if e.denominator == 1 and abs(e.numerator) < 4096:
            n = e.numerator
            if n >= 0:
                acc = Fraction(1)
                for _ in range(n):
                    acc = _round_binary128(acc * self.v)
                return _Wide(acc)
            return _Wide(_round_binary128(Fraction(1) / (self.v ** (-n))))
        # Fractional exponent — no exact rational result; fall back to double.
        return _Wide(float(self.v) ** float(e))

    def __rpow__(self, o):
        base = self._raw(o)
        if base is None:
            return NotImplemented
        return _Wide(base) ** self

    def _cmp(self, o):
        r = self._raw(o)
        return None if r is None else (self.v > r) - (self.v < r)

    def __eq__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c == 0

    def __ne__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c != 0

    def __lt__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c < 0

    def __le__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c <= 0

    def __gt__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c > 0

    def __ge__(self, o):
        c = self._cmp(o)
        return NotImplemented if c is None else c >= 0

    def __hash__(self):    return hash(self.v)
    def __bool__(self):    return self.v != 0
    def __float__(self):   return float(self.v)
    def __int__(self):     return int(self.v)

    def __str__(self):
        """The EXACT decimal expansion, as the compiled binary prints it."""
        from decimal import Decimal, localcontext
        n, d = self.v.numerator, self.v.denominator
        if d == 1:
            return str(n)
        # d is a power of two, so the expansion terminates; give Decimal
        # enough precision to hold every digit of it.
        with localcontext() as ctx:
            ctx.prec = d.bit_length() * 2 + 40
            return str(Decimal(n) / Decimal(d))

    __repr__ = __str__


def _round_f32(value):
    """Round to IEEE single precision — the compiler maps f32 to LLVM float."""
    import struct
    try:
        return struct.unpack('f', struct.pack('f', float(value)))[0]
    except (OverflowError, ValueError):
        return float(value)


def _apply_float_type(value, vtype):
    """Coerce a numeric result to the precision the given Rubidium float type
    actually has in the compiled program."""
    if value is None or isinstance(value, bool):
        return value
    if vtype in WIDE_FLOAT_TYPES:
        return _Wide(value)
    if vtype == 'f32':
        return _round_f32(value)
    return float(value)


def _edit_distance(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, lb + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return dp[lb]

def _closest_name(word: str, candidates, max_dist: int = 1) -> str | None:
    best, best_d = None, max_dist + 1
    for c in candidates:
        d = _edit_distance(word.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    return best if best_d <= max_dist else None

KNOWN_MODULES = {
    'random', 'math', 'time', 'json', 'os', 'FFI', 'net', 'crypto', 'io',
}

BUILTIN_FNS = {
    'print', 'println', 'input', 'len', 'range', 'type', 'str', 'int',
    'float', 'bool', 'thread', 'open', 'file', 'abs', 'min', 'max',
    'round', 'floor', 'ceil',
}

NUMERIC_TYPES = {
    'i32', 'i64', 'i128', 'i256', 'i512', 'i1024', 'i2048',
    'f32', 'f64', 'f128', 'f256', 'f512', 'f1024', 'f2048',
}

HEAP_TYPES = {'list', 'index', 'dict', 'str'}

ALL_TYPES = NUMERIC_TYPES | {'str', 'bool', 'list', 'index', 'dict', 'Any', 'Null'}

MUTATING_METHODS = {'add', 'remove', 'set', 'pop', 'clear', 'sort',
                    'reverse', 'insert', 'update', 'delete', 'push'}

ANSI = {
    'ERROR':   '\033[1;31m',
    'WARNING': '\033[1;33m',
    'INFO':    '\033[1;36m',
    'RESET':   '\033[0m',
    'DIM':     '\033[2m',
    'BOLD':    '\033[1m',
}


# ──────────────────────────────────────────────────────────────────────────────
# Issue
# ──────────────────────────────────────────────────────────────────────────────

class Issue:
    __slots__ = ('severity', 'line', 'category', 'message', 'suggestion')

    def __init__(self, severity: str, line, category: str,
                 message: str, suggestion: str = ''):
        self.severity   = severity    # 'ERROR' | 'WARNING' | 'INFO'
        self.line       = line        # int | None
        self.category   = category
        self.message    = message
        self.suggestion = suggestion


# ──────────────────────────────────────────────────────────────────────────────
# Lexical scope
# ──────────────────────────────────────────────────────────────────────────────

class Scope:
    """Linked-list scope chain for variable tracking."""

    def __init__(self, parent=None):
        self.parent = parent
        self.vars: dict = {}
        # BUGFIX/FEATURE: tracks which names IN THIS SCOPE are currently
        # link-aliases (share their info dict with another variable) — see
        # VarDecl/Assign's `link` handling. Must be per-name, not a flag on
        # the shared dict itself: since alias and target share the SAME
        # dict object, a flag on the dict would make the TARGET also look
        # "linked" the moment the alias is created, causing the target's
        # own reassignment to incorrectly unlink from the alias instead of
        # the other way around.
        self.linked_names: set = set()

    def declare(self, name: str, info: dict):
        self.vars[name] = info

    def lookup(self, name: str):
        if name in self.vars:
            return self.vars[name]
        return self.parent.lookup(name) if self.parent else None

    def mark_used(self, name: str) -> bool:
        if name in self.vars:
            self.vars[name]['used'] = True
            return True
        return self.parent.mark_used(name) if self.parent else False

    def mark_dropped(self, name: str, line) -> bool:
        if name in self.vars:
            self.vars[name]['dropped']   = True
            self.vars[name]['drop_line'] = line
            return True
        return self.parent.mark_dropped(name, line) if self.parent else False

    def is_dropped(self, name: str) -> bool:
        info = self.lookup(name)
        return bool(info and info.get('dropped'))

# ──────────────────────────────────────────────────────────────────────────────
# Control-flow sentinels  (not real errors — just signals through the call stack)
# ──────────────────────────────────────────────────────────────────────────────

class _Return(Exception):
    def __init__(self, value=None): self.value = value

class _Break(Exception):    pass
class _Continue(Exception): pass


# ──────────────────────────────────────────────────────────────────────────────
# Debugger  –  lightweight interpreter / runtime-crash detector
# ──────────────────────────────────────────────────────────────────────────────

class Debugger:

    def __init__(self, source_lines=None, tokens=None, source_dir=None):
        self.scope          = Scope()
        self._root_scope    = self.scope  # global pool — non-local 'let' lands here
        self.line           = "?"
        self.errors         = []
        self.output         = []
        self._fn_defs       = {}      # name  -> ast.FnDef
        self._class_defs    = {}      # name  -> ast.ClassDef
        self._source_lines  = source_lines or []
        self._lmap          = {}      # ('var'|'fn'|'class'|'for', name) -> line
        self._try_depth     = 0       # >0 means inside a try block; errors raise instead of print
        self._math_block_type = None  # typed math block `(expr): TYPE` — governs division/result type
        self._dynvars       = {}      # runtime SY reflection backing store — see DynResolve/DynVarDecl
        self._os_active     = set()   # open os.start(id) session ids — see OsStart/OsRun/OsDrop
        self._timers        = {}      # BUG-11: time.timer_start(id) state — see the time.* branch
        self._modules       = {}      # BUG-13: imported .rub namespaces — see _load_module
        self._ffi_libs      = {}      # BUG-14: handle name -> ctypes.CDLL
        self._ffi_fns       = {}      # BUG-14: Rubidium name -> {handle, symbol, params, ret}
        self._source_dir    = source_dir  # folder to resolve `import <file>` against
        if tokens:
            self._build_lmap(tokens)


    # ── Token → line map  (lets us show real line numbers despite no AST locs) ─

    def _build_lmap(self, tokens):
        n = len(tokens)
        i = 0
        while i < n:
            kind, val, line = tokens[i][0], tokens[i][1], tokens[i][2]
            if kind == 'LET':
                j = i + 1
                if j < n and tokens[j][0] == 'MUT': j += 1
                if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                    self._lmap[('var', tokens[j][1])] = line
            elif kind == 'FN':
                j = i + 1
                if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                    self._lmap[('fn', tokens[j][1])] = line
            elif kind == 'CLASS':
                j = i + 1
                if j < n and tokens[j][0] == 'IDENT':
                    self._lmap[('class', tokens[j][1])] = line
            elif kind == 'FOR':
                j = i + 1
                if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                    self._lmap[('for', tokens[j][1])] = line
            elif kind == 'IMPORT':
                j = i + 1
                if j < n and tokens[j][0] == 'IDENT':
                    self._lmap[('import', tokens[j][1])] = line
                    # Scan for 'as alias'
                    j2 = j + 1
                    while j2 < n and tokens[j2][0] == 'DOT':
                        j2 += 2   # skip dot and next ident
                    if j2 < n and tokens[j2][0] == 'AS':
                        j2 += 1
                        if j2 < n and tokens[j2][0] == 'IDENT':
                            self._lmap[('import', tokens[j2][1])] = line
            i += 1


    # ── Entry point ───────────────────────────────────────────────────────────

    # ── FFI (BUG-14) ─────────────────────────────────────────────────────────
    # FFILoad/FFIBind used to be pure no-ops in a debug run, so every foreign
    # call returned Null (and `lib.fn()` even reported "method called on None")
    # while the compiled binary dlopen'd the library and returned real values.
    # ctypes gives the debugger the same capability, so a debug run exercises
    # the actual library exactly as the compiled program does.
    _FFI_CTYPES = {
        'i32': 'c_int', 'i64': 'c_longlong', 'i128': 'c_longlong',
        'f32': 'c_float', 'f64': 'c_double',
        'str': 'c_char_p', 'str+': 'c_char_p',
        'bool': 'c_int', 'Any': 'c_longlong',
    }

    def _ffi_ctype(self, rub_type):
        import ctypes
        return getattr(ctypes, self._FFI_CTYPES.get(rub_type or 'i64', 'c_longlong'))

    def _ffi_open(self, path):
        """dlopen a shared library, mirroring the runtime's search order:
        the path as given, then relative to the source folder."""
        import ctypes
        path = str(path)
        candidates = [path]
        if self._source_dir and not os.path.isabs(path):
            candidates.append(os.path.join(self._source_dir, path))
        for cand in candidates:
            try:
                return ctypes.CDLL(cand)
            except OSError:
                continue
        return None

    def _ffi_invoke(self, name, arg_values):
        """Call a bound foreign symbol. Returns (True, result) when `name` is
        an FFI binding, (False, None) otherwise, so callers can fall through
        to normal function resolution."""
        bind = self._ffi_fns.get(name)
        if bind is None:
            return False, None
        lib = self._ffi_libs.get(bind['handle'])
        if lib is None:
            self.error(f"FFI call '{name}()' — library '{bind['handle']}' was not loaded")
            return True, None
        try:
            fn = getattr(lib, bind['symbol'])
        except AttributeError:
            self.error(f"FFI call '{name}()' — symbol '{bind['symbol']}' not found in library")
            return True, None
        fn.argtypes = [self._ffi_ctype(t) for _n, t in bind['params']]
        fn.restype = None if not bind['ret'] else self._ffi_ctype(bind['ret'])
        call_args = []
        for value, (_pn, ptype) in zip(arg_values, bind['params']):
            if ptype in ('str', 'str+'):
                call_args.append(str(value if value is not None else "").encode())
            elif ptype in ('f32', 'f64'):
                call_args.append(float(value or 0))
            else:
                call_args.append(int(value or 0))
        try:
            result = fn(*call_args)
        except Exception as e:
            self.error(f"FFI call '{name}()' failed: {e}")
            return True, None
        if isinstance(result, bytes):
            result = result.decode(errors='replace')
        return True, result

    def _load_module(self, mod):
        """BUG-13: load `<mod>.rub` from the importing file's folder, run its
        top level in an isolated scope, and return a namespace object holding
        its variables, functions and classes. Returns None when there is no
        such file (an unknown import is the analyzer's job to report, not the
        debug run's job to crash on). Imports are cached and re-entrant-safe,
        so a cycle can't recurse forever."""
        if mod in self._modules:
            return self._modules[mod]
        base = self._source_dir or os.getcwd()
        path = os.path.join(base, f"{mod}.rub")
        if not os.path.isfile(path):
            return None
        ns = {"__namespace__": mod, "vars": {}, "fns": {}, "classes": {}, "scope": None}
        self._modules[mod] = ns          # cache first: breaks import cycles
        try:
            with open(path, 'r') as f:
                src = f.read()
            nodes = Parser(tokenize(src)).parse()
        except Exception as e:
            self.error(f"Could not import '{mod}': {e}")
            return ns

        # Run the module's top level with its own global pool, so its
        # variables never leak into the importer's namespace (spec: "Imported
        # files are isolated through namespaces").
        saved_scope, saved_root = self.scope, self._root_scope
        saved_fns, saved_classes = self._fn_defs, self._class_defs
        mod_scope = Scope()
        self.scope = self._root_scope = mod_scope
        self._fn_defs, self._class_defs = {}, {}
        try:
            for n in nodes:
                try:
                    self.execute(n)
                except (_Return, _Break, _Continue):
                    pass
            ns["fns"] = self._fn_defs
            ns["classes"] = self._class_defs
            ns["vars"] = mod_scope.vars
            ns["scope"] = mod_scope
        except Exception as e:
            self.error(f"Error while importing '{mod}': {type(e).__name__}: {e}")
        finally:
            self.scope, self._root_scope = saved_scope, saved_root
            self._fn_defs, self._class_defs = saved_fns, saved_classes
        return ns

    def _namespace_of(self, node):
        """The namespace object behind `node` (a Var naming an import), or None."""
        if not isinstance(node, ast.Var):
            return None
        info = self.scope.lookup(node.name)
        value = info.get("value") if info else None
        if isinstance(value, dict) and "__namespace__" in value:
            return value
        return None

    def _call_namespaced(self, ns, name, arg_nodes):
        """Call `ns.name(...)`. The module's own functions and classes must be
        visible while its body runs, so swap them in for the duration."""
        fn = ns["fns"].get(name)
        if fn is None:
            self.error(f"Unknown function '{ns['__namespace__']}.{name}()'")
            return None
        # Arguments are evaluated in the CALLER's scope, before switching.
        arg_values = [self.evaluate(a) for a in arg_nodes]
        saved_fns, saved_classes = self._fn_defs, self._class_defs
        saved_scope, saved_root = self.scope, self._root_scope
        self._fn_defs = dict(saved_fns); self._fn_defs.update(ns["fns"])
        self._class_defs = dict(saved_classes); self._class_defs.update(ns["classes"])
        # The module's own globals must be visible while its body runs — the
        # spec's FFI-wrapper example is exactly this: an imported file whose
        # function bodies reference the FFI handle declared at that file's top
        # level. Without the scope swap the body saw the CALLER's globals and
        # reported "Variable 'native' used before declaration".
        if ns.get("scope") is not None:
            self.scope = self._root_scope = ns["scope"]
        try:
            return self._call_fn(fn, arg_values)
        finally:
            self._fn_defs, self._class_defs = saved_fns, saved_classes
            self.scope, self._root_scope = saved_scope, saved_root

    def run(self, nodes):
        """Execute all top-level nodes; stop & report on first unhandled crash."""
        for node in nodes:
            try:
                self.execute(node)
            except _Return:
                pass   # top-level return is fine
            except _Break:
                self.error("'break' used outside of a loop")
            except _Continue:
                self.error("'continue' used outside of a loop")
            except RecursionError:
                self.error("Maximum recursion depth exceeded — possible infinite recursion")
                break
            except Exception as e:
                self.error(f"Unhandled runtime crash: {type(e).__name__}: {e}")
                break

        # ── Phase 2: auto-call main() per Rubidium execution model ───────────
        if 'main' in self._fn_defs:
            try:
                self._call_fn(self._fn_defs['main'], [])
            except _Return:
                pass
            except RecursionError:
                self.error("Maximum recursion depth exceeded in main()")
            except Exception as e:
                self.error(f"Unhandled runtime crash in main(): {type(e).__name__}: {e}")

        return len(self.errors) == 0


    # ── Call a user-defined function ──────────────────────────────────────────

    def _call_fn(self, fn_def, arg_values):
        fn_scope = Scope(parent=self.scope)
        for i, (pname, ptype) in enumerate(fn_def.params):
            fn_scope.declare(pname, {
                "value":   arg_values[i] if i < len(arg_values) else None,
                "type":    ptype or "Any",
                "dropped": False,
                "mutable": True,
            })
        saved, self.scope = self.scope, fn_scope
        result = None
        try:
            for stmt in fn_def.body:
                self.execute(stmt)
        except _Return as r:
            result = r.value
        finally:
            self.scope = saved
        return result


    # ── Statement executor ────────────────────────────────────────────────────

    def execute(self, node):
        if node is None:
            return

        # ── Variable Declaration ──────────────────────────────────────────────
        if isinstance(node, ast.VarDecl):
            self.line = self._lmap.get(('var', node.name), "?")
            # BUGFIX/FEATURE: `let b = link a` for a SCALAR value (int/float/
            # bool/str) previously just evaluated `link a` like any other
            # expression, which (for scalars) copies the CURRENT value once
            # — so b never tracked later changes to a, unlike collections,
            # which already worked correctly via Python's natural mutable-
            # reference sharing (LinkArg's existing handling). Mirrors the
            # real compiler's fix: share the exact same info dict as the
            # link target, so reads/writes to EITHER name see the same
            # underlying storage — until this name is reassigned directly
            # (see the Assign branch below), which unlinks it.
            # BUGFIX (bugs.log OPEN-10 debugger-parity follow-up): the
            # exclusion below for list/dict targets assumed Python's natural
            # mutable-reference sharing would keep a linked collection in
            # sync on its own — true only if the value is never actually
            # copied. But falling through skips straight to
            # `_deep_copy_value(...)` a few lines down, which DOES copy it,
            # silently breaking `link` for every collection: `let y = link x`
            # then `x(0).set(99)` never showed up through `y`. Alias
            # collections the exact same way as scalars (share the info
            # dict) instead of special-casing them out.
            if isinstance(node.value, ast.LinkArg) and isinstance(node.value.expr, ast.Var):
                target_info = self.scope.lookup(node.value.expr.name)
                if target_info is not None:
                    target = self.scope if node.is_local else self._root_scope
                    target.declare(node.name, target_info)
                    target.linked_names.add(node.name)
                    return
            if isinstance(node.value, ast.FFILoad):
                # BUG-14: bind the loaded library to THIS variable's name, so
                # `fn <name> sym(...) as alias` and `<name>.alias(...)` resolve.
                value = self.evaluate(node.value)
                self._ffi_libs[node.name] = value.get("__lib__")
                if value.get("__lib__") is None:
                    self.error(f"FFI library not found: {value.get('__ffi__')}")
            else:
                value = self._deep_copy_value(self.evaluate(node.value))
            if node.vtype:
                value = self._clamp_int(value, node.vtype)
            # Per spec: 'let' without 'local' enters the global memory pool.
            target = self.scope if node.is_local else self._root_scope
            target.declare(node.name, {
                "value":   value,
                "type":    node.vtype or self.rub_type(value),
                "mutable": node.mutable,
                "dropped": False,
                "line":    self.line,
            })

        # ── Dynamic (runtime SY reflection) Declaration ────────────────────────
        elif isinstance(node, ast.DynVarDecl):
            # BUGFIX/FEATURE: `let (x): dict = {}` where x is a runtime-
            # dynamic SY variable. Mirrors the real compiler's
            # rub_dynvar_set — stores into self._dynvars keyed by x's
            # CURRENT string value, rather than a fixed compile-time name.
            key_info = self.scope.lookup(node.holder_name)
            key = key_info["value"] if key_info is not None else None
            if key is None:
                self.error(f"Variable '{node.holder_name}' used before declaration")
                return
            self._dynvars[key] = self._deep_copy_value(self.evaluate(node.value))

        # ── Assignment ────────────────────────────────────────────────────────
        elif isinstance(node, ast.Assign):
            info = self.scope.lookup(node.name)
            if info is None:
                self.error(f"Assignment to undeclared variable '{node.name}'")
                return
            if info.get("dropped"):
                self.error(f"Use of dropped variable '{node.name}'")
                return
            if not info.get("mutable", True):
                self.error(f"Assignment to immutable variable '{node.name}' (declare with 'mut')")
                return
            # Find which scope actually holds this name (needed both to
            # check/clear its linked_names set and, if unlinking, to
            # replace its entry there).
            s = self.scope
            while s is not None and node.name not in s.vars:
                s = s.parent
            owning_scope = s if s is not None else self.scope
            if node.name in owning_scope.linked_names:
                # BUGFIX/FEATURE: reassigning a linked scalar directly must
                # unlink it — give it its own independent info dict (a copy
                # of the shared one) instead of mutating the shared dict,
                # which would incorrectly also change the link target's
                # value. Checked by NAME in the scope that owns it, not a
                # flag on the dict itself (see linked_names' docstring for
                # why that distinction matters).
                info = dict(info)
                owning_scope.linked_names.discard(node.name)
                owning_scope.declare(node.name, info)
            declared_type = info.get("type")
            new_value = self._deep_copy_value(self.evaluate(node.value))
            if declared_type in self._INT_BOUNDS:
                # Reassignment keeps the variable's originally declared sized
                # type (Rubidium requires same-type reassignment) — clamp to
                # that width and preserve the declared type, rather than
                # re-inferring a generic "i64" from the raw Python value.
                info["value"] = self._clamp_int(new_value, declared_type)
                info["type"]  = declared_type
            else:
                info["value"] = new_value
                info["type"]  = self.rub_type(new_value)

        # ── Field Assignment ──────────────────────────────────────────────────
        elif isinstance(node, ast.FieldAssign):
            obj = self.evaluate(node.obj) if hasattr(node, 'obj') else None
            if isinstance(obj, dict):
                val = self.evaluate(node.value)
                obj[node.field] = val
                # BUGFIX (bugs.log OPEN-8 follow-up): _dispatch_method also
                # seeds a bare-name mirror of each field into fn_scope (so
                # `health = health - amount`, without `self.`, works) and
                # blindly writes THAT snapshot back into obj at the end of
                # the call. If this method instead used `self.field = value`
                # directly (as here), that end-of-call write-back would
                # clobber it with the stale pre-call value. Keep the mirror
                # in sync immediately so whichever style is used, the other
                # doesn't undo it.
                self_info = self.scope.lookup("__self")
                if self_info is not None and self_info.get("value") is obj:
                    mirror = self.scope.lookup(node.field)
                    if mirror is not None:
                        mirror["value"] = val

        # ── Element Drop: items(1).drop() — remove-and-shift, not Null ────────
        elif isinstance(node, ast.ElementDrop):
            keys = []
            curr = node.access_node
            while isinstance(curr, (ast.FnCall, ast.MethodCall)):
                if curr.args: keys = list(curr.args) + keys
                curr = curr.obj if isinstance(curr, ast.MethodCall) else curr.name
            # `curr` is now the base collection name (str) or an expr node
            coll = self.scope.lookup(curr)["value"] if isinstance(curr, str) else self.evaluate(curr)
            for k in keys[:-1]:
                key_v = self.evaluate(k)
                coll = coll[key_v] if isinstance(coll, dict) else coll[int(key_v)]
            if keys:
                last = self.evaluate(keys[-1])
                if isinstance(coll, list):
                    idx = int(last)
                    if 0 <= idx < len(coll): del coll[idx]
                elif isinstance(coll, dict):
                    if last in coll: del coll[last]

        # ── Drop ──────────────────────────────────────────────────────────────
        elif isinstance(node, ast.Drop):
            info = self.scope.lookup(node.name)
            if info is None:
                self.error(f"Cannot drop undeclared variable '{node.name}'")
            elif info.get("dropped"):
                self.error(f"Variable '{node.name}' is already dropped")
            else:
                info["dropped"] = True
                info["value"]   = None

        # ── Print ─────────────────────────────────────────────────────────────
        elif isinstance(node, ast.Print):
            value = self.evaluate(node.value)
            out   = self._format_value(value)
            print(out)
            self.output.append(out)

        elif isinstance(node, ast.Println):
            value = self.evaluate(node.value)
            out   = self._format_value(value)
            print(f"\r{out}", end="")
            self.output.append(out)

        # ── If ────────────────────────────────────────────────────────────────
        elif isinstance(node, ast.If):
            cond = self.evaluate(node.cond)
            body = node.then_body if cond else (node.else_body or [])
            for stmt in body:
                self.execute(stmt)

        # ── While ─────────────────────────────────────────────────────────────
        elif isinstance(node, ast.While):
            count = 0
            while self.evaluate(node.cond):
                try:
                    for stmt in node.body:
                        self.execute(stmt)
                except _Break:
                    break
                except _Continue:
                    pass
                count += 1
                if count > 100_000:
                    self.error("Possible infinite loop (exceeded 100 000 iterations)")
                    break

        # ── For ───────────────────────────────────────────────────────────────
        elif isinstance(node, ast.For):
            self.line = self._lmap.get(('for', node.var), "?")
            self._exec_for(node)

        # ── Return / Break / Continue ─────────────────────────────────────────
        elif isinstance(node, ast.Return):
            raise _Return(self.evaluate(node.value))

        elif isinstance(node, ast.Break):
            raise _Break()

        elif isinstance(node, ast.Continue):
            raise _Continue()

        # ── Raise ─────────────────────────────────────────────────────────────
        # BUG-9: `raise` was not handled at all, so the debug run silently fell
        # through it and kept executing — `try { check(-1) } error { ... }`
        # printed the function's success path instead of the error message the
        # compiled binary produces. Per spec, the nearest enclosing try catches
        # it (directly, or through a function called from inside that try);
        # outside any try it halts the program with the message. self.error()
        # already implements exactly that split via _try_depth.
        elif isinstance(node, ast.Raise):
            msg = self.evaluate(node.message)
            self.error(str(msg) if msg is not None else "raised error")

        # ── Try / Error ───────────────────────────────────────────────────────
        elif isinstance(node, ast.Try):
            self._try_depth += 1
            caught_exc = None
            try:
                for stmt in node.try_body:
                    self.execute(stmt)
            except (_Return, _Break, _Continue):
                self._try_depth -= 1
                raise   # let control-flow signals pass through
            except Exception as exc:
                caught_exc = exc
            # Decrement BEFORE running the error handler so that any
            # errors inside the handler are still reported normally.
            self._try_depth -= 1
            if caught_exc is not None:
                err_scope = Scope(parent=self.scope)
                err_scope.declare("error", {
                    "value": str(caught_exc), "type": "str",
                    "dropped": False,  "mutable": False,
                })
                saved, self.scope = self.scope, err_scope
                try:
                    for stmt in node.error_body:
                        self.execute(stmt)
                finally:
                    self.scope = saved

        # ── Function Definition ───────────────────────────────────────────────
        elif isinstance(node, ast.FnDef):
            self.line = self._lmap.get(('fn', node.name), "?")
            self._fn_defs[node.name] = node

        # ── Class Definition ──────────────────────────────────────────────────
        elif isinstance(node, ast.ClassDef):
            self.line = self._lmap.get(('class', node.name), "?")
            self._class_defs[node.name] = node

        # ── Expression statements ─────────────────────────────────────────────
        elif isinstance(node, (ast.FnCall, ast.MethodCall, ast.CollectionMethodCall)):
            self.evaluate(node)

        # ── Use / Import — declare a stub module object so method calls don't cascade ──
        elif isinstance(node, (ast.Use, ast.Import)):
            mod = node.module_name
            self.line = self._lmap.get(('import', mod), self.line)
            # BUG-13: `import <file>` was stubbed out exactly like `use <builtin
            # module>`, so every namespaced access into an imported .rub file
            # returned Null in a debug run while the compiled binary produced
            # real values. Actually load the file (spec: "import loads an
            # external .rub file from the same folder into your code with the
            # name of the file as the namespace"), run its top level in its own
            # isolated scope, and keep its functions/classes for later
            # `ns.fn()` / `ns.var` resolution. `use` keeps the old stub — those
            # ARE compiler-provided modules with no file behind them.
            value = None
            if isinstance(node, ast.Import):
                value = self._load_module(mod)
            if value is None:
                value = {"__module__": mod}
            self.scope.declare(mod, {"value": value, "type": "module",
                                     "mutable": False, "dropped": False})
            # Also register the alias (import tokeniser as tk → declare 'tk' too).
            # Per spec an alias "does not copy or duplicate the module", so it
            # shares the very same namespace object.
            alias = getattr(node, 'alias', None)
            if alias:
                self.line = self._lmap.get(('import', alias), self.line)
                self.scope.declare(alias, {"value": value, "type": "module",
                                           "mutable": False, "dropped": False})

        # ── Threads (bugs.log OPEN-7): actually run the task function's body
        # synchronously — the debugger is single-threaded, so this is an
        # approximation of concurrency, but it means real prints/side
        # effects/runtime errors from the task surface during a debug run,
        # instead of the task silently never running at all. ──────────────
        elif isinstance(node, ast.ThreadCall):
            self.evaluate(node.func_call)

        # ── Stubs: FFI / Thread wait-bookkeeping (still not simulated) ──
        # BUGFIX (bugs.log OPEN-7): OsStart/OsRun/OsDrop used to be stubbed
        # out here as no-ops, which meant os.start(1) (a bare statement)
        # never actually reached evaluate()'s real implementation below —
        # only os.run() did (since it's usually used as an expression, via
        # VarDecl/Assign calling evaluate() directly), so os.run() always
        # errored with "no os.start() was seen first" even right after a
        # real os.start() call. Removed from the stub list so all three
        # fall through to the real evaluate()-based implementation.
        # BUG-14: FFIBind registers a real binding now (FFILoad is handled in
        # evaluate(), reached through the VarDecl that holds it).
        elif isinstance(node, ast.FFIBind):
            self._ffi_fns[node.alias or node.symbol_name] = {
                'handle': node.handle_name,
                'symbol': node.symbol_name,
                'params': node.params or [],
                'ret':    node.ret_type,
            }

        elif isinstance(node, (ast.ThreadWait, ast.ThreadRunning)):
            pass   # bookkeeping only — see the evaluate() branches

        # ── File I/O — real file operations, matching the compiled runtime ──
        elif isinstance(node, ast.FileOpen):
            path = self.evaluate(node.path_expr)
            # BUG-12: per spec, "If the file does not exist, open() creates it,
            # raises a (non-fatal, catchable) error, and then continues" — and
            # the compiled runtime does exactly that (file_open returns -2:
            # created, error flag set, block still runs). The debugger instead
            # raised a bare Exception without creating anything, so the FIRST
            # `open()` of a not-yet-existing file aborted the whole debug run
            # ("Unhandled runtime crash in main()") while the real program ran
            # fine. Create the file, and only surface the error when there is a
            # try to catch it — matching the compiler, which otherwise just
            # leaves the flag set and carries on.
            missing = not os.path.exists(str(path))
            if missing:
                try:
                    open(str(path), "w").close()
                except OSError as e:
                    raise Exception(f"file error: {e}")
            if missing and self._try_depth > 0:
                raise RuntimeError("file not found")
            self.scope.declare(node.var_name, {
                "value": {"__file__": True, "path": str(path)},
                "type": "file", "dropped": False, "mutable": True,
            })
            for stmt in node.body:
                self.execute(stmt)

        elif isinstance(node, ast.FileNew):
            path = str(self.evaluate(node.path_expr))
            open(path, "w").close()
            if node.body:
                self.scope.declare("file", {
                    "value": {"__file__": True, "path": path},
                    "type": "file", "dropped": False, "mutable": True,
                })
                for stmt in node.body:
                    self.execute(stmt)

        elif isinstance(node, ast.FileHandleStmt):
            self._file_handle_op(node.var_name, node.method, node.args)

        elif isinstance(node, ast.FileExists):
            pass  # handled as an expression via evaluate(); nothing to do as a statement

        elif isinstance(node, ast.FileDelete):
            path = str(self.evaluate(node.path_expr))
            if os.path.exists(path): os.remove(path)

        elif isinstance(node, ast.FileRename):
            old, new = str(self.evaluate(node.old_path)), str(self.evaluate(node.new_path))
            if os.path.exists(old): os.rename(old, new)

        elif isinstance(node, ast.FileCopy):
            src, dst = str(self.evaluate(node.src_path)), str(self.evaluate(node.dst_path))
            if os.path.exists(src): shutil.copy(src, dst)

        else:
            self.evaluate(node)


    # ── For-loop helper ───────────────────────────────────────────────────────

    def _exec_for(self, node):
        if node.iterable is not None:
            # for x in <collection>
            iterable = self.evaluate(node.iterable)
            if isinstance(iterable, bool):
                pass  # fall through to the not-iterable error below
            elif isinstance(iterable, int):
                # Per spec: `for i in N` (e.g. `for i in str.len()`) iterates
                # i from 0 to N-1 — same as `for i in range(0, N)`.
                iterable = range(iterable)
            elif isinstance(iterable, dict) and iterable.get("__file__"):
                # for line in file — iterate the file's lines (no trailing \n)
                path = iterable["path"]
                with open(path, "r") as f:
                    iterable = [ln.rstrip("\n") for ln in f.readlines()]
            if iterable is None:
                self.error(
                    f"'for {node.var} in ...' — iterable resolved to None.\n"
                    f"         Check the variable exists and has not been dropped."
                )
                return
            if not hasattr(iterable, '__iter__'):
                self.error(
                    f"'for {node.var} in ...' — value of type "
                    f"'{self.rub_type(iterable)}' is not iterable"
                )
                return
            for item in iterable:
                self.scope.declare(node.var, {
                    "value": item, "type": self.rub_type(item),
                    "dropped": False, "mutable": False,
                })
                try:
                    for stmt in node.body:
                        self.execute(stmt)
                except _Break:
                    return
                except _Continue:
                    continue
        else:
            # for x in start..end
            start = self.evaluate(node.start)
            end   = self.evaluate(node.end)
            if start is None:
                self.error(f"'for {node.var}' — range start resolved to None")
                return
            if end is None:
                self.error(f"'for {node.var}' — range end resolved to None")
                return
            try:
                start, end = int(start), int(end)
            except (TypeError, ValueError):
                self.error(f"'for {node.var}' — range values must be integers, got '{start}' and '{end}'")
                return
            step = 1 if start <= end else -1
            for i in range(start, end, step):
                self.scope.declare(node.var, {
                    "value": i, "type": "i64",
                    "dropped": False, "mutable": False,
                })
                try:
                    for stmt in node.body:
                        self.execute(stmt)
                except _Break:
                    return
                except _Continue:
                    continue


    # ── Expression evaluator ──────────────────────────────────────────────────

    def evaluate(self, node):
        if node is None:
            return None

        if isinstance(node, ast.FileHandleMethod):
            return self._file_handle_op(node.var_name, node.method, node.args)

        if isinstance(node, ast.FileExists):
            path = self.evaluate(node.path_expr)
            return os.path.exists(str(path)) if path is not None else False

        if isinstance(node, ast.LinkArg):
            # Link Rule: pass-by-reference. _call_fn binds params directly
            # to the evaluated arg value with no copy (matching the
            # compiler), so evaluating the inner expr already gives the
            # shared-reference behavior the spec describes.
            return self.evaluate(node.expr)

        if isinstance(node, ast.Number):
            return node.value

        if isinstance(node, ast.Str):
            # BUGFIX: the real compiler interprets \n/\t escape sequences
            # at codegen time (CodeGen.intern_str), not in the lexer — so
            # the debugger's own separate interpreter, which never went
            # through that code path, was returning string literals with
            # escape sequences completely uninterpreted (e.g. a literal
            # backslash-n instead of an actual newline). Matches
            # intern_str's exact replacement set.
            return node.value.replace("\\n", "\n").replace("\\t", "\t")

        if isinstance(node, ast.Bool):
            v = str(node.value).lower()
            return True if v == "true" else (False if v == "false" else None)

        if isinstance(node, ast.None_):
            return None

        if isinstance(node, ast.InterpolatedStr):
            parts = []
            for part in node.parts:
                val = self.evaluate(part)
                # BUGFIX: was plain Python str(val) — showed e.g. "25.0" for
                # a whole-number float (Python's default float repr) instead
                # of matching the real compiler's %g-based formatting, and
                # wouldn't show Null/True/False Rubidium-style either.
                parts.append(self._format_value(val) if val is not None else "")
            return "".join(parts)

        if isinstance(node, ast.Var):
            # Type names used as arguments (e.g. random(0, 10, i32)) are not variables
            if node.name in ALL_TYPES:
                return node.name
            info = self.scope.lookup(node.name)
            if info is None:
                self.error(f"Variable '{node.name}' used before declaration")
                return None
            if info.get("dropped"):
                self.error(f"Variable '{node.name}' used after drop")
                return None
            return info["value"]

        if isinstance(node, ast.DynResolve):
            # BUGFIX/FEATURE: runtime SY reflection — mirrors the real
            # compiler's rub_dynvar_get. self._dynvars is a plain dict
            # standing in for the compiler's runtime hash-map; the key is
            # the holder variable's CURRENT string value (so it can differ
            # every time this is reached, e.g. inside a loop).
            key_info = self.scope.lookup(node.holder_name)
            key = key_info["value"] if key_info is not None else None
            if key is None or key not in self._dynvars:
                self.error(f"undefined dynamic variable '{key}'")
                return None
            return self._dynvars[key]

        if isinstance(node, ast.UnaryOp):
            val = self.evaluate(node.value)
            if node.op == "-":   return -val if val is not None else None
            if node.op == "not": return not val
            if node.op == "*/":
                import math; return math.sqrt(val) if val is not None else None
            return val

        if isinstance(node, ast.BinOp):
            left  = self.evaluate(node.left)
            right = self.evaluate(node.right)
            # BUG-6: inside a `(...): f128`/f256/... block every operation is
            # computed at binary128, exactly as the compiled program does.
            # Promoting one operand is enough — _Wide's operators handle the
            # other side and round the result.
            if (self._math_block_type in WIDE_FLOAT_TYPES
                    and node.op in ("+", "-", "*", "/", "**", "%")
                    and not isinstance(left, str) and not isinstance(right, str)
                    and left is not None and right is not None):
                if not isinstance(left, _Wide):
                    left = _Wide(left)
            # BUGFIX: these must run BEFORE the try/except below — the
            # generic `except Exception as e: self.error(f"Operation ...")`
            # handler was catching the RuntimeError that self.error() itself
            # raises (when inside a try/error block) and re-wrapping it with
            # an unwanted "Operation '/' failed:" prefix, so a plain
            # "Division by zero" never actually reached the user — it always
            # showed as "Operation '/' failed: Division by zero" instead,
            # diverging from the real compiler's plain message.
            if node.op == "/" and right == 0:
                self.error("Division by zero", highlight="/"); return None
            if node.op == "*/" and left == 0:
                self.error("Division by zero", highlight="*/"); return None
            try:
                if node.op == "+":
                    if isinstance(left, str) or isinstance(right, str):
                        return str(left or "") + str(right or "")
                    return left + right
                if node.op == "-":   return left - right
                if node.op == "*":   return left * right
                if node.op == "/":
                    # Typed math block with a float type: compute at that
                    # precision, i.e. float division even if both operands are
                    # ints (matches the compiler's _math_block_type override).
                    if self._math_block_type and self._math_block_type[0] == 'f':
                        return left / right
                    if isinstance(left, int) and isinstance(right, int):
                        # Truncate toward zero (matches the compiler's sdiv),
                        # not Python's floor division.
                        q = left / right
                        return int(q) if q >= 0 else -int(-q)
                    return left / right
                if node.op == "*/":
                    # n */ value -> value's n-th root (matches the unary
                    # `*/value` sqrt case, generalized to any degree).
                    return right ** (1.0 / left)
                if node.op == "**":  return left ** right
                if node.op == "%":
                    if right == 0:
                        self.error("Division by zero", highlight="%"); return None
                    return left % right
                if node.op == "and": return bool(left) and bool(right)
                if node.op == "or":  return bool(left) or bool(right)
            except Exception as e:
                self.error(f"Operation '{node.op}' failed: {e}")
            return None

        if isinstance(node, ast.Compare):
            left  = self.evaluate(node.left)
            right = self.evaluate(node.right)
            try:
                # Spec: Null (None) is smaller than every non-null value.
                # Null == Null is True; Null compared to non-null follows inequality rules.
                if left is None or right is None:
                    if node.op == "==": return (left is None) and (right is None)
                    if node.op == "!=": return not ((left is None) and (right is None))
                    if node.op == "<":  return (left is None) and (right is not None)
                    if node.op == ">":  return (right is None) and (left is not None)
                    if node.op == "<=": return left is None          # Null <= anything
                    if node.op == ">=": return right is None         # anything >= Null
                    return False
                if node.op == "==":  return left == right
                if node.op == "!=":  return left != right
                if node.op == ">":   return left > right
                if node.op == "<":   return left < right
                if node.op == ">=":  return left >= right
                if node.op == "<=":  return left <= right
            except Exception as e:
                self.error(f"Comparison '{node.op}' failed: {e}")
            return False

        if isinstance(node, ast.TypeCast):
            val = self.evaluate(node.expr)
            t   = node.target_type
            try:
                if t in ("i32","i64","i128","i256","i512","i1024","i2048"): return int(val)
                # BUG-6: f128+ are binary128, not double (syntax lines 275-281).
                if t in ("f32","f64") or t in WIDE_FLOAT_TYPES:
                    return _apply_float_type(val, t)
                if t == "str":   return str(val)
                if t == "bool":  return bool(val)
            except Exception as e:
                self.error(f"Type cast to '{t}' failed: {e}")
            return val

        if isinstance(node, ast.MathBlock):
            # FEATURE (typed math block): compute the inner expression at
            # node.vtype's kind. While it's active, division inside is float
            # division if the block type is a float (see the BinOp `/` case),
            # matching the compiler's "compute the whole block at that type".
            prev = self._math_block_type
            self._math_block_type = node.vtype
            try:
                val = self.evaluate(node.expr)
            finally:
                self._math_block_type = prev
            if val is None:
                return None
            try:
                if node.vtype and node.vtype[0] == 'f':  # float type
                    # BUG-6: round to the precision the type really has —
                    # binary128 for f128+, single for f32, double otherwise.
                    return _apply_float_type(val, node.vtype)
                if node.vtype and node.vtype[0] == 'i':  # int type -> int result
                    return int(val)
            except Exception:
                pass
            return val

        if isinstance(node, ast.ListExpr):
            return [self.evaluate(x) for x in node.elements]

        if isinstance(node, ast.DictExpr):
            result = {}
            for k, v in node.pairs:
                result[self.evaluate(k)] = self.evaluate(v)
            return result

        if isinstance(node, ast.FieldAccess):
            # BUG-13: `math_tools.pi` — a variable read out of an imported
            # file's namespace, which lives in that module's own scope rather
            # than as a key on the namespace dict itself.
            ns = self._namespace_of(node.obj)
            if ns is not None:
                entry = ns["vars"].get(node.field)
                if entry is None:
                    self.error(f"Unknown symbol '{ns['__namespace__']}.{node.field}'")
                    return None
                return entry.get("value")
            obj = self.evaluate(node.obj)
            if isinstance(obj, dict):
                return obj.get(node.field)
            return None

        if isinstance(node, ast.Input):
            return ""   # can't do real input in debug mode

        if isinstance(node, ast.FnCall):
            return self._eval_fn_call(node)

        if isinstance(node, (ast.MethodCall, ast.CollectionMethodCall)):
            return self._eval_method_call(node)

        # BUG-14: `let lib = FFI("libs/mylib.so")` — actually dlopen it. The
        # returned marker is what makes `lib.fn()` resolvable later; the
        # VarDecl branch registers the handle under the variable's name, which
        # is the name FFIBind refers to.
        if isinstance(node, ast.FFILoad):
            path = self.evaluate(node.path_expr)
            lib = self._ffi_open(path)
            return {"__ffi__": str(path), "__loaded__": lib is not None, "__lib__": lib}

        if isinstance(node, ast.OsStart):
            id_ = self.evaluate(node.id_expr)
            self._os_active.add(id_)
            return None

        if isinstance(node, ast.OsDrop):
            id_ = self.evaluate(node.id_expr)
            self._os_active.discard(id_)
            return None

        # BUG-11: `thread.running(id)` parses to its own AST node, not a
        # MethodCall, so it never reached the thread.* intercept and fell
        # through to `return None` — printing Null where the compiled binary
        # prints False. The debugger runs thread() bodies synchronously, so a
        # thread is always already finished by the time this is asked.
        if isinstance(node, ast.ThreadRunning):
            return False

        if isinstance(node, ast.ThreadWait):
            return None

        if isinstance(node, ast.OsRun):
            # BUGFIX (bugs.log OPEN-7): OsStart/OsRun/OsDrop had no runtime
            # handling at all — os.run() always fell through to `return
            # None`, printing "Null" instead of the command's real output
            # (diverging from the real compiler, which actually forks a
            # shell and captures it). Mirrors that behavior via subprocess.
            if node.struct_args is not None:
                fields = node.struct_args
                cmd = self.evaluate(fields.get("cmd"))
                if "args" in fields:
                    arg_list = self.evaluate(fields["args"]) or []
                    cmd = " ".join([str(cmd)] + [str(a) for a in arg_list])
                inp = self.evaluate(fields["input"]) if fields.get("input") is not None else None
                id_ = 0
                self._os_active.add(0)  # struct form auto-starts session 0
            else:
                id_ = self.evaluate(node.id_expr) if node.id_expr is not None else None
                cmd = self.evaluate(node.cmd_expr)
                inp = self.evaluate(node.input_expr) if node.input_expr is not None else None

            if id_ is not None and id_ not in self._os_active:
                self.error(f"os.run() used ID {id_}, but no os.start({id_}) was seen first.")
                return None
            try:
                proc = subprocess.run(
                    ["bash", "-c", str(cmd) if cmd is not None else ""],
                    input=(str(inp) if inp else None),
                    capture_output=True, text=True, timeout=10,
                )
                return proc.stdout + proc.stderr
            except Exception as e:
                self.error(f"os.run() failed: {e}")
                return None

        if isinstance(node, ast.ClassInstantiate):
            cls = self._class_defs.get(node.class_name)
            if cls:
                instance = {"__class__": node.class_name}
                for f in (cls.fields or []):
                    instance[f.name] = self.evaluate(f.value) if hasattr(f, 'value') else None
                return instance
            return None

        return None


    # ── FnCall evaluator ──────────────────────────────────────────────────────

    def _eval_fn_call(self, node):
        fname = node.name if isinstance(node.name, str) else None
        args  = [self.evaluate(a) for a in node.args]

        # BUG-14: a bound FFI symbol called by its Rubidium name
        # (`fn lib rb_sin(x: f64) -> f64 as sin` then `sin(0.5)`). Checked
        # before the builtins below so a binding named e.g. `sin` — exactly
        # the syntax file's FFI RENAMING example — reaches the library rather
        # than the interpreter's own math builtin.
        if fname is not None:
            handled, result = self._ffi_invoke(fname, args)
            if handled:
                return result

        # Built-ins
        if fname == "random":
            # BUGFIX/FEATURE: random(min, max, type) was entirely
            # unimplemented — fell through every dispatch branch below
            # (not a builtin match, not a user fn, not a class, not a
            # declared variable) straight to `return None`, silently
            # producing Null for any use of this builtin.
            if len(args) >= 2:
                lo, hi = args[0], args[1]
                want_float = len(args) >= 3 and str(args[2]) in (
                    "f32", "f64", "f128", "f256", "f512", "f1024", "f2048")
                if want_float:
                    return random.uniform(float(lo), float(hi))
                return random.randint(int(lo), int(hi))
            return None
        if fname == "print":
            if args:
                print(self._format_value(args[0]))
            return None
        if fname == "println":
            if args: print(f"\r{self._format_value(args[0])}", end="")
            return None
        if fname == "input":
            return ""
        if fname == "len":
            return len(args[0]) if args and hasattr(args[0], '__len__') else None
        if fname == "abs":
            return abs(args[0]) if args else None
        if fname == "min":
            return min(*args) if len(args) > 1 else (min(args[0]) if args else None)
        if fname == "max":
            return max(*args) if len(args) > 1 else (max(args[0]) if args else None)
        if fname == "round":
            return round(args[0], args[1] if len(args) > 1 else 0) if args else None
        if fname == "range":
            # Per spec: range(start, end) — end is exclusive (matches Python's range).
            if len(args) >= 2:
                return range(int(args[0]), int(args[1]))
            if len(args) == 1:
                return range(int(args[0]))

        # User-defined function
        if fname and fname in self._fn_defs:
            return self._call_fn(self._fn_defs[fname], args)

        # Class instantiation
        if fname and fname in self._class_defs:
            cls = self._class_defs[fname]
            instance = {"__class__": fname}
            for f in (cls.fields or []):
                instance[f.name] = self.evaluate(f.value) if hasattr(f, 'value') else None
            # BUGFIX (bugs.log OPEN-8): mirror the compiler's constructor fix —
            # if the class declares an __init__ method and constructor args
            # were passed, run it (via the normal method dispatch, which
            # already binds fields + "__self" into scope) so field values set
            # inside __init__ actually take effect, instead of always leaving
            # every field at its bare declared-default value.
            if args and any(m.name == "__init__" for m in (cls.methods or [])):
                self._dispatch_method(instance, "__init__", args)
            return instance

        # Collection index access: name is a declared variable  e.g. my_list(0)
        if fname:
            info = self.scope.lookup(fname)
            if info is not None:
                col = info["value"]
                for arg in args:
                    if isinstance(col, list):
                        try:
                            col = col[int(arg)]
                        except (IndexError, TypeError) as e:
                            self.error(f"Index error on '{fname}': {e}")
                            return None
                    elif isinstance(col, dict):
                        # BUGFIX: dict.get() silently returns None for a
                        # missing key — the real compiler raises a
                        # catchable "key not found in collection" runtime
                        # error instead, so try/error around an invalid
                        # lookup actually catches something instead of
                        # silently continuing with a bogus None/Null value.
                        if arg not in col:
                            self.error("collection access error" if self._try_depth > 0 else "key not found in collection")
                            return None
                        col = col[arg]
                    else:
                        return None
                return col
        elif node.name is not None:
            # BUGFIX: node.name being non-string means this is either the
            # existing chained-collection pattern (nested(0)(1) -> a nested
            # FnCall as .name) or the new DynResolve node (runtime SY
            # reflection) — both previously fell straight through to the
            # `return None` below, since only the fname branch actually
            # evaluated anything. Evaluate node.name itself to get the base
            # collection, then apply the same indexing loop.
            col = self.evaluate(node.name)
            for arg in args:
                if isinstance(col, list):
                    try:
                        col = col[int(arg)]
                    except (IndexError, TypeError) as e:
                        self.error(f"Index error: {e}")
                        return None
                elif isinstance(col, dict):
                    if arg not in col:
                        self.error("collection access error" if self._try_depth > 0 else "key not found in collection")
                        return None
                    col = col[arg]
                else:
                    return None
            return col

        return None


    # ── MethodCall evaluator ──────────────────────────────────────────────────

    def _eval_method_call(self, node):
        method = node.method
        # BUGFIX: `x.to(SY)` — SY here is a bare type-keyword argument (like
        # i32/f64/etc are elsewhere), not a real variable reference, but the
        # parser represents it the same as any other identifier (Var("SY")).
        # The real compiler special-cases this at the type-conversion call
        # site; evaluating it generically here crashed with "Variable 'SY'
        # used before declaration" before even reaching the method-specific
        # dispatch below (SY is never actually declared as a variable).
        args = [
            "SY" if isinstance(a, ast.Var) and a.name == "SY" else self.evaluate(a)
            for a in node.args
        ]

        # ── random.shuffle()/choice()/seed() — the `random` module has no
        # dedicated AST node (unlike os.*/time.*'s OsStart/OsRun/OsDrop), so
        # `random.choice(nums)` parses as an ordinary MethodCall on
        # Var("random"). `use random` only declares a generic module stub
        # dict in scope (see the ast.Use branch in execute()), which the
        # generic `_dispatch_method` below doesn't know "choice"/"shuffle"/
        # "seed" on — it silently returned None (printed as Null), diverging
        # from the real compiled binary. BUGFIX (bugs.log): intercept here,
        # before the generic obj/dispatch path.
        if (isinstance(node.obj, ast.Var) and node.obj.name == "random"
                and method in ("shuffle", "choice", "seed")):
            if method == "seed":
                if args: random.seed(args[0])
                return None
            if method == "shuffle":
                # args[0] is the SAME live list object stored in scope
                # (evaluate(Var) returns info["value"] directly, no copy),
                # so an in-place Python shuffle mutates the real variable —
                # matching the spec's "shuffle a list in place".
                if args and isinstance(args[0], list):
                    random.shuffle(args[0])
                return None
            if method == "choice":
                if args:
                    coll = args[0]
                    if isinstance(coll, list) and coll:
                        return random.choice(coll)
                    if isinstance(coll, dict) and coll:
                        return random.choice(list(coll.values()))
                return None

        # ── lib.alias(...) on an FFI handle — BUG-14. The syntax file's own
        # FFI WRAPPERS example calls a bound symbol this way.
        if isinstance(node.obj, ast.Var):
            _info = self.scope.lookup(node.obj.name)
            _val = _info.get("value") if _info else None
            if isinstance(_val, dict) and "__ffi__" in _val:
                handled, result = self._ffi_invoke(method, args)
                if handled:
                    return result
                self.error(f"Unknown FFI binding '{node.obj.name}.{method}()'")
                return None

        # ── ns.fn(...) on an imported .rub file — BUG-13. Must run before the
        # generic dispatch, which would otherwise treat the namespace dict as
        # a plain value and return None.
        _ns = self._namespace_of(node.obj)
        if _ns is not None:
            return self._call_namespaced(_ns, method, node.args)

        # ── time.* — BUG-11. Like random.* above, `time` has no dedicated AST
        # node, so time.wait()/timer_start()/timer_pause()/timer_stop()/
        # timer_read() all landed on the generic module stub and returned
        # None. That made `time.timer_read(1)` print Null (and any comparison
        # against it wrong) where the compiled binary returns real elapsed
        # seconds. Timers are keyed by integer ID exactly as the spec
        # describes, and multiple timers can run at once.
        if isinstance(node.obj, ast.Var) and node.obj.name == "time":
            import time as _time
            if method == "wait":
                if args:
                    try: _time.sleep(float(args[0]))
                    except (TypeError, ValueError): pass
                return None
            if method == "timer_start":
                if args:
                    self._timers[args[0]] = {"start": _time.time(), "elapsed": 0.0,
                                             "running": True}
                return None
            if method == "timer_pause":
                t = self._timers.get(args[0]) if args else None
                if t and t["running"]:
                    t["elapsed"] += _time.time() - t["start"]
                    t["running"] = False
                return None
            if method == "timer_stop":
                if args: self._timers.pop(args[0], None)
                return None
            if method == "timer_read":
                t = self._timers.get(args[0]) if args else None
                if not t:
                    return 0.0
                total = t["elapsed"]
                if t["running"]:
                    total += _time.time() - t["start"]
                return total

        # ── thread.wait()/running() — BUG-11. The debugger runs a thread()
        # call synchronously to completion (see the ThreadCall branch in
        # execute), so by the time either of these is reached the thread has
        # already finished: wait() is a no-op and running() is False. Both
        # previously returned None, so `print(thread.running(1))` printed Null
        # instead of False.
        if isinstance(node.obj, ast.Var) and node.obj.name == "thread":
            if method == "wait":
                return None
            if method == "running":
                return False

        # ── file.read()/write()/add()/writeln()/readln() as a plain
        # MethodCall (the parser only special-cases FileHandleMethod for
        # some contexts; expression position falls through to here) ──────
        if isinstance(node.obj, ast.Var) and method in ("write", "add", "read", "readln", "writeln"):
            info = self.scope.lookup(node.obj.name)
            if info is not None and isinstance(info.get("value"), dict) and "path" in info["value"]:
                return self._file_handle_op(node.obj.name, method, node.args)

    # ── Special case: mutating method on a collection-element access ─────
        # e.g. my_list(0).set("val")   →  my_list[0] = "val"
        #      my_index("k").set("v")  →  my_index["k"] = "v"
        #      my_dict("key", 1).set(x) → my_dict["key"][1] = x
        if method == "set" and isinstance(node.obj, (ast.FnCall, ast.MethodCall)):
            if isinstance(node.obj, ast.FnCall):
                col = self._resolve_collection_for_mutation(node.obj.name)
                fname = node.obj.name if isinstance(node.obj.name, str) else "<dynamic>"
            else:
                # BUG-10: `p.scores(0).set(99)` — a collection FIELD on a class
                # instance, mutated from OUTSIDE the class (the syntax file's
                # own CLASSES example). node.obj is a MethodCall here, not a
                # FnCall, so this branch never matched and the call fell
                # through to the generic path, which evaluated p.scores(0) to
                # the ELEMENT and then "set" it on that copy — a silent no-op.
                # (.add() happened to work because p.scores() evaluates to the
                # live list itself.) Resolve the field to the live collection.
                col = self._resolve_instance_field(node.obj)
                fname = f"{getattr(node.obj.obj, 'name', '?')}.{node.obj.method}"
            if col is not None:
                idx_args = [self.evaluate(a) for a in node.obj.args]
                new_val  = args[0] if args else None
                if isinstance(col, list) and len(idx_args) == 1:
                    try:
                        col[int(idx_args[0])] = new_val
                    except (IndexError, TypeError, ValueError) as e:
                        self.error(f"'.set()' index error on '{fname}': {e}")
                    return None
                elif isinstance(col, dict) and len(idx_args) == 1:
                    col[idx_args[0]] = new_val
                    return None
                elif isinstance(col, dict) and len(idx_args) == 2:
                    sub = col.get(idx_args[0])
                    if isinstance(sub, list):
                        try:
                            sub[int(idx_args[1])] = new_val
                        except (IndexError, TypeError) as e:
                            self.error(f"'.set()' index error on '{fname}': {e}")
                    return None

        # ── Special case: .add(val) on a dict key whose value is currently
        # Null (e.g. my_dict().add("new_key") then my_dict("new_key").add(99)).
        # Per spec/compiler behavior, adding to a Null-valued slot replaces
        # it with a new single-element list, matching the "[Null].add(5) →
        # [5]" collection rule. Must run before the generic obj=None check
        # below, since a Null slot evaluates to None and would otherwise be
        # rejected as "method called on None".
        if method == "add" and isinstance(node.obj, ast.FnCall):
            col = self._resolve_collection_for_mutation(node.obj.name)
            if isinstance(col, dict):
                idx_args = [self.evaluate(a) for a in node.obj.args]
                if len(idx_args) == 1 and idx_args[0] in col and col[idx_args[0]] is None:
                    col[idx_args[0]] = [args[0]] if args else []
                    return None

        obj    = self.evaluate(node.obj)
        if obj is None:
            self.error(f"Method '.{method}()' called on None — object may be undefined or dropped")
            return None
        try:
            result = self._dispatch_method(obj, method, args)
        except (IndexError, TypeError, ValueError) as e:
            self.error(f"Method '.{method}({', '.join(repr(a) for a in args)})' failed: {e}")
            return None

        # String-mutating methods return a new string; write it back to the source variable.
        # e.g. my_str.set(0, "J")  →  my_str = "Jello"
        if (result is not None
                and isinstance(result, str)
                and method in ("set", "insert", "replace")
                and isinstance(node.obj, ast.Var)):
            info = self.scope.lookup(node.obj.name)
            if info is not None and isinstance(info.get("value"), str):
                info["value"] = result

        return result

    def _resolve_instance_field(self, mcall):
        """BUG-10: for `p.scores(0)` (a MethodCall whose `method` is really a
        class FIELD name), return the instance's live collection so callers can
        mutate it in place. None when it isn't an instance-field access."""
        if not isinstance(mcall, ast.MethodCall):
            return None
        try:
            inst = self.evaluate(mcall.obj)
        except Exception:
            return None
        if isinstance(inst, dict) and "__class__" in inst and mcall.method in inst:
            value = inst[mcall.method]
            if isinstance(value, (list, dict)):
                return value
        return None

    def _resolve_collection_for_mutation(self, name_node):
        """Given the .name of an FnCall used as an index/mutation target
        (e.g. `quantities(sku).set(...)` -> name_node is "quantities"), return
        the underlying mutable collection object — whether name_node is a
        plain variable name (str) or a DynResolve node (runtime SY
        reflection, e.g. `tmp(...).add(...)` where tmp is SY-typed).
        Returns None if not resolvable. Python collections mutate in place,
        so returning the live object is enough for callers to mutate
        correctly regardless of which kind of name this was."""
        if isinstance(name_node, str):
            info = self.scope.lookup(name_node)
            return info["value"] if info is not None else None
        if isinstance(name_node, ast.DynResolve):
            key_info = self.scope.lookup(name_node.holder_name)
            key = key_info["value"] if key_info is not None else None
            return self._dynvars.get(key) if key is not None else None
        return None

    def _dispatch_method(self, obj, method, args):

        # ── Module stub — methods are no-ops in debug mode ───────────────────
        if isinstance(obj, dict) and "__module__" in obj:
            return None   # module calls silently return None; no error spam

        # ── Class instance method dispatch ────────────────────────────────────
        if isinstance(obj, dict) and "__class__" in obj:
            cls = self._class_defs.get(obj["__class__"])
            if cls:
                for m in (cls.methods or []):
                    if m.name == method:
                        fn_scope = Scope(parent=self.scope)
                        # BUGFIX: field names captured here so they can be
                        # written back after the call (see below) — fields
                        # are injected as fresh, independent scope entries,
                        # not live references back into `obj`.
                        field_names = [fname for fname in obj if fname != "__class__"]
                        for fname in field_names:
                            fn_scope.declare(fname, {
                                "value": obj[fname], "type": self.rub_type(obj[fname]),
                                "dropped": False, "mutable": True,
                            })
                        # BUGFIX (bugs.log OPEN-8 follow-up): `self.field` reads/
                        # writes inside a method body need a live "__self" binding
                        # pointing at the actual instance dict (FieldAccess/
                        # FieldAssign are already generic over any dict-valued
                        # obj expression) — previously nothing declared "__self"
                        # here at all, so `self.field` raised an undefined-
                        # variable error in a debug run even though the same
                        # code now works in the real compiled binary.
                        fn_scope.declare("__self", {
                            "value": obj, "type": obj.get("__class__"),
                            "dropped": False, "mutable": True,
                        })
                        for i, (pname, ptype) in enumerate(m.params):
                            fn_scope.declare(pname, {
                                "value": args[i] if i < len(args) else None,
                                "type": ptype or "Any", "dropped": False, "mutable": True,
                            })
                        saved, self.scope = self.scope, fn_scope
                        result = None
                        try:
                            for stmt in m.body:
                                self.execute(stmt)
                        except _Return as r:
                            result = r.value
                        finally:
                            self.scope = saved
                            # BUGFIX: `field = expr` inside a method body
                            # (e.g. `health = health - amount`) only updated
                            # fn_scope's own copy of "health", never `obj`
                            # itself — the instance's field mutation was
                            # silently discarded the moment the method
                            # returned. Write back whatever fn_scope ended up
                            # holding for each field name.
                            for fname in field_names:
                                info = fn_scope.vars.get(fname)
                                if info is not None:
                                    obj[fname] = info["value"]
                        return result
            # BUGFIX: field access via call syntax (e.g. b.items(key), where
            # "items" is a FIELD not a method) previously returned the raw
            # field value completely ignoring any index args — so
            # `b.items(key)` returned the whole dict instead of the value at
            # `key`. Matches the same fix made in the real compiler for the
            # identical pattern.
            field_val = obj.get(method)
            if args:
                col = field_val
                for a in args:
                    if isinstance(col, list):
                        try:
                            col = col[int(a)]
                        except (IndexError, TypeError, ValueError) as e:
                            self.error(f"Index error on '{method}': {e}")
                            return None
                    elif isinstance(col, dict):
                        if a not in col:
                            self.error("collection access error" if self._try_depth > 0 else "key not found in collection")
                            return None
                        col = col[a]
                    else:
                        return None
                return col
            return field_val   # field access fallback (no index args)

        # ── str methods ───────────────────────────────────────────────────────
        if isinstance(obj, str):
            if method == "len":      return len(obj)
            if method == "char":
                idx = int(args[0]) if args else 0
                return obj[idx] if 0 <= idx < len(obj) else ""
            if method == "slice":    return list(obj)
            if method == "has":      return args[0] in obj if args else False
            if method == "replace":  return obj.replace(str(args[0]), str(args[1])) if len(args) >= 2 else obj
            if method == "split":    return obj.split(str(args[0])) if args else list(obj)
            if method == "combine":  return obj
            if method == "insert":
                idx = int(args[0])
                return obj[:idx] + str(args[1]) + obj[idx:] if len(args) >= 2 else obj
            if method == "set":
                idx = int(args[0])
                return obj[:idx] + str(args[1])[0] + obj[idx+1:] if len(args) >= 2 else obj
            if method == "to":
                t = str(args[0]) if args else ""
                try:
                    if t in ("i32","i64","i128"): return int(obj)
                    if t in ("f32","f64","f128"): return float(obj)
                except Exception: pass
                return obj
            return None

        # ── list methods ──────────────────────────────────────────────────────
        if isinstance(obj, list):
            if method == "len":     return len(obj)
            if method == "has":     return args[0] in obj if args else False
            if method == "combine": return "".join(str(x) for x in obj)
            if method == "add":
                if len(obj) == 1 and obj[0] is None:
                    obj[0] = args[0] if args else None
                else:
                    obj.append(args[0]) if args else None
                return None
            if method == "set":
                if len(args) >= 2:
                    try: obj[int(args[0])] = args[1]
                    except IndexError: self.error("List index out of bounds in .set()")
                return None
            if method == "slice":
                return obj[int(args[0]):int(args[1])] if len(args) >= 2 else obj[:]
            if method == "drop":    return None
            return None

        # ── dict / index methods ──────────────────────────────────────────────
        if isinstance(obj, dict):
            if method == "len":  return len(obj)
            if method == "has":  return args[0] in obj if args else False
            if method == "add":
                if len(args) == 1:
                    # dict().add("key") — creates a new top-level key.
                    # Per spec the key's value starts as Null. The paired
                    # _eval_method_call special-case above upgrades it to a
                    # list the first time something is added to it, so we
                    # don't need a placeholder [] here anymore (bugs.log #3).
                    obj[args[0]] = None
                elif len(args) >= 2:
                    obj[args[0]] = args[1]
                return None
            if method == "set":
                if len(args) >= 2: obj[args[0]] = args[1]
                return None
            if method == "drop": return None
            return None

        # ── numeric methods ───────────────────────────────────────────────────
        if isinstance(obj, (int, float)):
            if method == "abs":   return abs(obj)
            if method == "floor": import math; return math.floor(obj)
            if method == "ceil":  import math; return math.ceil(obj)
            if method == "sqrt":  import math; return math.sqrt(obj)
            if method in ("to", "to_int"):
                # BUGFIX: was entirely unhandled for int/float objects,
                # always returning None — e.g. `x.to(SY)` (building a
                # dynamic name via concatenation, like 'w' + x.to(SY)) had
                # no implementation at all here.
                t = args[0] if args else ""
                if t == "SY":
                    return str(int(obj))
                if t in ("f32", "f64", "f128", "f256", "f512", "f1024", "f2048"):
                    return float(obj)
                return int(obj)
            return None

        return None


    # ── Helpers ───────────────────────────────────────────────────────────────

    def _deep_copy_value(self, value):
        """Per spec: every assignment/let creates a full deep copy (lists,
        dicts/index, and class instances are all Python dict/list objects
        here) so mutating one variable never affects another. Module stubs
        and None/scalars pass through unchanged (deepcopy is a no-op for
        immutables anyway, but module stubs shouldn't be duplicated)."""
        if isinstance(value, dict) and "__module__" in value:
            return value
        if isinstance(value, (dict, list)):
            return copy.deepcopy(value)
        return value

    def _format_value(self, value, nested=False):
        """Render a value the way Rubidium's compiled print() does: Null/True/
        False instead of Python's None/True/False reprs, and strings quoted
        only when nested inside a list/dict (top-level strings print bare)."""
        if value is None:
            return "Null"
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, str):
            return f'"{value}"' if nested else value
        if isinstance(value, list):
            return "[" + ", ".join(self._format_value(v, nested=True) for v in value) + "]"
        if isinstance(value, dict):
            if "__module__" in value: return f"<module {value['__module__']}>"
            if "__class__" in value:  return f"<{value['__class__']} instance>"
            return "{" + ", ".join(
                f"{self._format_value(k, nested=True)}: {self._format_value(v, nested=True)}"
                for k, v in value.items()) + "}"
        if isinstance(value, _Wide):
            # BUG-6: exact decimal expansion, matching the compiled binary.
            return str(value)
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def _file_handle_op(self, var_name, method, args):
        info = self.scope.lookup(var_name)
        if info is None or not isinstance(info.get("value"), dict) or "path" not in info["value"]:
            return None
        path = info["value"]["path"]
        arg_vals = [self.evaluate(a) for a in args]
        if method == "write":
            with open(path, "w") as f: f.write(str(arg_vals[0]) if arg_vals else "")
            return None
        if method == "add":
            with open(path, "a") as f: f.write(str(arg_vals[0]) if arg_vals else "")
            return None
        if method == "read":
            with open(path, "r") as f: return f.read()
        if method == "readln":
            # BUGFIX (bugs.log): syntax file says "Line indexing starts at
            # 0" (and the real compiler's file_readln/file_writeln in
            # compiler.py are explicitly 0-based) — this was 1-based
            # (`idx-1`/`0 < idx <= len`), an off-by-one that returned the
            # WRONG line for every call (e.g. readln(1) returned line 0
            # instead of line 1) instead of erroring outright, so it was
            # never caught until file I/O actually got exercised.
            idx = int(arg_vals[0]) if arg_vals else 0
            with open(path, "r") as f: lines = f.readlines()
            return lines[idx].rstrip("\n") if 0 <= idx < len(lines) else ""
        if method == "writeln":
            if len(arg_vals) < 2: return None
            line_num, data = int(arg_vals[0]), str(arg_vals[1])
            lines = []
            if os.path.exists(path):
                with open(path, "r") as f: lines = f.readlines()
            while len(lines) <= line_num: lines.append("\n")
            lines[line_num] = data + "\n"
            with open(path, "w") as f: f.writelines(lines)
            return None
        return None

    # BUGFIX (bugs.log): sized-integer overflow was never clamped in the
    # interpreter — arithmetic used raw unbounded Python ints, so a value
    # that overflows a declared type's range (e.g. i32 max + 1) diverged
    # from the real compiled binary, which clamps to the type's min/max and
    # prints a non-fatal "Runtime Warning" (see rub_overflow_check in
    # compiler.py / the narrowing-coerce clamp in codegen.py). Mirrors that
    # exact bound/message here so debug runs match compiled behavior.
    _INT_BOUNDS = {
        "i32":   (-(2**31),    2**31 - 1),
        "i64":   (-(2**63),    2**63 - 1),
        "i128":  (-(2**127),   2**127 - 1),
        "i256":  (-(2**255),   2**255 - 1),
        "i512":  (-(2**511),   2**511 - 1),
        "i1024": (-(2**1023),  2**1023 - 1),
        "i2048": (-(2**2047),  2**2047 - 1),
    }

    def _clamp_int(self, value, vtype):
        bounds = self._INT_BOUNDS.get(vtype)
        if bounds is None or not isinstance(value, int) or isinstance(value, bool):
            return value
        lo, hi = bounds
        if value > hi or value < lo:
            print(f"Runtime Warning: integer overflow — value clamped to {vtype} range",
                  file=sys.stderr)
            return hi if value > hi else lo
        return value

    def rub_type(self, value):
        if isinstance(value, bool):  return "bool"
        if isinstance(value, int):   return "i64"
        if isinstance(value, float): return "f64"
        if isinstance(value, str):   return "str"
        if isinstance(value, list):  return "list"
        if isinstance(value, dict):  return "index"
        if value is None:            return "Null"
        return "Any"

    def error(self, msg, highlight=None):
        import re
        # If we are inside a try block, raise so the try/error handler can catch it.
        # This prevents caught exceptions from also being logged as debug errors.
        if self._try_depth > 0:
            raise RuntimeError(msg)
        self.errors.append({"line": self.line, "message": msg})
        line_str = f"line {self.line}" if self.line != "?" else "unknown line"

        ctx = ""
        if self._source_lines and isinstance(self.line, int) and self.line > 0:
            try:
                src = self._source_lines[self.line - 1].rstrip()
                ctx = f"\n  {ANSI['DIM']}→  {src}{ANSI['RESET']}"

                # Determine what term to underline
                term = highlight
                if term is None:
                    # Auto-extract first single-quoted token from the error message
                    m = re.search(r"'([^']+)'", msg)
                    if m:
                        term = m.group(1)

                if term is not None:
                    # Strip leading dot for method names so we still find the word
                    search_term = term.lstrip('.')
                    # Remove trailing () if present so we match the name in source
                    search_term = re.sub(r'\(\)$', '', search_term)
                    col = src.find(search_term) if search_term else -1
                    if col >= 0:
                        # "  →  " prefix = 5 visual chars; replicate with spaces for alignment
                        underline = ' ' * col + '^' * max(len(search_term), 1)
                        ctx += f"\n     {ANSI['ERROR']}{underline}{ANSI['RESET']}"
            except IndexError:
                pass

        print(
            f"{ANSI['ERROR']}DEBUG ERROR{ANSI['RESET']} "
            f"({line_str}): {msg}{ctx}"
        )

class Analyzer:

    def __init__(self):
        self.issues: list = []
        self.functions: dict  = {}   
        self.classes:   dict  = {}   
        self.namespaces: set  = set()
        self.imports:    set  = set()
        self.thread_fns:  set  = set()   
        self.global_muts: dict = {}      
        self.heap_var_names: list = []   
        self.global_allocs:  int  = 0
        self._lmap: dict = {}
        self._leak_vars: list = []
        self._global_scope: Scope | None = None   # set in analyze(); used by _var_decl
        self._try_depth: int = 0    # >0: inside a try block; vars scoped locally, not globally
        self._fn_depth:  int = 0    # >0: inside a fn/method; allow re-decl of existing globals
        self._sy_holder_names: set = set()  # names declared `let x: SY = ...` — see _collect_sy_names

    def _emit(self, severity: str, line, category: str,
              message: str, suggestion: str = ''):
        self.issues.append(Issue(severity, line, category, message, suggestion))

    def _build_line_map(self, tokens: list):
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            kind = getattr(tok, 'kind', tok[0] if isinstance(tok, (tuple, list)) else '')
            val = getattr(tok, 'value', tok[1] if isinstance(tok, (tuple, list)) else '')
            line = getattr(tok, 'line', tok[2] if isinstance(tok, (tuple, list)) else 0)
            
            if kind == 'LET':
                j = i + 1
                if j < n:
                    j_kind = getattr(tokens[j], 'kind', tokens[j][0] if isinstance(tokens[j], (tuple, list)) else '')
                    if j_kind == 'MUT':
                        j += 1
                if j < n:
                    j_kind = getattr(tokens[j], 'kind', tokens[j][0] if isinstance(tokens[j], (tuple, list)) else '')
                    j_val = getattr(tokens[j], 'value', tokens[j][1] if isinstance(tokens[j], (tuple, list)) else '')
                    if j_kind in ('IDENT', 'TYPE'):
                        self._lmap[('var', j_val)] = line
            elif kind == 'FN':
                j = i + 1
                if j < n:
                    j_kind = getattr(tokens[j], 'kind', tokens[j][0] if isinstance(tokens[j], (tuple, list)) else '')
                    j_val = getattr(tokens[j], 'value', tokens[j][1] if isinstance(tokens[j], (tuple, list)) else '')
                    if j_kind in ('IDENT', 'TYPE'):
                        self._lmap[('fn', j_val)] = line
            elif kind == 'CLASS':
                j = i + 1
                if j < n:
                    j_kind = getattr(tokens[j], 'kind', tokens[j][0] if isinstance(tokens[j], (tuple, list)) else '')
                    j_val = getattr(tokens[j], 'value', tokens[j][1] if isinstance(tokens[j], (tuple, list)) else '')
                    if j_kind == 'IDENT':
                        self._lmap[('class', j_val)] = line
            i += 1

    def _ln(self, kind: str, name: str):
        return self._lmap.get((kind, name))

    def _infer(self, node, scope: Scope):
        if node is None:
            return None
        if isinstance(node, ast.Number):
            return 'f64' if '.' in str(node.value) else 'i32'
        if isinstance(node, ast.Str):
            return 'str'
        if isinstance(node, ast.Bool):
            v = str(node.value).lower()
            return 'Null' if v in ('null', 'none') else 'bool'
        if isinstance(node, ast.None_):
            return 'Null'
        if isinstance(node, ast.Var):
            info = scope.lookup(node.name)
            return info.get('vtype') if info else None
        if isinstance(node, ast.ListExpr):
            return 'list'
        if isinstance(node, ast.DictExpr):
            # BUGFIX (bugs.log): a dict+ literal (`let x: dict+ = {...}`) is
            # parsed as a plain DictExpr with `is_dictplus` set by the
            # parser — this never checked that flag, so it always inferred
            # 'dict', which then falsely flagged every dict+ declaration
            # (straight from the syntax file's own example) as a Type Error
            # and blocked compilation.
            if getattr(node, 'is_dictplus', False):
                return 'dict+'
            return 'index' if getattr(node, 'is_index', False) else 'dict'
        if isinstance(node, ast.ClassInstantiate):
            return node.class_name
        if isinstance(node, ast.BinOp):
            lt = self._infer(node.left, scope)
            rt = self._infer(node.right, scope)
            if lt == 'str' or rt == 'str':
                return 'str'
            if lt and rt and lt in NUMERIC_TYPES and rt in NUMERIC_TYPES:
                return lt
            return lt
        if isinstance(node, ast.Compare):
            return 'bool'
        if isinstance(node, ast.TypeCast):
            return node.target_type
        if isinstance(node, ast.MathBlock):
            return node.vtype
        if isinstance(node, ast.FFILoad):
            return 'i64'
        if isinstance(node, ast.FnCall):
            fname = node.name if isinstance(node.name, str) else None
            # BUG-16: `let mut p = player()` parses as a FnCall, not a
            # ClassInstantiate, so an instance variable's type was inferred as
            # None — which meant no `p.field` access ever marked the field
            # used, and every class field was reported "Unused Field".
            if fname and fname in self.classes:
                return fname
            if fname and fname in self.functions:
                return self.functions[fname].get('ret_type')
        return None

    # BUG-2: the analyzer used to type EVERY `for` variable as 'i32', which is
    # only right for `for i in range(a, b)`. Iterating a collection yields
    # values/keys/lines, so `for item in ["a","b"] { take_char(item) }` was
    # wrongly reported as "Expected str, Received i32". These two helpers work
    # out what a loop actually yields; anything not statically knowable falls
    # back to 'Any', which _types_compat treats as compatible with everything
    # (an unknown element type must never manufacture an error).
    @staticmethod
    def _unify_types(types):
        """The single type shared by every element, or 'Any' if mixed/unknown."""
        seen = {t for t in types if t}
        if len(seen) == 1 and len(types) > 0 and all(types):
            return seen.pop()
        return 'Any'

    def _iter_elem_type(self, node, scope: Scope) -> str:
        """Type of the value produced by one iteration of `for x in <node>`.
        Per spec: list -> values, index/dict/dict+ -> keys, file -> lines,
        str -> characters."""
        if node is None:
            return 'i32'                       # `for i in range(a, b)`
        if isinstance(node, (ast.Str, ast.InterpolatedStr)):
            return 'str'                       # iterating a string yields chars
        if isinstance(node, ast.ListExpr):
            return self._unify_types([self._infer(e, scope) for e in node.elements])
        if isinstance(node, ast.DictExpr):
            return self._unify_types([self._infer(k, scope) for k, _v in node.pairs])
        if isinstance(node, (ast.MethodCall, ast.CollectionMethodCall)):
            # "text".slice() yields single-character strings.
            if getattr(node, 'method', None) == 'slice':
                return 'str'
            return 'Any'
        if isinstance(node, ast.Var):
            info = scope.lookup(node.name)
            if not info:
                return 'Any'
            etype = info.get('etype')
            if etype:
                return etype
            vtype = info.get('vtype')
            if vtype in ('str', 'str+', 'file'):
                return 'str'                   # chars, or file lines
            return 'Any'
        return 'Any'

    def _decl_elem_type(self, node: ast.VarDecl, scope: Scope):
        """Element type recorded on a collection variable at declaration, used
        later by _iter_elem_type. Honours the spec's forced-element-type form
        (`let x: list: i32 = [...]`) first, then falls back to the literal."""
        forced = getattr(node, 'element_type', None)
        if forced:
            return forced
        value = node.value
        if isinstance(value, ast.ListExpr):
            return self._unify_types([self._infer(e, scope) for e in value.elements])
        if isinstance(value, ast.DictExpr):
            # index/dict iterate over KEYS, so that's what a loop over this
            # variable will produce.
            return self._unify_types([self._infer(k, scope) for k, _v in value.pairs])
        if isinstance(value, (ast.MethodCall, ast.CollectionMethodCall)):
            if getattr(value, 'method', None) == 'slice':
                return 'str'
        return None

    def _is_heap_node(self, node) -> bool:
        return isinstance(node, (ast.ListExpr, ast.DictExpr, ast.ClassInstantiate,
                                  ast.Str, ast.InterpolatedStr))

    def _literal_key(self, node):
        """Returns a hashable, comparable representation of a literal key
        node (Number/Str/Bool) for duplicate-key detection, or None if the
        key isn't a simple literal (e.g. a variable) and can't be checked
        statically."""
        if isinstance(node, ast.Number):
            return ('num', node.value)
        if isinstance(node, ast.Str):
            return ('str', node.value)
        if isinstance(node, ast.Bool):
            return ('bool', str(node.value).lower())
        return None

    def _check_index_values_scalar(self, dict_expr_node, var_name):
        """`index` is a key -> single SCALAR value map — never a list/index/
        dict/dict+. Mirrors codegen.py's compile-time check (same scoping
        caveat: only catches values written directly as a collection
        literal)."""
        for k, v in dict_expr_node.pairs:
            if isinstance(v, (ast.ListExpr, ast.DictExpr)):
                lit = self._literal_key(k)
                key_desc = f"{lit[1]!r}" if lit is not None else "a key"
                kind = "list" if isinstance(v, ast.ListExpr) else ("index" if getattr(v, "is_index", False) else "dict")
                self._emit('ERROR', self._ln('var', var_name), 'Invalid Index Value',
                           f"index '{var_name}': value for {key_desc} is a {kind}, not a scalar.",
                           "`index` holds exactly one scalar value per key — use `dict` "
                           "instead if a key needs to hold a collection of values.")

    def _is_null_node(self, node) -> bool:
        if isinstance(node, ast.Bool) and str(node.value).lower() in ('null', 'none'):
            return True
        return isinstance(node, ast.None_)

    def _types_compat(self, expected: str, received: str) -> bool:
        if expected == received:
            return True
        if 'Any' in (expected, received):
            return True
        if 'Null' in (expected, received):
            return True
        if expected in NUMERIC_TYPES and received in NUMERIC_TYPES:
            return True
        # BUGFIX (bugs.log #3): str+ is spec'd as "the same as str but it
        # uses 3 \" on each side and can use more than 1 line" — it's not a
        # distinct data type, just a literal-syntax variant. A `let x: str+`
        # declaration is satisfied by an ordinary str-typed value/literal
        # (this is exactly the syntax file's own str+ example), so treat
        # str and str+ as compatible in both directions instead of flagging
        # a false-positive Type Error that would block valid code.
        if {expected, received} == {'str', 'str+'}:
            return True
        return False

    def _pre_pass(self, nodes: list):
        for node in nodes:
            if isinstance(node, ast.FnDef):
                if node.name not in self.functions:
                    self.functions[node.name] = {
                        'params':   node.params,
                        'ret_type': node.ret_type,
                        'used':     False,
                        'line':     self._ln('fn', node.name),
                    }
            elif isinstance(node, ast.ClassDef):
                if node.name not in self.classes:
                    fields = {}
                    for f in node.fields:
                        if f.name in fields:
                            self._emit('ERROR', self._ln('class', node.name), 'Duplicate Symbol',
                                       f"Field '{f.name}' is declared more than once "
                                       f"in class '{node.name}'.",
                                       f"Rename one of the '{f.name}' fields.")
                        fields[f.name] = {'mutable': f.mutable, 'vtype': f.vtype, 'used': False}
                    methods = {}
                    for m in node.methods:
                        methods[m.name] = m
                    self.classes[node.name] = {
                        'fields':  fields,
                        'methods': methods,
                        'used':    False,
                        'line':    self._ln('class', node.name),
                    }
            elif isinstance(node, ast.Use):
                self.namespaces.add(node.module_name)
            elif isinstance(node, ast.Import):
                self.imports.add(node.module_name)
                self.namespaces.add(node.module_name)   # treat imported files as namespaces
                alias = getattr(node, 'alias', None)
                if alias:
                    self.imports.add(alias)
                    self.namespaces.add(alias)          # alias is also a valid namespace
            elif isinstance(node, ast.FFIBind):
                # Bug 11: the Rubidium-callable name for an FFI binding
                # (the `as alias`, or the raw symbol_name when there's no
                # alias) was never registered, so every legitimate call to
                # a bound FFI function was falsely reported as unknown.
                callable_name = node.alias or node.symbol_name
                if callable_name and callable_name not in self.functions:
                    self.functions[callable_name] = {
                        'params':   node.params,
                        'ret_type': node.ret_type,
                        'used':     False,
                        'line':     None,
                    }

        for node in nodes:
            self._find_thread_fns(node)

    def _find_thread_fns(self, node):
        if isinstance(node, ast.ThreadCall):
            fc = node.func_call
            if isinstance(fc, ast.FnCall) and isinstance(fc.name, str):
                self.thread_fns.add(fc.name)
            elif isinstance(fc, ast.Var):
                self.thread_fns.add(fc.name)
        for attr in ('body', 'then_body', 'else_body', 'try_body', 'error_body'):
            body = getattr(node, attr, None)
            if body:
                for child in body:
                    self._find_thread_fns(child)

    def _pre_declare_fn_globals(self, nodes: list, global_scope: Scope):
        """Scan function bodies for non-local 'let' declarations and stub them
        into global_scope early.  This prevents false 'unknown variable' errors
        when a function defined textually before main() uses a variable that
        main() (or any later function) will place into the global pool at runtime."""
        def _scan_body(stmts):
            for stmt in (stmts or []):
                if isinstance(stmt, ast.VarDecl) and not stmt.is_local:
                    if stmt.name not in global_scope.vars:
                        global_scope.declare(stmt.name, {
                            'mutable':       stmt.mutable,
                            'vtype':         stmt.vtype or 'Any',
                            'dropped':       False,
                            'used':          True,   # pre-mark so no spurious warnings
                            'is_heap':       stmt.vtype in HEAP_TYPES if stmt.vtype else False,
                            'line':          self._ln('var', stmt.name),
                            'drop_line':     None,
                            'possibly_null': False,
                            '__stub':        True,   # flag so _var_decl updates, not errors
                        })
                # Recurse into nested bodies
                for attr in ('body', 'then_body', 'else_body', 'try_body', 'error_body'):
                    _scan_body(getattr(stmt, attr, None))

        for node in nodes:
            if isinstance(node, ast.FnDef):
                _scan_body(node.body)

    def _collect_sy_names(self, tokens: list) -> set:
        """BUGFIX (bugs.log): `let x: SY = <expr>` is rewritten by the parser
        into a plain `VarDecl(..., vtype='str', ...)` (SY is a compile-time
        flavor of str, not a distinct runtime type — see parser.py's
        var_decl SY branch), so by the time debug.py sees the AST there is
        no way to tell "this is a SY holder" from vtype alone. Token names
        declared as `let (mut/local) NAME : SY = ...` are collected here so
        the unused-variable check can exempt them — a SY holder used only
        via `fn (name)() {...}` (parse-time name substitution, spec example
        in the syntax file's SY section) leaves no AST trace of being read,
        and would otherwise always be a false "Unused Variable" positive."""
        names = set()
        n = len(tokens)
        for i, tok in enumerate(tokens):
            if tok[0] != 'LET':
                continue
            j = i + 1
            while j < n and tokens[j][0] in ('MUT', 'LOCAL'):
                j += 1
            if j < n and tokens[j][0] == 'IDENT':
                nm = tokens[j][1]
                j += 1
                if j < n and tokens[j][0] == 'COLON':
                    j += 1
                    if j < n and tokens[j][0] == 'TYPE' and tokens[j][1] == 'SY':
                        names.add(nm)
        return names

    def analyze(self, nodes: list, tokens: list):
        self._build_line_map(tokens)
        self._sy_holder_names = self._collect_sy_names(tokens)
        self._pre_pass(nodes)
        global_scope = Scope()
        self._global_scope = global_scope   # non-local 'let' inside functions targets this
        # Pre-scan function bodies for non-local 'let' that go into the global pool.
        # Without this, a function defined before main() would falsely report variables
        # declared in main() (without 'local') as unknown.
        self._pre_declare_fn_globals(nodes, global_scope)
        for node in nodes:
            self._node(node, global_scope, in_loop=False)
        self._check_unused(global_scope)
        self._check_global_leaks(global_scope)
        self._ast_syntax_check(nodes)
        self._scan_thread_reuse(nodes)
        self._scan_os_sessions(nodes)

    # ── AST-level structural syntax checks ────────────────────────────────────

    def _ast_syntax_check(self, nodes: list):
        """Walk the AST for structural issues the parser silently accepts."""
        self._stx_stmts(nodes, in_loop=False, in_fn=None)

    def _stx_stmts(self, stmts: list, in_loop: bool, in_fn):
        """Recursively check a statement list for structural issues."""
        for i, node in enumerate(stmts):

            # Dead code: statements after a return in the same block
            if isinstance(node, ast.Return):
                remaining = len(stmts) - i - 1
                if remaining > 0:
                    self._emit('WARNING', self._ln('fn', in_fn) if in_fn else None,
                               'Dead Code',
                               f"{remaining} unreachable statement{'s' if remaining != 1 else ''} "
                               f"after 'return'"
                               + (f" in '{in_fn}'" if in_fn else "") + ".",
                               "Remove or move the code before the 'return'.")
                break  # no point checking the dead statements

            elif isinstance(node, ast.Break):
                if not in_loop:
                    self._emit('ERROR', None, 'Break Outside Loop',
                               "'break' is used outside of any loop.",
                               "Move 'break' inside a 'while' or 'for' loop.")
                else:
                    remaining = len(stmts) - i - 1
                    if remaining > 0:
                        self._emit('WARNING', None, 'Dead Code',
                                   f"{remaining} unreachable statement"
                                   f"{'s' if remaining != 1 else ''} after 'break'.",
                                   "Remove or move the code before the 'break'.")
                    break

            elif isinstance(node, ast.Continue):
                if not in_loop:
                    self._emit('ERROR', None, 'Continue Outside Loop',
                               "'continue' is used outside of any loop.",
                               "Move 'continue' inside a 'while' or 'for' loop.")
                else:
                    remaining = len(stmts) - i - 1
                    if remaining > 0:
                        self._emit('WARNING', None, 'Dead Code',
                                   f"{remaining} unreachable statement"
                                   f"{'s' if remaining != 1 else ''} after 'continue'.",
                                   "Remove or move the code before the 'continue'.")
                    break

            elif isinstance(node, ast.FnDef):
                ln = self._ln('fn', node.name)
                if not node.body:
                    self._emit('WARNING', ln, 'Empty Function Body',
                               f"Function '{node.name}' has an empty body.",
                               f"Add statements inside 'fn {node.name}()' or remove it.")
                else:
                    if node.ret_type and not self._any_path_returns(node.body):
                        # BUGFIX (bugs.log): verified against the real compiler
                        # (codegen.py) that a function which doesn't return on
                        # every path does NOT fail to compile and does NOT
                        # produce garbage/undefined output — it deterministically
                        # returns a type-appropriate default (e.g. 0 for i32)
                        # on the path with no explicit return. Flagging this as
                        # a hard ERROR made debug.py report "COMPILATION
                        # BLOCKED" for code that the real compiler accepts and
                        # runs correctly. Still worth surfacing as a lint (the
                        # implicit default is easy to trip over unintentionally),
                        # so downgraded to a non-blocking WARNING instead of
                        # removing it outright.
                        self._emit('WARNING', ln, 'Missing Return Statement',
                                   f"Function '{node.name}' declares return type "
                                   f"'{node.ret_type}' but may not return on all paths "
                                   f"(the real compiler falls back to a type default, "
                                   f"e.g. 0/Null, on paths with no explicit return).",
                                   f"Add 'return <{node.ret_type} value>' before the "
                                   f"end of '{node.name}'.")
                    self._stx_stmts(node.body, in_loop=False, in_fn=node.name)

            elif isinstance(node, ast.ClassDef):
                for method in (node.methods or []):
                    ln = self._ln('class', node.name)
                    if not method.body:
                        self._emit('WARNING', ln, 'Empty Method Body',
                                   f"Method '{node.name}.{method.name}' has an empty body.",
                                   f"Add statements or remove the method.")
                    else:
                        self._stx_stmts(method.body, in_loop=False, in_fn=method.name)

            elif isinstance(node, ast.If):
                self._stx_stmts(node.then_body or [], in_loop, in_fn)
                if node.else_body:
                    self._stx_stmts(node.else_body, in_loop, in_fn)

            elif isinstance(node, (ast.While, ast.For)):
                self._stx_stmts(node.body, in_loop=True, in_fn=in_fn)

            elif isinstance(node, ast.Try):
                self._stx_stmts(node.try_body, in_loop, in_fn)
                self._stx_stmts(node.error_body, in_loop, in_fn)

    def _any_path_returns(self, body: list) -> bool:
        """Return True if every execution path through body has a return."""
        if not body:
            return False
        last = body[-1]
        if isinstance(last, ast.Return):
            return True
        if isinstance(last, ast.If):
            # Both branches must return, and an else branch must exist
            return bool(last.else_body) and \
                   self._any_path_returns(last.then_body or []) and \
                   self._any_path_returns(last.else_body)
        if isinstance(last, ast.Try):
            return (self._any_path_returns(last.try_body) and
                    self._any_path_returns(last.error_body))
        # Return may appear earlier in the body (dead code case handled separately)
        return any(isinstance(s, ast.Return) for s in body)

    def _node(self, node, scope: Scope, in_loop: bool = False):
        if node is None:
            return
        t = type(node)

        if t is ast.VarDecl:
            self._var_decl(node, scope)
        elif t is ast.Assign:
            self._assign(node, scope)
        elif t is ast.FieldAssign:
            self._field_assign(node, scope)
        elif t is ast.FnDef:
            self._fn_def(node, scope)
        elif t is ast.ClassDef:
            self._class_def(node, scope)
        elif t is ast.Drop:
            self._drop(node, scope)
        elif t is ast.ThreadCall:
            self._thread_call(node, scope)
        elif t is ast.While:
            self._while(node, scope)
        elif t is ast.For:
            self._for(node, scope)
        elif t is ast.If:
            self._if(node, scope)
        elif t is ast.Try:
            try_scope = Scope(parent=scope)
            self._try_depth += 1
            for s in node.try_body:
                self._node(s, try_scope, in_loop)
            self._try_depth -= 1
            error_scope = Scope(parent=scope)
            error_scope.declare('error', {
                'mutable': False, 'vtype': 'str', 'dropped': False,
                'used': True, 'is_heap': False, 'line': None,
                'drop_line': None, 'possibly_null': False,
            })
            for s in node.error_body:
                self._node(s, error_scope, in_loop)
        elif t is ast.Return:
            self._expr(node.value, scope)
        elif t is ast.Print:
            self._expr(node.value, scope)
        elif t is ast.Println:
            self._expr(node.value, scope)
        elif t is ast.FnCall:
            self._fn_call(node, scope)
        elif t is ast.MethodCall:
            self._method_call(node, scope)
        elif t is ast.CollectionMethodCall:
            self._collection_method(node, scope)
        elif t is ast.Use:
            self.namespaces.add(node.module_name)
        elif t is ast.Import:
            self.imports.add(node.module_name)
            self.namespaces.add(node.module_name)
            alias = getattr(node, 'alias', None)
            if alias:
                self.imports.add(alias)
                self.namespaces.add(alias)
        elif t is ast.FFILoad:
            self._ffi_load(node, scope)
        elif t is ast.FFIBind:
            pass  
        elif t is ast.FileOpen:
            # BUGFIX (bugs.log): the file handle (`open(...) as f { ... }`)
            # was never declared into any scope here, so any plain
            # reference to it inside the block — e.g. `let data = f.read()`,
            # which the parser doesn't special-case as FileHandleMethod the
            # way `f.write(...)`/`f.add(...)` are — hit the generic
            # "Unknown Variable" check and blocked compilation for entirely
            # valid, spec-documented file I/O code (test.rub had no file
            # I/O coverage before, so this was never exercised).
            self._expr(node.path_expr, scope)
            file_scope = Scope(parent=scope)
            file_scope.declare(node.var_name, {
                'mutable': True, 'vtype': 'file', 'dropped': False, 'used': True,
                'is_heap': False, 'line': None, 'drop_line': None, 'possibly_null': False,
            })
            for s in node.body:
                self._node(s, file_scope, in_loop)
        elif t in (ast.FileHandleStmt, ast.FileHandleMethod):
            for a in node.args:
                self._expr(a, scope)
        elif t in (ast.FileExists, ast.FileDelete, ast.FileNew):
            self._expr(node.path_expr, scope)
        elif t is ast.FileRename:
            self._expr(node.old_path, scope)
            self._expr(node.new_path, scope)
        elif t is ast.FileCopy:
            self._expr(node.src_path, scope)
            self._expr(node.dst_path, scope)
        elif t is ast.OsStart:
            self._expr(node.id_expr, scope)
        elif t is ast.OsRun:
            self._expr(node.id_expr, scope)
            self._expr(node.cmd_expr, scope)
            if node.input_expr:
                self._expr(node.input_expr, scope)
        elif t is ast.OsDrop:
            self._expr(node.id_expr, scope)
        elif t in (ast.ThreadWait, ast.ThreadRunning, ast.Break, ast.Continue):
            pass
        else:
            self._expr(node, scope)

    def _expr(self, node, scope: Scope):
        if node is None:
            return
        t = type(node)

        if t is ast.Var:
            self._var_usage(node, scope)
        elif t is ast.FnCall:
            self._fn_call(node, scope)
        elif t is ast.MethodCall:
            self._method_call(node, scope)
        elif t is ast.CollectionMethodCall:
            self._collection_method(node, scope)
        elif t is ast.BinOp:
            self._null_arith(node.left, scope)
            self._null_arith(node.right, scope)
            self._expr(node.left, scope)
            self._expr(node.right, scope)
        elif t is ast.UnaryOp:
            self._expr(node.value, scope)
        elif t is ast.Compare:
            self._null_compare(node, scope)
            self._expr(node.left, scope)
            self._expr(node.right, scope)
        elif t is ast.ListExpr:
            for e in node.elements:
                self._expr(e, scope)
        elif t is ast.DictExpr:
            if getattr(node, 'is_index', False):
                seen_keys = set()
                for k, v in node.pairs:
                    lit = self._literal_key(k)
                    if lit is not None:
                        if lit in seen_keys:
                            self._emit('ERROR', None, 'Duplicate Key',
                                       f"Duplicate key {lit[1]!r} in 'index' literal.",
                                       "Each key in an index must be unique; "
                                       "remove or rename the duplicate.")
                        seen_keys.add(lit)
            for k, v in node.pairs:
                self._expr(k, scope)
                self._expr(v, scope)
        elif t is ast.InterpolatedStr:
            for part in node.parts:
                self._expr(part, scope)
        elif t is ast.TypeCast:
            self._expr(node.expr, scope)

        elif t is ast.MathBlock:
            # typed math block `(expr): TYPE` — walk the inner expression so
            # variable-usage / null-arith analysis still sees inside it.
            self._null_arith(node.expr, scope)
            self._expr(node.expr, scope)

        elif t is ast.DynResolve:
            # BUGFIX (bugs.log): `(holder_name)` — runtime SY reflection —
            # reads the holder variable's current value to resolve the real
            # target, but this walker never touched it, so every SY holder
            # only ever used through dynamic resolution (its entire purpose)
            # was flagged as an "Unused Variable" false positive.
            info = scope.lookup(node.holder_name)
            if info is not None:
                scope.mark_used(node.holder_name)

        elif t in (ast.FileExists, ast.FileNew, ast.FileDelete):
            self._expr(node.path_expr, scope)
        elif t is ast.FileRename:
            self._expr(node.old_path, scope)
            self._expr(node.new_path, scope)
        elif t is ast.FileCopy:
            self._expr(node.src_path, scope)
            self._expr(node.dst_path, scope)

        elif t is ast.FieldAccess:
            self._expr(node.obj, scope)

            if isinstance(node.obj, ast.Var):
                obj_info = scope.lookup(node.obj.name)

                if obj_info:
                    obj_type = obj_info.get('vtype')

                    if obj_type in self.classes:
                        fields = self.classes[obj_type]['fields']

                        if node.field in fields:
                            fields[node.field]['used'] = True

        elif t is ast.ClassInstantiate:
            if node.class_name not in self.classes:
                suggestion = _closest_name(node.class_name, list(self.classes))
                msg = f"Unknown class: {node.class_name}"
                if suggestion:
                    msg += f"\n\nDid you mean: {suggestion}?"
                self._emit('ERROR', None, 'Unknown Class', msg)
            else:
                self.classes[node.class_name]['used'] = True
        elif t is ast.Input:
            if node.prompt:
                self._expr(node.prompt, scope)
        elif t is ast.FFILoad:
            self._ffi_load(node, scope)
        elif t is ast.ThreadRunning:
            pass

    def _var_decl(self, node: ast.VarDecl, scope: Scope):
        is_heap = self._is_heap_node(node.value) or (node.vtype in HEAP_TYPES)
        possibly_null = self._is_null_node(node.value)
        inferred_type = self._infer(node.value, scope) if node.value else node.vtype

        info = {
            'mutable':       node.mutable,
            'vtype':         node.vtype or inferred_type,
            'dropped':       False,
            'used':          False,
            'is_heap':       is_heap,
            'line':          self._ln('var', node.name),
            'drop_line':     None,
            'possibly_null': possibly_null,
            # BUG-2: what `for x in <this var>` will yield (None = unknown).
            'etype':         self._decl_elem_type(node, scope),
            # BUG-15: `let local` is auto-dropped at scope exit, so it must
            # never be leak-reported.
            'is_local':      bool(node.is_local),
        }
        # Bug 14: remember literal keys from an `index` literal initializer
        # so later `.add(key, ...)` calls can be checked against them.
        if isinstance(node.value, ast.DictExpr) and getattr(node.value, 'is_index', False):
            keys = set()
            for k, _v in node.value.pairs:
                lit = self._literal_key(k)
                if lit is not None:
                    keys.add(lit)
            info['index_keys'] = keys
        # `index` holds exactly one SCALAR value per key (never a list/index/
        # dict/dict+) — mirrors codegen's compile-time check. Only fires when
        # the variable is actually declared `index` (a `dict` literal reuses
        # the same [key: value] bracket syntax and legitimately allows
        # collection values).
        if node.vtype == "index" and isinstance(node.value, ast.DictExpr):
            self._check_index_values_scalar(node.value, node.name)
        # Per spec: 'let' without 'local' enters the global memory pool,
        # even when declared inside a function or class method body.
        # Only 'let local' and function parameters stay function-scoped.
        # Exception 1: inside a try block, declarations are try-local to
        #   prevent false duplicate errors across separate try blocks.
        # Exception 2: inside a function body, re-declaring an existing
        #   global is treated as an update (not a duplicate).
        if node.is_local or self._try_depth > 0:
            target_scope = scope
        else:
            target_scope = self._global_scope or scope

        if node.name in target_scope.vars:
            existing = target_scope.vars[node.name]
            if existing.get('__stub'):
                # Pre-declared stub — replace with real declaration info
                existing.update({
                    'mutable':       node.mutable,
                    'vtype':         node.vtype or inferred_type,
                    'is_heap':       is_heap,
                    'possibly_null': possibly_null,
                    'line':          self._ln('var', node.name),
                    'etype':         info['etype'],
                    '__stub':        False,
                })
                self._expr(node.value, scope)
                return
            # Inside a function/method, re-declaring an existing global variable
            # is a valid update of that global — not a duplicate symbol error.
            if self._fn_depth > 0 and target_scope is self._global_scope:
                existing.update({
                    'mutable':       node.mutable,
                    'vtype':         node.vtype or inferred_type,
                    'is_heap':       is_heap,
                    'possibly_null': possibly_null,
                    'etype':         info['etype'],
                })
                self._expr(node.value, scope)
                return
            # Per spec's Variables Overwrite Rule: re-using `let` with an
            # existing variable name drops and recreates it (even with a new
            # type) — this is NOT a duplicate-symbol error. (Genuine conflicts
            # with a function/class name are tracked in separate scope dicts
            # and checked elsewhere, so anything reaching here is var-vs-var.)
            existing.update({
                'mutable':       node.mutable,
                'vtype':         node.vtype or inferred_type,
                'is_heap':       is_heap,
                'possibly_null': possibly_null,
                'line':          self._ln('var', node.name),
                'etype':         info['etype'],
                'dropped':       False,
            })
            self._expr(node.value, scope)
            return

        target_scope.declare(node.name, info)

        if is_heap:
            self.global_allocs += 1

        if node.mutable and not node.is_local and target_scope.parent is None:
            self.global_muts[node.name] = info

        if node.vtype and node.value is not None and not possibly_null:
            inferred = self._infer(node.value, scope)
            if inferred and not self._types_compat(node.vtype, inferred):
                self._emit(
                    'ERROR', info['line'], 'Type Error',
                    f"Expected:\n{node.vtype}\n\nReceived:\n{inferred}",
                    f"let {node.name}: {node.vtype} = ..."
                )

        self._expr(node.value, scope)

    def _assign(self, node: ast.Assign, scope: Scope):
        name = node.name if isinstance(node.name, str) else None
        if name:
            info = scope.lookup(name)
            if info is None:
                self._emit('ERROR', None, 'Unknown Variable',
                           f"Unknown variable: {name}")
            else:
                scope.mark_used(name)
                if not info.get('mutable'):
                    self._emit(
                        'ERROR', info.get('line'), 'Mutability Violation',
                        f"Variable '{name}' is immutable.",
                        f"Declare '{name}' with 'let mut {name}' to reassign."
                    )
                if info.get('dropped'):
                    self._emit(
                        'ERROR', info.get('line'), 'Use After Drop',
                        f"Variable '{name}' was dropped on line {info.get('drop_line', '?')}."
                    )
                # Bug 2: reassigning to an incompatible type was never checked.
                vtype = info.get('vtype')
                if vtype and node.value is not None and not self._is_null_node(node.value):
                    new_type = self._infer(node.value, scope)
                    if new_type and not self._types_compat(vtype, new_type):
                        self._emit(
                            'ERROR', info.get('line'), 'Type Error',
                            f"Expected:\n{vtype}\n\nReceived:\n{new_type}",
                            f"{name} = <{vtype} value>"
                        )
        self._expr(node.value, scope)

    def _field_assign(self, node: ast.FieldAssign, scope: Scope):
        if isinstance(node.obj, ast.Var):
            info = scope.lookup(node.obj.name)
            if info:
                scope.mark_used(node.obj.name)
                vtype = info.get('vtype')
                # Mark field as used in class map
                if vtype and vtype in self.classes:
                    finfo = self.classes[vtype]['fields'].get(node.field)
                    if finfo:
                        finfo['used'] = True
                if not info.get('mutable'):
                    self._emit(
                        'ERROR', info.get('line'), 'Mutability Violation',
                        f"Field '{node.field}' is immutable.",
                        f"Declare '{node.obj.name}' with 'mut' to modify fields."
                    )
                else:
                    if vtype and vtype in self.classes:
                        finfo = self.classes[vtype]['fields'].get(node.field)
                        if finfo and not finfo.get('mutable'):
                            self._emit(
                                'ERROR', info.get('line'), 'Mutability Violation',
                                f"Field '{node.field}' is immutable."
                            )
        self._expr(node.value, scope)

    def _fn_def(self, node: ast.FnDef, parent_scope: Scope):
        if node.name in self.functions and self.functions[node.name].get('_seen'):
            self._emit('ERROR', self._ln('fn', node.name), 'Duplicate Symbol',
                       f"Function '{node.name}' already exists.")
            return
        if node.name in self.functions:
            self.functions[node.name]['_seen'] = True
        else:
            self.functions[node.name] = {
                'params':   node.params,
                'ret_type': node.ret_type,
                'used':     False,
                'line':     self._ln('fn', node.name),
                '_seen':    True,
            }

        fn_scope = Scope(parent=parent_scope)
        seen_params = set()
        for pname, ptype in (node.params or []):
            if pname in seen_params:
                self._emit('ERROR', self._ln('fn', node.name), 'Duplicate Symbol',
                           f"Parameter '{pname}' is declared more than once "
                           f"in function '{node.name}'.",
                           f"Rename one of the '{pname}' parameters.")
            seen_params.add(pname)
            fn_scope.declare(pname, {
                'mutable':       True,
                'vtype':         ptype,
                'dropped':       False,
                'used':          False,
                'is_heap':       ptype in HEAP_TYPES if ptype else False,
                'line':          self._ln('fn', node.name),
                'drop_line':     None,
                'possibly_null': False,
            })

        if node.name in self.thread_fns:
            self._scan_race(node.body, node.name)

        self._scan_thread_reuse(node.body)
        self._scan_os_sessions(node.body)

        found_return = False
        self._fn_depth += 1
        for stmt in node.body:
            if found_return:
                self._emit('WARNING', None, 'Unreachable Code',
                           f"Code after return in function '{node.name}'.")
                break
            self._node(stmt, fn_scope, in_loop=False)
            if isinstance(stmt, ast.Return):
                found_return = True
                if node.ret_type and stmt.value is not None:
                    inferred = self._infer(stmt.value, fn_scope)
                    if inferred and not self._types_compat(node.ret_type, inferred):
                        self._emit(
                            'ERROR', self._ln('fn', node.name), 'Return Type Error',
                            f"Expected:\n{node.ret_type}\n\nReceived:\n{inferred}"
                        )
                elif node.ret_type and stmt.value is None:
                    # Bug 7: a bare `return` was previously ignored by the
                    # type check entirely instead of being flagged.
                    self._emit(
                        'ERROR', self._ln('fn', node.name), 'Return Type Error',
                        f"Function '{node.name}' declares return type "
                        f"'{node.ret_type}' but 'return' has no value.",
                        f"Use 'return <{node.ret_type} value>'."
                    )

        param_names = {p[0] for p in (node.params or [])}
        self._fn_depth -= 1
        for vname, vinfo in fn_scope.vars.items():
            if vname in param_names:
                continue
            # BUGFIX (bugs.log): a SY holder used only as `fn (name)() {...}`
            # is substituted into the function's name at PARSE time (a pure
            # compile-time string operation — see parser.py's fn_def) and
            # leaves no AST trace the usage-tracking walker can see, so it
            # was always flagged "unused" even for the syntax file's own SY
            # example. SY variables are exempt from this check.
            if not vinfo.get('used') and vname not in self._sy_holder_names:
                self._emit('INFO', vinfo.get('line'), 'Unused Variable',
                           f"Unused variable: {vname}")
            # BUG-15: a `let local` is released automatically when its block
            # ends — spec ("Local variables ... automatically dropped when
            # their scope ends", "Automatically dropped at scope exit") and,
            # since the BUG-3 fix, the generated code really does free it.
            # Warning about it told the user to write a .drop() that is both
            # unnecessary and, for a value read out of a collection, actively
            # misleading.
            if (vinfo.get('is_heap') and not vinfo.get('dropped')
                    and not vinfo.get('is_local')):
                self._emit(
                    'WARNING', vinfo.get('line'), 'Possible Memory Leak',
                    f"Variable '{vname}' was never dropped.",
                    f"Call {vname}.drop() before leaving scope."
                )

    def _scan_os_sessions(self, body: list, active: set | None = None):
        """Bug 12: walk a function body sequentially tracking which literal
        OS session IDs are currently open (started via os.start(id) and not
        yet os(id).drop()'d). Flags os.run()/os(id).drop() on an ID that was
        never started. Best-effort/static, same straight-line approach as
        _scan_thread_reuse."""
        if active is None:
            active = set()
        for stmt in (body or []):
            # OsRun/OsDrop/OsStart are often used as expressions, e.g.
            # `let output = os.run(1, "echo hello")` — unwrap one level so
            # those are still checked, not just bare-statement calls.
            check = stmt
            if isinstance(stmt, (ast.VarDecl, ast.Assign, ast.Return, ast.Print, ast.Println)) \
                    and getattr(stmt, 'value', None) is not None:
                check = stmt.value
            if isinstance(check, ast.OsStart):
                if isinstance(check.id_expr, ast.Number):
                    active.add(check.id_expr.value)
            elif isinstance(check, ast.OsRun):
                if check.id_expr is not None and isinstance(check.id_expr, ast.Number):
                    if check.id_expr.value not in active:
                        self._emit(
                            'ERROR', None, 'OS Session Not Started',
                            f"os.run() used ID {check.id_expr.value}, but no "
                            f"os.start({check.id_expr.value}) was seen first.",
                            f"Call os.start({check.id_expr.value}) before running commands on it."
                        )
            elif isinstance(check, ast.OsDrop):
                if isinstance(check.id_expr, ast.Number):
                    if check.id_expr.value not in active:
                        self._emit(
                            'ERROR', None, 'OS Session Not Started',
                            f"os({check.id_expr.value}).drop() called, but no "
                            f"os.start({check.id_expr.value}) was seen first.",
                            f"Call os.start({check.id_expr.value}) before dropping it."
                        )
                    else:
                        active.discard(check.id_expr.value)
            for attr in ('then_body', 'else_body', 'body', 'try_body', 'error_body'):
                sub = getattr(stmt, attr, None)
                if sub:
                    self._scan_os_sessions(sub, active)

    def _scan_thread_reuse(self, body: list, active: set | None = None, ever_started: set | None = None):
        """Bug 10/13: walk a function body tracking thread IDs.
        `active`      — IDs currently in-flight (ThreadCall → add, ThreadWait → discard)
        `ever_started`— all IDs ever launched in this scope (never removed)
        Reuse check uses `active`; wait/running checks use `ever_started` so
        that thread.running(id) after thread.wait(id) is NOT a false positive."""
        if active is None:
            active = set()
        if ever_started is None:
            ever_started = set()
        for stmt in (body or []):
            check = stmt
            if isinstance(stmt, (ast.VarDecl, ast.Assign, ast.Return, ast.Print, ast.Println)) \
                    and getattr(stmt, 'value', None) is not None:
                check = stmt.value
            if isinstance(check, ast.ThreadCall):
                tid = check.thread_id
                if isinstance(tid, ast.Number):
                    if tid.value in active:
                        self._emit(
                            'ERROR', None, 'Thread ID Reuse',
                            f"Thread ID {tid.value} is reused before the "
                            f"previous thread on that ID has been waited on.",
                            f"Call thread.wait({tid.value}) before starting "
                            f"another thread with the same ID."
                        )
                    active.add(tid.value)
                    ever_started.add(tid.value)
            elif isinstance(check, ast.ThreadWait):
                for tid_expr in (check.thread_ids or []):
                    if isinstance(tid_expr, ast.Number):
                        if tid_expr.value not in ever_started:
                            self._emit(
                                'ERROR', None, 'Thread Not Started',
                                f"thread.wait({tid_expr.value}) used, but no "
                                f"thread(..., {tid_expr.value}) was seen first.",
                                f"Start the thread with that ID before waiting on it."
                            )
                        active.discard(tid_expr.value)
            elif isinstance(check, ast.ThreadRunning):
                tid_expr = check.thread_id
                if isinstance(tid_expr, ast.Number) and tid_expr.value not in ever_started:
                    self._emit(
                        'ERROR', None, 'Thread Not Started',
                        f"thread.running({tid_expr.value}) used, but no "
                        f"thread(..., {tid_expr.value}) was seen first.",
                        f"Start the thread with that ID before checking it."
                    )
            for attr in ('then_body', 'else_body', 'body', 'try_body', 'error_body'):
                sub = getattr(stmt, attr, None)
                if sub:
                    self._scan_thread_reuse(sub, active, ever_started)

    def _class_def(self, node: ast.ClassDef, scope: Scope):
        if node.name in self.classes and self.classes[node.name].get('_seen'):
            self._emit('ERROR', self._ln('class', node.name), 'Duplicate Symbol',
                       f"Class '{node.name}' already exists.")
        else:
            if node.name in self.classes:
                self.classes[node.name]['_seen'] = True

        # Bug 9: field default values were never type-checked against
        # their declared vtype (unlike top-level `let`, checked in
        # Analyzer._var_decl()).
        for f in (node.fields or []):
            if f.vtype and f.value is not None and not self._is_null_node(f.value):
                inferred = self._infer(f.value, scope)
                if inferred and not self._types_compat(f.vtype, inferred):
                    self._emit(
                        'ERROR', self._ln('class', node.name), 'Type Error',
                        f"Field '{node.name}.{f.name}'\n\n"
                        f"Expected:\n{f.vtype}\n\nReceived:\n{inferred}",
                        f"let {f.name}: {f.vtype} = ..."
                    )

        for method in node.methods:
            method_scope = Scope(parent=scope)
            method_scope.declare('__self', {
                'mutable': True, 'vtype': node.name, 'dropped': False,
                'used': True, 'is_heap': False, 'line': None,
                'drop_line': None, 'possibly_null': False,
            })
            # Add class fields to method scope so they can be referenced directly
            for fname, finfo in self.classes[node.name]['fields'].items():
                method_scope.declare(fname, {
                    'mutable': finfo.get('mutable', True),
                    'vtype': finfo.get('vtype'),
                    'dropped': False,
                    'used': True,  # pre-mark used to avoid spurious warnings inside methods
                    'is_heap': finfo.get('vtype') in HEAP_TYPES if finfo.get('vtype') else False,
                    'line': None,
                    'drop_line': None,
                    'possibly_null': False,
                })
            seen_params = set()
            for pname, ptype in (method.params or []):
                if pname in seen_params:
                    self._emit('ERROR', self._ln('class', node.name), 'Duplicate Symbol',
                               f"Parameter '{pname}' is declared more than once "
                               f"in method '{node.name}.{method.name}'.",
                               f"Rename one of the '{pname}' parameters.")
                seen_params.add(pname)
                method_scope.declare(pname, {
                    'mutable': True, 'vtype': ptype, 'dropped': False,
                    'used': True, 'is_heap': ptype in HEAP_TYPES if ptype else False,
                    'line': None, 'drop_line': None, 'possibly_null': False,
                })
            self._fn_depth += 1
            for stmt in (method.body or []):
                self._node(stmt, method_scope, in_loop=False)
            self._fn_depth -= 1

        # BUG-16: the unused-FIELD report used to run right here, while the
        # ClassDef itself was being analysed — i.e. BEFORE any of the code that
        # actually uses the instance had been walked. Every field of a class
        # declared above its first use was therefore reported unused, which is
        # the normal layout (and the syntax file's own CLASSES example).
        # Reporting is deferred to _check_unused, after the whole program has
        # been analysed.

    def _drop(self, node: ast.Drop, scope: Scope):
        info = scope.lookup(node.name)
        if info is None:
            self._emit('ERROR', None, 'Unknown Variable',
                       f"Unknown variable: {node.name}")
            return
        if info.get('dropped'):
            self._emit(
                'WARNING', info.get('drop_line'), 'Redundant Drop',
                f"Variable '{node.name}' was already dropped."
            )
        else:
            scope.mark_dropped(node.name, info.get('line'))

    def _thread_call(self, node: ast.ThreadCall, scope: Scope):
        self._expr(node.func_call, scope)
        self._expr(node.thread_id, scope)

    def _while(self, node: ast.While, scope: Scope):
        is_const_true  = (isinstance(node.cond, ast.Bool) and
                          str(node.cond.value).lower() == 'true')
        is_const_false = (isinstance(node.cond, ast.Bool) and
                          str(node.cond.value).lower() == 'false')

        if is_const_false:
            self._emit('INFO', None, 'Unreachable Loop',
                       "Loop never executes.",
                       "Condition is always False.")
        elif is_const_true and not self._has_break(node.body):
            self._emit('WARNING', None, 'Potential Infinite Loop',
                       "Loop condition never changes.",
                       "Add a break condition or modify the loop variable.")

        loop_scope = Scope(parent=scope)
        for stmt in node.body:
            self._node(stmt, loop_scope, in_loop=True)

    def _has_break(self, body: list) -> bool:
        for node in body:
            if isinstance(node, ast.Break):
                return True
            for attr in ('then_body', 'else_body', 'body'):
                sub = getattr(node, attr, None)
                if sub and self._has_break(sub):
                    return True
        return False

    def _for(self, node: ast.For, scope: Scope):
        loop_scope = Scope(parent=scope)
        # BUG-2: type the loop variable from what the loop actually yields
        # (i32 only for `range`), not unconditionally i32.
        loop_vtype = self._iter_elem_type(node.iterable, scope)
        loop_scope.declare(node.var, {
            'mutable': True, 'vtype': loop_vtype, 'dropped': False, 'used': True,
            # is_heap stays False: loop variables are auto-dropped at the end
            # of the loop per spec, so they must never be leak-reported.
            'is_heap': False, 'line': None, 'drop_line': None,
            'possibly_null': False,
        })
        if node.iterable:
            self._expr(node.iterable, scope)
        for stmt in node.body:
            self._node(stmt, loop_scope, in_loop=True)

    def _if(self, node: ast.If, scope: Scope):
        if isinstance(node.cond, ast.Compare):
            self._null_compare(node.cond, scope)
        self._expr(node.cond, scope)
        for stmt in (node.then_body or []):
            self._node(stmt, scope)
        for stmt in (node.else_body or []):
            self._node(stmt, scope)

    def _fn_call(self, node: ast.FnCall, scope: Scope):
        name = node.name if isinstance(node.name, str) else None
        if name is None:
            # node.name is itself a node here — either a chained-collection
            # FnCall (e.g. nested(0)(1)) or a DynResolve (`(fn_name)()`,
            # runtime SY function reflection). BUGFIX (bugs.log): this used
            # to only walk the call args, never node.name itself, so a SY
            # holder used solely to call a dynamically-named function was
            # never marked used — a false "Unused Variable" positive.
            self._expr(node.name, scope)
            for a in node.args:
                self._expr(a, scope)
            return

        # Intercept valid collection call syntax (e.g. layer_var().add())
        if scope.lookup(name) is not None:
            scope.mark_used(name)
            for a in node.args:
                self._expr(a, scope)
            return

        if '.' in name:
            ns = name.split('.')[0]
            if ns not in self.namespaces:
                if ns in KNOWN_MODULES:
                    self._emit('ERROR', None, 'Module Not Enabled',
                               f"Module not enabled: {ns}",
                               f"use {ns}")
                else:
                    self._emit('ERROR', None, 'Unknown Namespace',
                               f"Unknown namespace: {ns}")
            for a in node.args:
                self._expr(a, scope)
            return

        if name not in self.functions and name not in BUILTIN_FNS \
                and name not in self.classes \
                and name not in self.namespaces:
            all_fns = list(self.functions) + list(BUILTIN_FNS) + list(self.classes)
            suggestion = _closest_name(name, all_fns)
            msg = f"Unknown function: {name}()"
            if suggestion:
                msg += f"\n\nDid you mean: {suggestion}?"
            self._emit('ERROR', None, 'Unknown Function', msg)
            for a in node.args:
                self._expr(a, scope)
            return

        if name in self.functions:
            self.functions[name]['used'] = True
            params = self.functions[name].get('params') or []
            if len(node.args) != len(params):
                self._emit(
                    'ERROR', None, 'Function Argument Error',
                    f"Function '{name}' expects {len(params)} argument(s), "
                    f"got {len(node.args)}."
                )
            else:
                for arg, (pname, ptype) in zip(node.args, params):
                    if ptype and ptype != 'Any':
                        atype = self._infer(arg, scope)
                        if atype and not self._types_compat(ptype, atype):
                            self._emit(
                                'ERROR', None, 'Function Argument Error',
                                f"Parameter '{pname}'\n\nExpected:\n{ptype}\n\nReceived:\n{atype}"
                            )

        # Class instantiation called as a function: let x = MyClass()
        if name in self.classes:
            self.classes[name]['used'] = True

        for a in node.args:
            self._expr(a, scope)

    def _var_usage(self, node: ast.Var, scope: Scope):
        name = node.name
        
        # Intercept native types passed as configuration/function args
        # BUGFIX: SY isn't part of ALL_TYPES (it's a reflection-specific
        # pseudo-type, not a real value type), so `x.to(SY)` was flagged as
        # a reference to an undeclared variable named "SY" — a false
        # positive on entirely valid code.
        if name in ALL_TYPES or name == "SY":
            return

        # Check scope first — a declared variable shadows a namespace with the same name.
        # This matches Rubidium's shadowing rule: local/global scope beats namespace.
        info = scope.lookup(name)
        if info is not None:
            if info.get('dropped'):
                self._emit('ERROR', info.get('drop_line'), 'Use After Drop',
                           f"Variable '{name}' was dropped and cannot be used.",
                           f"Remove the drop, or recreate the variable before using it.")
            else:
                scope.mark_used(name)
                if info.get('possibly_null'):
                    pass   # null propagation tracked elsewhere
            return

        # Allow module/namespace names (only when NOT shadowed by a variable above)
        if name in self.namespaces or name in KNOWN_MODULES:
            return

        if '.' in name:
            ns = name.split('.')[0]
            if ns not in self.namespaces and ns in KNOWN_MODULES:
                self._emit('ERROR', None, 'Module Not Enabled',
                           f"Module not enabled: {ns}", f"use {ns}")
            return

        # Variable not in scope and not a namespace — unknown symbol
        if (name not in self.functions and name not in self.classes
                and name not in BUILTIN_FNS):
            known = (list(self.functions) + list(self.classes) +
                     list(BUILTIN_FNS) + list(scope.vars.keys()))
            suggestion = _closest_name(name, known)
            msg = f"Unknown variable: {name}"
            if suggestion:
                msg += f"\n\nDid you mean: {suggestion}?"
            self._emit('ERROR', None, 'Unknown Variable', msg)

    def _method_call(self, node: ast.MethodCall, scope: Scope):
        # BUG-16: `p.scores(0).set(99)` / `p.scores().add(50)` reach a class's
        # COLLECTION field through a MethodCall whose `method` is the field
        # name, never through a FieldAccess — so those fields were still
        # reported "Unused Field" even though the spec's own CLASSES example
        # mutates them exactly this way. Mark the field used here too.
        obj = node.obj
        while isinstance(obj, ast.MethodCall):
            inner = obj.obj
            if isinstance(inner, ast.Var):
                oinfo = scope.lookup(inner.name)
                vtype = oinfo.get('vtype') if oinfo else None
                if vtype in self.classes:
                    finfo = self.classes[vtype]['fields'].get(obj.method)
                    if finfo:
                        finfo['used'] = True
            obj = inner
        if isinstance(node.obj, ast.Var):
            oinfo = scope.lookup(node.obj.name)
            vtype = oinfo.get('vtype') if oinfo else None
            if vtype in self.classes:
                finfo = self.classes[vtype]['fields'].get(node.method)
                if finfo:
                    finfo['used'] = True

        # Handles plain `.method()` calls. The parser never emits
        # CollectionMethodCall (see bugs.log #1), so collection mutations
        # like `my_list().add(x)` / `my_list(0).set(x)` arrive here as a
        # MethodCall whose .obj is a FnCall naming the collection. Detect
        # and flag mutability violations for those mutating methods.
        if node.method in MUTATING_METHODS and isinstance(node.obj, ast.FnCall):
            cname = node.obj.name if isinstance(node.obj.name, str) else None
            if cname:
                info = scope.lookup(cname)
                if info is not None and not info.get('mutable'):
                    self._emit(
                        'ERROR', info.get('line'), 'Mutability Violation',
                        f"Collection '{cname}' is immutable.",
                        f"Declare '{cname}' with 'let mut' to modify it."
                    )
                # Bug 14: idx().add("existing_key", val) where "existing_key"
                # is provably already a key in idx's literal initializer.
                if info is not None and node.method == 'add' and node.args:
                    existing_keys = info.get('index_keys')
                    if existing_keys:
                        new_key = self._literal_key(node.args[0])
                        if new_key is not None and new_key in existing_keys:
                            self._emit(
                                'ERROR', info.get('line'), 'Duplicate Key',
                                f"Key {new_key[1]!r} already exists in index '{cname}'.\n"
                                f".add() on an existing key is a runtime error.",
                                f"Use {cname}({new_key[1]!r}).set(...) to update "
                                f"an existing key instead of .add()."
                            )
                # `index` values must be scalar — idx().add(key, [list]) or
                # idx(key).set([list]) are invalid, mirroring codegen's
                # compile-time check.
                bad_val = None
                if (info is not None and info.get('vtype') == 'index'
                        and node.method == 'add' and len(node.args) == 2
                        and isinstance(node.args[1], (ast.ListExpr, ast.DictExpr))):
                    bad_val = node.args[1]
                elif (info is not None and info.get('vtype') == 'index'
                        and node.method == 'set' and len(node.args) == 1
                        and isinstance(node.args[0], (ast.ListExpr, ast.DictExpr))):
                    bad_val = node.args[0]
                if bad_val is not None:
                    kind = "list" if isinstance(bad_val, ast.ListExpr) else ("index" if getattr(bad_val, "is_index", False) else "dict")
                    self._emit(
                        'ERROR', info.get('line'), 'Invalid Index Value',
                        f"index '{cname}': value is a {kind}, not a scalar.",
                        "`index` holds exactly one scalar value per key — use `dict` "
                        "instead if a key needs to hold a collection of values."
                    )
        # Bug 6: string-mutation methods (.set/.insert/.replace) called
        # directly on a variable (e.g. `text.set(0, "J")`) need the same
        # mutability check — these reach us with node.obj as a plain Var,
        # not a FnCall, so the check above doesn't cover them.
        STRING_MUTATING_METHODS = {'set', 'insert', 'replace'}
        if node.method in STRING_MUTATING_METHODS and isinstance(node.obj, ast.Var):
            vname = node.obj.name
            info = scope.lookup(vname)
            if info is not None and info.get('vtype') == 'str' and not info.get('mutable'):
                self._emit(
                    'ERROR', info.get('line'), 'Mutability Violation',
                    f"String '{vname}' is immutable.",
                    f"Declare '{vname}' with 'let mut' to modify it."
                )
        self._expr(node.obj, scope)
        for a in node.args:
            self._expr(a, scope)

    def _collection_method(self, node: ast.CollectionMethodCall, scope: Scope):
        self._expr(node.obj, scope)
        if isinstance(node.obj, ast.Var) and node.method in MUTATING_METHODS:
            info = scope.lookup(node.obj.name)
            if info and not info.get('mutable'):
                self._emit(
                    'ERROR', info.get('line'), 'Mutability Violation',
                    'Collection is immutable.',
                    f"Declare '{node.obj.name}' with 'let mut' to modify it."
                )
        for a in node.args:
            self._expr(a, scope)

    def _ffi_load(self, node: ast.FFILoad, scope: Scope):
        if isinstance(node.path_expr, ast.Str):
            path = node.path_expr.value.strip('"')
            if os.path.exists(path):
                return
            # GLFW/FFI bundling report: a BARE library name (no '/' — the
            # common case, e.g. FFI("libglfw.so")) is resolved by the dynamic
            # linker's OWN search paths at runtime (system lib dirs, or the
            # project's bundled build/lib/ — see ffi_load()'s runtime fallback
            # in compiler.py), never literally relative to the debugger's
            # current directory. Checking only os.path.exists() against a bare
            # name is almost always a false positive for a real, correctly-
            # installed system library (e.g. libglfw.so.3 via apt) — it warned
            # on every clean build regardless of whether the library was
            # actually findable. Also try ctypes.util.find_library, which
            # mirrors how the system's own linker would resolve it, before
            # warning.
            if '/' not in path:
                # Strip a trailing version suffix first (e.g. "libglfw.so.3"
                # -> "libglfw.so" — .so.N/.so.N.N is the normal versioned-
                # shared-object naming on Linux), then the extension itself.
                bare = re.sub(r'\.so(\.\d+)*$', '', path)
                for suffix in ('.dll', '.dylib'):
                    if bare.endswith(suffix):
                        bare = bare[: -len(suffix)]
                        break
                # find_library wants the bare name without a leading "lib" too
                lookup_name = bare[3:] if bare.startswith('lib') else bare
                try:
                    import ctypes.util
                    if ctypes.util.find_library(lookup_name):
                        return
                except Exception:
                    pass
            self._emit('WARNING', None, 'Library Not Found',
                       f"Library not found: {path}")

    def _null_arith(self, node, scope: Scope):
        if self._is_null_node(node):
            self._emit('WARNING', None, 'Possible Null Usage',
                       "Expression uses Null in arithmetic.",
                       "Null in arithmetic evaluates to 0 or False.")

    def _null_compare(self, node: ast.Compare, scope: Scope):
        # Per spec: Null is smaller than every non-null value.
        # - Null == Null  → True
        # - Null < n      → True  (for any non-null n)
        # - Null > n      → False
        # - Null != n     → True  (for any non-null n)
        # Only flag comparisons that are always False per these rules.
        l_null = self._is_null_node(node.left)
        r_null = self._is_null_node(node.right)
        if not (l_null or r_null):
            return
        both_null = l_null and r_null
        op = node.op
        # Determine whether this comparison is always False
        always_false = False
        if both_null:
            # Null == Null → True, Null != Null → False, Null < Null → False
            if op in ("<", ">", "<=", ">="):
                always_false = op not in ("<=", ">=")   # Null <= Null and >= Null are True
            elif op == "!=":
                always_false = True
        else:
            # BUG-17: only a LITERAL on the other side is provably non-Null.
            # `Null` is a valid value for every type (spec: NULL BEHAVIOR), so
            # `let e: i32 = Null` followed by `e == Null` is True — and the
            # compiled binary prints True. Judging a variable comparison here
            # told the user their correct code was "always False". This mirrors
            # the compiler, which likewise only constant-folds a Null
            # comparison against Number/Str/Bool/UnaryOp literals.
            other = node.right if l_null else node.left
            if not isinstance(other, (ast.Number, ast.Str, ast.Bool, ast.UnaryOp)):
                return
            if self._is_null_node(other):
                return
            # One side is Null, other is a concrete non-null literal
            # Null < x → True  (not always false)
            # Null > x → False (always false)
            # Null == x → False (always false)
            # Null != x → True  (not always false)
            if (l_null and op == ">") or (r_null and op == "<"):
                always_false = True
            elif (l_null and op == "==") or (r_null and op == "=="):
                always_false = True
            elif (l_null and op == ">=") or (r_null and op == "<="):
                always_false = True
        if always_false:
            self._emit('INFO', None, 'Condition Always False',
                       "Condition always evaluates to False.",
                       f"Comparison with Null is always False.")

    def _scan_race(self, body: list, fn_name: str):
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                name = stmt.name if isinstance(stmt.name, str) else None
                if name and name in self.global_muts:
                    self._emit(
                        'WARNING', None, 'Potential Race Condition',
                        f"Variable '{name}' is a shared global modified by multiple threads.",
                        f"Accessed in thread function '{fn_name}'."
                    )
            for attr in ('body', 'then_body', 'else_body'):
                sub = getattr(stmt, attr, None)
                if sub:
                    self._scan_race(sub, fn_name)

    def _check_unused(self, global_scope: Scope):
        for fname, finfo in self.functions.items():
            if fname == 'main':
                continue
            if not finfo.get('used'):
                self._emit('INFO', finfo.get('line'), 'Unused Function',
                           f"Unused function: {fname}()")
        for vname, vinfo in global_scope.vars.items():
            # BUGFIX (bugs.log): see the identical SY exemption in the
            # per-function unused-variable check above.
            if not vinfo.get('used') and vname not in self._sy_holder_names:
                self._emit('INFO', vinfo.get('line'), 'Unused Variable',
                           f"Unused variable: {vname}")
        # BUG-16: unused fields, now that every use site has been seen.
        for cname, cinfo in self.classes.items():
            for fname, finfo in (cinfo.get('fields') or {}).items():
                if not finfo.get('used'):
                    self._emit('INFO', self._ln('class', cname), 'Unused Field',
                               f"Unused field: {cname}.{fname}")
        # Unused class definitions
        for cname, cinfo in self.classes.items():
            if not cinfo.get('used'):
                self._emit('INFO', cinfo.get('line'), 'Unused Class',
                           f"Class '{cname}' is defined but never instantiated.",
                           f"Instantiate it somewhere or remove the definition.")

    def _check_global_leaks(self, global_scope: Scope):
        leaks = []

        for vname, vinfo in global_scope.vars.items():

            # Only warn heap allocations. SY holders are exempt (BUG-15): SY is
            # a compile-time construct — codegen emits a NoOp for the
            # declaration and substitutes the name at parse time — so there is
            # no runtime allocation to drop, and `.drop()`ing one isn't even
            # expressible.
            if (vinfo.get('is_heap') and not vinfo.get('dropped')
                    and vname not in self._sy_holder_names):

                leaks.append(vname)

                self._emit(
                    'WARNING',
                    vinfo.get('line'),
                    'Possible Memory Leak',
                    f"Variable '{vname}' was never dropped.",
                    f"Call drop {vname} before leaving scope."
                )

        self._leak_vars = leaks

    def report(self, filepath: str, strict: bool = False) -> bool:
        errors   = [i for i in self.issues if i.severity == 'ERROR']
        warnings = [i for i in self.issues if i.severity == 'WARNING']
        infos    = [i for i in self.issues if i.severity == 'INFO']

        print()
        print(f"{ANSI['BOLD']}Rubidium Static Analyzer{ANSI['RESET']}")
        print(f"{ANSI['DIM']}Checking: {filepath}{ANSI['RESET']}")
        if strict:
            print(f"{ANSI['DIM']}Mode: strict{ANSI['RESET']}")
        print()

        if not self.issues:
            print(f"{ANSI['INFO']}✔ No issues found.{ANSI['RESET']}\n")
        else:
            for issue in self.issues:
                color = ANSI.get(issue.severity, '')
                reset = ANSI['RESET']
                print(f"{color}{issue.severity}{reset}:")
                print()
                if issue.line:
                    print(f"Line {issue.line}:")
                print(issue.category)
                print()
                print(issue.message)
                if issue.suggestion:
                    print()
                    print("Suggestion:")
                    print(f"  {issue.suggestion}")
                print()
                print(f"{ANSI['DIM']}{'─' * 44}{ANSI['RESET']}")
                print()

        leaks = self._leak_vars
        print(f"{ANSI['DIM']}{'─' * 44}{ANSI['RESET']}")
        print("Analysis Complete")
        print()
        print(f"Errors:   {len(errors)}")
        print(f"Warnings: {len(warnings)}")
        print(f"Info:     {len(infos)}")

        if leaks:
            print()
            print("Potential Leaks:")
            for v in leaks:
                print(f"  {v}")

        print()
        print(f"Estimated Global Allocations:")
        print(f"  {self.global_allocs}")
        print()

        compilation_ok = (
            len(errors) == 0 and
            (not strict or len(warnings) == 0)
        )
        status_color = ANSI['INFO'] if compilation_ok else ANSI['ERROR']
        status_text  = "COMPILATION ALLOWED" if compilation_ok else "COMPILATION BLOCKED"
        print(f"Status:")
        print(f"{status_color}{status_text}{ANSI['RESET']}")
        print()

        return compilation_ok


# ──────────────────────────────────────────────────────────────────────────────
# Token-level structural syntax checker
# ──────────────────────────────────────────────────────────────────────────────

def _token_syntax_check(tokens: list, source_lines: list) -> list:
    """
    Scan the raw token stream for structural syntax errors the parser
    silently swallows (mismatched braces, missing 'in', missing names, etc.).
    Returns a list of Issue objects.
    """
    issues = []
    n = len(tokens)

    def src(line):
        return source_lines[line - 1].rstrip() if 0 < line <= len(source_lines) else ""

    def emit(line, category, msg, suggestion=""):
        issues.append(Issue('ERROR', line, category, msg, suggestion))

    def emit_w(line, category, msg, suggestion=""):
        issues.append(Issue('WARNING', line, category, msg, suggestion))

    # ── 1. Brace / bracket / paren matching ───────────────────────────────────
    OPEN  = {'LPAREN': '(', 'LBRACE': '{', 'LBRACKET': '['}
    CLOSE = {'RPAREN': ')', 'RBRACE': '}', 'RBRACKET': ']'}
    PAIR  = {')': '(', '}': '{', ']': '['}
    NEED  = {'(': ')', '{': '}', '[': ']'}
    stack = []  # list of (char, line)

    for tok in tokens:
        kind, line = tok[0], tok[2]
        if kind in OPEN:
            stack.append((OPEN[kind], line))
        elif kind in CLOSE:
            ch = CLOSE[kind]
            if not stack:
                emit(line, 'Mismatched Bracket',
                     f"Unexpected '{ch}' — no matching opening bracket",
                     f"Remove the extra '{ch}' or add the missing opening '{PAIR[ch]}'")
            else:
                top, top_line = stack[-1]
                if top == PAIR[ch]:
                    stack.pop()
                else:
                    emit(line, 'Mismatched Bracket',
                         f"Closing '{ch}' does not match the '{top}' opened on line {top_line}\n"
                         f"  {ANSI['DIM']}→  {src(top_line)}{ANSI['RESET']}",
                         f"Add '{NEED[top]}' to close the '{top}' on line {top_line}")

    for char, line in stack:
        emit(line, 'Unclosed Bracket',
             f"'{char}' on line {line} is never closed — missing '{NEED[char]}'\n"
             f"  {ANSI['DIM']}→  {src(line)}{ANSI['RESET']}",
             f"Add a closing '{NEED[char]}'")

    # ── 2. 'for' loop missing 'in' ────────────────────────────────────────────
    for i, tok in enumerate(tokens):
        if tok[0] == 'FOR':
            line = tok[2]
            j = i + 1
            if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                var_name = tokens[j][1]
                j += 1
                if j < n and tokens[j][0] != 'IN':
                    found = tokens[j][1] if j < n else '?'
                    emit(line, 'Missing \'in\' Keyword',
                         f"'for' loop is missing the 'in' keyword\n\n"
                         f"  Found:    for {var_name} {found} ...\n"
                         f"  Expected: for {var_name} in ...",
                         f"for {var_name} in <list or range(0, n)> {{ ... }}")

    # ── 3. 'fn' missing a name ────────────────────────────────────────────────
    # BUGFIX (bugs.log): `fn (function_name)() { ... }` — the SY dynamic
    # function-name form documented in the syntax file (SY section) — was
    # flagged as "Missing Function Name" here, since it also starts with
    # `FN LPAREN`. This check only meant to catch a genuinely nameless
    # `fn(...)`; the SY form is `FN LPAREN IDENT RPAREN`, distinguishable by
    # the single bare identifier immediately inside the parens.
    for i, tok in enumerate(tokens):
        if tok[0] == 'FN':
            j = i + 1
            if (j < n and tokens[j][0] == 'LPAREN'
                    and not (j + 2 < n and tokens[j + 1][0] == 'IDENT'
                             and tokens[j + 2][0] == 'RPAREN')):
                emit(tok[2], 'Missing Function Name',
                     "Function definition is missing a name",
                     "fn my_function(param: type) -> return_type { ... }")

    # ── 4. 'fn' missing parameter list ───────────────────────────────────────
    for i, tok in enumerate(tokens):
        if tok[0] == 'FN':
            j = i + 1
            # Skip optional second IDENT (FFI handle name)
            if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                name = tokens[j][1]
                j += 1
                # FFI: fn handle symbol(...) — two idents before LPAREN is valid
                if j < n and tokens[j][0] in ('IDENT', 'TYPE'):
                    j += 1  # skip FFI symbol name
                if j < n and tokens[j][0] not in ('LPAREN', 'LBRACE'):
                    pass  # parser will handle this, avoid false positives

    # ── 5. 'class' missing name or missing '()' ──────────────────────────────
    for i, tok in enumerate(tokens):
        if tok[0] == 'CLASS':
            j = i + 1
            if j >= n:
                continue
            if tokens[j][0] not in ('IDENT', 'TYPE'):
                emit(tok[2], 'Missing Class Name',
                     "Class declaration is missing a name",
                     "class MyClass() { ... }")
            elif j + 1 < n and tokens[j + 1][0] not in ('LPAREN',):
                emit(tok[2], 'Missing Class Parentheses',
                     f"Class '{tokens[j][1]}' is missing '()' after the name",
                     f"class {tokens[j][1]}() {{ ... }}")

    # ── 6. 'let' / 'let mut' missing variable name ───────────────────────────
    for i, tok in enumerate(tokens):
        if tok[0] == 'LET':
            j = i + 1
            if j < n and tokens[j][0] == 'MUT':   j += 1
            if j < n and tokens[j][0] == 'LOCAL':  j += 1
            if j < n and tokens[j][0] not in ('IDENT', 'TYPE', 'FILE'):
                # BUGFIX: `let (name): type = ...` is valid SY-reflection
                # syntax (the declared name is dynamically substituted from
                # a previously-declared SY symbol) — LPAREN IDENT RPAREN,
                # not a bare name token. Don't flag it as missing a name.
                is_sy_reflection = (
                    tokens[j][0] == 'LPAREN' and j + 2 < n and
                    tokens[j + 1][0] == 'IDENT' and tokens[j + 2][0] == 'RPAREN'
                )
                if not is_sy_reflection:
                    found = tokens[j][1] if j < n else '?'
                    emit(tok[2], 'Missing Variable Name',
                         f"Variable declaration is missing a name — found '{found}' instead",
                         "let my_variable: type = value")

    # ── 7. 'if' / 'while' / 'for' body missing '{' ───────────────────────────
    # Track which tokens are condition-enders so we can spot missing braces.
    # Strategy: after a FOR/WHILE/IF we expect LBRACE eventually at the same
    # depth — we detect the common mistake of writing a single statement without
    # braces by seeing IF/WHILE/FOR followed eventually by a non-LBRACE token
    # at depth 0 after consuming the condition.
    # This is approximate but catches the most common case.
    depth = 0
    i = 0
    while i < n:
        kind, line = tokens[i][0], tokens[i][2]
        if kind in ('LPAREN', 'LBRACKET', 'LBRACE'):
            depth += 1
        elif kind in ('RPAREN', 'RBRACKET', 'RBRACE'):
            depth -= 1
        elif kind in ('IF', 'WHILE') and depth == 0:
            # Scan forward past the condition (depth inside parens handled naturally),
            # then check if LBRACE follows.
            j = i + 1
            cond_depth = 0
            while j < n:
                kk = tokens[j][0]
                if kk in ('LPAREN', 'LBRACKET'): cond_depth += 1
                elif kk in ('RPAREN', 'RBRACKET'):
                    if cond_depth == 0: break
                    cond_depth -= 1
                elif kk == 'LBRACE' and cond_depth == 0:
                    break
                elif kk in ('LET','FN','CLASS','IF','WHILE','FOR','PRINT','PRINTLN','RETURN') and cond_depth == 0:
                    # Hit a statement keyword before a brace — brace is missing
                    kw = tokens[i][1]
                    emit(line, f"Missing '{{' After '{kw}'",
                         f"The '{kw}' block is missing its opening '{{'\n"
                         f"  {ANSI['DIM']}→  {src(line)}{ANSI['RESET']}",
                         f"{kw} <condition> {{ ... }}")
                    break
                j += 1
        i += 1

    return issues


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def check_file(filepath: str, strict: bool = False) -> bool:
    if not os.path.exists(filepath):
        print(f"✖ Error: File not found: {filepath}")
        sys.exit(1)

    with open(filepath, 'r') as f:
        source = f.read()

    source_lines = source.split('\n')

    try:
        tokens = tokenize(source)

        # ── Token-level Structural Syntax Check ──────────────────────────────
        syntax_issues = _token_syntax_check(tokens, source_lines)
        if syntax_issues:
            print(f"\n{ANSI['BOLD']}Rubidium Syntax Check{ANSI['RESET']}")
            print(f"{ANSI['DIM']}Checking: {filepath}{ANSI['RESET']}\n")
            for issue in syntax_issues:
                color = ANSI.get(issue.severity, '')
                print(f"{color}{issue.severity}{ANSI['RESET']}:")
                print()
                if issue.line:
                    print(f"Line {issue.line}:")
                print(issue.category)
                print()
                print(issue.message)
                if issue.suggestion:
                    print()
                    print("Suggestion:")
                    print(f"  {issue.suggestion}")
                print()
                print(f"{ANSI['DIM']}{'─' * 44}{ANSI['RESET']}")
                print()
            errs = [i for i in syntax_issues if i.severity == 'ERROR']
            if errs:
                print(f"{ANSI['ERROR']}✖  {len(errs)} syntax error{'s' if len(errs) != 1 else ''} "
                      f"— analysis may be unreliable{ANSI['RESET']}\n")

        # ── Pre-parsing Keyword & Typo Pass ──
        KEYWORD_MAPPING = {
            'def': 'fn', 'function': 'fn', 'func': 'fn',
            'var': 'let', 'const': 'let',
            'elif': 'else if', 'elseif': 'else if',
            'nil': 'Null', 'none': 'Null', 'null': 'Null',
        }
        RUBIDIUM_KEYWORDS = [
            'let', 'mut', 'fn', 'class', 'drop', 'thread', 'while', 
            'for', 'if', 'else', 'return', 'print', 'println', 'use', 'import'
        ]

        has_typo_errors = False
        for tok in tokens:
            kind = getattr(tok, 'kind', tok[0] if isinstance(tok, (tuple, list)) else '')
            val = getattr(tok, 'value', tok[1] if isinstance(tok, (tuple, list)) else '')
            line = getattr(tok, 'line', tok[2] if isinstance(tok, (tuple, list)) else 0)
            col_offset = getattr(tok, 'col', 0)

            if kind == 'IDENT':
                if val.isupper():
                    continue

                if val in KEYWORD_MAPPING:
                    correct = KEYWORD_MAPPING[val]
                    error_line_str = source_lines[line - 1] if 0 < line <= len(source_lines) else ""
                    padding = " " * col_offset
                    underline = "^" * len(val)

                    print(f"\n\033[1;31mERROR[Syntax]\033[0m on Line {line}: Invalid Keyword")
                    print(f"Found '{val}', but Rubidium uses '{correct}'.")
                    print(f" \033[1;36m-->\033[0m line {line}")
                    print(f" \033[1;36m{line:3} |\033[0m {error_line_str}")
                    print(f"     | \033[1;31m{padding}{underline}\033[0m")
                    print(f"\nSuggestion:\n  Use '{correct}' instead of '{val}'.\n")
                    has_typo_errors = True

        if has_typo_errors:
            sys.exit(1)

        ast_tree = Parser(tokens).parse()

        debugger = Debugger(source_lines=source_lines, tokens=tokens,
                            source_dir=os.path.dirname(os.path.abspath(filepath)))

        print(f"\n{ANSI['BOLD']}Rubidium Debug Run{ANSI['RESET']}")
        print(f"{ANSI['DIM']}Running: {filepath}{ANSI['RESET']}\n")

        debugger.run(ast_tree)

        print()
        print(f"{ANSI['DIM']}{'─' * 44}{ANSI['RESET']}")
        if debugger.errors:
            n = len(debugger.errors)
            print(f"{ANSI['ERROR']}✖  Debug run found {n} error{'s' if n != 1 else ''}{ANSI['RESET']}")
        else:
            print(f"\033[1;32m✔  Debug run completed — no runtime errors\033[0m")
        print(f"{ANSI['DIM']}{'─' * 44}{ANSI['RESET']}\n")

    except SyntaxError as e:
        print(f"✖ Syntax Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✖ Parse Error: {e}")
        sys.exit(1)

    analyzer = Analyzer()
    analyzer.analyze(ast_tree, tokens)
    return analyzer.report(filepath, strict=strict)


def main():
    ap = argparse.ArgumentParser(
        description='Rubidium Static Analyzer & Debugger',
        usage='%(prog)s <file.rub> [--strict]'
    )

    ap.add_argument(
        'file',
        help='Rubidium source file (.rub)'
    )

    ap.add_argument(
        '--strict',
        action='store_true',
        help='Strict mode: warnings become errors'
    )

    args = ap.parse_args()

    ok = check_file(args.file, strict=args.strict)

    sys.exit(0 if ok else 1)

if __name__ == '__main__':
    main()