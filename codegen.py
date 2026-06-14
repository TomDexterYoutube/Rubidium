from rub_ast import *

extern_decls = '''
declare i32 @pthread_create(i64*, i64*, i8* (i8*)*, i8*)
declare i32 @pthread_join(i64, i8**)
declare i32 @pthread_tryjoin_np(i64, i8**)
declare i1 @_thread_is_running(i64)
declare double @strtod(i8*, i8**)
declare double @_rubidium_pow(double, double)
declare double @sin(double)
declare double @cos(double)
declare double @tan(double)
declare double @sqrt(double)
declare double @log(double)
declare double @log10(double)
declare double @exp(double)
declare double @fabs(double)
declare double @floor(double)
declare double @ceil(double)
declare double @round(double)
%Box = type opaque
declare %Box* @box_i(i64)
declare %Box* @box_f(double)
declare %Box* @box_s(i8*)
declare %Box* @box_p(i8*)
declare i64 @unbox_i(%Box*)
declare double @unbox_f(%Box*)
declare i8* @unbox_s(%Box*)
declare i8* @unbox_p(%Box*)
declare %Box* @make_list()
declare void @list_append(%Box*, %Box*)
declare void @list_swap(%Box*, i32, i32)
declare %Box* @make_dict()
declare void @dict_set(%Box*, %Box*, %Box*)
declare %Box* @collection_get(%Box*, %Box*)
declare void @collection_set(%Box*, %Box*, %Box*)
declare void @print_boxed(%Box*)
declare i8* @box_to_cstr(%Box*)
declare i32 @collection_len(%Box*)
declare %Box* @collection_get_at(%Box*, i32)
declare void @box_drop(%Box*)
declare %Box* @box_copy(%Box*)
declare i1 @collection_has(%Box*, %Box*)
declare i32 @_rub_str_hash(i8*)
declare i64 @time(i64*)
declare i32 @rand()
declare void @srand(i32)
declare i32 @usleep(i32)
declare i32 @sleep(i32)
declare void @time_timer_start(i64, double)
declare void @time_timer_pause(i64)
declare void @time_timer_stop(i64)
declare double @time_timer_read(i64)
declare void @_thread_smart_wait(i64)
declare i64 @file_open(i64, i8*, i32)
declare void @file_close(i64)
declare void @file_append_all(i64, i8*)
declare void @file_write_all(i64, i8*)
declare i8* @file_read_all(i64)
declare i8* @file_readln(i64, i64)
declare void @file_writeln(i64, i64, i8*)
declare i32 @file_exists(i8*)
declare i32 @file_delete(i8*)
declare i32 @file_rename_file(i8*, i8*)
declare i32 @file_copy_file(i8*, i8*)
'''


class RubidiumTypeError(Exception): pass
class RubidiumNameError(Exception): pass

class CodeGen:
    def __init__(self):
        self.fn_lines    = []
        self.global_decls = []
        self.str_count   = 0
        self.tmp_count   = 0
        self.label_count = 0
        self.global_vars  = {}
        self.local_vars_stack = [[]]  # Stack of scopes, each scope is a dict of variable names to types
        self.mutable_vars = set()
        self.dropped_vars = set()
        self.class_defs   = {}
        self.instances    = {}
        self.cur_fn       = None
        self.functions    = {}
        self.loop_end_stack = []
        self.loop_cond_stack = []  # continue target labels
        self.cur_class    = None
        self._alloca_emitted = set()  # local var names that already have an alloca in current fn
        self._try_error_label = None  # set while inside a try body for div-by-zero guards
        self._pending_trampolines = []  # trampoline fn_lines to emit after current function
        self._file_slot_counter = 0     # unique slot number for each open() block
        self._file_handle_vars = {}     # var_name -> slot_int while inside an open() block

    def is_class_field(self, name):
        if not self.cur_class: return False
        cls = self.class_defs.get(self.cur_class)
        if not cls: return False
        for f in cls.fields:
            if f.name == name: return True
        return False

    def _emit_input_line_helper(self):
        self.fn_lines += [
            "define i8* @_rubidium_input_line() {",
            "  %buf = call i8* @malloc(i64 1024)",
            "  %stdin = load i8*, i8** @_stdin_ptr", # Changed from @.stdin_ptr
            "  %res = call i8* @fgets(i8* %buf, i32 1024, i8* %stdin)",
            "  %len = call i64 @strlen(i8* %buf)",
            "  %is_empty = icmp eq i64 %len, 0",
            "  br i1 %is_empty, label %done, label %check_nl",
            "check_nl:",
            "  %last_char_idx = sub i64 %len, 1",
            "  %last_char_ptr = getelementptr i8, i8* %buf, i64 %last_char_idx",
            "  %last_char = load i8, i8* %last_char_ptr",
            "  %is_nl = icmp eq i8 %last_char, 10",
            "  br i1 %is_nl, label %strip, label %done",
            "strip:",
            "  store i8 0, i8* %last_char_ptr",
            "  br label %done",
            "done:",
            "  ret i8* %buf",
            "}"
        ]

    def new_tmp(self):
        self.tmp_count += 1
        return f"%t{self.tmp_count}"

    def new_label(self, prefix="lbl"):
        self.label_count += 1
        return f"{prefix}{self.label_count}"

    def emit(self, line):
        self.fn_lines.append(line)

    # -------------------------------------------------------
    # Type system: rank-based promotion for mixed-width math
    # -------------------------------------------------------
    # Integer IR types are exact LLVM widths (iN).
    # Float types: f32→float, f64→double, f128+→fp128
    # (LLVM only has native float/double/fp128; f256+ map to fp128)
    # Null sentinel: INT32_MIN (-2147483648). Chosen because sign-extension/truncation
    # preserves this exact value across ALL int widths (i32..i2048), so a Null stored
    # in an i32 var compares equal to a Null stored in an i64 var. Per spec, Null
    # behaves as -infinity (smaller than any non-null value) — see syntax NULL BEHAVIOR.
    _NULL_SENTINEL = "-2147483648"
    _NULL_SENTINEL_FLOAT = "0xFFF0000000000000"  # IEEE -infinity, exact across float/double

    _INT_IR = {"i32": "i32", "i64": "i64", "i128": "i128",
               "i256": "i256", "i512": "i512", "i1024": "i1024", "i2048": "i2048"}
    _FLT_IR = {"f32": "float", "f64": "double",
               "f128": "fp128", "f256": "fp128", "f512": "fp128",
               "f1024": "fp128", "f2048": "fp128"}
    # Rank: higher = wider. Floats outrank all ints.
    _TYPE_RANK = {
        "i1": 0, "i32": 1, "i64": 2, "i128": 3,
        "i256": 4, "i512": 5, "i1024": 6, "i2048": 7,
        "float": 10, "double": 11, "fp128": 12,
    }
    # Which IR types are integers vs floats
    _INT_IR_SET  = {"i1", "i32", "i64", "i128", "i256", "i512", "i1024", "i2048"}
    _FLOAT_IR_SET = {"float", "double", "fp128"}

    def rubi_type_to_ir(self, t):
        if t in ("list", "index", "dict", "Any"): return "%Box*"
        if t == "bool":  return "i1"
        if t == "str":   return "i8*"
        if t in self._INT_IR:  return self._INT_IR[t]
        if t in self._FLT_IR:  return self._FLT_IR[t]
        # Fallback: already an IR type (e.g. "i64" from internal use)
        if t in self._TYPE_RANK: return t
        return "i64"

    def promote_type(self, a, b):
        """Return the higher-ranked IR type for mixed-width arithmetic."""
        ra = self._TYPE_RANK.get(a, 2)   # default to i64 rank
        rb = self._TYPE_RANK.get(b, 2)
        # If one is float, result is always float
        if a in self._FLOAT_IR_SET or b in self._FLOAT_IR_SET:
            if a in self._FLOAT_IR_SET and b in self._FLOAT_IR_SET:
                return a if ra >= rb else b
            return a if a in self._FLOAT_IR_SET else b
        # Both ints
        return a if ra >= rb else b

    def intern_str(self, text):
        raw = text.replace("\\n", "\n").replace("\\t", "\t")
        byte_len = len(raw.encode("utf-8")) + 1
        escaped = ""
        for ch in raw:
            for b in ch.encode("utf-8"):
                escaped += f"\\{b:02X}"
        escaped += "\\00"
        lbl = f"@.str{self.str_count}"
        self.str_count += 1
        self.global_decls.append(f'{lbl} = private unnamed_addr constant [{byte_len} x i8] c"{escaped}"')
        return lbl, byte_len

    def declare_global(self, name, ir_type):
        if name in self.global_vars: return
        # Mangle names that conflict with C library functions
        c_lib_funcs = {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        if name in c_lib_funcs:
            name = f"_var_{name}"
        if name in self.global_vars: return
        self.global_vars[name] = ir_type
        # Prefix with _var_ to avoid conflicts with C library functions
        ir_name = f"_var_{name}" if name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else name
        if ir_type.endswith("*"):
            self.global_decls.append(f"@{ir_name} = global {ir_type} null")
        elif ir_type == "fp128":
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0xL00000000000000000000000000000000")
        elif ir_type in ("float", "double"):
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0.0")
        else:
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0")

    def get_var_ptr(self, name):
        if "." in name:
            mangled = name.replace(".", "_")
            if mangled in self.global_vars:
                return f"@{mangled}", self.global_vars[mangled]
        
        # Look for variable in the innermost scope first
        for scope in reversed(self.local_vars_stack):
            if name in scope:
                return f"%ptr_{name}", scope[name]
        # Check for mangled name if original conflicts with C lib func
        c_lib_funcs = {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        mangled = f"_var_{name}" if name in c_lib_funcs else name
        if mangled in self.global_vars: return f"@{mangled}", self.global_vars[mangled]
        if name in self.global_vars: return f"@{name}", self.global_vars[name]
        raise RubidiumNameError(f"Undefined variable '{name}'")

    def class_ir_type(self, class_name): return f"%class_{class_name}"

    def emit_class_type(self, cls):
        field_types = []
        for f in cls.fields:
            ir_t = self.rubi_type_to_ir(f.vtype) if f.vtype else self._infer_type(f.value)
            field_types.append(ir_t)
        field_types_str = ", ".join(field_types)
        if not field_types_str: field_types_str = "i8"
        self.global_decls.append(f"%class_{cls.name} = type {{ {field_types_str} }}")

    def field_index(self, class_name, field_name):
        cls = self.class_defs[class_name]
        for i, f in enumerate(cls.fields):
            if f.name == field_name:
                ir_t = self.rubi_type_to_ir(f.vtype) if f.vtype else self._infer_type(f.value)
                return i, ir_t
        raise RubidiumNameError(f"Class '{class_name}' has no field '{field_name}'")

    def method_ir_name(self, class_name, method_name):
        return f"{class_name}__{method_name}"

    def gen(self, stmts):
        for s in stmts:
            if isinstance(s, ClassDef):
                self.class_defs[s.name] = s
                for m in s.methods:
                    mangled_name = self.method_ir_name(s.name, m.name)
                    mfn = FnDef(mangled_name, [("__self", s.name)] + m.params, m.ret_type, m.body)
                    mfn.class_name = s.name
                    self.functions[mangled_name] = mfn
            elif isinstance(s, FnDef):
                self.functions[s.name] = s

        self.collect_globals(stmts)
        for cls in self.class_defs.values(): self.emit_class_type(cls)

        self.global_decls += extern_decls.split("\n")
        self.global_decls += [
            "", "declare i32 @printf(i8* noundef, ...)", "declare i32 @puts(i8* noundef)", "declare i32 @fflush(i8*)",
            "declare i8* @malloc(i64)", "declare void @free(i8*)", "declare i64 @strlen(i8*)",
            "declare i32 @scanf(i8*, ...)", "declare i8* @fgets(i8*, i32, i8*)",
            "declare i8* @strcpy(i8*, i8*)", "declare i32 @strcmp(i8*, i8*)",
            "declare i8* @strcat(i8*, i8*)", "declare i8* @strstr(i8*, i8*)",
            "declare i8* @strncpy(i8*, i8*, i64)",
            "declare i8* @str_replace(i8*, i8*, i8*)",
            "declare %Box* @str_slice(i8*)",
            "declare i64 @strtol(i8*, i8**, i32)", "declare i64 @atol(i8*)",
            "declare i8* @strndup(i8*, i64)", "declare i32 @fclose(i8*)",
            "declare i8* @list_combine(%Box*)",
            "declare %Box* @box_deep_copy(%Box*)",
            "declare i8* @fopen(i8*, i8*)", "declare i64 @fread(i8*, i64, i64, i8*)",
            "declare i64 @fwrite(i8*, i64, i64, i8*)", "declare i64 @fseek(i8*, i64, i32)",
            "declare i64 @ftell(i8*)", "declare void @rewind(i8*)",
            "declare i32 @sprintf(i8*, i8*, ...)",
            "@_stdin_ptr = external global i8*", # Changed from @.stdin_ptr
            "@_thread_handles = global [1024 x i64] zeroinitializer",
            "@_thread_results = external global [1024 x %Box*]", # <--- ADD THIS LINE
            "declare void @os_start(i64)",
            "declare i8* @os_run(i64, i8*, i8*)",
            "declare void @os_terminal_drop(i64)",
            "declare i64 @ffi_load(i8*)",
            "declare i64 @ffi_sym(i64, i8*)",
            ""
        ]

        self._emit_input_line_helper()
        
        # Inject RNG seed
        self.fn_lines += [
            "define void @_rubidium_init_rng() {",
            "  %t_seed = call i64 @time(i64* null)",
            "  %t_seed_i32 = trunc i64 %t_seed to i32",
            "  call void @srand(i32 %t_seed_i32)",
            "  ret void", "}", ""
        ]
        
        top_init = [s for s in stmts if not isinstance(s, (FnDef, ClassDef, Import, Use, FFIBind))]
        self.cur_fn = None
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.emit_fn(FnDef("_rubidium_init", [], None, top_init))

        for s in stmts:
            if isinstance(s, FnDef): self.emit_fn(s)
            elif isinstance(s, FFIBind): self.emit_ffi_bind(s)

        for cls in self.class_defs.values():
            for m in cls.methods:
                self._emit_class_method(self.functions[self.method_ir_name(cls.name, m.name)], cls.name)

        if "main" not in self.functions:
            self.emit_fn(FnDef("main", [], "i32", []))

        self._inject_init_call()

        out = ["; Rubidium compiled output", 'source_filename = "rubidium"', ""]
        out += self.global_decls + [""] + self.fn_lines
        return "\n".join(out)

    def _inject_init_call(self):
        patched = []; in_main = False; injected = False
        for line in self.fn_lines:
            patched.append(line)
            if line.strip() == "define i32 @main() {": in_main = True
            elif in_main and not injected and line.strip() == "entry:":
                patched.append("  call void @_rubidium_init_rng()")
                patched.append("  call i64 @_rubidium_init()")
                injected = True
        self.fn_lines = patched

    def _deep_copy_if_var(self, val, val_t, source_node):
        """Deep copy a %Box* value when the source is an existing variable (not a fresh allocation).
        Called on VarDecl/Assign to implement the spec's 'every assignment creates a full deep copy' rule."""
        if val_t == "%Box*" and isinstance(source_node, Var):
            copied = self.new_tmp()
            self.emit(f"  {copied} = call %Box* @box_deep_copy(%Box* {val})")
            return copied
        return val

    def _infer_type(self, node):
        if isinstance(node, Number): return "double" if isinstance(node.value, float) else "i64"
        if isinstance(node, Bool): return "i1"
        if isinstance(node, None_): return "i64"
        if isinstance(node, Str): return "i8*"
        if isinstance(node, InterpolatedStr): return "i8*"
        if isinstance(node, (ListExpr, DictExpr)): return "%Box*"
        if isinstance(node, Input): return "i8*"
        if isinstance(node, FileHandleStmt):
            if node.method in ("read", "readln"): return "i8*"
            return "i64"
        
        if isinstance(node, MethodCall):
            obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)
            
            # Check for module function call (e.g., math_tools.sin -> math_tools_sin)
            if isinstance(node.obj, Var) and f"{obj_name}_{node.method}" in self.functions:
                fn = self.functions[f"{obj_name}_{node.method}"]
                return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"

            # FFI bindings: c_lib.my_c_func -> method name alone is the bound symbol
            if isinstance(node.obj, Var) and node.method in self.functions and obj_name not in self.instances:
                fn = self.functions[node.method]
                return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
            
            if obj_name in self.instances:
                class_name = self.instances[obj_name]
                mangled = self.method_ir_name(class_name, node.method)
                if mangled in self.functions:
                    fn = self.functions[mangled]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
            
            if node.method == "len": return "i64"
            if node.method == "char": return "i8*"
            if node.method in ("contains", "has"): return "i1"
            if node.method == "combine": return "i8*"
            if node.method == "slice": 
                if len(node.args) == 0:
                    return "%Box*"
                return "i8*"
            if node.method == "to": 
                if len(node.args) == 1:
                    arg = node.args[0]
                    if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                        return self.rubi_type_to_ir(arg.name)
                    elif hasattr(arg, 'value'):
                        return self.rubi_type_to_ir(arg.value)
                return "i64"
# Built-in module methods
            if obj_name == "time":
                if node.method == "timer_read":
                    return "double"
                return "i64"
            if obj_name == "random":
                if node.method == "choice": return "%Box*"
                return "i64"  # shuffle returns void/0
            # File handle methods (inside open() block)
            if obj_name in self._file_handle_vars:
                if node.method in ("read", "readln"): return "i8*"
                return "i64"
            
            raise RubidiumNameError(f"Cannot infer type for method call '{node.method}' on object '{obj_name}'")

        if isinstance(node, OsRun):
            return "i8*"
        if isinstance(node, FnCall):
            # Normalization check
            name = node.name.replace(".", "_") if isinstance(node.name, str) and "." in node.name else node.name
            if isinstance(name, str):
                # Check if name is a collection variable — emit_call_expr priority 4 returns %Box*
                for scope in reversed(self.local_vars_stack):
                    if name in scope:
                        if scope[name] == "%Box*":
                            return "%Box*"
                        break
                if name in self.global_vars and self.global_vars[name] == "%Box*":
                    return "%Box*"
                if name == "random":
                    # Check if third arg is a float type (TYPE token parsed as Var)
                    type_name = ""
                    if len(node.args) >= 3:
                        arg3 = node.args[2]
                        if isinstance(arg3, Var) and arg3.name in ("f32", "f64", "f128", "f256", "f512", "f1024", "f2048"):
                            type_name = arg3.name
                        elif isinstance(arg3, Str):
                            type_name = arg3.value
                    is_float = type_name.startswith("f")
                    if is_float:
                        return "double"
                    return "i64"
                if name in self.functions:
                    fn = self.functions[name]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                # Check for class instantiation
                if name in self.class_defs:
                    return f"{self.class_ir_type(name)}*"
            return "i64"
            
        if isinstance(node, BinOp):
            lt = self._infer_type(node.left)
            rt = self._infer_type(node.right)
            if lt == "i8*" or rt == "i8*": return "i8*"
            if lt == "double" or rt == "double": return "double"
            return lt
        if isinstance(node, Compare): return "i1"
        if isinstance(node, UnaryOp):
            if node.op == "not": return "i1"
            return self._infer_type(node.value)
        if isinstance(node, TypeCast): return self.rubi_type_to_ir(node.target_type)
        if isinstance(node, Var):
            # Look for variable in the innermost scope first
            for scope in reversed(self.local_vars_stack):
                if node.name in scope:
                    return scope[node.name]
            if node.name in self.global_vars: return self.global_vars[node.name]
            return "i64"
        return "i64"

    def collect_globals(self, stmts):
        for s in stmts: self._collect_global(s)

    def _collect_global(self, node):
        if isinstance(node, VarDecl):
            # Local variables are function-scoped, not global
            if node.is_local:
                return
            cn = None
            if isinstance(node.value, ClassInstantiate):
                cn = node.value.class_name
            elif isinstance(node.value, FnCall) and node.value.name in self.class_defs:
                cn = node.value.name
            
            if cn:
                ir_t = f"{self.class_ir_type(cn)}*"
                self.declare_global(node.name, ir_t)
                self.instances[node.name] = cn
            else:
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                self.declare_global(node.name, ir_t)
            if node.mutable: self.mutable_vars.add(node.name)
        elif isinstance(node, FFIBind):
            # Pre-register the bound symbol so calls resolve in collect pass
            self.functions[node.symbol_name] = FnDef(node.symbol_name, node.params, node.ret_type, [])
        elif isinstance(node, If):
            for s in node.then_body: self._collect_global(s)
            for s in (node.else_body or []): self._collect_global(s)
        elif isinstance(node, While):
            for s in node.body: self._collect_global(s)
        elif isinstance(node, For):
            if not node.iterable:
                ir_t = "i64"
            elif isinstance(node.iterable, MethodCall) and node.iterable.method == "len":
                ir_t = "i64"
            else:
                ir_t = "%Box*"
            self.declare_global(node.var, ir_t)
            for s in node.body: self._collect_global(s)
        elif isinstance(node, Try):
            for s in node.try_body: self._collect_global(s)
            for s in node.error_body: self._collect_global(s)
        elif isinstance(node, FnDef):
            # Variable pool: let declarations inside regular functions go to the global pool
            for s in node.body: self._collect_global(s)
        elif isinstance(node, FileOpen):
            if node.var_name:
                self._file_handle_vars[node.var_name] = -1  # sentinel during collect pass
            for s in node.body: self._collect_global(s)
            if node.var_name:
                self._file_handle_vars.pop(node.var_name, None)

    def emit_fn(self, node):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, node.name, None
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.dropped_vars = set()
        self._alloca_emitted = set()  # track which local names have had alloca emitted
        
        ret_ir = "i32" if node.name == "main" else (self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64")
        param_ir = ", ".join(f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in node.params)
        self.emit(f"define {ret_ir} @{node.name}({param_ir}) {{")
        self.emit("entry:")
        
        for pn, pt in node.params:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars_stack[-1][pn] = ir_t
            self.emit(f"  %ptr_{pn} = alloca {ir_t}")
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* %ptr_{pn}")
            
        if not self.emit_body(node.body):
            if node.name == "main": self.emit("  ret i32 0")
            elif ret_ir == "i64": self.emit("  ret i64 0")
            elif ret_ir == "i1": self.emit("  ret i1 0")
            elif ret_ir in ("float","double"): self.emit(f"  ret {ret_ir} 0.0")
            elif ret_ir == "i8*": self.emit("  ret i8* null")
            else: self.emit(f"  ret {ret_ir} null")
        self.emit("}\n")
        # Flush any trampolines generated during this function
        if self._pending_trampolines:
            self.fn_lines += self._pending_trampolines
            self._pending_trampolines = []

    def emit_body(self, stmts):
        # Push a new scope for this block
        self.local_vars_stack.append({})
        returned = False
        for s in stmts:
            if returned: break
            if self.emit_stmt(s): returned = True
        # Pop the scope when leaving the block
        self.local_vars_stack.pop()
        return returned

    def _emit_class_method(self, mfn, class_name):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, mfn.name, class_name
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.dropped_vars = set()
        self._alloca_emitted = set()
        
        struct_t, ret_ir = self.class_ir_type(class_name), (self.rubi_type_to_ir(mfn.ret_type) if mfn.ret_type else "i64")
        param_str = ", ".join([f"{struct_t}* %param___self"] + [f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in mfn.params[1:]])
        self.emit(f"define {ret_ir} @{mfn.name}({param_str}) {{"  )
        self.emit("entry:")
        
        # Use ptr___self naming to match get_var_ptr convention for local_vars
        self.emit(f"  %ptr___self = alloca {struct_t}*")
        self.emit(f"  store {struct_t}* %param___self, {struct_t}** %ptr___self")
        # Register __self so field access (Var("__self")) works inside method bodies
        self.local_vars_stack[-1]["__self"] = f"{struct_t}*"
        self.instances["__self"] = class_name
        for pn, pt in mfn.params[1:]:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars_stack[-1][pn] = ir_t
            self.emit(f"  %ptr_{pn} = alloca {ir_t}")
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* %ptr_{pn}")
            
        if not self.emit_body(mfn.body):
            if ret_ir == "i64": self.emit("  ret i64 0")
            elif ret_ir == "i1": self.emit("  ret i1 0")
            elif ret_ir in ("float","double"): self.emit(f"  ret {ret_ir} 0.0")
            elif ret_ir == "i8*": self.emit("  ret i8* null")
            else: self.emit(f"  ret {ret_ir} null")
        self.emit("}\n")

    def emit_stmt(self, node):
        if isinstance(node, VarDecl):
            if node.name in self.dropped_vars: self.dropped_vars.discard(node.name)
            
            is_class = False
            cn = ""
            is_class_copy_src = None  # set when source is an existing instance var
            if isinstance(node.value, (ClassInstantiate, FnCall)):
                raw_cn = node.value.class_name if isinstance(node.value, ClassInstantiate) else (node.value.name if isinstance(node.value.name, str) else "")
                if raw_cn in self.class_defs:
                    cn = raw_cn
                    is_class = True
                elif f"main_{raw_cn}" in self.class_defs:
                    cn = f"main_{raw_cn}"
                    is_class = True
            elif isinstance(node.value, Var) and node.value.name in self.instances:
                # let p2 = p1 where p1 is a class instance — deep copy semantics
                cn = self.instances[node.value.name]
                is_class = True
                is_class_copy_src = node.value.name  # source var to copy from

            # Local variables are function-scoped (not global)
            if node.is_local:
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                self.local_vars_stack[-1][node.name] = ir_t
                if node.mutable: self.mutable_vars.add(node.name)
                ptr_str = f"%ptr_{node.name}"
                if node.name not in self._alloca_emitted:
                    self.emit(f"  {ptr_str} = alloca {ir_t}")
                    self._alloca_emitted.add(node.name)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                val = self._deep_copy_if_var(val, val_t, node.value)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                return False
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init" and self.cur_class is not None:
                # Inside a class method — keep variables local (class instances isolate memory)
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.local_vars_stack[-1][node.name] = f"{struct_t}*"
                    if node.mutable: self.mutable_vars.add(node.name)
                    ptr_str = f"%ptr_{node.name}"
                    self.emit(f"  {ptr_str} = alloca {struct_t}*")
                    if is_class_copy_src:
                        self.emit_class_copy(ptr_str, cn, is_class_copy_src)
                    else:
                        init_args = node.value.args if isinstance(node.value, FnCall) else []
                        self.emit_class_init(ptr_str, cn, init_args)
                    return False

                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                self.local_vars_stack[-1][node.name] = ir_t
                if node.mutable: self.mutable_vars.add(node.name)
                ptr_str = f"%ptr_{node.name}"
                if node.name not in self._alloca_emitted:
                    self.emit(f"  {ptr_str} = alloca {ir_t}")
                    self._alloca_emitted.add(node.name)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                val = self._deep_copy_if_var(val, val_t, node.value)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                if isinstance(node.value, FFILoad):
                    slot = f"@_ffi_slot_{node.name}"
                    if not any(d.startswith(slot) for d in self.global_decls):
                        self.global_decls.append(f"{slot} = global i64 -1")
                    self.emit(f"  store i64 {val}, i64* {slot}")
            elif self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                # Inside a regular function (not class method) — keep variables local
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.local_vars_stack[-1][node.name] = f"{struct_t}*"
                    if node.mutable: self.mutable_vars.add(node.name)
                    ptr_str = f"%ptr_{node.name}"
                    if node.name not in self._alloca_emitted:
                        self.emit(f"  {ptr_str} = alloca {struct_t}*")
                        self._alloca_emitted.add(node.name)
                    if is_class_copy_src:
                        self.emit_class_copy(ptr_str, cn, is_class_copy_src)
                    else:
                        init_args = node.value.args if isinstance(node.value, (FnCall, ClassInstantiate)) else []
                        self.emit_class_init(ptr_str, cn, init_args)
                    return False
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                self.local_vars_stack[-1][node.name] = ir_t
                if node.mutable: self.mutable_vars.add(node.name)
                ptr_str = f"%ptr_{node.name}"
                if node.name not in self._alloca_emitted:
                    self.emit(f"  {ptr_str} = alloca {ir_t}")
                    self._alloca_emitted.add(node.name)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                val = self._deep_copy_if_var(val, val_t, node.value)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                if isinstance(node.value, FFILoad):
                    slot = f"@_ffi_slot_{node.name}"
                    if not any(d.startswith(slot) for d in self.global_decls):
                        self.global_decls.append(f"{slot} = global i64 -1")
                    self.emit(f"  store i64 {val}, i64* {slot}")
            else:
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.declare_global(node.name, f"{struct_t}*")
                    if node.mutable: self.mutable_vars.add(node.name)
                    init_args = node.value.args if isinstance(node.value, FnCall) else []
                    ir_name = f"_var_{node.name}" if node.name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else node.name
                    if is_class_copy_src:
                        self.emit_class_copy(f"@{ir_name}", cn, is_class_copy_src)
                    else:
                        self.emit_class_init(f"@{ir_name}", cn, init_args)
                    return False
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                if node.mutable: self.mutable_vars.add(node.name)
                self.declare_global(node.name, ir_t)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                val = self._deep_copy_if_var(val, val_t, node.value)
                ir_name = f"_var_{node.name}" if node.name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else node.name
                self.emit(f"  store {ir_t} {val}, {ir_t}* @{ir_name}")
                if isinstance(node.value, FFILoad):
                    slot = f"@_ffi_slot_{node.name}"
                    if not any(d.startswith(slot) for d in self.global_decls):
                        self.global_decls.append(f"{slot} = global i64 -1")
                    self.emit(f"  store i64 {val}, i64* {slot}")
                return False

            
        elif isinstance(node, Assign):
            if node.name in self.dropped_vars: raise RubidiumNameError(f"Var '{node.name}' is dropped")
            
            # --- FIX: Check for Field Assignment ---
            if self.is_class_field(node.name):
                self.emit_field_assign(FieldAssign(Var("__self"), node.name, node.value))
            # ----------------------------------------
            elif node.name in self.instances: pass
            else:
                if node.name not in self.mutable_vars: raise RubidiumTypeError(f"Immutable '{node.name}'")
                ptr_str, ir_t = self.get_var_ptr(node.name)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                val = self._deep_copy_if_var(val, val_t, node.value)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                
        elif isinstance(node, FieldAssign): self.emit_field_assign(node)
        elif isinstance(node, Print): self.emit_print(node.value)
        elif isinstance(node, Println): self.emit_println(node.value)
        elif isinstance(node, If): self.emit_if(node)
        elif isinstance(node, While): self.emit_while(node)
        elif isinstance(node, For): self.emit_for(node)
        elif isinstance(node, Return):
            val, val_t = self.emit_expr(node.value)
            expected = (self.rubi_type_to_ir(self.functions[self.cur_fn].ret_type) if self.cur_fn in self.functions and self.functions[self.cur_fn].ret_type else val_t)
            val = self.coerce(val, val_t, expected)
            self.emit(f"  ret {expected} {val}")
            return True
        elif isinstance(node, FnCall): self.emit_call_expr(node)
        elif isinstance(node, MethodCall): 
            if node.method == "set" and isinstance(node.obj, (FnCall, MethodCall)):
                self.emit_collection_set(node)
                return False
            if node.method == "add" and isinstance(node.obj, (FnCall, MethodCall)):
                self.emit_collection_add(node)
                return False
            # String mutation: s.set(idx, char) / s.insert(idx, str) / s.replace(old, new)
            # These return a new string that must be stored back to the variable.
            if node.method in ("set", "insert", "replace") and isinstance(node.obj, Var):
                obj_t = self._infer_type(node.obj)
                if obj_t == "i8*":
                    obj_val, _ = self.emit_expr(node.obj)
                    result, _ = self.emit_string_method(obj_val, node.method, node.args)
                    ptr_str, _ = self.get_var_ptr(node.obj.name)
                    self.emit(f"  store i8* {result}, i8** {ptr_str}")
                    return False
            self.emit_method_call_expr(node)
        elif isinstance(node, Drop):
            self.dropped_vars.add(node.name)
            # Look for variable in the innermost scope first
            ir_t = None
            for scope in reversed(self.local_vars_stack):
                if node.name in scope:
                    ir_t = scope[node.name]
                    break
            if ir_t is None:
                ir_t = self.global_vars.get(node.name)
            if ir_t:
                ptr_str = f"%ptr_{node.name}" if any(node.name in scope for scope in self.local_vars_stack) else f"@_var_{node.name}" if node.name in {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"} else f"@{node.name}"
                val = self.new_tmp()
                self.emit(f"  {val} = load {ir_t}, {ir_t}* {ptr_str}")
                if ir_t == "%Box*": self.emit(f"  call void @box_drop(%Box* {val})")
                elif ir_t == "i8*": self.emit(f"  call void @free(i8* {val})")
        elif isinstance(node, Break):
            if self.loop_end_stack: self.emit(f"  br label %{self.loop_end_stack[-1]}")
            return True
        elif isinstance(node, Continue):
            # Continue: branch to the loop condition (skip to end of current iteration)
            if self.loop_cond_stack:
                self.emit(f"  br label %{self.loop_cond_stack[-1]}")
            return False
        elif isinstance(node, Try): self.emit_try(node)
        elif isinstance(node, ThreadCall):
            self.emit_call_expr(FnCall("thread", [node.func_call, node.thread_id]))
        elif isinstance(node, ThreadWait):
            for texpr in node.thread_ids:
                tid_v, tid_t = self.emit_expr(texpr)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
        elif isinstance(node, ThreadRunning):
            self.emit_thread_running_stmt(node)
#         elif isinstance(node, FileWrite): self.emit_file_write(node)
        elif isinstance(node, FileHandleStmt):
            self.emit_file_handle_method(node.var_name, node.method, node.args)
        elif isinstance(node, FileOpen): self.emit_file_open(node)
        elif isinstance(node, FileExists): self.emit_file_exists(node)
        elif isinstance(node, FileDelete): self.emit_file_delete(node)
        elif isinstance(node, FileRename): self.emit_file_rename(node)
        elif isinstance(node, FileCopy): self.emit_file_copy(node)
        elif isinstance(node, FileNew): self.emit_file_new(node)
        elif isinstance(node, OsStart):
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            self.emit(f"  call void @os_start(i64 {id_v})")
        elif isinstance(node, OsRun):
            self.emit_os_run(node)
        elif isinstance(node, OsDrop):
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            self.emit(f"  call void @os_terminal_drop(i64 {id_v})")
        elif isinstance(node, FFIBind):
            self.emit_ffi_bind(node)
        elif isinstance(node, Use):
            pass  # module activation — tracked at parse time, no IR needed
        return False

    def emit_collection_set(self, method_call_node):
        access_node = method_call_node.obj
        val_node = method_call_node.args[0]
        
        # Handle MethodCall on class instances where method is actually a field (e.g., p.scores(0).set(99))
        if isinstance(access_node, MethodCall):
            inner = access_node
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) - inner is MethodCall(Var('p'), 'scores', [Number(0)])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    # Get the index from inner.args[0]
                    idx_v, idx_t = self.emit_expr(inner.args[0])
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    
                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(val_node)
                    val_b = self.coerce_to_box(val_v, val_t)
                    
                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"
        
        keys = []
        curr = access_node
        while isinstance(curr, (FnCall, MethodCall)):
            if curr.args:
                keys = curr.args + keys
            curr = curr.obj if isinstance(curr, MethodCall) else curr.name
            
        # Handle Var that is a class field (e.g., scores inside a class method)
        if isinstance(curr, Var) and self.is_class_field(curr.name):
            col_v, col_t = self.emit_field_access(Var("__self"), curr.name)
        elif isinstance(curr, str) and self.is_class_field(curr):
            col_v, col_t = self.emit_field_access(Var("__self"), curr)
        elif isinstance(curr, str):
            col_v, col_t = self.emit_expr(Var(curr))
        else:
            col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)
        
        for i in range(len(keys) - 1):
            arg = keys[i]
            if isinstance(arg, FnCall) and isinstance(arg.name, str) and arg.name not in self.functions and arg.name not in self.global_vars and not any(arg.name in scope for scope in self.local_vars_stack):
                key_str = arg.name
                key_lbl, key_len = self.intern_str(key_str)
                key_ptr = self.new_tmp(); key_b = self.new_tmp()
                self.emit(f"  {key_ptr} = getelementptr [{key_len} x i8], [{key_len} x i8]* {key_lbl}, i64 0, i64 0")
                self.emit(f"  {key_b} = call %Box* @box_s(i8* {key_ptr})")
                next_col = self.new_tmp()
                self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {key_b})")
                col_b = next_col
                
                idx_val, idx_t = self.emit_expr(arg.args[0])
                idx_b = self.coerce_to_box(idx_val, idx_t)
                next_col2 = self.new_tmp()
                self.emit(f"  {next_col2} = call %Box* @collection_get(%Box* {col_b}, %Box* {idx_b})")
                col_b = next_col2
            else:
                k_v, k_t = self.emit_expr(arg)
                k_b = self.coerce_to_box(k_v, k_t)
                next_col = self.new_tmp()
                self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {k_b})")
                col_b = next_col

        last_arg = keys[-1]
        val_v, val_t = self.emit_expr(val_node)
        val_b = self.coerce_to_box(val_v, val_t)
        
        if isinstance(last_arg, FnCall) and isinstance(last_arg.name, str) and last_arg.name not in self.functions and last_arg.name not in self.global_vars and last_arg.name not in self.local_vars:
            key_str = last_arg.name
            key_lbl, key_len = self.intern_str(key_str)
            key_ptr = self.new_tmp(); key_b = self.new_tmp()
            self.emit(f"  {key_ptr} = getelementptr [{key_len} x i8], [{key_len} x i8]* {key_lbl}, i64 0, i64 0")
            self.emit(f"  {key_b} = call %Box* @box_s(i8* {key_ptr})")
            next_col = self.new_tmp()
            self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {key_b})")
            col_b = next_col
            
            idx_val, idx_t = self.emit_expr(last_arg.args[0])
            idx_b = self.coerce_to_box(idx_val, idx_t)
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        else:
            k_v, k_t = self.emit_expr(last_arg)
            k_b = self.coerce_to_box(k_v, k_t)
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {k_b}, %Box* {val_b})")
            
        return "0", "i64"

    def emit_collection_add(self, method_call_node):
        access_node = method_call_node.obj
        val_nodes = method_call_node.args
        
        # Handle MethodCall on class instances where method is actually a field (e.g., p.scores().add(50))
        if isinstance(access_node, MethodCall):
            inner = access_node
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    if len(val_nodes) == 1:
                        val_v, val_t = self.emit_expr(val_nodes[0])
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
                    elif len(val_nodes) == 2:
                        key_v, key_t = self.emit_expr(val_nodes[0])
                        val_v, val_t = self.emit_expr(val_nodes[1])
                        key_b = self.coerce_to_box(key_v, key_t)
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
                    return "0", "i64"
        
        keys = []
        curr = access_node
        while isinstance(curr, (FnCall, MethodCall)):
            if curr.args:
                keys = curr.args + keys
            curr = curr.obj if isinstance(curr, MethodCall) else curr.name
        
        # Handle Var that is a class field (e.g., scores inside a class method)
        if isinstance(curr, Var) and self.is_class_field(curr.name):
            col_v, col_t = self.emit_field_access(Var("__self"), curr.name)
        elif isinstance(curr, str) and self.is_class_field(curr):
            col_v, col_t = self.emit_field_access(Var("__self"), curr)
        elif isinstance(curr, str): 
            col_v, col_t = self.emit_expr(Var(curr))
        else: 
            col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)
        
        for arg in keys:
            k_v, k_t = self.emit_expr(arg)
            k_b = self.coerce_to_box(k_v, k_t)
            next_col = self.new_tmp()
            self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {k_b})")
            col_b = next_col
        
        if method_call_node.method == "add":
            if len(val_nodes) == 1:
                val_v, val_t = self.emit_expr(val_nodes[0])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
            elif len(val_nodes) == 2:
                key_v, key_t = self.emit_expr(val_nodes[0])
                val_v, val_t = self.emit_expr(val_nodes[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
        
        return "0", "i64"

    def emit_collection_set_on_field(self, method_call_node, field_val, field_t):
        """Handle p.scores(0).set(99) - collection set on a class field value."""
        # The method_call_node.obj is a FieldAccess, and we already have the field value
        # The args are the index/key and value
        val_node = method_call_node.args[0]
        
        # For p.scores(0).set(99), the args are [0, 99]
        # We need to get the field value, then do collection_set
        col_b = self.coerce_to_box(field_val, field_t)
        
        # Get the index from the first arg
        idx_v, idx_t = self.emit_expr(method_call_node.args[0])
        idx_b = self.coerce_to_box(idx_v, idx_t)
        
        val_v, val_t = self.emit_expr(val_node)
        val_b = self.coerce_to_box(val_v, val_t)
        
        self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_set_on_field_nested(self, outer_method_call, field_val, field_t):
        """Handle p.scores(0).set(99) where p.scores(0) is parsed as MethodCall(Var('p'), 'scores', [0])."""
        # outer_method_call is MethodCall(Var('p'), 'scores', [MethodCall(Var('p'), 0, [99])])
        # Actually it's MethodCall(Var('p'), 'scores', [MethodCall(Var('p'), 0, [99])])
        # Wait, let me re-examine: p.scores(0).set(99)
        # This is parsed as: MethodCall(MethodCall(Var('p'), 'scores', [Number(0)]), 'set', [Number(99)])
        # So outer_method_call.obj is MethodCall(Var('p'), 'scores', [Number(0)])
        # And outer_method_call.args is [Number(99)]
        
        # We already have field_val and field_t from the field access
        col_b = self.coerce_to_box(field_val, field_t)
        
        # The inner MethodCall has the index
        inner = outer_method_call.args[0] if outer_method_call.args else None
        if isinstance(inner, MethodCall):
            # inner.args[0] is the index
            idx_v, idx_t = self.emit_expr(inner.args[0])
            idx_b = self.coerce_to_box(idx_v, idx_t)
            
            val_v, val_t = self.emit_expr(outer_method_call.args[0] if len(outer_method_call.args) > 1 else Number(0))
            val_b = self.coerce_to_box(val_v, val_t)
            
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_add_on_field(self, method_call_node, field_val, field_t):
        """Handle p.scores().add(50) - collection add on a class field value."""
        col_b = self.coerce_to_box(field_val, field_t)
        
        if len(method_call_node.args) == 1:
            val_v, val_t = self.emit_expr(method_call_node.args[0])
            val_b = self.coerce_to_box(val_v, val_t)
            self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
        elif len(method_call_node.args) == 2:
            key_v, key_t = self.emit_expr(method_call_node.args[0])
            val_v, val_t = self.emit_expr(method_call_node.args[1])
            key_b = self.coerce_to_box(key_v, key_t)
            val_b = self.coerce_to_box(val_v, val_t)
            self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_add_on_field_nested(self, outer_method_call, field_val, field_t):
        """Handle p.scores().add(50) where p.scores() is parsed as MethodCall(Var('p'), 'scores', [])."""
        col_b = self.coerce_to_box(field_val, field_t)
        
        inner = outer_method_call.args[0] if outer_method_call.args else None
        if isinstance(inner, MethodCall):
            # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
            # outer_method_call.args[0] is the add MethodCall
            # Actually: p.scores().add(50) is MethodCall(MethodCall(Var('p'), 'scores', []), 'add', [Number(50)])
            # So outer_method_call.args is [Number(50)]
            if len(outer_method_call.args) == 1:
                val_v, val_t = self.emit_expr(outer_method_call.args[0])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
            elif len(outer_method_call.args) == 2:
                key_v, key_t = self.emit_expr(outer_method_call.args[0])
                val_v, val_t = self.emit_expr(outer_method_call.args[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_class_init(self, ptr_str, class_name, init_args=None):
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)
        size_ptr, size_int, raw_ptr, typed_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {typed_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {typed_ptr}, {struct_t}** {ptr_str}")
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype) if field.vtype else self._infer_type(field.value)
            fptr = self.new_tmp()
            self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {typed_ptr}, i32 0, i32 {i}")
            val, val_t = self.emit_expr(field.value)
            val = self.coerce(val, val_t, ir_t)
            self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")
        # Call __init__ if it exists and we have init_args
        if init_args and f"{class_name}__init__" in self.functions:
            mangled = f"{class_name}__init__"
            fn = self.functions[mangled]
            args_ir = [f"{struct_t}* {typed_ptr}"]
            for i, arg_node in enumerate(init_args):
                v, t = self.emit_expr(arg_node)
                if i + 1 < len(fn.params):
                    expected_t = self.rubi_type_to_ir(fn.params[i + 1][1])
                    v = self.coerce(v, t, expected_t)
                    args_ir.append(f"{expected_t} {v}")
                else:
                    args_ir.append(f"{t} {v}")
            self.emit(f"  call i64 @{mangled}({', '.join(args_ir)})")

    def emit_class_copy(self, ptr_str, class_name, src_var_name):
        """Deep copy a class instance into ptr_str. Allocates a new struct and copies all fields."""
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)
        src_ptr_ptr, _ = self.get_var_ptr(src_var_name)
        src_ptr = self.new_tmp()
        self.emit(f"  {src_ptr} = load {struct_t}*, {struct_t}** {src_ptr_ptr}")
        size_ptr, size_int, raw_ptr, dst_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {dst_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {dst_ptr}, {struct_t}** {ptr_str}")
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype) if field.vtype else self._infer_type(field.value)
            src_fptr, dst_fptr, fval = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {src_fptr} = getelementptr {struct_t}, {struct_t}* {src_ptr}, i32 0, i32 {i}")
            self.emit(f"  {fval} = load {ir_t}, {ir_t}* {src_fptr}")
            if ir_t == "%Box*":
                copied = self.new_tmp()
                self.emit(f"  {copied} = call %Box* @box_deep_copy(%Box* {fval})")
                fval = copied
            self.emit(f"  {dst_fptr} = getelementptr {struct_t}, {struct_t}* {dst_ptr}, i32 0, i32 {i}")
            self.emit(f"  store {ir_t} {fval}, {ir_t}* {dst_fptr}")

    def emit_field_assign(self, node):
        obj_name = node.obj.name if hasattr(node.obj, 'name') else node.obj
        if obj_name not in self.instances: return
        class_name = self.instances[obj_name]

        # Mutation rules (see syntax CLASSES section):
        # - If the instance itself is not declared 'mut', nothing can be changed.
        # - If the instance is 'mut', only fields declared 'mut' inside the class can change.
        if obj_name != "__self" and obj_name not in self.mutable_vars:
            raise RubidiumTypeError(f"Cannot assign to field '{node.field}': instance '{obj_name}' is not mutable")
        cls = self.class_defs[class_name]
        field_def = next((f for f in cls.fields if f.name == node.field), None)
        if field_def is not None and not field_def.mutable:
            raise RubidiumTypeError(f"Cannot assign to field '{node.field}': field is not declared 'mut' in class '{class_name}'")

        idx, ir_t  = self.field_index(class_name, node.field)
        struct_t   = self.class_ir_type(class_name)
        
        ptr_str, _ = self.get_var_ptr(obj_name)
        inst_ptr = self.new_tmp(); fptr = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        val, val_t = self.emit_expr(node.value)
        val = self.coerce(val, val_t, ir_t)
        self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")

    def emit_print(self, value):
         # If printing a dropped variable, emit error message only
        if isinstance(value, Var) and value.name in self.dropped_vars:
            err_lbl, err_len = self.intern_str("ERROR: variable does not exist\\n")
            ptr = self.new_tmp()
            self.emit(f"  {ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
            self.emit(f"  call i32 (i8*, ...) @printf(i8* {ptr})")
            return
        val, val_t = self.emit_expr(value)
        if val_t == "%Box*":
            self.emit(f"  call void @print_boxed(%Box* {val})")  # print_boxed already calls fflush
        elif val_t == "i1":
            # Print True or False
            true_lbl, true_len = self.intern_str("True\n")
            false_lbl, false_len = self.intern_str("False\n")
            true_ptr = self.new_tmp(); false_ptr = self.new_tmp(); sel_ptr = self.new_tmp()
            self.emit(f"  {true_ptr} = getelementptr [{true_len} x i8], [{true_len} x i8]* {true_lbl}, i64 0, i64 0")
            self.emit(f"  {false_ptr} = getelementptr [{false_len} x i8], [{false_len} x i8]* {false_lbl}, i64 0, i64 0")
            self.emit(f"  {sel_ptr} = select i1 {val}, i8* {true_ptr}, i8* {false_ptr}")
            self.emit(f"  call i32 (i8*, ...) @printf(i8* {sel_ptr})")
            self.emit(f"  call i32 @fflush(i8* null)")
        elif val_t in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048"):
            cv = self.coerce(val, val_t, "i64")
            fmt, flen = self.intern_str("%lld\n")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t in ("float", "double", "fp128"):
            fmt, flen = self.intern_str("%g\n")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            dv = self.coerce(val, val_t, "double")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, double {dv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "i8*":
            self.emit(f'  call i32 @puts(i8* {val})')
            self.emit(f'  call i32 @fflush(i8* null)')

    def emit_println(self, value):
        # println prints without newline; subsequent calls overwrite same line via \r
        val, val_t = self.emit_expr(value)
        if val_t in ("i32", "i64", "i1", "i128", "i256", "i512", "i1024", "i2048"):
            fmt, flen = self.intern_str("\r%lld")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            cv = self.coerce(val, val_t, "i64")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t in ("float", "double", "fp128"):
            fmt, flen = self.intern_str("\r%g")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            dv = self.coerce(val, val_t, "double")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, double {dv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "i8*":
            fmt, flen = self.intern_str("\r%s")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i8* {val})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "%Box*":
            # Box printing for println — use printf with \r instead of puts
            fmt, flen = self.intern_str("\r<value>")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            self.emit(f'  call i32 @printf(i8* {ptr})')
            self.emit(f'  call i32 @fflush(i8* null)')

    def to_bool(self, val, t):
        if t == "i1": return val
        tmp = self.new_tmp()
        if t in ("float","double"): self.emit(f"  {tmp} = fcmp une {t} {val}, 0.0")
        elif t == "i8*": self.emit(f"  {tmp} = icmp ne i8* {val}, null")
        elif t == "%Box*":
            c_int = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = icmp ne i64 {c_int}, 0")
        else: self.emit(f"  {tmp} = icmp ne {t} {val}, 0")
        return tmp

    def emit_if(self, node):
        cond, ct = self.emit_expr(node.cond); cond = self.to_bool(cond, ct)
        then_l, else_l, end_l = self.new_label("then"), self.new_label("else"), self.new_label("endif")
        self.emit(f"  br i1 {cond}, label %{then_l}, label %{else_l}")
        self.emit(f"{then_l}:")
        if not self.emit_body(node.then_body): self.emit(f"  br label %{end_l}")
        self.emit(f"{else_l}:")
        if not (node.else_body and self.emit_body(node.else_body)): self.emit(f"  br label %{end_l}")
        self.emit(f"{end_l}:")

    def emit_while(self, node):
        cond_l, body_l, end_l = self.new_label("wcond"), self.new_label("wbody"), self.new_label("wend")
        self.emit(f"  br label %{cond_l}\n{cond_l}:")
        cond, ct = self.emit_expr(node.cond); cond = self.to_bool(cond, ct)
        self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
        self.loop_end_stack.append(end_l)
        self.loop_cond_stack.append(cond_l)
        self.emit_body(node.body)
        self.loop_cond_stack.pop()
        self.loop_end_stack.pop()
        self.emit(f"  br label %{cond_l}\n{end_l}:")

    def emit_for(self, node):
        if node.iterable:
            # Check if the iterable is a file handle var
            if isinstance(node.iterable, Var) and node.iterable.name in self._file_handle_vars:
                self._emit_for_file(node)
                return
            iter_v, iter_t = self.emit_expr(node.iterable)

            # Integer iterable: for i in N → loop from 1 to N (inclusive)
            if iter_t in self._INT_IR_SET:
                iter_v_64 = self.coerce(iter_v, iter_t, "i64")
                if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                    var_ptr = f"%ptr_{node.var}"
                    if node.var not in self._alloca_emitted:
                        self.local_vars_stack[-1][node.var] = "i64"
                        self.emit(f"  {var_ptr} = alloca i64")
                        self._alloca_emitted.add(node.var)
                    else:
                        self.local_vars_stack[-1][node.var] = "i64"
                else:
                    self.declare_global(node.var, "i64")
                    var_ptr = f"@{node.var}"
                self.emit(f"  store i64 0, i64* {var_ptr}")
                cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
                self.emit(f"  br label %{cond_l}\n{cond_l}:")
                cur, cond = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur} = load i64, i64* {var_ptr}\n  {cond} = icmp slt i64 {cur}, {iter_v_64}")
                self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
                self.loop_end_stack.append(end_l)
                self.loop_cond_stack.append(cont_l)
                self.emit_body(node.body)
                self.loop_cond_stack.pop()
                self.loop_end_stack.pop()
                self.emit(f"  br label %{cont_l}\n{cont_l}:")
                inc, cur2 = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur2} = load i64, i64* {var_ptr}\n  {inc} = add i64 {cur2}, 1\n  store i64 {inc}, i64* {var_ptr}")
                self.emit(f"  br label %{cond_l}\n{end_l}:")
                return

            iter_b = self.coerce_to_box(iter_v, iter_t)
            
            idx_ptr = self.new_tmp()
            self.emit(f"  {idx_ptr} = alloca i32")
            self.emit(f"  store i32 0, i32* {idx_ptr}")
            
            item_t = "%Box*"
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                var_ptr = f"%ptr_{node.var}"
                if node.var not in self._alloca_emitted:
                    self.local_vars_stack[-1][node.var] = item_t
                    self.emit(f"  {var_ptr} = alloca {item_t}")
                    self._alloca_emitted.add(node.var)
                else:
                    self.local_vars_stack[-1][node.var] = item_t
                    # Already alloca'd — just update the type in case it changed
                    self.local_vars_stack[-1][node.var] = item_t
            else:
                self.declare_global(node.var, item_t)
                var_ptr = f"@{node.var}"
                
            len_val = self.new_tmp()
            self.emit(f"  {len_val} = call i32 @collection_len(%Box* {iter_b})")
            
            cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            
            cur_idx, cond = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur_idx} = load i32, i32* {idx_ptr}")
            self.emit(f"  {cond} = icmp slt i32 {cur_idx}, {len_val}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            
            item_val = self.new_tmp()
            item_copy = self.new_tmp()
            self.emit(f"  {item_val} = call %Box* @collection_get_at(%Box* {iter_b}, i32 {cur_idx})")
            self.emit(f"  {item_copy} = call %Box* @box_copy(%Box* {item_val})")
            self.emit(f"  store %Box* {item_copy}, %Box** {var_ptr}")
            
            self.loop_end_stack.append(end_l)
            self.loop_cond_stack.append(cont_l)
            self.emit_body(node.body)
            self.loop_cond_stack.pop()
            self.loop_end_stack.pop()
            
            # Continue target: drop loop variable, increment, and go to condition
            self.emit(f"  br label %{cont_l}\n{cont_l}:")
            drop_val = self.new_tmp()
            self.emit(f"  {drop_val} = load %Box*, %Box** {var_ptr}")
            self.emit(f"  call void @box_drop(%Box* {drop_val})")
            inc_idx = self.new_tmp()
            self.emit(f"  {inc_idx} = add i32 {cur_idx}, 1")
            self.emit(f"  store i32 {inc_idx}, i32* {idx_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")
            
        else:
            sv, st = self.emit_expr(node.start); ev, et = self.emit_expr(node.end)
            sv = self.coerce(sv, st, "i64"); ev = self.coerce(ev, et, "i64")

            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                var_ptr = f"%ptr_{node.var}"
                if node.var not in self._alloca_emitted:
                    self.local_vars_stack[-1][node.var] = "i64"
                    self.emit(f"  {var_ptr} = alloca i64")
                    self._alloca_emitted.add(node.var)
                else:
                    self.local_vars_stack[-1][node.var] = "i64"
            else:
                self.declare_global(node.var, "i64")
                var_ptr = f"@{node.var}"

            # Determine direction at codegen time: is_up = (start < end)
            is_up = self.new_tmp()
            self.emit(f"  {is_up} = icmp slt i64 {sv}, {ev}")
            self.emit(f"  store i64 {sv}, i64* {var_ptr}")
            cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            cur, cur_lt, cur_gt, cond = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur} = load i64, i64* {var_ptr}")
            self.emit(f"  {cur_lt} = icmp slt i64 {cur}, {ev}")
            self.emit(f"  {cur_gt} = icmp sgt i64 {cur}, {ev}")
            self.emit(f"  {cond} = select i1 {is_up}, i1 {cur_lt}, i1 {cur_gt}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            self.loop_end_stack.append(end_l)
            self.loop_cond_stack.append(cont_l)
            self.emit_body(node.body)
            self.loop_cond_stack.pop()
            self.loop_end_stack.pop()
            self.emit(f"  br label %{cont_l}\n{cont_l}:")
            step, inc, cur2 = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {step} = select i1 {is_up}, i64 1, i64 -1")
            self.emit(f"  {cur2} = load i64, i64* {var_ptr}\n  {inc} = add i64 {cur2}, {step}\n  store i64 {inc}, i64* {var_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")

    def _emit_for_file(self, node):
        """for line in file_handle { body } — iterate file lines one-by-one."""
        slot = self._file_handle_vars[node.iterable.name]
        # Loop variable stores the current line as i8*
        if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
            var_ptr = f"%ptr_{node.var}"
            if node.var not in self._alloca_emitted:
                self.local_vars_stack[-1][node.var] = "%Box*"
                self.emit(f"  {var_ptr} = alloca %Box*")
                self._alloca_emitted.add(node.var)
        else:
            self.declare_global(node.var, "%Box*")
            var_ptr = f"@{node.var}"

        # Line counter (1-based)
        line_ptr = self.new_tmp()
        self.emit(f"  {line_ptr} = alloca i64")
        self.emit(f"  store i64 1, i64* {line_ptr}")

        cond_l = self.new_label("ffilecond")
        body_l = self.new_label("ffilebody")
        cont_l = self.new_label("ffilecont")
        end_l  = self.new_label("ffileend")
        self.emit(f"  br label %{cond_l}\n{cond_l}:")

        cur_line = self.new_tmp()
        line_s   = self.new_tmp()
        cond     = self.new_tmp()
        self.emit(f"  {cur_line} = load i64, i64* {line_ptr}")
        self.emit(f"  {line_s} = call i8* @file_readln(i64 {slot}, i64 {cur_line})")
        # Empty string → end of file
        empty_lbl, elen = self.intern_str("")
        empty_ptr = self.new_tmp()
        self.emit(f"  {empty_ptr} = getelementptr [{elen} x i8], [{elen} x i8]* {empty_lbl}, i64 0, i64 0")
        cmp_res = self.new_tmp()
        self.emit(f"  {cmp_res} = call i32 @strcmp(i8* {line_s}, i8* {empty_ptr})")
        self.emit(f"  {cond} = icmp eq i32 {cmp_res}, 0")
        self.emit(f"  br i1 {cond}, label %{end_l}, label %{body_l}\n{body_l}:")

        # Box the line string and store as loop variable
        boxed = self.new_tmp()
        self.emit(f"  {boxed} = call %Box* @box_s(i8* {line_s})")
        self.emit(f"  store %Box* {boxed}, %Box** {var_ptr}")

        self.loop_end_stack.append(end_l)
        self.loop_cond_stack.append(cont_l)
        self.emit_body(node.body)
        self.loop_cond_stack.pop()
        self.loop_end_stack.pop()

        # Continue target: increment and go to condition
        self.emit(f"  br label %{cont_l}\n{cont_l}:")
        inc_v = self.new_tmp()
        cur2  = self.new_tmp()
        self.emit(f"  {cur2} = load i64, i64* {line_ptr}")
        self.emit(f"  {inc_v} = add i64 {cur2}, 1")
        self.emit(f"  store i64 {inc_v}, i64* {line_ptr}")
        self.emit(f"  br label %{cond_l}\n{end_l}:")

    def emit_thread_running(self, node):
        """thread.running(id) -> bool: True if thread is still running, False if done."""
        # Lookup thread handle in _thread_handles array
        tid_v, tid_t = self.emit_expr(node.thread_id)
        tid_v = self.coerce(tid_v, tid_t, "i64")
        result = self.new_tmp()
        # pthread_tryjoin_np returns 0 (EBUSY) if thread is still running, ESRCH/0 if done
        # We call it with null retval; if it returns EBUSY (16) → still running
        # For simplicity use our runtime helper to check via non-destructive probe
        self.emit(f"  {result} = call i1 @_thread_is_running(i64 {tid_v})")
        return result, "i1"

    def emit_thread_running_stmt(self, node):
        self.emit_thread_running(node)

    def emit_file_new(self, node):
        """file.new("path") { body } - create a new file with handle and auto-close"""
        path_val, path_t = self.emit_expr(node.path_expr)
        path_s = self.coerce(path_val, path_t, "i8*")
        mode_lbl, mlen = self.intern_str("w")
        mode_ptr = self.new_tmp()
        self.emit(f"  {mode_ptr} = getelementptr [{mlen} x i8], [{mlen} x i8]* {mode_lbl}, i64 0, i64 0")
        slot = self._file_slot_counter
        self._file_slot_counter += 1
        self.emit(f"  call i64 @file_open(i64 {slot}, i8* {path_s}, i32 1)")
        handle_name = f"_file_new_{slot}"
        self._file_handle_vars["file"] = slot
        if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
            self.local_vars_stack[-1][handle_name] = "i64"
            ptr = f"%ptr_{handle_name}"
            self.emit(f"  {ptr} = alloca i64")
            self._alloca_emitted.add(handle_name)
            self.emit(f"  store i64 {slot}, i64* {ptr}")
        else:
            self.declare_global(handle_name, "i64")
            self.emit(f"  store i64 {slot}, i64* @{handle_name}")
        saved_loop = self.loop_end_stack[:]
        self.emit_body(node.body)
        self.loop_end_stack = saved_loop
        del self._file_handle_vars["file"]
        self.emit(f"  call void @file_close(i64 {slot})")

    def emit_try(self, node):
        # NOTE: Rubidium's try/on_error does NOT use LLVM invoke/landingpad.
        # It cannot catch hardware exceptions like segfaults or FPE signals.
        # What it CAN catch is explicit runtime division-by-zero: any sdiv
        # inside the try body is emitted with a zero-check that branches to
        # on_error instead of faulting. All other errors still crash.
        ok_l, err_l, end_l = self.new_label("tok"), self.new_label("terr"), self.new_label("tend")
        self._try_error_label = err_l
        self.emit(f"  br label %{ok_l}\n{ok_l}:")
        self.emit_body(node.try_body)
        self._try_error_label = None
        self.emit(f"  br label %{end_l}\n{err_l}:")
        # Declare 'error' variable as a string in the error scope
        err_str = "Division by zero"
        err_lbl, err_len = self.intern_str(err_str)
        err_ptr = self.new_tmp()
        self.emit(f"  %{err_ptr[1:]} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
        self.declare_global("error", "i8*")
        self.emit(f'  store i8* %{err_ptr[1:]}, i8** @error')
        self.emit_body(node.error_body)
        self.emit(f"  br label %{end_l}\n{end_l}:")

    def emit_file_write(self, node):
        path_val, path_t  = self.emit_expr(node.path_expr)
        cont_val, cont_t  = self.emit_expr(node.content_expr)
        mode_lbl, mlen = self.intern_str("w"); mode_ptr = self.new_tmp()
        self.emit(f"  {mode_ptr} = getelementptr [{mlen} x i8], [{mlen} x i8]* {mode_lbl}, i64 0, i64 0")
        fp, clen = self.new_tmp(), self.new_tmp()
        self.emit(f"  {fp} = call i8* @fopen(i8* {path_val}, i8* {mode_ptr})")
        self.emit(f"  {clen} = call i64 @strlen(i8* {cont_val})")
        self.emit(f"  call i64 @fwrite(i8* {cont_val}, i64 1, i64 {clen}, i8* {fp})")
        self.emit(f"  call i32 @fclose(i8* {fp})")

    def emit_file_open(self, node):
        """open("path") as var { body } - file handle with automatic close"""
        path_val, path_t = self.emit_expr(node.path_expr)
        path_s = self.coerce(path_val, path_t, "i8*")
        # Assign a unique slot for this open() block
        slot = self._file_slot_counter
        self._file_slot_counter += 1
        self.emit(f"  call i64 @file_open(i64 {slot}, i8* {path_s}, i32 0)")
        # Register this var_name as a file handle pointing to this slot
        self._file_handle_vars[node.var_name] = slot
        # Allocate the variable in local/global scope so Var(f) can be resolved
        slot_val = str(slot)
        # Check if variable already exists in any local scope
        var_exists = any(node.var_name in scope for scope in self.local_vars_stack)
        if not var_exists and node.var_name not in self.global_vars:
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                self.local_vars_stack[-1][node.var_name] = "i64"
                ptr = f"%ptr_{node.var_name}"
                self.emit(f"  {ptr} = alloca i64")
                self._alloca_emitted.add(node.var_name)
                self.emit(f"  store i64 {slot_val}, i64* {ptr}")
            else:
                self.declare_global(node.var_name, "i64")
                self.emit(f"  store i64 {slot_val}, i64* @{node.var_name}")
        # Emit body — method calls on node.var_name will be intercepted
        saved_loop = self.loop_end_stack[:]
        self.emit_body(node.body)
        self.loop_end_stack = saved_loop
        # Unregister the handle and auto-close
        del self._file_handle_vars[node.var_name]
        self.emit(f"  call void @file_close(i64 {slot})")

    def emit_file_handle_method(self, var_name, method, args):
        """Emit a method call on a file handle variable (inside open() block)"""
        slot = self._file_handle_vars[var_name]
        if method == "write":
            data_v, data_t = self.emit_expr(args[0])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_write_all(i64 {slot}, i8* {data_s})")
            return "0", "i64"
        elif method in ("append", "add"):
            data_v, data_t = self.emit_expr(args[0])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_append_all(i64 {slot}, i8* {data_s})")
            return "0", "i64"
        elif method == "read":
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @file_read_all(i64 {slot})")
            return result, "i8*"
        elif method == "readln":
            line_v, line_t = self.emit_expr(args[0])
            line_i = self.coerce(line_v, line_t, "i64")
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @file_readln(i64 {slot}, i64 {line_i})")
            return result, "i8*"
        elif method == "writeln":
            line_v, line_t = self.emit_expr(args[0])
            line_i = self.coerce(line_v, line_t, "i64")
            data_v, data_t = self.emit_expr(args[1])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_writeln(i64 {slot}, i64 {line_i}, i8* {data_s})")
            return "0", "i64"
        else:
            raise RubidiumNameError(f"Unknown file handle method '.{method}()'")



    def emit_file_exists(self, node):
        path_val, path_t = self.emit_expr(node.path_expr)
        raw = self.new_tmp()
        cmp = self.new_tmp()
        self.emit(f"  {raw} = call i32 @file_exists(i8* {path_val})")
        self.emit(f"  {cmp} = icmp ne i32 {raw}, 0")
        return cmp, "i1"

    def emit_file_delete(self, node):
        path_val, path_t = self.emit_expr(node.path_expr)
        self.emit(f"  call i32 @file_delete(i8* {path_val})")

    def emit_file_rename(self, node):
        old_val, old_t = self.emit_expr(node.old_path)
        new_val, new_t = self.emit_expr(node.new_path)
        self.emit(f"  call i32 @file_rename_file(i8* {old_val}, i8* {new_val})")

    def emit_file_copy(self, node):
        src_val, src_t = self.emit_expr(node.src_path)
        dst_val, dst_t = self.emit_expr(node.dst_path)
        self.emit(f"  call i32 @file_copy_file(i8* {src_val}, i8* {dst_val})")

    def _os_run_core(self, node):
        """Shared logic: emit os_run() call, return (result_tmp, "i8*")"""
        null_lbl, null_len = self.intern_str("")
        null_ptr = self.new_tmp()
        self.emit(f"  {null_ptr} = getelementptr [{null_len} x i8], [{null_len} x i8]* {null_lbl}, i64 0, i64 0")
        if node.struct_args is not None:
            # os.run({ cmd: "...", args: [...], input: "..." })
            # Auto-start terminal 0 if not already started
            self.emit(f"  call void @os_start(i64 0)")
            # Build the command string: cmd + " " + joined args
            fields = node.struct_args
            cmd_v, cmd_t = self.emit_expr(fields["cmd"])
            cmd_s = self.coerce(cmd_v, cmd_t, "i8*")
            # If args present, build "cmd arg1 arg2 ..."
            if "args" in fields:
                args_node = fields["args"]
                args_v, args_t = self.emit_expr(args_node)
                args_b = self.coerce_to_box(args_v, args_t)
                # Build full cmd string by concatenating: cmd + " " + each arg
                sp_lbl, sp_len = self.intern_str(" ")
                sp_ptr = self.new_tmp()
                self.emit(f"  {sp_ptr} = getelementptr [{sp_len} x i8], [{sp_len} x i8]* {sp_lbl}, i64 0, i64 0")
                len_cmd = self.new_tmp(); len_args = self.new_tmp()
                len_v = self.new_tmp(); buf_sz = self.new_tmp(); buf = self.new_tmp()
                buf_ptr = self.new_tmp()
                self.emit(f"  {buf_ptr} = alloca i8*")
                self.emit(f"  {len_cmd} = call i64 @strlen(i8* {cmd_s})")
                # Concatenate all args: cmd + " " + arg0 + " " + arg1 + ...
                first_arg_b = self.new_tmp()
                self.emit(f"  {first_arg_b} = call %Box* @collection_get_at(%Box* {args_b}, i32 0)")
                first_arg_s = self.new_tmp()
                self.emit(f"  {first_arg_s} = call i8* @unbox_s(%Box* {first_arg_b})")
                self.emit(f"  {len_args} = call i64 @strlen(i8* {first_arg_s})")
                self.emit(f"  {len_v} = add i64 {len_cmd}, {len_args}")
                self.emit(f"  {buf_sz} = add i64 {len_v}, 2")  # space + null
                self.emit(f"  {buf} = call i8* @malloc(i64 {buf_sz})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {cmd_s})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {sp_ptr})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {first_arg_s})")
                self.emit(f"  store i8* {buf}, i8** {buf_ptr}")
                # Loop over remaining args and append each with a space
                idx_tmp = self.new_tmp()
                self.emit(f"  store i32 1, i32* {idx_tmp}")
                loop_lbl = self.new_label("osrun_args")
                loop_end = self.new_label("osrun_args_end")
                self.emit(f"  br label %{loop_lbl}")
                self.emit(f"{loop_lbl}:")
                i_val = self.new_tmp()
                self.emit(f"  {i_val} = load i32, i32* {idx_tmp}")
                i64_val = self.new_tmp()
                self.emit(f"  {i64_val} = sext i32 {i_val} to i64")
                len_box = self.new_tmp()
                self.emit(f"  {len_box} = call i64 @collection_len(%Box* {args_b})")
                cond = self.new_tmp()
                self.emit(f"  {cond} = icmp slt i64 {i64_val}, {len_box}")
                self.emit(f"  br i1 {cond}, label %{loop_lbl}_body, label %{loop_lbl}_end")
                self.emit(f"{loop_lbl}_body:")
                arg_b = self.new_tmp()
                self.emit(f"  {arg_b} = call %Box* @collection_get_at(%Box* {args_b}, i32 {i_val})")
                arg_s = self.new_tmp()
                self.emit(f"  {arg_s} = call i8* @unbox_s(%Box* {arg_b})")
                arg_len = self.new_tmp()
                self.emit(f"  {arg_len} = call i64 @strlen(i8* {arg_s})")
                cur_buf = self.new_tmp()
                self.emit(f"  {cur_buf} = load i8*, i8** {buf_ptr}")
                new_len = self.new_tmp()
                self.emit(f"  {new_len} = add i64 {len_v}, {arg_len}")
                new_sz = self.new_tmp()
                self.emit(f"  {new_sz} = add i64 {new_len}, 2")  # space + null
                new_buf = self.new_tmp()
                self.emit(f"  {new_buf} = call i8* @malloc(i64 {new_sz})")
                self.emit(f"  call i8* @strcpy(i8* {new_buf}, i8* {cur_buf})")
                self.emit(f"  call i8* @strcat(i8* {new_buf}, i8* {sp_ptr})")
                self.emit(f"  call i8* @strcat(i8* {new_buf}, i8* {arg_s})")
                self.emit(f"  call void @free(i8* {cur_buf})")
                self.emit(f"  store i8* {new_buf}, i8** {buf_ptr}")
                self.emit(f"  store i64 {new_len}, i64* {len_v}")
                next_i = self.new_tmp()
                self.emit(f"  {next_i} = add i32 {i_val}, 1")
                self.emit(f"  store i32 {next_i}, i32* {idx_tmp}")
                self.emit(f"  br label %{loop_lbl}")
                self.emit(f"{loop_lbl}_end:")
                self.emit(f"  br label %{loop_end}")
                self.emit(f"{loop_end}:")
                final_buf = self.new_tmp()
                self.emit(f"  {final_buf} = load i8*, i8** {buf_ptr}")
                cmd_s = final_buf
            input_s = null_ptr
            if "input" in fields:
                inp_v, inp_t = self.emit_expr(fields["input"])
                input_s = self.coerce(inp_v, inp_t, "i8*")
            # id is not needed for struct form — use terminal 0 by default
            res = self.new_tmp()
            self.emit(f"  {res} = call i8* @os_run(i64 0, i8* {cmd_s}, i8* {input_s})")
            return res, "i8*"
        else:
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            cmd_v, cmd_t = self.emit_expr(node.cmd_expr)
            cmd_s = self.coerce(cmd_v, cmd_t, "i8*")
            if node.input_expr is not None:
                inp_v, inp_t = self.emit_expr(node.input_expr)
                inp_s = self.coerce(inp_v, inp_t, "i8*")
            else:
                inp_s = null_ptr
            res = self.new_tmp()
            self.emit(f"  {res} = call i8* @os_run(i64 {id_v}, i8* {cmd_s}, i8* {inp_s})")
            return res, "i8*"

    def emit_os_run(self, node):
        """Statement form: os.run(...) — result discarded"""
        self._os_run_core(node)

    def emit_os_run_expr(self, node):
        """Expression form: let data = os.run(...) — result is string"""
        return self._os_run_core(node)

    def emit_ffi_bind(self, node):
        """
        FFI binding: fn lib symbol(params) -> ret
        Emits an LLVM function that:
          1. Calls ffi_sym(handle_idx, "symbol") to get the function pointer
          2. Bitcasts it to the right function type
          3. Calls it with the provided args
        We store the binding so calling symbol(args) works normally afterwards.
        """
        # Build param IR types
        param_ir_types = [self.rubi_type_to_ir(pt) for _, pt in node.params]
        ret_ir = self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64"
        fn_ptr_t = f"{ret_ir} ({', '.join(param_ir_types)})*" if param_ir_types else f"{ret_ir} ()*"
        fn_name = node.symbol_name
        params_ir = ", ".join(f"{pt} %p{i}" for i, pt in enumerate(param_ir_types))

        # Register immediately so calls in the same scope resolve
        self.functions[fn_name] = FnDef(fn_name, node.params, node.ret_type, [])

        # Buffer the wrapper — flush after current function closes (like trampolines)
        # so we don't emit a define inside another define
        pending = []
        pending.append(f"\ndefine {ret_ir} @{fn_name}({params_ir}) {{")
        pending.append("entry:")

        # Get handle index from the dedicated global slot written when FFI("path") ran.
        # Using the slot (not a local alloca) means the wrapper always finds it.
        slot_name = f"@_ffi_slot_{node.handle_name}"
        # Declare the slot if not yet declared (collect pass may run before emit_ffi_bind)
        if not any(d.startswith(slot_name) for d in self.global_decls):
            self.global_decls.append(f"@_ffi_slot_{node.handle_name} = global i64 -1")
        handle_var_v = f"%ffi_h_{self.new_tmp()[1:]}"
        pending.append(f"  {handle_var_v} = load i64, i64* {slot_name}")

        # Intern the symbol name string constant
        sym_lbl, sym_len = self.intern_str(node.symbol_name)
        sym_ptr_t = f"%ffi_sp_{self.new_tmp()[1:]}"
        pending.append(f"  {sym_ptr_t} = getelementptr [{sym_len} x i8], [{sym_len} x i8]* {sym_lbl}, i64 0, i64 0")

        # Get raw fn pointer as i64 via ffi_sym
        raw_fp = f"%ffi_raw_{self.new_tmp()[1:]}"
        pending.append(f"  {raw_fp} = call i64 @ffi_sym(i64 {handle_var_v}, i8* {sym_ptr_t})")

        # Null check — if symbol not resolved, skip the call and return zero/null
        ok_lbl  = f"ffi_ok_{self.new_tmp()[1:]}"
        bad_lbl = f"ffi_bad_{self.new_tmp()[1:]}"
        is_null = f"%ffi_null_{self.new_tmp()[1:]}"
        pending.append(f"  {is_null} = icmp eq i64 {raw_fp}, 0")
        pending.append(f"  br i1 {is_null}, label %{bad_lbl}, label %{ok_lbl}")
        pending.append(f"{bad_lbl}:")
        if ret_ir in ("float", "double"):
            pending.append(f"  ret {ret_ir} 0.0")
        elif ret_ir in ("i8*", "%Box*") or ret_ir.endswith("*"):
            pending.append(f"  ret {ret_ir} null")
        else:
            pending.append(f"  ret {ret_ir} 0")
        pending.append(f"{ok_lbl}:")

        # Bitcast i64 → fn ptr type
        fp_cast = f"%ffi_fp_{self.new_tmp()[1:]}"
        pending.append(f"  {fp_cast} = inttoptr i64 {raw_fp} to {fn_ptr_t}")

        # Call the foreign function
        args_str = ", ".join(f"{pt} %p{i}" for i, pt in enumerate(param_ir_types))
        call_tmp = f"%ffi_ret_{self.new_tmp()[1:]}"
        pending.append(f"  {call_tmp} = call {ret_ir} {fp_cast}({args_str})")
        pending.append(f"  ret {ret_ir} {call_tmp}")
        pending.append("}")

        self._pending_trampolines += pending

    def emit_expr(self, node):
        if isinstance(node, Number):
            if isinstance(node.value, float): return f"{node.value:.17e}", "double"
            return str(int(node.value)), "i64"
        if isinstance(node, Bool): return ("1" if node.value else "0"), "i1"
        if isinstance(node, None_): return self._NULL_SENTINEL, "i64"
        if isinstance(node, Str):
            lbl, blen = self.intern_str(node.value); ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{blen} x i8], [{blen} x i8]* {lbl}, i64 0, i64 0')
            return ptr, "i8*"
        if isinstance(node, InterpolatedStr):
            # Build result by concatenating all parts
            # Start with an empty string, then strcat each part
            # Pre-compute total length at runtime and malloc once
            # For simplicity: build iteratively with malloc+strcpy+strcat
            # First convert all parts to i8* strings
            part_vals = []
            for part in node.parts:
                pv, pt = self.emit_expr(part)
                pv = self.coerce_to_string(pv, pt)
                part_vals.append(pv)
            # Sum all lengths
            if len(part_vals) == 0:
                empty_lbl, elen = self.intern_str("")
                ep = self.new_tmp()
                self.emit(f'  {ep} = getelementptr [{elen} x i8], [{elen} x i8]* {empty_lbl}, i64 0, i64 0')
                return ep, "i8*"
            if len(part_vals) == 1:
                return part_vals[0], "i8*"
            # Sum lengths of all parts
            total = self.new_tmp()
            lens = [self.new_tmp() for _ in part_vals]
            for i, pv in enumerate(part_vals):
                self.emit(f"  {lens[i]} = call i64 @strlen(i8* {pv})")
            # Add them up
            acc = lens[0]
            for i in range(1, len(lens)):
                nacc = self.new_tmp()
                self.emit(f"  {nacc} = add i64 {acc}, {lens[i]}")
                acc = nacc
            total_plus1 = self.new_tmp()
            self.emit(f"  {total_plus1} = add i64 {acc}, 1")
            buf = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 {total_plus1})")
            # Copy first part
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {part_vals[0]})")
            # Concatenate remaining parts
            for pv in part_vals[1:]:
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {pv})")
            return buf, "i8*"

        if isinstance(node, ListExpr):
            lst = self.new_tmp()
            self.emit(f"  {lst} = call %Box* @make_list()")
            for e in node.elements:
                ev, et = self.emit_expr(e)
                eb = self.coerce_to_box(ev, et)
                self.emit(f"  call void @list_append(%Box* {lst}, %Box* {eb})")
            return lst, "%Box*"
        if isinstance(node, DictExpr):
            dct = self.new_tmp()
            self.emit(f"  {dct} = call %Box* @make_dict()")
            for k, v in node.pairs:
                kv, kt = self.emit_expr(k); vv, vt = self.emit_expr(v)
                kb = self.coerce_to_box(kv, kt); vb = self.coerce_to_box(vv, vt)
                self.emit(f"  call void @dict_set(%Box* {dct}, %Box* {kb}, %Box* {vb})")
            return dct, "%Box*"
        if isinstance(node, Input):
            if node.prompt is not None:
                pv, pt = self.emit_expr(node.prompt)
                if pt == "i8*":
                    fmt, flen = self.intern_str("%s"); ptr = self.new_tmp()
                    self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
                    self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i8* {pv})')
                    self.emit(f'  call i32 @fflush(i8* null)')
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @_rubidium_input_line()")
            return result, "i8*"

        if isinstance(node, ThreadRunning):
            return self.emit_thread_running(node)
        if isinstance(node, FileHandleStmt):
            return self.emit_file_handle_method(node.var_name, node.method, node.args)
        if isinstance(node, FileExists):
            return self.emit_file_exists(node)
        if isinstance(node, Var):
            if node.name in self.dropped_vars:
                # Runtime behavior: print error message instead of crash
                err_lbl, err_len = self.intern_str("ERROR: variable does not exist\\n")
                fmt_ptr = self.new_tmp()
                self.emit(f"  {fmt_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
                self.emit(f"  call i32 (i8*, ...) @printf(i8* {fmt_ptr})")
                return "0", "i64"
            
            # --- FIX: Implicitly access class field if it exists ---
            if self.is_class_field(node.name):
                return self.emit_field_access(Var("__self"), node.name)
            # -------------------------------------------------------
            
            ptr_str, ir_t = self.get_var_ptr(node.name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = load {ir_t}, {ir_t}* {ptr_str}")
            return tmp, ir_t
            
        if isinstance(node, FieldAccess): return self.emit_field_access(node.obj, node.field)
        if isinstance(node, BinOp): return self.emit_binop(node)
        if isinstance(node, Compare): return self.emit_compare(node)
        if isinstance(node, UnaryOp):
            val, t = self.emit_expr(node.value)
            if node.op == "-":
                tmp = self.new_tmp()
                if t in ("float","double"): self.emit(f"  {tmp} = fneg {t} {val}")
                else: self.emit(f"  {tmp} = sub {t} 0, {val}")
                return tmp, t
            if node.op == "*/":
                # Square root operator - call sqrt() from math library
                val_d = self.coerce(val, t, "double")
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call double @sqrt(double {val_d})")
                return tmp, "double"
            if node.op == "not":
                v = self.to_bool(val, t); tmp = self.new_tmp()
                self.emit(f"  {tmp} = xor i1 {v}, 1")
                return tmp, "i1"
            return val, t
        if isinstance(node, FnCall): return self.emit_call_expr(node)
        if isinstance(node, ClassInstantiate):
            struct_t = self.class_ir_type(node.class_name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call {struct_t}* @_{node.class_name}_new()")
            return tmp, f"{struct_t}*"
        if isinstance(node, MethodCall):
            if node.method == "set" and isinstance(node.obj, FnCall):
                self.emit_collection_set(node)
                return "0", "i64"
            if node.method == "add" and isinstance(node.obj, FnCall):
                self.emit_collection_add(node)
                return "0", "i64"
            return self.emit_method_call_expr(node)
        if isinstance(node, TypeCast): return self.emit_type_cast(node)
        if isinstance(node, FFILoad):
            path_v, path_t = self.emit_expr(node.path_expr)
            path_s = self.coerce(path_v, path_t, "i8*")
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @ffi_load(i8* {path_s})")
            return tmp, "i64"
        if isinstance(node, OsRun):
            return self.emit_os_run_expr(node)
        return "0", "i64"

    def emit_field_access(self, obj, field_name):
        # Extract the name if it's an ast.Var object
        obj_name = obj.name if hasattr(obj, 'name') else str(obj)
        
        if obj_name not in self.instances: 
            raise RubidiumNameError(f"'{obj_name}' is not an instance")
            
        class_name = self.instances[obj_name]
        idx, ir_t  = self.field_index(class_name, field_name)
        struct_t   = self.class_ir_type(class_name)
        
        ptr_str, _ = self.get_var_ptr(obj_name)
        inst_ptr = self.new_tmp(); fptr = self.new_tmp(); val = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        self.emit(f"  {val} = load {ir_t}, {ir_t}* {fptr}")
        return val, ir_t

    def emit_call_expr(self, node):
        if isinstance(node.name, str):
            node.name = node.name.replace(".", "_")

        # 1. PRIORITY 1: Hardcoded System Built-ins
        # If it's one of these, it CANNOT be a collection or class method.
        if node.name == "print":
            # Direct to your print logic
            val, t = self.emit_expr(node.args[0])
            self.emit_print(node.args[0])
            return "0", "i64"

        if node.name == "input":
            return self.emit_expr(Input(node.args[0] if node.args else None))

        # C library math functions
        c_math_fns = {"sin", "cos", "tan", "sqrt", "pow", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        if node.name in c_math_fns:
            args_ir = []
            for a in node.args:
                v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
            tmp = self.new_tmp()
            ret_ir = "double" if node.name in {"sin", "cos", "tan", "sqrt", "pow", "log", "log10", "exp", "fabs", "floor", "ceil", "round"} else "i64"
            # Use @_rubidium_pow for pow to avoid conflict with global variable
            fn_name = "@_rubidium_pow" if node.name == "pow" else f"@{node.name}"
            self.emit(f"  {tmp} = call {ret_ir} {fn_name}({', '.join(args_ir)})")
            return tmp, ret_ir

        if node.name == "random" and len(node.args) >= 2:
            # random(min, max, type) — generate random number in [min, max]
            # type arg (3rd) is optional and guides float vs int output
            min_v, min_t = self.emit_expr(node.args[0])
            max_v, max_t = self.emit_expr(node.args[1])
            type_name = ""
            if len(node.args) >= 3 and isinstance(node.args[2], Var):
                type_name = node.args[2].name
            is_float = type_name.startswith("f") or min_t in ("float","double") or max_t in ("float","double")
            if is_float:
                # (rand() / RAND_MAX) * (max - min) + min
                r_raw = self.new_tmp(); r_f = self.new_tmp()
                rmax_f = self.new_tmp(); ratio = self.new_tmp()
                range_v = self.new_tmp(); result = self.new_tmp()
                self.emit(f"  {r_raw} = call i32 @rand()")
                self.emit(f"  {r_f} = sitofp i32 {r_raw} to double")
                self.emit(f"  {rmax_f} = sitofp i32 2147483647 to double")
                self.emit(f"  {ratio} = fdiv double {r_f}, {rmax_f}")
                mn = self.coerce(min_v, min_t, "double")
                mx = self.coerce(max_v, max_t, "double")
                self.emit(f"  {range_v} = fsub double {mx}, {mn}")
                self.emit(f"  {result} = fmul double {ratio}, {range_v}")
                final = self.new_tmp()
                self.emit(f"  {final} = fadd double {result}, {mn}")
                return final, "double"
            else:
                # rand() % (max - min + 1) + min
                mn = self.coerce(min_v, min_t, "i64")
                mx = self.coerce(max_v, max_t, "i64")
                r_raw = self.new_tmp(); r_i64 = self.new_tmp()
                range_v = self.new_tmp(); range1 = self.new_tmp()
                rem = self.new_tmp(); result = self.new_tmp()
                self.emit(f"  {r_raw} = call i32 @rand()")
                self.emit(f"  {r_i64} = sext i32 {r_raw} to i64")
                self.emit(f"  {range_v} = sub i64 {mx}, {mn}")
                self.emit(f"  {range1} = add i64 {range_v}, 1")
                self.emit(f"  {rem} = srem i64 {r_i64}, {range1}")
                self.emit(f"  {result} = add i64 {rem}, {mn}")
                return result, "i64"


        if node.name == "thread" and len(node.args) == 2:
            func_call_node = node.args[0]
            tid_v, tid_t   = self.emit_expr(node.args[1])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            # Store handle: _thread_handles[tid]
            h_ptr = self.new_tmp()
            self.emit(f"  {h_ptr} = getelementptr [1024 x i64], [1024 x i64]* @_thread_handles, i64 0, i64 {tid_v}")

            fn_name = func_call_node.name if isinstance(func_call_node, FnCall) else str(func_call_node)
            call_args = func_call_node.args if isinstance(func_call_node, FnCall) else []
            fn_def = self.functions.get(fn_name)
            param_types = [self.rubi_type_to_ir(pt) for _, pt in fn_def.params] if (fn_def and fn_def.params) else []
            ret_ir = self.rubi_type_to_ir(fn_def.ret_type) if (fn_def and fn_def.ret_type) else "i64"
            tramp = f"_tramp_{fn_name}"

            # Marshal arguments (if any) into a heap-allocated struct for the trampoline
            if call_args and param_types:
                struct_t = "{" + ", ".join(param_types) + "}"
                size_ptr, size_int, raw_ptr, struct_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
                self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
                self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
                self.emit(f"  {struct_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
                for i, (arg, pt) in enumerate(zip(call_args, param_types)):
                    av, at = self.emit_expr(arg)
                    av = self.coerce(av, at, pt)
                    fptr = self.new_tmp()
                    self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {struct_ptr}, i32 0, i32 {i}")
                    self.emit(f"  store {pt} {av}, {pt}* {fptr}")
                arg_ptr_for_create = raw_ptr
            else:
                arg_ptr_for_create = "null"

            if tramp not in self.functions:
                tramp_lines = [f"define i8* @{tramp}(i8* %_arg) {{", "entry:"]
                if call_args and param_types:
                    struct_t = "{" + ", ".join(param_types) + "}"
                    tramp_lines.append(f"  %s = bitcast i8* %_arg to {struct_t}*")
                    call_arg_strs = []
                    for i, pt in enumerate(param_types):
                        tramp_lines.append(f"  %fp{i} = getelementptr {struct_t}, {struct_t}* %s, i32 0, i32 {i}")
                        tramp_lines.append(f"  %v{i} = load {pt}, {pt}* %fp{i}")
                        call_arg_strs.append(f"{pt} %v{i}")
                    tramp_lines.append(f"  call {ret_ir} @{fn_name}({', '.join(call_arg_strs)})")
                    tramp_lines.append(f"  call void @free(i8* %_arg)")
                else:
                    tramp_lines.append(f"  call {ret_ir} @{fn_name}()")
                tramp_lines += ["  ret i8* null", "}", ""]
                self._pending_trampolines += tramp_lines
                self.functions[tramp] = FnDef(tramp, [], None, [])

            tramp_ptr = self.new_tmp()
            self.emit(f"  {tramp_ptr} = bitcast i8* (i8*)* @{tramp} to i8* (i8*)*")
            self.emit(f"  call i32 @pthread_create(i64* {h_ptr}, i64* null, i8* (i8*)* {tramp_ptr}, i8* {arg_ptr_for_create})")
            return "0", "i64"

        if node.name == "thread_wait":
            for arg in node.args:
                tid_v, tid_t = self.emit_expr(arg)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
            return "0", "i64"

        if node.name == "thread_result":
            tid_v, tid_t = self.emit_expr(node.args[0])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            r_ptr = self.new_tmp(); r_val = self.new_tmp()
            self.emit(f"  {r_ptr} = getelementptr [1024 x %Box*], [1024 x %Box*]* @_thread_results, i64 0, i64 {tid_v}")
            self.emit(f"  {r_val} = load %Box*, %Box** {r_ptr}")
            return r_val, "%Box*"

        # 3. PRIORITY 3: Actual Functions (main, user functions)
        # We check if it exists in self.functions FIRST. 
        # This prevents main() or user-defined functions from being caught by the collection logic.
        target_name = node.name
        if f"main_{node.name}" in self.functions:
            target_name = f"main_{node.name}"
        
        if target_name in self.functions:
            args_ir = []
            for a in node.args:
                v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
            tmp = self.new_tmp()
            fn_ret = self.functions[target_name].ret_type
            ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
            self.emit(f"  {tmp} = call {ret_ir} @{target_name}({', '.join(args_ir)})")
            return tmp, ret_ir

        # 4. PRIORITY 4: Dynamic Collection Access
        # Only reach here if it's NOT a function, NOT a built-in
        # Check if variable exists in any local scope or global scope
        var_exists = any(node.name in scope for scope in self.local_vars_stack) or node.name in self.global_vars
        if var_exists:
            col_v, col_t = self.emit_expr(Var(node.name))
            col_b = self.coerce_to_box(col_v, col_t)
            # Chain multiple get calls for nested access: col("key", 2) -> col["key"][2]
            for arg in node.args:
                idx_v, idx_t = self.emit_expr(arg)
                idx_b = self.coerce_to_box(idx_v, idx_t)
                res = self.new_tmp()
                self.emit(f"  {res} = call %Box* @collection_get(%Box* {col_b}, %Box* {idx_b})")
                col_b = res
            return col_b, "%Box*"

        # 4.5 Class field access via call syntax: e.g., inv() inside class → loads field "inv"
        if self.is_class_field(node.name):
            field_val, field_t = self.emit_field_access(Var("__self"), node.name)
            # If args provided, treat as collection access on the field
            if node.args:
                col_b = self.coerce_to_box(field_val, field_t)
                for arg in node.args:
                    idx_v, idx_t = self.emit_expr(arg)
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    res = self.new_tmp()
                    self.emit(f"  {res} = call %Box* @collection_get(%Box* {col_b}, %Box* {idx_b})")
                    col_b = res
                return col_b, "%Box*"
            return field_val, field_t

        # 5. PRIORITY 5: Class instantiation (ClassName())
        if node.name in self.class_defs:
            struct_t = self.class_ir_type(node.name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call {struct_t}* @_{node.name}_new()")
            return tmp, f"{struct_t}*"

        raise RubidiumNameError(f"Undefined function or variable: {node.name}")

    def emit_method_call_expr(self, node):
        # 1. Resolve the object name
        obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)

        # 0. File handle method calls — intercept before any other logic
        if obj_name in self._file_handle_vars:
            return self.emit_file_handle_method(obj_name, node.method, node.args)

        # 0a. Handle thread.wait and thread.running on the 'thread' module
        if obj_name == "thread" and node.method == "wait":
            for texpr in node.args:
                tid_v, tid_t = self.emit_expr(texpr)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
            return "0", "i64"
        if obj_name == "thread" and node.method == "running":
            tid_v, tid_t = self.emit_expr(node.args[0])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            result = self.new_tmp()
            self.emit(f"  {result} = call i1 @_thread_is_running(i64 {tid_v})")
            return result, "i1"

        # 2. Handle .to() type conversion for any type (numeric, string, float)
        if node.method == "to" and isinstance(node.obj, Var) and node.args:
            val, val_t = self.emit_expr(node.obj)
            arg = node.args[0]
            if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                target_type = arg.name
            elif hasattr(arg, 'value'):
                target_type = arg.value
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            # Special handling for numeric->string
            if target_ir == "i8*":
                return self.coerce_to_string(val, val_t), "i8*"
            # If the object is a string (i8*) and we are converting to a non-string type, parse the string
            if val_t == "i8*" and target_ir != "i8*":
                if target_ir in self._INT_IR_SET:
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = call i64 @atol(i8* {val})")
                    # Now, if target_ir is not i64, we need to truncate or sign-extend
                    if target_ir == "i32":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = trunc i64 {tmp} to i32")
                        return tmp2, target_ir
                    elif target_ir == "i1":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = trunc i64 {tmp} to i1")
                        return tmp2, target_ir
                    elif target_ir == "i64":
                        return tmp, target_ir
                    else: # wider than i64
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = sext i64 {tmp} to {target_ir}")
                        return tmp2, target_ir
                elif target_ir in self._FLOAT_IR_SET:
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = call double @strtod(i8* {val}, i8* null)")
                    if target_ir == "float":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = fptrunc double {tmp} to float")
                        return tmp2, target_ir
                    elif target_ir == "double":
                        return tmp, target_ir
                    else: # fp128
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = fpext double {tmp} to fp128")
                        return tmp2, target_ir
            return self.coerce(val, val_t, target_ir), target_ir
        
        # 2. Handle Collection 'set' method separately
        # This bypasses the class method logic entirely
        if node.method == "set" and isinstance(node.obj, FnCall):
            return self.emit_collection_set(node)
        
        # 2b. Handle Collection 'add' method on FnCall (e.g., chars().add(c))
        if node.method == "add" and isinstance(node.obj, FnCall):
            return self.emit_collection_add(node)
        
        # 2a. Handle set on MethodCall (e.g., p.scores(0).set(99))
        if node.method == "set" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) - inner is MethodCall(Var('p'), 'scores', [Number(0)])
                    # We need to get the field value, then do collection_set with the index
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    # Get the index from inner.args[0]
                    idx_v, idx_t = self.emit_expr(inner.args[0])
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    
                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_b = self.coerce_to_box(val_v, val_t)
                    
                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2a2. Handle add on MethodCall (e.g., p.scores().add(50))
        if node.method == "add" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    if len(node.args) == 1:
                        val_v, val_t = self.emit_expr(node.args[0])
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
                    elif len(node.args) == 2:
                        key_v, key_t = self.emit_expr(node.args[0])
                        val_v, val_t = self.emit_expr(node.args[1])
                        key_b = self.coerce_to_box(key_v, key_t)
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2a2b. Handle add on Var where Var is a class field (e.g., scores().add(0) inside a class method)
        if node.method == "add" and isinstance(node.obj, Var) and self.is_class_field(obj_name):
            field_val, field_t = self.emit_field_access(Var("__self"), obj_name)
            col_b = self.coerce_to_box(field_val, field_t)
            
            if len(node.args) == 1:
                val_v, val_t = self.emit_expr(node.args[0])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @list_append(%Box* {col_b}, %Box* {val_b})")
            elif len(node.args) == 2:
                key_v, key_t = self.emit_expr(node.args[0])
                val_v, val_t = self.emit_expr(node.args[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
            return "0", "i64"

        # 2a2c. Handle set on Var where Var is a class field (e.g., scores(0).set(0) inside a class method)
        if node.method == "set" and isinstance(node.obj, Var) and self.is_class_field(obj_name):
            field_val, field_t = self.emit_field_access(Var("__self"), obj_name)
            col_b = self.coerce_to_box(field_val, field_t)
            
            # For scores(0).set(0), the args are [0, 0] - index and value
            if len(node.args) >= 2:
                idx_v, idx_t = self.emit_expr(node.args[0])
                idx_b = self.coerce_to_box(idx_v, idx_t)
                val_v, val_t = self.emit_expr(node.args[1])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
            return "0", "i64"

        # 2a3. Handle set on MethodCall where inner is a field access (e.g., p.scores(0).set(99))
        if node.method == "set" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) - inner is MethodCall(Var('p'), 'scores', [Number(0)])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    # Get the index from inner.args[0]
                    idx_v, idx_t = self.emit_expr(inner.args[0])
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    
                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_b = self.coerce_to_box(val_v, val_t)
                    
                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2b. Handle FieldAccess.set(value) for class fields (e.g., class_one.obj_val.set(99))
        if node.method == "set" and isinstance(node.obj, FieldAccess):
            return self.emit_field_assign(FieldAssign(node.obj.obj, node.obj.field, node.args[0]))

        # 2b. Handle built-in module method calls: time.sleep, random.shuffle, random.choice
        if obj_name == "time":
            if node.method == "sleep":
                secs_v, secs_t = self.emit_expr(node.args[0])
                secs_v = self.coerce(secs_v, secs_t, "i64")
                sec_i32 = self.new_tmp()
                self.emit(f"  {sec_i32} = trunc i64 {secs_v} to i32")
                self.emit(f"  call i32 @sleep(i32 {sec_i32})")
                return "0", "i64"
            if node.method == "wait":
                # Syntax says time.wait(n) waits for n seconds - use sleep, not usleep
                secs_v, secs_t = self.emit_expr(node.args[0])
                secs_v = self.coerce(secs_v, secs_t, "i64")
                sec_i32 = self.new_tmp()
                self.emit(f"  {sec_i32} = trunc i64 {secs_v} to i32")
                self.emit(f"  call i32 @sleep(i32 {sec_i32})")
                return "0", "i64"
            if node.method == "timer_start":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                # type_hint is second arg but we just need it for type checking
                self.emit(f"  call void @time_timer_start(i64 {tid_v}, double 0.0)")
                return "0", "i64"
            if node.method == "timer_pause":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @time_timer_pause(i64 {tid_v})")
                return "0", "i64"
            if node.method == "timer_stop":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @time_timer_stop(i64 {tid_v})")
                return "0", "i64"
            if node.method == "timer_read":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call double @time_timer_read(i64 {tid_v})")
                return tmp, "double"

        if obj_name == "random":
            if node.method == "shuffle":
                # random.shuffle(list) — Fisher-Yates shuffle on collection
                col_v, col_t = self.emit_expr(node.args[0])
                col_b = self.coerce_to_box(col_v, col_t)
                len_v = self.new_tmp()
                self.emit(f"  {len_v} = call i32 @collection_len(%Box* {col_b})")
                idx_ptr = self.new_tmp()
                self.emit(f"  {idx_ptr} = alloca i32")
                self.emit(f"  store i32 0, i32* {idx_ptr}")
                cond_l, body_l, end_l = self.new_label("sfl_cond"), self.new_label("sfl_body"), self.new_label("sfl_end")
                self.emit(f"  br label %{cond_l}\n{cond_l}:")
                cur_i, cond = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur_i} = load i32, i32* {idx_ptr}")
                self.emit(f"  {cond} = icmp slt i32 {cur_i}, {len_v}")
                self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
                # pick random j in [i, len)
                range_v, r_raw, j_v, j_add = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {range_v} = sub i32 {len_v}, {cur_i}")
                self.emit(f"  {r_raw} = call i32 @rand()")
                self.emit(f"  {j_v} = srem i32 {r_raw}, {range_v}")
                self.emit(f"  {j_add} = add i32 {j_v}, {cur_i}")
                # Safe swap using list_swap (no ownership issues)
                self.emit(f"  call void @list_swap(%Box* {col_b}, i32 {cur_i}, i32 {j_add})")
                inc_i = self.new_tmp()
                self.emit(f"  {inc_i} = add i32 {cur_i}, 1")
                self.emit(f"  store i32 {inc_i}, i32* {idx_ptr}")
                self.emit(f"  br label %{cond_l}\n{end_l}:")
                return "0", "i64"

            if node.method == "seed":
                seed_v, seed_t = self.emit_expr(node.args[0])
                if seed_t == "i8*":
                    seed_hash = self.new_tmp()
                    self.emit(f"  {seed_hash} = call i32 @_rub_str_hash(i8* {seed_v})")
                    self.emit(f"  call void @srand(i32 {seed_hash})")
                else:
                    seed_i = self.coerce(seed_v, seed_t, "i64")
                    seed_i32 = self.new_tmp()
                    self.emit(f"  {seed_i32} = trunc i64 {seed_i} to i32")
                    self.emit(f"  call void @srand(i32 {seed_i32})")
                return "0", "i64"

            if node.method == "choice":
                # random.choice(collection) — pick random element/key
                col_v, col_t = self.emit_expr(node.args[0])
                col_b = self.coerce_to_box(col_v, col_t)
                len_v = self.new_tmp()
                self.emit(f"  {len_v} = call i32 @collection_len(%Box* {col_b})")
                # Guard: if len == 0 return box_i(0) to avoid division by zero
                safe_l, zero_l, merge_l = self.new_label("choice_safe"), self.new_label("choice_zero"), self.new_label("choice_merge")
                cmp_v = self.new_tmp()
                self.emit(f"  {cmp_v} = icmp sgt i32 {len_v}, 0")
                self.emit(f"  br i1 {cmp_v}, label %{safe_l}, label %{zero_l}\n{safe_l}:")
                r_raw, r_idx = self.new_tmp(), self.new_tmp()
                self.emit(f"  {r_raw} = call i32 @rand()")
                self.emit(f"  {r_idx} = srem i32 {r_raw}, {len_v}")
                result_safe = self.new_tmp()
                self.emit(f"  {result_safe} = call %Box* @collection_get_at(%Box* {col_b}, i32 {r_idx})")
                result_copy = self.new_tmp()
                self.emit(f"  {result_copy} = call %Box* @box_copy(%Box* {result_safe})")
                self.emit(f"  br label %{merge_l}\n{zero_l}:")
                zero_box = self.new_tmp()
                self.emit(f"  {zero_box} = call %Box* @box_i(i64 0)")
                self.emit(f"  br label %{merge_l}\n{merge_l}:")
                result = self.new_tmp()
                self.emit(f"  {result} = phi %Box* [ {result_copy}, %{safe_l} ], [ {zero_box}, %{zero_l} ]")
                return result, "%Box*"

        # 2c. Handle MethodCall on class instances where method is actually a field (e.g., p.scores(0))
        # This must come BEFORE the class method check
        if isinstance(node.obj, Var) and obj_name in self.instances:
            class_name = self.instances[obj_name]
            cls = self.class_defs.get(class_name)
            if cls and any(f.name == node.method for f in cls.fields):
                # This is a field access on a class instance - get the field value
                field_val, field_t = self.emit_field_access(node.obj, node.method)
                if node.args:
                    # Collection access on the field - e.g., p.scores(0) or p.scores().add(50)
                    # For p.scores(0), this returns the element at index 0
                    # For p.scores().add(50), this is handled by the outer MethodCall
                    # Here we just need to handle p.scores(0) as a collection get
                    if node.method in ("set", "add"):
                        # This is handled by the outer MethodCall, so we shouldn't reach here
                        pass
                    else:
                        # p.scores(0) - get element at index
                        idx_v, idx_t = self.emit_expr(node.args[0])
                        idx_b = self.coerce_to_box(idx_v, idx_t)
                        res = self.new_tmp()
                        self.emit(f"  {res} = call %Box* @collection_get(%Box* {field_val}, %Box* {idx_b})")
                        return res, "%Box*"
                return field_val, field_t

        # 2d. Handle FieldAccess on class instances (e.g., p.scores(0).set(99))
        if isinstance(node.obj, FieldAccess):
            field_obj = node.obj.obj
            field_name = node.obj.field
            field_obj_name = field_obj.name if hasattr(field_obj, 'name') else str(field_obj)
            if field_obj_name in self.instances:
                class_name = self.instances[field_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == field_name for f in cls.fields):
                    # This is a field access on a class instance - get the field value
                    field_val, field_t = self.emit_field_access(field_obj, field_name)
                    if node.method in ("set", "add"):
                        # Collection operation on a class field
                        if node.method == "set":
                            return self.emit_collection_set_on_field(node, field_val, field_t)
                        else:
                            return self.emit_collection_add_on_field(node, field_val, field_t)
                    # For other methods, treat as method call on the field value
                    # Create a synthetic MethodCall node to continue processing
                    node.obj = Var(f"_field_temp_{field_name}")
                    # Store field value in a temp and continue
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = alloca {field_t}")
                    self.emit(f"  store {field_t} {field_val}, {field_t}* {tmp}")
                    # Continue to fallback handling below

        # 3. Handle Class Method Calls
        if obj_name in self.instances:
            class_name = self.instances[obj_name]
            mangled = self.method_ir_name(class_name, node.method)

            if mangled in self.functions:
                fn = self.functions[mangled]
                struct_t = self.class_ir_type(class_name)
                ptr_str, _ = self.get_var_ptr(obj_name)

                inst_ptr = self.new_tmp()
                self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")

                args_ir = [f"{struct_t}* {inst_ptr}"]
                for i, arg_node in enumerate(node.args):
                    v, t = self.emit_expr(arg_node)
                    if i + 1 < len(fn.params):
                        expected_t = self.rubi_type_to_ir(fn.params[i + 1][1])
                        v = self.coerce(v, t, expected_t)
                        args_ir.append(f"{expected_t} {v}")
                    else:
                        args_ir.append(f"{t} {v}")

                ret_t = self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                tmp = self.new_tmp()
                if ret_t == "void":
                    self.emit(f"  call void @{mangled}({', '.join(args_ir)})")
                    return "0", "i64"
                self.emit(f"  {tmp} = call {ret_t} @{mangled}({', '.join(args_ir)})")
                return tmp, ret_t
            else:
                raise RubidiumNameError(f"Class '{class_name}' has no method '{node.method}'")

        # 4. Fallback: String method OR module function call (e.g. math.add -> math_add)
        # Check for module-namespaced function call before evaluating the object
        if isinstance(node.obj, Var):
            target_name = f"{obj_name}_{node.method}"
            if target_name in self.functions:
                args_ir = []
                for a in node.args:
                    v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
                tmp = self.new_tmp()
                fn_ret = self.functions[target_name].ret_type
                ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
                self.emit(f"  {tmp} = call {ret_ir} @{target_name}({', '.join(args_ir)})")
                return tmp, ret_ir

            # FFI bindings: c_lib.my_c_func -> method name alone is the bound symbol
            if node.method in self.functions:
                args_ir = []
                for a in node.args:
                    v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
                tmp = self.new_tmp()
                fn_ret = self.functions[node.method].ret_type
                ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
                self.emit(f"  {tmp} = call {ret_ir} @{node.method}({', '.join(args_ir)})")
                return tmp, ret_ir

        # 4.5 Handle namespace.var.method() - e.g., wrap.glfw.glfwInit()
        # Resolves FieldAccess(Var(ns), attr).method() by trying ns_attr_method, ns_method, method
        if isinstance(node.obj, FieldAccess) and isinstance(node.obj.obj, Var):
            ns_name  = node.obj.obj.name
            attr_name = node.obj.field
            for target in (f"{ns_name}_{attr_name}_{node.method}", f"{ns_name}_{node.method}", node.method):
                if target in self.functions:
                    args_ir = []
                    for a in node.args:
                        v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
                    tmp = self.new_tmp()
                    fn_ret = self.functions[target].ret_type
                    ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
                    self.emit(f"  {tmp} = call {ret_ir} @{target}({', '.join(args_ir)})")
                    return tmp, ret_ir

        obj_val, obj_t = self.emit_expr(node.obj)
        
        # Handle .to() for numeric types (i64, i32, f64, f32, etc.)
        if node.method == "to" and obj_t in ("i32", "i64", "i1", "float", "double"):
            if len(args) == 1:
                # Check if arg is a Var with type-looking name (str, i32, etc.)
                arg = args[0]
                if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                    target_type = arg.name
                elif hasattr(arg, 'value'):
                    target_type = arg.value
                else:
                    target_type = "i64"
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            # Special handling for numeric->string
            if target_ir == "i8*":
                return self.coerce_to_string(obj_val, obj_t), "i8*"
            return self.coerce(obj_val, obj_t, target_ir), target_ir
        
        # Collection .has() check — must come before string dispatch
        if obj_t == "%Box*" and node.method == "has" and node.args:
            needle_v, needle_t = self.emit_expr(node.args[0])
            needle_b = self.coerce_to_box(needle_v, needle_t)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i1 @collection_has(%Box* {obj_val}, %Box* {needle_b})")
            return tmp, "i1"
        
        # Collection .len() check
        if obj_t == "%Box*" and node.method == "len" and not node.args:
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i32 @collection_len(%Box* {obj_val})")
            return tmp, "i32"

        # Collection .combine() — join all items as a string
        if obj_t == "%Box*" and node.method == "combine" and not node.args:
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i8* @list_combine(%Box* {obj_val})")
            return tmp, "i8*"
        
        if obj_t == "i8*":
            # Emit as string method; raises a clear RubidiumNameError on unknown methods
            # instead of silently falling through to a confusing "Undefined function" error.
            known_str_methods = ("len", "to_int", "contains", "slice", "split", "concat", "combine", "has", "to", "char", "set", "insert", "replace")
            if node.method not in known_str_methods:
                raise RubidiumNameError(
                    f"Unknown string method '.{node.method}()'. "
                    f"Available string methods: {', '.join(known_str_methods)}"
                )
            return self.emit_string_method(obj_val, node.method, node.args)

        # Not a string — treat as a module-namespaced function call (e.g. math.add -> math_add)
        target_name = f"{obj_name}_{node.method}"
        if target_name in self.functions:
            args_ir = []
            for a in node.args:
                v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
            tmp = self.new_tmp()
            fn_ret = self.functions[target_name].ret_type
            ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
            self.emit(f"  {tmp} = call {ret_ir} @{target_name}({', '.join(args_ir)})")
            return tmp, ret_ir
        node.name = target_name
        node.obj = None
        return self.emit_call_expr(node)

    def emit_string_method(self, obj_val, method, args):
        if method == "len":
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @strlen(i8* {obj_val})")
            return tmp, "i64"
        if method == "char" and len(args) == 1:
            # str.char(1) returns the character at 1-based index
            idx_v, idx_t = self.emit_expr(args[0])
            idx_i = self.coerce(idx_v, idx_t, "i64")
            char_ptr = self.new_tmp()
            self.emit(f"  {char_ptr} = getelementptr i8, i8* {obj_val}, i64 {idx_i}")
            char_val = self.new_tmp()
            self.emit(f"  {char_val} = load i8, i8* {char_ptr}")
            # Return as a single-char string by allocating and storing
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @malloc(i64 2)")
            char_store_ptr = self.new_tmp()
            self.emit(f"  {char_store_ptr} = getelementptr i8, i8* {result}, i64 0")
            self.emit(f"  store i8 {char_val}, i8* {char_store_ptr}")
            # Store null terminator
            next_ptr = self.new_tmp()
            self.emit(f"  {next_ptr} = getelementptr i8, i8* {result}, i64 1")
            self.emit(f"  store i8 0, i8* {next_ptr}")
            return result, "i8*"
        if method == "contains" and len(args) == 1:
            needle, _ = self.emit_expr(args[0])
            strstr_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {strstr_r} = call i8* @strstr(i8* {obj_val}, i8* {needle})")
            self.emit(f"  {tmp} = icmp ne i8* {strstr_r}, null")
            return tmp, "i1"
        if method == "has" and len(args) == 1:
            needle, _ = self.emit_expr(args[0])
            strstr_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {strstr_r} = call i8* @strstr(i8* {obj_val}, i8* {needle})")
            self.emit(f"  {tmp} = icmp ne i8* {strstr_r}, null")
            return tmp, "i1"
        if method == "to" or method == "to_int":
            if len(args) == 1:
                target_type = args[0].name if hasattr(args[0], 'name') else (args[0].value if hasattr(args[0], 'value') else "i64")
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            tmp = self.new_tmp()
            if target_ir in ("i1", "i32", "i64"):
                self.emit(f"  {tmp} = call i64 @atol(i8* {obj_val})")
                tmp = self.coerce(tmp, "i64", target_ir)
            elif target_ir in ("float", "double", "fp128"):
                self.emit(f"  {tmp} = call double @strtod(i8* {obj_val}, i8* null)")
            elif target_ir == "i8*":
                return obj_val, "i8*"
            return tmp, target_ir
        if method == "slice" and len(args) == 2:
            start_v, start_t = self.emit_expr(args[0]); end_v, end_t = self.emit_expr(args[1])
            start_v = self.coerce(start_v, start_t, "i64"); end_v = self.coerce(end_v, end_t, "i64")
            length, src_ptr, result = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {length}  = sub i64 {end_v}, {start_v}")
            self.emit(f"  {src_ptr} = getelementptr i8, i8* {obj_val}, i64 {start_v}")
            self.emit(f"  {result}  = call i8* @strndup(i8* {src_ptr}, i64 {length})")
            return result, "i8*"
        if method == "concat" and len(args) == 1:
            other, _ = self.emit_expr(args[0])
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {other})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {obj_val})")
            self.emit(f"  call i8* @strcat(i8* {buf}, i8* {other})")
            return buf, "i8*"
        if method == "split" and len(args) == 1:
            delim, _ = self.emit_expr(args[0])
            tok_ptr = self.new_tmp()
            self.emit(f"  {tok_ptr} = call i8* @strstr(i8* {obj_val}, i8* {delim})")
            result, dist, o_int, t_int = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {o_int}  = ptrtoint i8* {obj_val} to i64")
            self.emit(f"  {t_int}  = ptrtoint i8* {tok_ptr} to i64")
            self.emit(f"  {dist}   = sub i64 {t_int}, {o_int}")
            self.emit(f"  {result} = call i8* @strndup(i8* {obj_val}, i64 {dist})")
            return result, "i8*"
        if method == "combine" and len(args) == 1:
            other, _ = self.emit_expr(args[0])
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {other})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {obj_val})")
            self.emit(f"  call i8* @strcat(i8* {buf}, i8* {other})")
            return buf, "i8*"
        if method == "set" and len(args) == 2:
            idx_v, idx_t = self.emit_expr(args[0])
            val_v, val_t = self.emit_expr(args[1])
            idx_i = self.coerce(idx_v, idx_t, "i64")
            char_val = self.new_tmp()
            self.emit(f"  {char_val} = load i8, i8* {val_v}")
            llen, total, buf = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen} = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {total} = add i64 {llen}, 1")
            self.emit(f"  {buf} = call i8* @malloc(i64 {total})")
            self.emit(f"  call void @strcpy(i8* {buf}, i8* {obj_val})")
            char_ptr = self.new_tmp()
            self.emit(f"  {char_ptr} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  store i8 {char_val}, i8* {char_ptr}")
            return buf, "i8*"
        if method == "insert" and len(args) == 2:
            idx_v, idx_t = self.emit_expr(args[0])
            val_v, val_t = self.emit_expr(args[1])
            idx_i = self.coerce(idx_v, idx_t, "i64")
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {val_v})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call void @strncpy(i8* {buf}, i8* {obj_val}, i64 {idx_i})")
            null_pos = self.new_tmp()
            self.emit(f"  {null_pos} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  store i8 0, i8* {null_pos}")
            val_ptr = self.new_tmp()
            self.emit(f"  {val_ptr} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  call void @strcpy(i8* {val_ptr}, i8* {val_v})")
            src_ptr = self.new_tmp()
            self.emit(f"  {src_ptr} = getelementptr i8, i8* {obj_val}, i64 {idx_i}")
            self.emit(f"  call void @strcat(i8* {buf}, i8* {src_ptr})")
            return buf, "i8*"
        if method == "replace" and len(args) == 2:
            old_v, old_t = self.emit_expr(args[0])
            new_v, new_t = self.emit_expr(args[1])
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @str_replace(i8* {obj_val}, i8* {old_v}, i8* {new_v})")
            return result, "i8*"
        if method == "slice" and len(args) == 0:
            result = self.new_tmp()
            self.emit(f"  {result} = call %Box* @str_slice(i8* {obj_val})")
            return result, "%Box*"
        return "0", "i64"

    def emit_type_cast(self, node):
        val, from_t = self.emit_expr(node.expr)
        to_t = self.rubi_type_to_ir(node.target_type)
        tmp = self.new_tmp()
        if from_t == to_t: return val, to_t
        # Box* casts: delegate to coerce() which handles unbox_f/unbox_i/unbox_s
        if from_t == "%Box*":
            return self.coerce(val, from_t, to_t), to_t
        # Integer ↔ Integer — delegate to coerce() which handles all widths
        if from_t in self._INT_IR_SET and to_t in self._INT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        # Integer ↔ Float
        if from_t in self._INT_IR_SET and to_t in self._FLOAT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        if from_t in self._FLOAT_IR_SET and to_t in self._INT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        # Float ↔ Float
        if from_t in self._FLOAT_IR_SET and to_t in self._FLOAT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        if from_t in self._INT_IR_SET and to_t == "i8*":
            buf, fmt_ptr = self.new_tmp(), self.new_tmp()
            fmt_lbl, flen = self.intern_str("%lld")
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            cv = self.coerce(val, from_t, "i64")
            self.emit(f"  call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, i64 {cv})")
            return buf, "i8*"
        if from_t in ("float","double") and to_t == "i8*":
            buf, fmt_ptr = self.new_tmp(), self.new_tmp()
            fmt_lbl, flen = self.intern_str("%g")
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            dv = self.coerce(val, from_t, "double")
            self.emit(f"  call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, double {dv})")
            return buf, "i8*"
        if from_t == "i8*" and to_t in ("i1","i64"):
            t2 = self.new_tmp()
            self.emit(f"  {t2} = call i64 @atol(i8* {val})")
            return self.coerce(t2, "i64", to_t), to_t
        return val, to_t

    def emit_binop(self, node):
        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)

        # If either side is Box*, unbox to a concrete type first.
        # For arithmetic ops we unbox to numeric; for + we check box type at runtime.
        # Simple approach: unbox Box* to i64 for arithmetic, to i8* for string context.
        # For +, if the other operand is i8* or the box might be a string, use box_to_cstr.
        if node.op == "+":
            # Unbox any Box* to string when mixed with i8*
            if lt == "%Box*" and rt == "i8*":
                l = self.coerce_to_string(l, lt); lt = "i8*"
            elif lt == "i8*" and rt == "%Box*":
                r = self.coerce_to_string(r, rt); rt = "i8*"
            elif lt == "%Box*" and rt == "%Box*":
                # Both boxed — convert both to string (covers int+int→concat in string context)
                l = self.coerce_to_string(l, lt); lt = "i8*"
                r = self.coerce_to_string(r, rt); rt = "i8*"
        # For non-string arithmetic, unbox Box* to i64/double
        if lt == "%Box*" and node.op not in ("+",) :
            l = self.coerce(l, lt, "i64"); lt = "i64"
        if rt == "%Box*" and node.op not in ("+",):
            r = self.coerce(r, rt, "i64"); rt = "i64"
        # After unboxing, re-check: if both are now i64 and op is +, it's numeric add (correct)
        # If one is still Box* after + special-casing above, unbox to i64
        if lt == "%Box*": l = self.coerce(l, lt, "i64"); lt = "i64"
        if rt == "%Box*": r = self.coerce(r, rt, "i64"); rt = "i64"

        if node.op == "+":
            # String + String -> String concatenation
            if lt == "i8*" and rt == "i8*":
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {r})")
                return buf, "i8*"
            # String + Other -> Convert other to string and concatenate  
            if lt == "i8*" and rt != "i8*":
                str_r = self.coerce_to_string(r, rt)
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {str_r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {str_r})")
                return buf, "i8*"
            # Other + String -> Convert other to string and concatenate
            if lt != "i8*" and rt == "i8*":
                str_l = self.coerce_to_string(l, lt)
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {str_l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {str_l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {r})")
                return buf, "i8*"
        common = "double" if (lt in ("float","double") or rt in ("float","double")) else "i64"
        l = self.coerce(l, lt, common); r = self.coerce(r, rt, common)
        tmp = self.new_tmp()
        if common == "i64":
            if node.op in ("and","or"):
                li = self.to_bool(l, "i64"); ri = self.to_bool(r, "i64")
                instr = "and" if node.op == "and" else "or"
                self.emit(f"  {tmp} = {instr} i1 {li}, {ri}")
                return tmp, "i1"
            instr = {"+":"add","-":"sub","*":"mul","/":"sdiv","%":"srem"}.get(node.op)
            if instr:
                if node.op in ("/", "%") and self._try_error_label:
                    # Runtime div-by-zero guard: branch to on_error instead of faulting
                    is_zero = self.new_tmp()
                    safe_l  = self.new_label("divok")
                    self.emit(f"  {is_zero} = icmp eq i64 {r}, 0")
                    self.emit(f"  br i1 {is_zero}, label %{self._try_error_label}, label %{safe_l}")
                    self.emit(f"{safe_l}:")
                self.emit(f"  {tmp} = {instr} i64 {l}, {r}")
                return tmp, "i64"
            # Handle power operator (**)
            if node.op == "**":
                # Call pow() from math library
                l_d = self.coerce(l, "i64", "double")
                r_d = self.coerce(r, "i64", "double")
                self.emit(f"  {tmp} = call double @_rubidium_pow(double {l_d}, double {r_d})")
                return tmp, "double"
        else:
            instr = {"+":"fadd","-":"fsub","*":"fmul","/":"fdiv"}.get(node.op)
            if instr:
                self.emit(f"  {tmp} = {instr} double {l}, {r}")
                return tmp, "double"
            # Handle power operator (**) for float types
            if node.op == "**":
                self.emit(f"  {tmp} = call double @_rubidium_pow(double {l}, double {r})")
                return tmp, "double"
        return "0", "i64"

    def emit_compare(self, node):
        # Null comparisons: Null behaves as negative infinity per spec.
        left_is_null  = isinstance(node.left,  None_)
        right_is_null = isinstance(node.right, None_)
        if left_is_null or right_is_null:
            # Null op Null — always constant fold
            if left_is_null and right_is_null:
                result = {"==": True, "!=": False, "<": False, ">": False, "<=": True, ">=": True}[node.op]
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = add i1 0, {'1' if result else '0'}")
                return tmp, "i1"
            # Null op Literal (Number/Str/Bool/UnaryOp negation) — can constant fold safely
            non_null = node.right if left_is_null else node.left
            if isinstance(non_null, (Number, Str, Bool, UnaryOp)):
                if left_is_null:
                    result = {"==": False, "!=": True, "<": True, ">": False, "<=": True, ">=": False}[node.op]
                else:
                    result = {"==": False, "!=": True, "<": False, ">": True, "<=": False, ">=": True}[node.op]
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = add i1 0, {'1' if result else '0'}")
                return tmp, "i1"
            # Null op Variable — fall through to runtime comparison.
            # Variable assigned Null stores 0, so 0 == 0 → True correctly for == and !=.
            # Note: Null < variable (ordinal) won't work correctly without runtime tagging (see bugs.log).

        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)
        if lt == "i8*" and rt == "i8*":
            cmp_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cmp_r} = call i32 @strcmp(i8* {l}, i8* {r})")
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i32 {cmp_r}, 0")
            return tmp, "i1"
        # Handle string vs Null comparison
        if (lt == "i8*" and rt == "i64") or (lt == "i64" and rt == "i8*"):
            tmp = self.new_tmp()
            if lt == "i8*":
                ptr = l
                null_val = "null"
            else:
                ptr = r
                null_val = "null"
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i8* {ptr}, {null_val}")
            return tmp, "i1"
        common = "double" if (lt in ("float","double") or rt in ("float","double")) else "i64"
        l = self.coerce(l, lt, common); r = self.coerce(r, rt, common)
        tmp = self.new_tmp()
        if common == "double":
            pred = {"==":"oeq","!=":"one","<":"olt",">":"ogt","<=":"ole",">=":"oge"}[node.op]
            self.emit(f"  {tmp} = fcmp {pred} double {l}, {r}")
        else:
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i64 {l}, {r}")
        return tmp, "i1"

    def coerce_to_box(self, val, t):
        if t == "%Box*": return val
        tmp = self.new_tmp()
        if t in ("i1", "i32", "i64"):
            v = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = call %Box* @box_i(i64 {v})")
        elif t in ("float", "double"):
            v = self.coerce(val, t, "double")
            self.emit(f"  {tmp} = call %Box* @box_f(double {v})")
        elif t == "i8*":
            self.emit(f"  {tmp} = call %Box* @box_s(i8* {val})")
        else:
            v = self.new_tmp()
            self.emit(f"  {v} = bitcast {t} {val} to i8*")
            self.emit(f"  {tmp} = call %Box* @box_p(i8* {v})")
        return tmp

    def coerce_to_string(self, val, t):
        """Convert a value of any type to a string (i8*)"""
        if t == "i8*":
            return val
        tmp = self.new_tmp()
        buf = self.new_tmp()
        if t == "%Box*":
            self.emit(f"  {tmp} = call i8* @box_to_cstr(%Box* {val})")
            return tmp
        if t == "i1":
            true_lbl, true_len = self.intern_str("True")
            false_lbl, false_len = self.intern_str("False")
            true_ptr = self.new_tmp(); false_ptr = self.new_tmp(); sel_ptr = self.new_tmp()
            self.emit(f"  {true_ptr} = getelementptr [{true_len} x i8], [{true_len} x i8]* {true_lbl}, i64 0, i64 0")
            self.emit(f"  {false_ptr} = getelementptr [{false_len} x i8], [{false_len} x i8]* {false_lbl}, i64 0, i64 0")
            self.emit(f"  {sel_ptr} = select i1 {val}, i8* {true_ptr}, i8* {false_ptr}")
            return sel_ptr
        if t in ("i32", "i64"):
            fmt_lbl, flen = self.intern_str("%lld")
            fmt_ptr = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            cv = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, i64 {cv})")
            return buf
        elif t in ("float", "double"):
            fmt_lbl, flen = self.intern_str("%g")
            fmt_ptr = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            dv = self.coerce(val, t, "double")
            self.emit(f"  {tmp} = call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, double {dv})")
            return buf
        else:
            return val

    def coerce(self, val, from_t, to_t):
        if from_t == to_t: return val
        tmp = self.new_tmp()

        # ---- Unbox Box* to concrete type ----
        if from_t == "%Box*":
            if to_t in self._INT_IR_SET:
                # Unbox to i64 then widen/narrow
                i64_tmp = self.new_tmp()
                self.emit(f"  {i64_tmp} = call i64 @unbox_i(%Box* {val})")
                if to_t == "i64": return i64_tmp
                if to_t == "i1":
                    self.emit(f"  {tmp} = trunc i64 {i64_tmp} to i1"); return tmp
                if to_t == "i32":
                    self.emit(f"  {tmp} = trunc i64 {i64_tmp} to i32"); return tmp
                # Wider than i64 — sext
                self.emit(f"  {tmp} = sext i64 {i64_tmp} to {to_t}"); return tmp
            if to_t in self._FLOAT_IR_SET:
                dbl_tmp = self.new_tmp()
                self.emit(f"  {dbl_tmp} = call double @unbox_f(%Box* {val})")
                if to_t == "double": return dbl_tmp
                if to_t == "float":
                    self.emit(f"  {tmp} = fptrunc double {dbl_tmp} to float"); return tmp
                if to_t == "fp128":
                    self.emit(f"  {tmp} = fpext double {dbl_tmp} to fp128"); return tmp
                return dbl_tmp
            if to_t == "i8*":
                self.emit(f"  {tmp} = call i8* @unbox_s(%Box* {val})"); return tmp
            # Pointer cast
            p_tmp = self.new_tmp()
            self.emit(f"  {p_tmp} = call i8* @unbox_p(%Box* {val})")
            self.emit(f"  {tmp} = bitcast i8* {p_tmp} to {to_t}"); return tmp

        if to_t == "%Box*": return self.coerce_to_box(val, from_t)

        # ---- i8* (string) special case: 0 or Null sentinel converts to null pointer ----
        if to_t == "i8*" and from_t in self._INT_IR_SET:
            if val == "0" or val == self._NULL_SENTINEL:
                return "null"
            self.emit(f"  {tmp} = inttoptr {from_t} {val} to i8*")
            return tmp

        # ---- Integer ↔ Integer ----
        if from_t in self._INT_IR_SET and to_t in self._INT_IR_SET:
            fr = self._TYPE_RANK.get(from_t, 2)
            tr = self._TYPE_RANK.get(to_t, 2)
            if fr == tr: return val
            if fr > tr:  # narrowing → trunc
                self.emit(f"  {tmp} = trunc {from_t} {val} to {to_t}"); return tmp
            else:        # widening → sext
                self.emit(f"  {tmp} = sext {from_t} {val} to {to_t}"); return tmp

        # ---- Float ↔ Float ----
        if from_t in self._FLOAT_IR_SET and to_t in self._FLOAT_IR_SET:
            fr = self._TYPE_RANK.get(from_t, 11)
            tr = self._TYPE_RANK.get(to_t, 11)
            if fr == tr: return val
            if fr > tr:
                self.emit(f"  {tmp} = fptrunc {from_t} {val} to {to_t}"); return tmp
            else:
                self.emit(f"  {tmp} = fpext {from_t} {val} to {to_t}"); return tmp

        # ---- Integer → Float ----
        if from_t in self._INT_IR_SET and to_t in self._FLOAT_IR_SET:
            if val == self._NULL_SENTINEL and to_t in ("float", "double"):
                return self._NULL_SENTINEL_FLOAT
            self.emit(f"  {tmp} = sitofp {from_t} {val} to {to_t}"); return tmp

        # ---- Float → Integer ----
        if from_t in self._FLOAT_IR_SET and to_t in self._INT_IR_SET:
            self.emit(f"  {tmp} = fptosi {from_t} {val} to {to_t}"); return tmp

        # ---- i8* (string) to Integer ----
        if from_t == "i8*" and to_t in self._INT_IR_SET:
            if val == "null":
                return "0"
            self.emit(f"  {tmp} = call i64 @atol(i8* {val})")
            if to_t == "i64":
                return tmp
            if to_t == "i32":
                t2 = self.new_tmp()
                self.emit(f"  {t2} = trunc i64 {tmp} to i32")
                return t2
            if to_t == "i1":
                t2 = self.new_tmp()
                self.emit(f"  {t2} = trunc i64 {tmp} to i1")
                return t2
            return tmp

        return val