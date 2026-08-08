class Number:
    def __init__(self, value, raw=None):
        self.value = value
        # OPEN-5: original source text for a float literal, preserved
        # alongside the already-lossy Python `float(value)`. Only codegen
        # needs this — to emit a literal directly at fp128 precision (via
        # LLVM IR's decimal-constant parsing, which uses APFloat rather
        # than going through a double intermediate) when the literal has
        # more significant digits than a double can represent exactly.
        self.raw = raw

class Bool:
    def __init__(self, value):
        self.value = value

class None_:
    pass

class Str:
    def __init__(self, value):
        self.value = value

class InterpolatedStr:
    """Represents i"..." strings with {expr} interpolation.
    parts is a list of alternating Str and expression nodes."""
    def __init__(self, parts):
        self.parts = parts

class Var:
    def __init__(self, name):
        self.name = name

class ListExpr:
    def __init__(self, elements):
        self.elements = elements

class DictExpr:
    def __init__(self, pairs, is_index: bool = False, is_dictplus: bool = False):
        self.pairs = pairs
        self.is_index = is_index  # True for [key: val] index literals, False for {key = val} dict literals
        # FEATURE: dict+ — recursively-nestable dict where a value is either
        # a list (leaf, same as regular dict) or another {..} block (deeper
        # nesting). Same runtime RDict layout as dict, distinguished only by
        # a different magic number (see IS_DICT_MAGIC in the C runtime) —
        # this flag is what tells codegen to create it with that magic
        # number instead of a regular dict's.
        self.is_dictplus = is_dictplus

class BinOp:
    def __init__(self, left, op, right):
        self.left = left
        self.op = op
        self.right = right

class UnaryOp:
    def __init__(self, op, value):
        self.op = op
        self.value = value

class Compare:
    def __init__(self, left, op, right):
        self.left = left
        self.op = op
        self.right = right

class VarDecl:
    def __init__(self, name, mutable, is_local, vtype, value, element_type=None):
        self.name = name
        self.mutable = mutable
        self.is_local = is_local
        self.vtype = vtype
        self.value = value
        self.element_type = element_type
        self.element_type = element_type

class Assign:
    def __init__(self, name, value):
        self.name = name
        self.value = value

class FieldAssign:
    def __init__(self, obj, field, value):
        self.obj = obj
        self.field = field
        self.value = value

class Print:
    def __init__(self, value):
        self.value = value

class Println:
    def __init__(self, value):
        self.value = value

class If:
    def __init__(self, cond, then_body, else_body):
        self.cond = cond
        self.then_body = then_body
        self.else_body = else_body

class While:
    def __init__(self, cond, body):
        self.cond = cond
        self.body = body

class FnDef:
    def __init__(self, name, params, ret_type, body, is_callback=False):
        self.name = name
        self.params = params
        self.ret_type = ret_type
        self.body = body
        # syntax: FFI CALLBACKS — `fn callback name(...) { ... }`. Kept as a
        # flag on ordinary FnDef (rather than a separate AST node type) so
        # every existing FnDef-handling call site across the parser/codegen/
        # debugger/multi-file-import machinery keeps working unchanged; only
        # codegen's function-emission and bare-name-as-value resolution
        # need to actually branch on it.
        self.is_callback = is_callback

class FnCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args

class Return:
    def __init__(self, value):
        self.value = value

class Try:
    def __init__(self, try_body, error_body):
        self.try_body = try_body
        self.error_body = error_body

class Drop:
    def __init__(self, name):
        self.name = name

class ElementDrop:
    """items(1).drop() — remove an element/key from a collection and shift,
    per spec (unlike collection_set's index(0).set(...), this does NOT
    replace with Null; it removes the slot entirely)."""
    def __init__(self, access_node):
        self.access_node = access_node

class LinkArg:
    """link expr — pass-by-reference marker for the Link Rule (avoids a
    deep copy when passing a large local value to a function)."""
    def __init__(self, expr):
        self.expr = expr

class For:
    def __init__(self, var, start, end, body, iterable=None):
        self.var = var
        self.start = start
        self.end = end
        self.body = body
        self.iterable = iterable

class MethodCall:
    def __init__(self, obj, method, args):
        self.obj = obj
        self.method = method
        self.args = args

class FieldAccess:
    def __init__(self, obj, field):
        self.obj = obj
        self.field = field

class ClassDef:
    def __init__(self, name, fields, methods=None):
        self.name = name
        self.fields = fields
        self.methods = methods if methods else []

class ClassInstantiate:
    def __init__(self, class_name):
        self.class_name = class_name

# BUGFIX/FEATURE (bugs.log #9): runtime SY reflection. `let x: SY = <expr>`
# is now a real runtime string variable (see parser.py), so a name generated
# from it (e.g. inside a loop, via concatenation) can differ every iteration.
# DynResolve("x") means "look up the dynamic variable currently named by the
# runtime value of x" (via a runtime hash-map, see rub_dynvar_get/set in the
# C runtime) — used as the `name` of a FnCall so it composes for free with
# the existing chained-collection-access codegen (FnCall(<non-str-expr>, args)).
# DynVarDecl is the declaration-side equivalent: `let (x): dict = {}` creates
# a new dynamic-hash-map entry keyed by x's current runtime value.
class DynResolve:
    def __init__(self, holder_name):
        self.holder_name = holder_name

class DynVarDecl:
    def __init__(self, holder_name, mutable, is_local, vtype, value):
        self.holder_name = holder_name
        self.mutable = mutable
        self.is_local = is_local
        self.vtype = vtype
        self.value = value

class ThreadCall:
    def __init__(self, func_call, thread_id):
        self.func_call = func_call
        self.thread_id = thread_id

class ThreadWait:
    def __init__(self, thread_ids):
        self.thread_ids = thread_ids

class ThreadRunning:
    def __init__(self, thread_id):
        self.thread_id = thread_id

class Import:
    def __init__(self, module_name, alias=None, is_xeon_pkg=False, is_local=False):
        self.module_name = module_name
        self.alias = alias          # e.g.  import math_tools as mt  →  alias="mt"
        # `import local X` — this file gets its OWN private instance of X,
        # independent of every other importer's. A plain `import X` shares one
        # single instance across the whole program (see MODULES & IMPORTS).
        self.is_local = is_local
        self.is_xeon_pkg = is_xeon_pkg  # True for `xeon math_tools` (installed package,
                                         # resolved from ~/.xeon/packages/ instead of a relative path)

class Use:
    def __init__(self, module_name):
        self.module_name = module_name

class TypeCast:
    def __init__(self, expr, target_type):
        self.expr = expr
        self.target_type = target_type

class MathBlock:
    """A parenthesised math expression with a result-type annotation:
    `(expr): TYPE`. Every arithmetic operation inside is computed AT that
    type/precision (not just the final value cast) — e.g.
    `(10 as i32 / 3 as i32): f2048` narrows the operands to i32 then runs
    the division at f2048, giving 3.333..., not integer 3. See codegen's
    `_math_block_type` override in emit_binop."""
    def __init__(self, expr, vtype):
        self.expr = expr
        self.vtype = vtype

class Break:
    pass

class Continue:
    pass

class Input:
    def __init__(self, prompt=None):
        self.prompt = prompt

# OS module nodes
class OsStart:
    def __init__(self, id_expr):
        self.id_expr = id_expr

class OsRun:
    def __init__(self, id_expr, cmd_expr, input_expr=None, struct_args=None):
        self.id_expr = id_expr          # None if struct style
        self.cmd_expr = cmd_expr        # None if struct style
        self.input_expr = input_expr    # optional stdin string
        self.struct_args = struct_args  # dict of {cmd, args, input} for struct form

class OsDrop:
    def __init__(self, id_expr):
        self.id_expr = id_expr

# FFI nodes
class FFILoad:
    """let lib = FFI("path.so") — loads a shared library via dlopen"""
    def __init__(self, path_expr):
        self.path_expr = path_expr

class FFIBind:
    """fn lib symbol(params) -> ret as alias — binds a symbol from a loaded FFI handle"""
    def __init__(self, handle_name, symbol_name, params, ret_type, alias=None):
        self.handle_name = handle_name
        self.symbol_name = symbol_name
        self.params      = params
        self.ret_type    = ret_type
        self.alias       = alias    # Rubidium-side callable name; falls back to symbol_name


# File handle nodes
class FileOpen:
    def __init__(self, path_expr, var_name, body=None):
        self.path_expr = path_expr
        self.var_name = var_name
        self.body = body or []

class FileHandleMethod:
    def __init__(self, var_name, method, args):
        self.var_name = var_name
        self.method = method
        self.args = args

class FileExists:
    def __init__(self, path_expr):
        self.path_expr = path_expr

class FileDelete:
    def __init__(self, path_expr):
        self.path_expr = path_expr

class FileRename:
    def __init__(self, old_path, new_path):
        self.old_path = old_path
        self.new_path = new_path

class FileCopy:
    def __init__(self, src_path, dst_path):
        self.src_path = src_path
        self.dst_path = dst_path

class FileList:
    """file.list(path) — directory listing (like `ls`); directories get a
    trailing '/' appended to their name."""
    def __init__(self, path_expr):
        self.path_expr = path_expr

class FileNew:
    def __init__(self, path_expr, body):
        self.path_expr = path_expr
        self.body = body

# Collection method call nodes
class CollectionMethodCall:
    def __init__(self, obj, method, args):
        self.obj = obj
        self.method = method
        self.args = args

class FileHandleStmt:
    def __init__(self, var_name, method, args):
        self.var_name = var_name
        self.method = method
        self.args = args

class NoOp:
    """A statement with no runtime effect — used for SY declarations
    (bugs.log #2), which are pure compile-time name substitutions."""
    def __init__(self):
        pass

class Raise:
    """raise <expr> — raises a runtime error with the given message.
    If inside a try block, control jumps to that try's error handler
    (with `error` set to the message); otherwise it's an uncaught
    runtime error (program exits after printing the message)."""
    def __init__(self, message):
        self.message = message