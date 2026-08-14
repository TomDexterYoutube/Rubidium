import sys
import os
import re
import argparse

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
    # RIG report: clear() (syntax's PRINT & INPUT section — wipes the
    # terminal) is implemented for real in codegen.py but was never added
    # here, so every legitimate call was reported as an unknown function.
    'clear',
}

NUMERIC_TYPES = {
    'i32', 'i64', 'i128', 'i256', 'i512', 'i1024', 'i2048',
    'f32', 'f64', 'f128', 'f256', 'f512', 'f1024', 'f2048',
}

HEAP_TYPES = {'list', 'index', 'dict', 'str'}

# syntax: FFI C TYPES — mirror codegen.py's identical constants, kept in
# sync so the analyzer flags the same violations the real compiler will,
# instead of reporting "COMPILATION ALLOWED" for FFI bindings that actually
# fail to compile.
FFI_ABI_ILLEGAL_BARE_TYPES = {"list", "index", "dict", "dict+"}
# index/dict/dict+ have no defined raw-ABI layout for FFI.type(...) to
# convert them into (unlike `list` — see codegen.py's identical constant for
# why) — not implemented, explicit rejection instead of a silent fallback.
FFI_TYPE_UNSUPPORTED_COLLECTIONS = {"index", "dict", "dict+"}

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


class Analyzer:

    def __init__(self):
        self.issues: list = []
        self.functions: dict  = {}
        self.classes:   dict  = {}
        self.namespaces: set  = set()
        self.imports:    set  = set()
        # RIG report: name of the class whose method body is currently
        # being walked (None outside any class), so a bare call to a
        # SIBLING method can be told apart from a genuinely unknown
        # function — see _fn_call's sibling-method-call check.
        self.cur_class = None
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
            # OPEN-C follow-up: distinct tags for int vs float — Python's own
            # `1 == 1.0` would otherwise make this static check (the
            # interpreter itself is fixed via RKey/_rtype_eq above) treat an
            # i32 key `1` and a float key `1.0` as the same literal key, the
            # same way 'bool' already keeps `1` and `True` apart below. Stays
            # a 2-tuple, like every other branch here — callers display
            # lit[1] as the raw key value (e.g. f"{lit[1]!r}"), which a wider
            # tuple would have broken.
            tag = 'num_float' if isinstance(node.value, float) else 'num_int'
            return (tag, node.value)
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

    def _is_ffi_type_call(self, t):
        """True if t LOOKS LIKE an attempted `FFI.type(...)` call — valid or
        not. Mirrors codegen.py's identical helper."""
        return isinstance(t, ast.MethodCall) and t.method == "type" \
            and isinstance(t.obj, ast.Var) and t.obj.name == "FFI"

    def _c_type_name(self, t):
        """If t is an `FFI.type(name)` marker (an FFI-bind parameter type
        parsed as MethodCall(Var("FFI"), "type", [Var(name)]) — the argument
        is a bare Rubidium type keyword, e.g. `list`), return its name
        string, else None (including when it LOOKS like an attempted
        FFI.type(...) call but is malformed — see _is_ffi_type_call).
        Mirrors codegen.py's identical helper."""
        if self._is_ffi_type_call(t) and t.args and isinstance(t.args[0], ast.Var):
            return t.args[0].name
        return None

    def _types_compat(self, expected: str, received: str) -> bool:
        # syntax: FFI C TYPES — FFI.type(list) accepts an ordinary Rubidium
        # list (converted to a raw C buffer at the call site by codegen.py);
        # FFI.type(X) for any other X is just X itself, same compatibility
        # rules as writing X plainly. Neither is a plain string type name,
        # so the checks below (which assume both sides are strings) never
        # apply to the marker itself.
        c_type = self._c_type_name(expected)
        if c_type is not None:
            if c_type == "list":
                return received == "list"
            return self._types_compat(c_type, received)
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

    def _check_ffi_param_types(self, fn_display_name, params):
        """Analyzer-side mirror of codegen.py's identically-named method —
        same ABI-illegality rule, reported as an Issue instead of raised."""
        for pname, ptype in params:
            c_type_name = self._c_type_name(ptype)
            if c_type_name is not None:
                # FFI.type(...) only actually CONVERTS `list` — everything
                # else it accepts is a passthrough of an already-ABI-safe
                # scalar. A class instance wrapped in it is still illegal.
                if c_type_name in self.classes:
                    self._emit(
                        'ERROR', None, 'FFI Type Error',
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type({c_type_name})', but '{c_type_name}' is "
                        f"a class instance with no defined raw-ABI layout — "
                        f"FFI.type(...) converts a list, not a class instance."
                    )
                if c_type_name in FFI_TYPE_UNSUPPORTED_COLLECTIONS:
                    self._emit(
                        'ERROR', None, 'FFI Type Error',
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type({c_type_name})' — {c_type_name} isn't "
                        f"supported by FFI.type(...); only list is convertible today."
                    )
                # syntax: void means "no value at all" — meaningful only as
                # an FFI binding's RETURN type. A parameter always receives
                # a real argument on every call, so "a parameter of no
                # value" has no meaning here either, same as in C itself.
                if c_type_name == "void":
                    self._emit(
                        'ERROR', None, 'FFI Type Error',
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type(void)' — void isn't a valid parameter "
                        f"type (only a return type). Remove this parameter if the "
                        f"foreign function doesn't take it, or declare it 'Any' and "
                        f"pass Null if it expects a real argument that just carries "
                        f"no value."
                    )
                continue
            if self._is_ffi_type_call(ptype):
                self._emit(
                    'ERROR', None, 'FFI Type Error',
                    f"FFI binding '{fn_display_name}': parameter '{pname}' has a "
                    f"malformed FFI.type(...) — it takes exactly one bare type "
                    f"keyword argument (e.g. FFI.type(list), FFI.type(i32)), not "
                    f"a string or a missing/extra argument."
                )
                continue
            if ptype in FFI_ABI_ILLEGAL_BARE_TYPES or ptype in self.classes:
                self._emit(
                    'ERROR', None, 'FFI Type Error',
                    f"FFI binding '{fn_display_name}': parameter '{pname}' "
                    f"has type '{ptype}', which can't cross an FFI call directly "
                    f"— there's no defined raw-ABI layout for a Rubidium "
                    f"{'collection' if ptype in FFI_ABI_ILLEGAL_BARE_TYPES else 'class instance'}.",
                    f'Use a primitive/pointer type, or FFI.type(list) for a list.'
                )

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
                # syntax: FFI C TYPES — flag the same ABI-illegal parameter
                # types codegen.py's compiler will reject, so this doesn't
                # report "COMPILATION ALLOWED" for code that fails to
                # actually compile.
                self._check_ffi_param_types(callable_name or node.symbol_name, node.params)

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
            saved_cur_class, self.cur_class = self.cur_class, node.name
            for stmt in (method.body or []):
                self._node(stmt, method_scope, in_loop=False)
            self.cur_class = saved_cur_class
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

        # RIG report: a bare call to a SIBLING method of the class
        # currently being analyzed (`heal(40)` from inside another
        # method, no `self` — a documented, working call shape, see
        # syntax's own CLASSES section and the real compiler's identical
        # handling in codegen.py) was only ever checked against the
        # top-level `self.functions` dict below, never against the
        # current class's own methods — so every legitimate sibling call
        # was reported as an unknown function.
        if self.cur_class and name in self.classes.get(self.cur_class, {}).get('methods', {}):
            method = self.classes[self.cur_class]['methods'][name]
            params = method.params or []
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
        elif kind in ('IF', 'WHILE'):
            # Scan forward past the condition (depth inside parens handled naturally),
            # then check if LBRACE follows. Not gated on `depth == 0` — real
            # if/while statements are almost always nested inside a function
            # body (depth >= 1), so requiring top-level depth here meant this
            # check silently never fired for ordinary code.
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
                elif kk == 'OP' and tokens[j][1] == '=' and cond_depth == 0:
                    # '=' is assignment, not a valid comparison operator — the
                    # expression parser simply stops before it, which is what
                    # lets `if x = 0 { ... }` silently misparse. Report every
                    # occurrence (don't break — keep scanning the same
                    # condition so a later missing '{' is still caught too).
                    kw = tokens[i][1]
                    cond_line = tokens[j][2]
                    emit(cond_line, 'Assignment In Condition',
                         f"'=' is assignment, not comparison, inside this '{kw}' condition\n"
                         f"  {ANSI['DIM']}→  {src(cond_line)}{ANSI['RESET']}",
                         "Use '==' to compare values, e.g. 'if x == 0 { ... }'")
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
                # Stop here rather than also handing the same broken tokens to
                # the parser: the parser now raises on the FIRST structural
                # problem it hits and exits, which would bury the full list
                # above under one more, differently-formatted single-line
                # error instead of leaving every issue visible together.
                sys.exit(1)

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

        def _tok_kind(t):
            return getattr(t, 'kind', t[0] if isinstance(t, (tuple, list)) else '')

        # A word like 'var' is only a mistyped keyword when it's actually
        # sitting in a keyword slot (`var x = 5`). The same word is a
        # completely legal identifier elsewhere — a parameter name
        # (`fn f(var: i32)`), a field/method access (`obj.var`), an alias
        # (`as var`), the bound name itself (`fn var(...)`), or a plain
        # variable declared with the real `let`/`mut` keyword already
        # correctly present (`let var = 5` — the token right after `let`/
        # `mut` is always the name being declared, never another keyword) —
        # and used to be flagged as a hard error there too, blocking valid
        # code (e.g. an FFI-bound parameter literally named `var`). Skip the
        # flag whenever neighboring tokens show it's being used as a name,
        # not a keyword.
        _NAME_SLOT_PREV = {'LPAREN', 'COMMA', 'DOT', 'AS', 'FN', 'LET', 'MUT'}
        _NAME_SLOT_NEXT = {'COLON', 'RPAREN', 'COMMA', 'DOT'}

        has_typo_errors = False
        for idx, tok in enumerate(tokens):
            kind = _tok_kind(tok)
            val = getattr(tok, 'value', tok[1] if isinstance(tok, (tuple, list)) else '')
            line = getattr(tok, 'line', tok[2] if isinstance(tok, (tuple, list)) else 0)
            col_offset = getattr(tok, 'col', 0)

            if kind == 'IDENT':
                if val.isupper():
                    continue

                if val in KEYWORD_MAPPING:
                    prev_kind = _tok_kind(tokens[idx - 1]) if idx > 0 else None
                    next_kind = _tok_kind(tokens[idx + 1]) if idx + 1 < len(tokens) else None
                    if prev_kind in _NAME_SLOT_PREV or next_kind in _NAME_SLOT_NEXT:
                        continue
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