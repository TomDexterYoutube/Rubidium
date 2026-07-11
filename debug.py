import sys
import os
import argparse
import copy
import shutil

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from lexer import tokenize
from parser import Parser
import rub_ast as ast

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

    def __init__(self, source_lines=None, tokens=None):
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
            value = self._deep_copy_value(self.evaluate(node.value))
            # Per spec: 'let' without 'local' enters the global memory pool.
            target = self.scope if node.is_local else self._root_scope
            target.declare(node.name, {
                "value":   value,
                "type":    node.vtype or self.rub_type(value),
                "mutable": node.mutable,
                "dropped": False,
                "line":    self.line,
            })

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
            info["value"] = self._deep_copy_value(self.evaluate(node.value))
            info["type"]  = self.rub_type(info["value"])

        # ── Field Assignment ──────────────────────────────────────────────────
        elif isinstance(node, ast.FieldAssign):
            obj = self.evaluate(node.obj) if hasattr(node, 'obj') else None
            if isinstance(obj, dict):
                obj[node.field] = self.evaluate(node.value)

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
            stub = {"value": {"__module__": mod}, "type": "module",
                    "mutable": False, "dropped": False}
            self.scope.declare(mod, stub)
            # Also register the alias (import tokeniser as tk → declare 'tk' too)
            alias = getattr(node, 'alias', None)
            if alias:
                self.line = self._lmap.get(('import', alias), self.line)
                self.scope.declare(alias, {"value": {"__module__": mod}, "type": "module",
                                           "mutable": False, "dropped": False})

        # ── Stubs: OS / FFI / Threads (still not simulated in debug mode) ────
        elif isinstance(node, (
            ast.FFILoad, ast.FFIBind,
            ast.ThreadCall, ast.ThreadWait, ast.ThreadRunning,
            ast.OsStart, ast.OsRun, ast.OsDrop,
        )):
            pass   # not executed in debug mode

        # ── File I/O — real file operations, matching the compiled runtime ──
        elif isinstance(node, ast.FileOpen):
            path = self.evaluate(node.path_expr)
            if not os.path.exists(str(path)):
                raise Exception("file not found")
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
            return node.value

        if isinstance(node, ast.Bool):
            v = str(node.value).lower()
            return True if v == "true" else (False if v == "false" else None)

        if isinstance(node, ast.None_):
            return None

        if isinstance(node, ast.InterpolatedStr):
            parts = []
            for part in node.parts:
                val = self.evaluate(part)
                parts.append(str(val) if val is not None else "")
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
            try:
                if node.op == "+":
                    if isinstance(left, str) or isinstance(right, str):
                        return str(left or "") + str(right or "")
                    return left + right
                if node.op == "-":   return left - right
                if node.op == "*":   return left * right
                if node.op == "/":
                    if right == 0: self.error("Division by zero", highlight="/"); return None
                    if isinstance(left, int) and isinstance(right, int):
                        # Truncate toward zero (matches the compiler's sdiv),
                        # not Python's floor division.
                        q = left / right
                        return int(q) if q >= 0 else -int(-q)
                    return left / right
                if node.op == "*/":
                    # n */ value -> value's n-th root (matches the unary
                    # `*/value` sqrt case, generalized to any degree).
                    if left == 0: self.error("Division by zero", highlight="*/"); return None
                    return right ** (1.0 / left)
                if node.op == "**":  return left ** right
                if node.op == "%":   return left % right
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
                if t in ("i32","i64","i128"): return int(val)
                if t in ("f32","f64","f128"): return float(val)
                if t == "str":   return str(val)
                if t == "bool":  return bool(val)
            except Exception as e:
                self.error(f"Type cast to '{t}' failed: {e}")
            return val

        if isinstance(node, ast.ListExpr):
            return [self.evaluate(x) for x in node.elements]

        if isinstance(node, ast.DictExpr):
            result = {}
            for k, v in node.pairs:
                result[self.evaluate(k)] = self.evaluate(v)
            return result

        if isinstance(node, ast.FieldAccess):
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

        # Built-ins
        if fname == "print":
            if args: print(args[0])
            return None
        if fname == "println":
            if args: print(f"\r{args[0]}", end="")
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
                        col = col.get(arg)
                    else:
                        return None
                return col

        return None


    # ── MethodCall evaluator ──────────────────────────────────────────────────

    def _eval_method_call(self, node):
        method = node.method
        args   = [self.evaluate(a) for a in node.args]

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
        if method == "set" and isinstance(node.obj, ast.FnCall):
            fname = node.obj.name if isinstance(node.obj.name, str) else None
            if fname:
                info = self.scope.lookup(fname)
                if info is not None:
                    col      = info["value"]
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
            fname = node.obj.name if isinstance(node.obj.name, str) else None
            if fname:
                info = self.scope.lookup(fname)
                if info is not None and isinstance(info.get("value"), dict):
                    idx_args = [self.evaluate(a) for a in node.obj.args]
                    col = info["value"]
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
                        for fname, fval in obj.items():
                            if fname != "__class__":
                                fn_scope.declare(fname, {
                                    "value": fval, "type": self.rub_type(fval),
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
                        return result
            return obj.get(method)   # field access fallback

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
            idx = int(arg_vals[0]) if arg_vals else 1
            with open(path, "r") as f: lines = f.readlines()
            return lines[idx-1].rstrip("\n") if 0 < idx <= len(lines) else ""
        if method == "writeln":
            if len(arg_vals) < 2: return None
            line_num, data = int(arg_vals[0]), str(arg_vals[1])
            lines = []
            if os.path.exists(path):
                with open(path, "r") as f: lines = f.readlines()
            while len(lines) < line_num: lines.append("\n")
            lines[line_num - 1] = data + "\n"
            with open(path, "w") as f: f.writelines(lines)
            return None
        return None

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
        if isinstance(node, ast.FFILoad):
            return 'i64'
        if isinstance(node, ast.FnCall):
            fname = node.name if isinstance(node.name, str) else None
            if fname and fname in self.functions:
                return self.functions[fname].get('ret_type')
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

    def analyze(self, nodes: list, tokens: list):
        self._build_line_map(tokens)
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
                        self._emit('ERROR', ln, 'Missing Return Statement',
                                   f"Function '{node.name}' declares return type "
                                   f"'{node.ret_type}' but may not return on all paths.",
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
            self._expr(node.path_expr, scope)
            for s in node.body:
                self._node(s, scope, in_loop)
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
            if not vinfo.get('used'):
                self._emit('INFO', vinfo.get('line'), 'Unused Variable',
                           f"Unused variable: {vname}")
            if vinfo.get('is_heap') and not vinfo.get('dropped'):
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
            method_scope.declare('self', {
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

        for fname, finfo in self.classes[node.name]['fields'].items():
            if not finfo.get('used'):
                self._emit(
                    'INFO',
                    self._ln('class', node.name),
                    'Unused Field',
                    f"Unused field: {node.name}.{fname}"
            )

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
        loop_scope.declare(node.var, {
            'mutable': True, 'vtype': 'i32', 'dropped': False, 'used': True,
            'is_heap': False, 'line': None, 'drop_line': None, 'possibly_null': False,
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
        if name in ALL_TYPES:
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
            if not os.path.exists(path):
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
            # One side is Null, other is a concrete non-null value
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
            if not vinfo.get('used'):
                self._emit('INFO', vinfo.get('line'), 'Unused Variable',
                           f"Unused variable: {vname}")
        # Unused class definitions
        for cname, cinfo in self.classes.items():
            if not cinfo.get('used'):
                self._emit('INFO', cinfo.get('line'), 'Unused Class',
                           f"Class '{cname}' is defined but never instantiated.",
                           f"Instantiate it somewhere or remove the definition.")

    def _check_global_leaks(self, global_scope: Scope):
        leaks = []

        for vname, vinfo in global_scope.vars.items():

            # Only warn heap allocations
            if vinfo.get('is_heap') and not vinfo.get('dropped'):

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
    for i, tok in enumerate(tokens):
        if tok[0] == 'FN':
            j = i + 1
            if j < n and tokens[j][0] == 'LPAREN':
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

        debugger = Debugger(source_lines=source_lines, tokens=tokens)

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