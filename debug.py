import sys
import os
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

class Debugger:

    def __init__(self):
        self.scope = Scope()
        self.line = 0
        self.errors = []
        self.output = []


    def run(self, nodes):

        for node in nodes:
            self.execute(node)

        return len(self.errors) == 0



    def execute(self, node):

        if node is None:
            return

        self.line = getattr(node, "line", "?")

        # -----------------------------
        # Variable Declaration
        # -----------------------------

        if isinstance(node, ast.VarDecl):

            value = self.evaluate(node.value)

            self.scope.declare(
                node.name,
                {
                    "value": value,
                    "type": self.rub_type(value),
                    "dropped": False,
                    "line": self.line
                }
            )


        # -----------------------------
        # Assignment
        # -----------------------------

        elif isinstance(node, ast.Assign):

            info = self.scope.lookup(node.name)

            if info is None:
                self.error(
                    f"Unknown variable '{node.name}'"
                )
                return


            if info.get("dropped"):
                self.error(
                    f"Variable '{node.name}' was dropped"
                )
                return


            info["value"] = self.evaluate(node.value)
            info["type"] = self.rub_type(info["value"])



        # -----------------------------
        # Drop
        # -----------------------------

        elif isinstance(node, ast.Drop):

            info = self.scope.lookup(node.name)

            if info is None:

                self.error(
                    f"Cannot drop unknown variable '{node.name}'"
                )

            elif info.get("dropped"):

                self.error(
                    f"Variable '{node.name}' already dropped"
                )

            else:

                info["dropped"] = True
                info["value"] = None



        # -----------------------------
        # Print
        # -----------------------------

        elif isinstance(node, ast.Print):

            value = self.evaluate(node.value)

            print(value)

            self.output.append(value)



        elif isinstance(node, ast.Println):

            value = self.evaluate(node.value)

            print(value)

            self.output.append(value)



        # -----------------------------
        # If
        # -----------------------------

        elif isinstance(node, ast.If):

            condition = self.evaluate(node.cond)


            if condition:

                for stmt in node.then_body:
                    self.execute(stmt)

            else:

                for stmt in (node.else_body or []):
                    self.execute(stmt)



        # -----------------------------
        # While
        # -----------------------------

        elif isinstance(node, ast.While):

            count = 0

            while self.evaluate(node.cond):

                for stmt in node.body:
                    self.execute(stmt)


                count += 1


                # emergency brake
                # because infinite loops are
                # the compiler equivalent of a toddler with scissors

                if count > 100000:

                    self.error(
                        "Possible infinite loop"
                    )

                    break



        # -----------------------------
        # Function Call
        # -----------------------------

        elif isinstance(node, ast.FnCall):

            for arg in node.args:
                self.evaluate(arg)



        else:

            # fallback expression
            self.evaluate(node)




    def evaluate(self,node):

        if node is None:
            return None



        # Numbers

        if isinstance(node, ast.Number):

            return node.value



        # Strings

        if isinstance(node, ast.Str):

            return node.value



        # Bool

        if isinstance(node, ast.Bool):

            value = str(node.value).lower()

            if value == "true":
                return True

            if value == "false":
                return False

            if value in ("null","none"):
                return None

            return value



        # Variables

        if isinstance(node, ast.Var):

            info = self.scope.lookup(node.name)


            if info is None:

                self.error(
                    f"Variable '{node.name}' does not exist"
                )

                return None



            if info.get("dropped"):

                self.error(
                    f"Variable '{node.name}' used after drop"
                )

                return None



            return info["value"]




        # Binary Operations

        if isinstance(node, ast.BinOp):

            left = self.evaluate(node.left)
            right = self.evaluate(node.right)


            try:

                if node.op == "+":
                    return left + right

                if node.op == "-":
                    return left - right

                if node.op == "*":
                    return left * right

                if node.op == "/":
                    return left / right


            except Exception as e:

                self.error(
                    f"Operation failed: {e}"
                )

                return None




        # Comparisons

        if isinstance(node, ast.Compare):

            left = self.evaluate(node.left)
            right = self.evaluate(node.right)


            try:

                if node.op == "==":
                    return left == right

                if node.op == "!=":
                    return left != right

                if node.op == ">":
                    return left > right

                if node.op == "<":
                    return left < right


            except Exception as e:

                self.error(
                    f"Comparison failed: {e}"
                )

                return False



        # Lists

        if isinstance(node, ast.ListExpr):

            return [
                self.evaluate(x)
                for x in node.elements
            ]



        return None




    def rub_type(self,value):

        if isinstance(value,bool):
            return "bool"

        if isinstance(value,int):
            return "i32"

        if isinstance(value,float):
            return "f64"

        if isinstance(value,str):
            return "str"

        if isinstance(value,list):
            return "list"

        if value is None:
            return "Null"


        return "Any"




    def error(self,msg):

        issue = {
            "line": self.line,
            "message": msg
        }

        self.errors.append(issue)


        print(
            f"\033[1;31mDEBUG ERROR\033[0m "
            f"line {self.line}: {msg}"
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
        if expected in NUMERIC_TYPES and received in NUMERIC_TYPES:
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

    def analyze(self, nodes: list, tokens: list):
        self._build_line_map(tokens)
        self._pre_pass(nodes)
        global_scope = Scope()
        for node in nodes:
            self._node(node, global_scope, in_loop=False)
        self._check_unused(global_scope)
        self._check_global_leaks(global_scope)

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
            for s in node.try_body:
                self._node(s, try_scope, in_loop)
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

        if is_heap:
            self.global_allocs += 1

        if node.mutable and not node.is_local and scope.parent is None:
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

        if node.name in self.thread_fns:
            self._scan_race(node.body, node.name)

        found_return = False
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

    def _class_def(self, node: ast.ClassDef, scope: Scope):
        if node.name in self.classes and self.classes[node.name].get('_seen'):
            self._emit('ERROR', self._ln('class', node.name), 'Duplicate Symbol',
                       f"Class '{node.name}' already exists.")
        else:
            if node.name in self.classes:
                self.classes[node.name]['_seen'] = True

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
            for pname, ptype in (method.params or []):
                method_scope.declare(pname, {
                    'mutable': True, 'vtype': ptype, 'dropped': False,
                    'used': True, 'is_heap': ptype in HEAP_TYPES if ptype else False,
                    'line': None, 'drop_line': None, 'possibly_null': False,
                })
            for stmt in (method.body or []):
                self._node(stmt, method_scope, in_loop=False)

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

        for a in node.args:
            self._expr(a, scope)

    def _var_usage(self, node: ast.Var, scope: Scope):
        name = node.name
        
        # Intercept native types passed as configuration/function args
        if name in ALL_TYPES:
            return

        # Allow module/namespace names
        if name in self.namespaces or name in KNOWN_MODULES:
            return

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
                known = (list(self.functions) + list(self.classes) +
                         list(BUILTIN_FNS) + list(scope.vars.keys()))
                suggestion = _closest_name(name, known)
                msg = f"Unknown variable: {name}"
                if suggestion:
                    msg += f"\n\nDid you mean: {suggestion}?"
                self._emit('ERROR', None, 'Unknown Variable', msg)
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
        if self._is_null_node(node.left) or self._is_null_node(node.right):
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

        debugger = Debugger()
        debugger.run(ast_tree)
        
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