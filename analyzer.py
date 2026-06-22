#!/usr/bin/env python3
"""
Rubidium Static Analyzer
Usage:  python analyzer.py check <file.rub> [--strict]
"""

import sys
import os
import argparse

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

from lexer import tokenize
from parser import Parser
import rub_ast as ast

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

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

HEAP_TYPES = {'list', 'index', 'dict'}

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

    # VarInfo keys: mutable, vtype, dropped, used, is_heap, line, drop_line,
    #               possibly_null

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
# Analyzer
# ──────────────────────────────────────────────────────────────────────────────

class Analyzer:

    def __init__(self):
        self.issues: list = []

        # Global symbol tables (populated before main walk)
        self.functions: dict  = {}   # name → {params, ret_type, used, line}
        self.classes:   dict  = {}   # name → {fields, methods, used, line}
        self.namespaces: set  = set()
        self.imports:    set  = set()

        # Thread / race analysis
        self.thread_fns:  set  = set()   # fn names launched via thread()
        self.global_muts: dict = {}      # global mut var_name → info

        # Memory accounting
        self.heap_var_names: list = []   # names of globally heap-allocated vars
        self.global_allocs:  int  = 0

        # Line-number map: ('fn'|'var'|'class', name) → line
        self._lmap: dict = {}

        # Collected leak vars (filled after analysis)
        self._leak_vars: list = []

    # ── Emit ──────────────────────────────────────────────────────────────────

    def _emit(self, severity: str, line, category: str,
              message: str, suggestion: str = ''):
        self.issues.append(Issue(severity, line, category, message, suggestion))

    # ── Line map ──────────────────────────────────────────────────────────────

    def _build_line_map(self, tokens: list):
        i = 0
        n = len(tokens)
        while i < n:
            kind, val, line = tokens[i][0], tokens[i][1], tokens[i][2]
            if kind == 'LET':
                j = i + 1
                if j < n and tokens[j][0] == 'MUT':
                    j += 1
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
            i += 1

    def _ln(self, kind: str, name: str):
        """Return the declared line for a symbol, or None."""
        return self._lmap.get((kind, name))

    # ── Type helpers ──────────────────────────────────────────────────────────

    def _infer(self, node, scope: Scope):
        """Return a type string for node, or None if unknown."""
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
            return 'dict'
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
        return isinstance(node, (ast.ListExpr, ast.DictExpr, ast.ClassInstantiate))

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
        # Numerics: widening allowed
        if expected in NUMERIC_TYPES and received in NUMERIC_TYPES:
            return True
        return False

    # ── Top-level pre-pass ────────────────────────────────────────────────────

    def _pre_pass(self, nodes: list):
        """Collect fn/class/namespace/thread symbols before the main walk."""
        for node in nodes:
            if isinstance(node, ast.FnDef):
                if node.name not in self.functions:
                    self.functions[node.name] = {
                        'params':   node.params,
                        'ret_type': node.ret_type,
                        'used':     False,
                        'line':     self._ln('fn', node.name),
                    }
                # Don't emit duplicate here — handled in main walk
            elif isinstance(node, ast.ClassDef):
                if node.name not in self.classes:
                    fields = {}
                    for f in node.fields:
                        fields[f.name] = {'mutable': f.mutable, 'vtype': f.vtype}
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

        # Second micro-pass: find thread(fn, id) calls to know which fns are threaded
        for node in nodes:
            self._find_thread_fns(node)

    def _find_thread_fns(self, node):
        """Recursively find all thread(fn, id) calls and record fn names."""
        if isinstance(node, ast.ThreadCall):
            fc = node.func_call
            if isinstance(fc, ast.FnCall) and isinstance(fc.name, str):
                self.thread_fns.add(fc.name)
            elif isinstance(fc, ast.Var):
                self.thread_fns.add(fc.name)
        # Recurse into bodies
        for attr in ('body', 'then_body', 'else_body', 'try_body', 'error_body'):
            body = getattr(node, attr, None)
            if body:
                for child in body:
                    self._find_thread_fns(child)

    # ── Main analysis entry ───────────────────────────────────────────────────

    def analyze(self, nodes: list, tokens: list):
        self._build_line_map(tokens)
        self._pre_pass(nodes)
        global_scope = Scope()
        # Walk top-level nodes
        for node in nodes:
            self._node(node, global_scope, in_loop=False)
        # Post analysis
        self._check_unused(global_scope)
        self._check_global_leaks(global_scope)

    # ── Node dispatcher ───────────────────────────────────────────────────────

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
            for s in node.try_body:
                self._node(s, scope, in_loop)
            for s in node.error_body:
                self._node(s, scope, in_loop)
        elif t is ast.Return:
            self._expr(node.value, scope)
        elif t is ast.Print:
            self._expr(node.value, scope)
        elif t is ast.Println:
            self._expr(node.value, scope)
        elif t is ast.FnCall:
            self._fn_call(node, scope)
        elif t is ast.MethodCall:
            self._expr(node.obj, scope)
            for a in node.args:
                self._expr(a, scope)
        elif t is ast.CollectionMethodCall:
            self._collection_method(node, scope)
        elif t is ast.Use:
            self.namespaces.add(node.module_name)
        elif t is ast.Import:
            self.imports.add(node.module_name)
        elif t is ast.FFILoad:
            self._ffi_load(node, scope)
        elif t is ast.FFIBind:
            pass  # trust caller
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
            # Fallback: treat as expression
            self._expr(node, scope)

    # ── Expression visitor ────────────────────────────────────────────────────

    def _expr(self, node, scope: Scope):
        if node is None:
            return
        t = type(node)

        if t is ast.Var:
            self._var_usage(node, scope)
        elif t is ast.FnCall:
            self._fn_call(node, scope)
        elif t is ast.MethodCall:
            self._expr(node.obj, scope)
            for a in node.args:
                self._expr(a, scope)
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
            for k, v in node.pairs:
                self._expr(k, scope)
                self._expr(v, scope)
        elif t is ast.InterpolatedStr:
            for part in node.parts:
                self._expr(part, scope)
        elif t is ast.TypeCast:
            self._expr(node.expr, scope)
        elif t is ast.FieldAccess:
            self._expr(node.obj, scope)
        elif t is ast.ClassInstantiate:
            if node.class_name not in self.classes:
                self._emit('ERROR', None, 'Unknown Class',
                           f"Unknown class: {node.class_name}")
            else:
                self.classes[node.class_name]['used'] = True
        elif t is ast.Input:
            if node.prompt:
                self._expr(node.prompt, scope)
        elif t is ast.FFILoad:
            self._ffi_load(node, scope)
        elif t is ast.ThreadRunning:
            pass
        # Literals: Number, Str, Bool, None_ → no action needed

    # ── VarDecl ───────────────────────────────────────────────────────────────

    def _var_decl(self, node: ast.VarDecl, scope: Scope):
        # Duplicate check (local scope only)
        if node.name in scope.vars:
            self._emit('ERROR', self._ln('var', node.name), 'Duplicate Symbol',
                       f"Variable '{node.name}' already exists.")
            self._expr(node.value, scope)
            return

        is_heap = self._is_heap_node(node.value)
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
        scope.declare(node.name, info)

        # Track global allocations
        if is_heap:
            self.global_allocs += 1

        # Track global mut vars for race-condition analysis
        if node.mutable and not node.is_local and scope.parent is None:
            self.global_muts[node.name] = info

        # Type check: declared type vs inferred type
        if node.vtype and node.value is not None and not possibly_null:
            inferred = self._infer(node.value, scope)
            if inferred and not self._types_compat(node.vtype, inferred):
                self._emit(
                    'ERROR', info['line'], 'Type Error',
                    f"Expected:\n{node.vtype}\n\nReceived:\n{inferred}",
                    f"let {node.name}: {node.vtype} = ..."
                )

        self._expr(node.value, scope)

    # ── Assignment ────────────────────────────────────────────────────────────

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
        self._expr(node.value, scope)

    # ── Field assignment ──────────────────────────────────────────────────────

    def _field_assign(self, node: ast.FieldAssign, scope: Scope):
        if isinstance(node.obj, ast.Var):
            info = scope.lookup(node.obj.name)
            if info:
                scope.mark_used(node.obj.name)
                if not info.get('mutable'):
                    self._emit(
                        'ERROR', info.get('line'), 'Mutability Violation',
                        f"Field '{node.field}' is immutable.",
                        f"Declare '{node.obj.name}' with 'mut' to modify fields."
                    )
                else:
                    # Check field-level mutability in the class definition
                    vtype = info.get('vtype')
                    if vtype and vtype in self.classes:
                        finfo = self.classes[vtype]['fields'].get(node.field)
                        if finfo and not finfo.get('mutable'):
                            self._emit(
                                'ERROR', info.get('line'), 'Mutability Violation',
                                f"Field '{node.field}' is immutable."
                            )
        self._expr(node.value, scope)

    # ── Function definition ───────────────────────────────────────────────────

    def _fn_def(self, node: ast.FnDef, parent_scope: Scope):
        # Duplicate
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
        for pname, ptype in (node.params or []):
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

        # Race condition: if this fn is launched in a thread, check global writes
        if node.name in self.thread_fns:
            self._scan_race(node.body, node.name)

        # Walk body, detect unreachable code after return
        found_return = False
        for stmt in node.body:
            if found_return:
                self._emit('WARNING', None, 'Unreachable Code',
                           f"Code after return in function '{node.name}'.")
                break
            self._node(stmt, fn_scope, in_loop=False)
            if isinstance(stmt, ast.Return):
                found_return = True
                # Type-check return value
                if node.ret_type and stmt.value is not None:
                    inferred = self._infer(stmt.value, fn_scope)
                    if inferred and not self._types_compat(node.ret_type, inferred):
                        self._emit(
                            'ERROR', self._ln('fn', node.name), 'Return Type Error',
                            f"Expected:\n{node.ret_type}\n\nReceived:\n{inferred}"
                        )

        # Unused locals & local heap leaks
        param_names = {p[0] for p in (node.params or [])}
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

    # ── Class definition ──────────────────────────────────────────────────────

    def _class_def(self, node: ast.ClassDef, scope: Scope):
        if node.name in self.classes and self.classes[node.name].get('_seen'):
            self._emit('ERROR', self._ln('class', node.name), 'Duplicate Symbol',
                       f"Class '{node.name}' already exists.")
        else:
            if node.name in self.classes:
                self.classes[node.name]['_seen'] = True

    # ── Drop ──────────────────────────────────────────────────────────────────

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
            # Approximate drop line from token scan
            scope.mark_dropped(node.name, info.get('line'))

    # ── Thread call ───────────────────────────────────────────────────────────

    def _thread_call(self, node: ast.ThreadCall, scope: Scope):
        self._expr(node.func_call, scope)
        self._expr(node.thread_id, scope)

    # ── While loop ────────────────────────────────────────────────────────────

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

    # ── For loop ──────────────────────────────────────────────────────────────

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

    # ── If ────────────────────────────────────────────────────────────────────

    def _if(self, node: ast.If, scope: Scope):
        if isinstance(node.cond, ast.Compare):
            self._null_compare(node.cond, scope)
        self._expr(node.cond, scope)
        for stmt in (node.then_body or []):
            self._node(stmt, scope)
        for stmt in (node.else_body or []):
            self._node(stmt, scope)

    # ── Function call ─────────────────────────────────────────────────────────

    def _fn_call(self, node: ast.FnCall, scope: Scope):
        name = node.name if isinstance(node.name, str) else None
        if name is None:
            for a in node.args:
                self._expr(a, scope)
            return

        # Namespace check: e.g. math.sin(x) → need 'use math'
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

        # Unknown function
        if name not in self.functions and name not in BUILTIN_FNS:
            self._emit('ERROR', None, 'Unknown Function',
                       f"Unknown function: {name}()")
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

        for a in node.args:
            self._expr(a, scope)

    # ── Variable usage ────────────────────────────────────────────────────────

    def _var_usage(self, node: ast.Var, scope: Scope):
        name = node.name
        if '.' in name:
            ns = name.split('.')[0]
            if ns not in self.namespaces and ns in KNOWN_MODULES:
                self._emit('ERROR', None, 'Module Not Enabled',
                           f"Module not enabled: {ns}", f"use {ns}")
            return

        info = scope.lookup(name)
        if info is None:
            if (name not in self.functions and name not in self.classes
                    and name not in BUILTIN_FNS):
                self._emit('ERROR', None, 'Unknown Variable',
                           f"Unknown variable: {name}")
            return

        scope.mark_used(name)

        if info.get('dropped'):
            self._emit(
                'ERROR', info.get('line'), 'Use After Drop',
                f"Variable '{name}' was dropped on line {info.get('drop_line', '?')}."
            )
        if info.get('possibly_null'):
            self._emit('WARNING', info.get('line'), 'Possible Null Usage',
                       f"Variable '{name}' may contain Null.")

    # ── Collection method call ────────────────────────────────────────────────

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

    # ── FFI load ──────────────────────────────────────────────────────────────

    def _ffi_load(self, node: ast.FFILoad, scope: Scope):
        if isinstance(node.path_expr, ast.Str):
            path = node.path_expr.value.strip('"')
            if not os.path.exists(path):
                self._emit('WARNING', None, 'Library Not Found',
                           f"Library not found: {path}")

    # ── Null arithmetic ───────────────────────────────────────────────────────

    def _null_arith(self, node, scope: Scope):
        # Only flag literal Null here; Var null-checks are handled in _var_usage
        if self._is_null_node(node):
            self._emit('WARNING', None, 'Possible Null Usage',
                       "Expression uses Null in arithmetic.",
                       "Null in arithmetic evaluates to 0 or False.")

    # ── Null comparison ───────────────────────────────────────────────────────

    def _null_compare(self, node: ast.Compare, scope: Scope):
        if self._is_null_node(node.left) or self._is_null_node(node.right):
            self._emit('INFO', None, 'Condition Always False',
                       "Condition always evaluates to False.",
                       f"Comparison with Null is always False.")

    # ── Race condition scan ───────────────────────────────────────────────────

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

    # ── Unused symbols ────────────────────────────────────────────────────────

    def _check_unused(self, global_scope: Scope):
        for fname, finfo in self.functions.items():
            if not finfo.get('used'):
                self._emit('INFO', finfo.get('line'), 'Unused Function',
                           f"Unused function: {fname}()")
        for vname, vinfo in global_scope.vars.items():
            if not vinfo.get('used'):
                self._emit('INFO', vinfo.get('line'), 'Unused Variable',
                           f"Unused variable: {vname}")

    # ── Global heap leak check ────────────────────────────────────────────────

    def _check_global_leaks(self, global_scope: Scope):
        leaks = []
        for vname, vinfo in global_scope.vars.items():
            if vinfo.get('is_heap') and not vinfo.get('dropped'):
                leaks.append(vname)
                self._emit(
                    'WARNING', vinfo.get('line'), 'Possible Memory Leak',
                    f"Variable '{vname}' was never dropped."
                )
        self._leak_vars = leaks

    # ── Report ────────────────────────────────────────────────────────────────

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

        # Memory summary
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
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def check_file(filepath: str, strict: bool = False) -> bool:
    if not os.path.exists(filepath):
        print(f"✖ Error: File not found: {filepath}")
        sys.exit(1)

    with open(filepath, 'r') as f:
        source = f.read()

    try:
        tokens   = tokenize(source)
        ast_tree = Parser(tokens).parse()
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
        description='Rubidium Static Analyzer',
        usage='%(prog)s check <file.rub> [--strict]'
    )
    ap.add_argument('command', choices=['check'],
                    help="'check' to analyze a source file")
    ap.add_argument('file',
                    help='Rubidium source file (.rub)')
    ap.add_argument('--strict', action='store_true',
                    help='Strict mode: warnings become errors')
    args = ap.parse_args()

    ok = check_file(args.file, strict=args.strict)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
