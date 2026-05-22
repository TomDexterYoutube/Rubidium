class Number:
    def __init__(self, value):
        self.value = value

class Bool:
    def __init__(self, value):
        self.value = value

class Str:
    def __init__(self, value):
        self.value = value

class Var:
    def __init__(self, name):
        self.name = name

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
    def __init__(self, name, mutable, vtype, value):
        self.name = name
        self.mutable = mutable
        self.vtype = vtype
        self.value = value

class Assign:
    def __init__(self, name, value):
        self.name = name
        self.value = value

class FieldAssign:
    """obj.field = value"""
    def __init__(self, obj, field, value):
        self.obj = obj
        self.field = field
        self.value = value

class Print:
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
    """x.drop() — marks variable as dropped (no-op at runtime, type-level)"""
    def __init__(self, name):
        self.name = name

class For:
    def __init__(self, var, start, end, body):
        self.var = var
        self.start = start
        self.end = end
        self.body = body

class MethodCall:
    def __init__(self, obj, method, args):
        self.obj = obj
        self.method = method
        self.args = args

class FieldAccess:
    """obj.field  (as an expression — not a method call)"""
    def __init__(self, obj, field):
        self.obj = obj
        self.field = field

class ClassDef:
    """class Foo() { let mut x i32 = 3 }"""
    def __init__(self, name, fields):
        # fields: list of VarDecl
        self.name = name
        self.fields = fields

class ClassInstantiate:
    """let stuff = my_class()"""
    def __init__(self, class_name):
        self.class_name = class_name

class ThreadCall:
    def __init__(self, func_call, thread_id):
        self.func_call = func_call
        self.thread_id = thread_id

class ThreadWait:
    def __init__(self, thread_ids):
        self.thread_ids = thread_ids

class Import:
    def __init__(self, module_name):
        self.module_name = module_name

class Use:
    def __init__(self, module_name):
        self.module_name = module_name
