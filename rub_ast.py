class Number:
    def __init__(self, value):
        self.value = value

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
    def __init__(self, pairs, is_index: bool = False):
        self.pairs = pairs
        self.is_index = is_index  # True for [key: val] index literals, False for {key = val} dict literals

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
    def __init__(self, name, mutable, is_local, vtype, value):
        self.name = name
        self.mutable = mutable
        self.is_local = is_local
        self.vtype = vtype
        self.value = value

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
    def __init__(self, name, params, ret_type, body):
        self.name = name
        self.params = params
        self.ret_type = ret_type
        self.body = body

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
    def __init__(self, module_name, alias=None):
        self.module_name = module_name
        self.alias = alias          # e.g.  import math_tools as mt  →  alias="mt"

class Use:
    def __init__(self, module_name):
        self.module_name = module_name

class TypeCast:
    def __init__(self, expr, target_type):
        self.expr = expr
        self.target_type = target_type

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