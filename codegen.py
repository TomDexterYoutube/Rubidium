from ast import *

extern_decls = '''
declare i32 @pthread_create(i64*, i64*, i8* (i8*)*, i8*)
declare i32 @pthread_join(i64, i8**)
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
declare i32 @collection_len(%Box*)
declare %Box* @collection_get_at(%Box*, i32)
declare void @box_drop(%Box*)
declare i64 @time(i64*)
declare i32 @rand()
declare void @srand(i32)
declare i32 @usleep(i32)
declare i32 @sleep(i32)
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
        self.local_vars   = {}
        self.mutable_vars = set()
        self.dropped_vars = set()
        self.class_defs   = {}
        self.instances    = {}
        self.cur_fn       = None
        self.functions    = {}
        self.loop_end_stack = []
        self.cur_class    = None
        self._try_error_label = None  # set while inside a try body for div-by-zero guards
        self._pending_trampolines = []  # trampoline fn_lines to emit after current function

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

    def rubi_type_to_ir(self, t):
        if t in ("list", "index", "dict"):         return "%Box*"
        if t == "i4":                              return "i8"  # 4-bit stored in 8-bit
        if t == "i8":                              return "i8"
        if t == "i16":                             return "i16"
        if t in ("i32", None):                     return "i64"
        if t in ("i64", "i128", "i256"):           return "i64"
        if t in ("f4", "f8", "f16", "f32"):        return "float"
        if t in ("f64", "f128", "f256"):           return "double"
        if t == "bool":                            return "i1"
        if t == "str":                             return "i8*"
        return "i64"

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
        self.global_vars[name] = ir_type
        if ir_type.endswith("*"):
            self.global_decls.append(f"@{name} = global {ir_type} null")
        elif ir_type in ("float", "double"):
            self.global_decls.append(f"@{name} = global {ir_type} 0.0")
        else:
            self.global_decls.append(f"@{name} = global {ir_type} 0")

    def get_var_ptr(self, name):
        # Handle namespaced variables (math.PI -> math_PI)
        if "." in name:
            mangled = name.replace(".", "_")
            if mangled in self.global_vars:
                return f"@{mangled}", self.global_vars[mangled]
        
        if name in self.local_vars: return f"%ptr_{name}", self.local_vars[name]
        if name in self.global_vars: return f"@{name}", self.global_vars[name]
        raise RubidiumNameError(f"Undefined variable '{name}'")

    def class_ir_type(self, class_name): return f"%class_{class_name}"

    def emit_class_type(self, cls):
        field_types = ", ".join(self.rubi_type_to_ir(f.vtype) for f in cls.fields)
        if not field_types: field_types = "i8"
        self.global_decls.append(f"%class_{cls.name} = type {{ {field_types} }}")

    def field_index(self, class_name, field_name):
        cls = self.class_defs[class_name]
        for i, f in enumerate(cls.fields):
            if f.name == field_name: return i, self.rubi_type_to_ir(f.vtype)
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
            "declare i64 @strtol(i8*, i8**, i32)", "declare i64 @atol(i8*)",
            "declare i8* @strndup(i8*, i64)", "declare i32 @fclose(i8*)",
            "declare i8* @fopen(i8*, i8*)", "declare i64 @fread(i8*, i64, i64, i8*)",
            "declare i64 @fwrite(i8*, i64, i64, i8*)", "declare i64 @fseek(i8*, i64, i32)",
            "declare i64 @ftell(i8*)", "declare void @rewind(i8*)",
            "declare i32 @sprintf(i8*, i8*, ...)",
            "@_stdin_ptr = external global i8*", # Changed from @.stdin_ptr
            "@_thread_handles = global [1024 x i64] zeroinitializer",
            "@_thread_results = external global [1024 x %Box*]", # <--- ADD THIS LINE
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
        
        top_init = [s for s in stmts if not isinstance(s, (FnDef, ClassDef, Import, Use))]
        self.cur_fn = None
        self.local_vars = {}
        self.emit_fn(FnDef("_rubidium_init", [], None, top_init))

        for s in stmts:
            if isinstance(s, FnDef): self.emit_fn(s)

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

    def _infer_type(self, node):
        if isinstance(node, Number): return "double" if isinstance(node.value, float) else "i64"
        if isinstance(node, Bool): return "i1"
        if isinstance(node, None_): return "i64"
        if isinstance(node, Str): return "i8*"
        if isinstance(node, InterpolatedStr): return "i8*"
        if isinstance(node, (ListExpr, DictExpr)): return "%Box*"
        if isinstance(node, (Input, FileRead)): return "i8*"
        
        if isinstance(node, MethodCall):
            obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)
            
            # --- DEBUGGING LINE ---
            if "." in obj_name:
                return "i64" # Namespaced call, treat as global function
            
            if obj_name in self.instances:
                class_name = self.instances[obj_name]
                mangled = self.method_ir_name(class_name, node.method)
                if mangled in self.functions:
                    fn = self.functions[mangled]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
            
            if node.method in ("len", "to"): return "i64"
            if node.method in ("contains", "has"): return "i1"
            if node.method in ("combine"): return "i8*"
            # Built-in module methods
            if obj_name == "time": return "i64"
            if obj_name == "random":
                if node.method == "choice": return "%Box*"
                return "i64"  # shuffle returns void/0
            
            raise RubidiumNameError(f"Cannot infer type for method call '{node.method}' on object '{obj_name}'")

        if isinstance(node, FnCall):
            # Normalization check
            name = node.name.replace(".", "_") if isinstance(node.name, str) and "." in node.name else node.name
            if isinstance(name, str) and name in self.functions:
                fn = self.functions[name]
                return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
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
            if node.name in self.local_vars: return self.local_vars[node.name]
            if node.name in self.global_vars: return self.global_vars[node.name]
            return "i64"
        return "i64"

    def collect_globals(self, stmts):
        for s in stmts: self._collect_global(s)

    def _collect_global(self, node):
        if isinstance(node, VarDecl):
            ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
            self.declare_global(node.name, ir_t)
            if node.mutable: self.mutable_vars.add(node.name)
        elif isinstance(node, If):
            for s in node.then_body: self._collect_global(s)
            for s in (node.else_body or []): self._collect_global(s)
        elif isinstance(node, While):
            for s in node.body: self._collect_global(s)
        elif isinstance(node, For):
            self.declare_global(node.var, "i64" if not node.iterable else "%Box*")
            for s in node.body: self._collect_global(s)
        elif isinstance(node, Try):
            for s in node.try_body: self._collect_global(s)
            for s in node.error_body: self._collect_global(s)

    def emit_fn(self, node):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, node.name, None
        self.local_vars = {}
        self.dropped_vars = set()
        
        ret_ir = "i32" if node.name == "main" else (self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64")
        param_ir = ", ".join(f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in node.params)
        self.emit(f"define {ret_ir} @{node.name}({param_ir}) {{")
        self.emit("entry:")
        
        for pn, pt in node.params:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars[pn] = ir_t
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

    def _emit_class_method(self, mfn, class_name):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, mfn.name, class_name
        self.local_vars = {}
        self.dropped_vars = set()
        
        struct_t, ret_ir = self.class_ir_type(class_name), (self.rubi_type_to_ir(mfn.ret_type) if mfn.ret_type else "i64")
        param_str = ", ".join([f"{struct_t}* %param___self"] + [f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in mfn.params[1:]])
        self.emit(f"define {ret_ir} @{mfn.name}({param_str}) {{"  )
        self.emit("entry:")
        
        # Use ptr___self naming to match get_var_ptr convention for local_vars
        self.emit(f"  %ptr___self = alloca {struct_t}*")
        self.emit(f"  store {struct_t}* %param___self, {struct_t}** %ptr___self")
        # Register __self so field access (Var("__self")) works inside method bodies
        self.local_vars["__self"] = f"{struct_t}*"
        self.instances["__self"] = class_name
        for pn, pt in mfn.params[1:]:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars[pn] = ir_t
            self.emit(f"  %ptr_{pn} = alloca {ir_t}")
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* %ptr_{pn}")
            
        if not self.emit_body(mfn.body):
            if ret_ir == "i64": self.emit("  ret i64 0")
            elif ret_ir == "i1": self.emit("  ret i1 0")
            elif ret_ir in ("float","double"): self.emit(f"  ret {ret_ir} 0.0")
            elif ret_ir == "i8*": self.emit("  ret i8* null")
            else: self.emit(f"  ret {ret_ir} null")
        self.emit("}\n")

    def emit_body(self, stmts):
        returned = False
        for s in stmts:
            if returned: break
            if self.emit_stmt(s): returned = True
        return returned

    def emit_stmt(self, node):
        if isinstance(node, VarDecl):
            if node.name in self.dropped_vars: self.dropped_vars.discard(node.name)
            
            is_class = False
            cn = ""
            if isinstance(node.value, (ClassInstantiate, FnCall)):
                raw_cn = node.value.class_name if isinstance(node.value, ClassInstantiate) else (node.value.name if isinstance(node.value.name, str) else "")
                if raw_cn in self.class_defs:
                    cn = raw_cn
                    is_class = True
                elif f"main_{raw_cn}" in self.class_defs:
                    cn = f"main_{raw_cn}"
                    is_class = True
                    
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.local_vars[node.name] = f"{struct_t}*"
                    if node.mutable: self.mutable_vars.add(node.name)
                    ptr_str = f"%ptr_{node.name}"
                    self.emit(f"  {ptr_str} = alloca {struct_t}*")
                    self.emit_class_init(ptr_str, cn)
                    return False
                
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                self.local_vars[node.name] = ir_t
                if node.mutable: self.mutable_vars.add(node.name)
                ptr_str = f"%ptr_{node.name}"
                self.emit(f"  {ptr_str} = alloca {ir_t}")
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
            else:
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.declare_global(node.name, f"{struct_t}*")
                    if node.mutable: self.mutable_vars.add(node.name)
                    self.emit_class_init(f"@{node.name}", cn)
                    return False
                ir_t = self.global_vars.get(node.name, "i64")
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* @{node.name}")
                
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
            if node.method == "set" and isinstance(node.obj, FnCall):
                self.emit_collection_set(node)
                return False
            self.emit_method_call_expr(node)
        elif isinstance(node, Drop):
            self.dropped_vars.add(node.name)
            ir_t = self.local_vars.get(node.name) or self.global_vars.get(node.name)
            if ir_t:
                ptr_str = f"%ptr_{node.name}" if node.name in self.local_vars else f"@{node.name}"
                val = self.new_tmp()
                self.emit(f"  {val} = load {ir_t}, {ir_t}* {ptr_str}")
                if ir_t == "%Box*": self.emit(f"  call void @box_drop(%Box* {val})")
                elif ir_t == "i8*": self.emit(f"  call void @free(i8* {val})")
        elif isinstance(node, Break):
            if self.loop_end_stack: self.emit(f"  br label %{self.loop_end_stack[-1]}")
            return True
        elif isinstance(node, Try): self.emit_try(node)
        elif isinstance(node, ThreadCall):
            self.emit_call_expr(FnCall("thread", [node.func_call, node.thread_id]))
        elif isinstance(node, ThreadWait):
            for texpr in node.thread_ids:
                tid_v, tid_t = self.emit_expr(texpr)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                h_ptr = self.new_tmp(); h_val = self.new_tmp()
                self.emit(f"  {h_ptr} = getelementptr [1024 x i64], [1024 x i64]* @_thread_handles, i64 0, i64 {tid_v}")
                self.emit(f"  {h_val} = load i64, i64* {h_ptr}")
                self.emit(f"  call i32 @pthread_join(i64 {h_val}, i8** null)")
        elif isinstance(node, FileWrite): self.emit_file_write(node)
        return False

    def emit_collection_set(self, method_call_node):
        access_node = method_call_node.obj
        val_node = method_call_node.args[0]
        
        keys = []
        curr = access_node
        while isinstance(curr, FnCall):
            keys = curr.args + keys
            curr = curr.name
            
        if isinstance(curr, str): col_v, col_t = self.emit_expr(Var(curr))
        else: col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)
        
        for i in range(len(keys) - 1):
            arg = keys[i]
            if isinstance(arg, FnCall) and isinstance(arg.name, str) and arg.name not in self.functions and arg.name not in self.global_vars and arg.name not in self.local_vars:
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

    def emit_class_init(self, ptr_str, class_name):
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)
        size_ptr, size_int, raw_ptr, typed_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {typed_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {typed_ptr}, {struct_t}** {ptr_str}")
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype)
            fptr = self.new_tmp()
            self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {typed_ptr}, i32 0, i32 {i}")
            val, val_t = self.emit_expr(field.value)
            val = self.coerce(val, val_t, ir_t)
            self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")

    def emit_field_assign(self, node):
        obj_name = node.obj.name if hasattr(node.obj, 'name') else node.obj
        if obj_name not in self.instances: return
        class_name = self.instances[obj_name]
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
        val, val_t = self.emit_expr(value)
        if val_t == "%Box*":
            self.emit(f"  call void @print_boxed(%Box* {val})")  # print_boxed already calls fflush
        elif val_t in ("i64","i32","i16","i8","i1"):
            fmt, flen = self.intern_str("%lld\n")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            cv = self.coerce(val, val_t, "i64")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t in ("float","double"):
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
        if val_t in ("i64","i32","i16","i8","i1"):
            fmt, flen = self.intern_str("\r%lld")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            cv = self.coerce(val, val_t, "i64")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t in ("float","double"):
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
        self.emit_body(node.body)
        self.loop_end_stack.pop()
        self.emit(f"  br label %{cond_l}\n{end_l}:")

    def emit_for(self, node):
        if node.iterable:
            iter_v, iter_t = self.emit_expr(node.iterable)
            iter_b = self.coerce_to_box(iter_v, iter_t)
            
            idx_ptr = self.new_tmp()
            self.emit(f"  {idx_ptr} = alloca i32")
            self.emit(f"  store i32 0, i32* {idx_ptr}")
            
            item_t = "%Box*"
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                self.local_vars[node.var] = item_t
                var_ptr = f"%ptr_{node.var}"
                self.emit(f"  {var_ptr} = alloca {item_t}")
            else:
                self.declare_global(node.var, item_t)
                var_ptr = f"@{node.var}"
                
            len_val = self.new_tmp()
            self.emit(f"  {len_val} = call i32 @collection_len(%Box* {iter_b})")
            
            cond_l, body_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            
            cur_idx, cond = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur_idx} = load i32, i32* {idx_ptr}")
            self.emit(f"  {cond} = icmp slt i32 {cur_idx}, {len_val}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            
            item_val = self.new_tmp()
            self.emit(f"  {item_val} = call %Box* @collection_get_at(%Box* {iter_b}, i32 {cur_idx})")
            self.emit(f"  store %Box* {item_val}, %Box** {var_ptr}")
            
            self.loop_end_stack.append(end_l)
            self.emit_body(node.body)
            self.loop_end_stack.pop()
            
            inc_idx = self.new_tmp()
            self.emit(f"  {inc_idx} = add i32 {cur_idx}, 1")
            self.emit(f"  store i32 {inc_idx}, i32* {idx_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")
            
        else:
            sv, st = self.emit_expr(node.start); ev, et = self.emit_expr(node.end)
            sv = self.coerce(sv, st, "i64"); ev = self.coerce(ev, et, "i64")
            
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                self.local_vars[node.var] = "i64"
                var_ptr = f"%ptr_{node.var}"
                self.emit(f"  {var_ptr} = alloca i64")
            else:
                self.declare_global(node.var, "i64")
                var_ptr = f"@{node.var}"
                
            self.emit(f"  store i64 {sv}, i64* {var_ptr}")
            cond_l, body_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            cur, cond = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur} = load i64, i64* {var_ptr}\n  {cond} = icmp slt i64 {cur}, {ev}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            self.loop_end_stack.append(end_l)
            self.emit_body(node.body)
            self.loop_end_stack.pop()
            inc, cur2 = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur2} = load i64, i64* {var_ptr}\n  {inc} = add i64 {cur2}, 1\n  store i64 {inc}, i64* {var_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")

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

    def emit_expr(self, node):
        if isinstance(node, Number):
            if isinstance(node.value, float): return f"{node.value:.17e}", "double"
            return str(int(node.value)), "i64"
        if isinstance(node, Bool): return ("1" if node.value else "0"), "i1"
        if isinstance(node, None_): return "0", "i64"
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
        if isinstance(node, FileRead):
            path_val, _ = self.emit_expr(node.path_expr)
            mode_lbl, mlen = self.intern_str("r"); mode_ptr = self.new_tmp()
            self.emit(f"  {mode_ptr} = getelementptr [{mlen} x i8], [{mlen} x i8]* {mode_lbl}, i64 0, i64 0")
            fp, sz0, sz1, buf, read = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {fp}  = call i8* @fopen(i8* {path_val}, i8* {mode_ptr})")
            self.emit(f"  call i64 @fseek(i8* {fp}, i64 0, i32 2)")
            self.emit(f"  {sz0} = call i64 @ftell(i8* {fp})")
            self.emit(f"  call void @rewind(i8* {fp})")
            self.emit(f"  {sz1} = add i64 {sz0}, 1")
            self.emit(f"  {buf} = call i8* @malloc(i64 {sz1})")
            self.emit(f"  {read} = call i64 @fread(i8* {buf}, i64 1, i64 {sz0}, i8* {fp})")
            term_ptr = self.new_tmp()
            self.emit(f"  {term_ptr} = getelementptr i8, i8* {buf}, i64 {read}")
            self.emit(f"  store i8 0, i8* {term_ptr}")
            self.emit(f"  call i32 @fclose(i8* {fp})")
            return buf, "i8*"
            
        if isinstance(node, Var):
            if node.name in self.dropped_vars: raise RubidiumNameError(f"Dropped '{node.name}'")
            
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
            if node.op == "not":
                v = self.to_bool(val, t); tmp = self.new_tmp()
                self.emit(f"  {tmp} = xor i1 {v}, 1")
                return tmp, "i1"
            if node.op == "-":
                tmp = self.new_tmp()
                if t in ("float","double"): self.emit(f"  {tmp} = fneg {t} {val}")
                else: self.emit(f"  {tmp} = sub {t} 0, {val}")
                return tmp, t
            return val, t
        if isinstance(node, FnCall): return self.emit_call_expr(node)
        if isinstance(node, MethodCall):
            if node.method == "set" and isinstance(node.obj, FnCall):
                self.emit_collection_set(node)
                return "0", "i64"
            return self.emit_method_call_expr(node)
        if isinstance(node, TypeCast): return self.emit_type_cast(node)
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
        
        if node.name == "file_write":
            temp_node = FileWrite(node.args[0], node.args[1])
            self.emit_file_write(temp_node)
            return "0", "i64"

        if node.name == "input":
            return self.emit_expr(Input(node.args[0] if node.args else None))

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
            # Build a wrapper: emit a trampoline that calls the function with no args via pthread
            fn_name = func_call_node.name if isinstance(func_call_node, FnCall) else str(func_call_node)
            tramp = f"_tramp_{fn_name}"
            if tramp not in self.functions:
                # defer trampoline emission until after the current function
                self._pending_trampolines += [
                    f"define i8* @{tramp}(i8* %_ignored) {{",
                    "entry:",
                    f"  call i64 @{fn_name}()",
                    "  ret i8* null",
                    "}", ""
                ]
                self.functions[tramp] = FnDef(tramp, [], None, [])
            tramp_ptr = self.new_tmp()
            self.emit(f"  {tramp_ptr} = bitcast i8* (i8*)* @{tramp} to i8* (i8*)*")
            self.emit(f"  call i32 @pthread_create(i64* {h_ptr}, i64* null, i8* (i8*)* {tramp_ptr}, i8* null)")
            return "0", "i64"

        if node.name == "thread_wait":
            for arg in node.args:
                tid_v, tid_t = self.emit_expr(arg)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                h_ptr = self.new_tmp(); h_val = self.new_tmp()
                self.emit(f"  {h_ptr} = getelementptr [1024 x i64], [1024 x i64]* @_thread_handles, i64 0, i64 {tid_v}")
                self.emit(f"  {h_val} = load i64, i64* {h_ptr}")
                self.emit(f"  call i32 @pthread_join(i64 {h_val}, i8** null)")
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
        if (node.name in self.local_vars or node.name in self.global_vars):
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

        raise RubidiumNameError(f"Undefined function or variable: {node.name}")

    def emit_method_call_expr(self, node):
        # 1. Resolve the object name
        obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)
        
        # 2. Handle Collection 'set' method separately
        # This bypasses the class method logic entirely
        if node.method == "set" and isinstance(node.obj, FnCall):
            return self.emit_collection_set(node)
        
        # 2a. Handle FieldAccess.set(value) for class fields (e.g., class_one.obj_val.set(99))
        if node.method == "set" and isinstance(node.obj, FieldAccess):
            return self.emit_field_assign(FieldAssign(node.obj.obj, node.obj.field, node.args[0]))

        # 2b. Handle built-in module method calls: time.sleep, random.shuffle, random.choice
        if obj_name == "time" and node.method == "sleep":
            secs_v, secs_t = self.emit_expr(node.args[0])
            secs_v = self.coerce(secs_v, secs_t, "i64")
            sec_i32 = self.new_tmp()
            self.emit(f"  {sec_i32} = trunc i64 {secs_v} to i32")
            self.emit(f"  call i32 @sleep(i32 {sec_i32})")
            return "0", "i64"

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

            if node.method == "choice":
                # random.choice(collection) — pick random element/key
                col_v, col_t = self.emit_expr(node.args[0])
                col_b = self.coerce_to_box(col_v, col_t)
                len_v = self.new_tmp()
                self.emit(f"  {len_v} = call i32 @collection_len(%Box* {col_b})")
                r_raw, r_idx = self.new_tmp(), self.new_tmp()
                self.emit(f"  {r_raw} = call i32 @rand()")
                self.emit(f"  {r_idx} = srem i32 {r_raw}, {len_v}")
                result = self.new_tmp()
                self.emit(f"  {result} = call %Box* @collection_get_at(%Box* {col_b}, i32 {r_idx})")
                return result, "%Box*"

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

         # 4. Fallback: String method OR module function call (e.g. math.add)
        obj_val, obj_t = self.emit_expr(node.obj)
        if obj_t == "i8*":
            # Emit as string method; raises a clear RubidiumNameError on unknown methods
            # instead of silently falling through to a confusing "Undefined function" error.
            known_str_methods = ("len", "to_int", "contains", "slice", "split", "concat", "combine", "has", "to")
            if node.method not in known_str_methods:
                raise RubidiumNameError(
                    f"Unknown string method '.{node.method}()'. "
                    f"Available string methods: {', '.join(known_str_methods)}"
                )
            return self.emit_string_method(obj_val, node.method, node.args)

        # Not a string — treat as a module-namespaced function call (e.g. math.add -> math_add)
        node.name = f"{obj_name}_{node.method}"
        node.obj = None
        return self.emit_call_expr(node)

    def emit_string_method(self, obj_val, method, args):
        if method == "len":
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @strlen(i8* {obj_val})")
            return tmp, "i64"
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
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @atol(i8* {obj_val})")
            return tmp, "i64"
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
        return "0", "i64"

    def emit_type_cast(self, node):
        val, from_t = self.emit_expr(node.expr)
        to_t = self.rubi_type_to_ir(node.target_type)
        tmp = self.new_tmp()
        if from_t == to_t: return val, to_t
        if from_t in ("i1","i8","i16","i32","i64") and to_t in ("float","double"):
            self.emit(f"  {tmp} = sitofp {from_t} {val} to {to_t}"); return tmp, to_t
        if from_t in ("float","double") and to_t in ("i1","i8","i16","i32","i64"):
            self.emit(f"  {tmp} = fptosi {from_t} {val} to {to_t}"); return tmp, to_t
        if from_t == "float" and to_t == "double":
            self.emit(f"  {tmp} = fpext float {val} to double"); return tmp, to_t
        if from_t == "double" and to_t == "float":
            self.emit(f"  {tmp} = fptrunc double {val} to float"); return tmp, to_t
        int_sizes = {"i1":1,"i8":8,"i16":16,"i32":32,"i64":64}
        if from_t in int_sizes and to_t in int_sizes:
            instr = "sext" if int_sizes[to_t] > int_sizes[from_t] else "trunc"
            self.emit(f"  {tmp} = {instr} {from_t} {val} to {to_t}"); return tmp, to_t
        if from_t in ("i1","i8","i16","i32","i64") and to_t == "i8*":
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
        if from_t == "i8*" and to_t in ("i1","i8","i16","i32","i64"):
            t2 = self.new_tmp()
            self.emit(f"  {t2} = call i64 @atol(i8* {val})")
            return self.coerce(t2, "i64", to_t), to_t
        return val, to_t

    def emit_binop(self, node):
        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)
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
        else:
            instr = {"+":"fadd","-":"fsub","*":"fmul","/":"fdiv"}.get(node.op)
            if instr:
                self.emit(f"  {tmp} = {instr} double {l}, {r}")
                return tmp, "double"
        return "0", "i64"

    def emit_compare(self, node):
        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)
        if lt == "i8*" and rt == "i8*":
            cmp_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cmp_r} = call i32 @strcmp(i8* {l}, i8* {r})")
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i32 {cmp_r}, 0")
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
        if t in ("i1", "i8", "i16", "i32", "i64"):
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
        if t in ("i1", "i8", "i16", "i32", "i64"):
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
        if from_t == "%Box*":
            if to_t in ("i1", "i8", "i16", "i32", "i64"):
                self.emit(f"  {tmp} = call i64 @unbox_i(%Box* {val})")
                if to_t != "i64":
                    tmp2 = self.new_tmp()
                    self.emit(f"  {tmp2} = trunc i64 {tmp} to {to_t}")
                    return tmp2
                return tmp
            if to_t in ("float", "double"):
                self.emit(f"  {tmp} = call double @unbox_f(%Box* {val})")
                if to_t == "float":
                    tmp2 = self.new_tmp()
                    self.emit(f"  {tmp2} = fptrunc double {tmp} to float")
                    return tmp2
                return tmp
            if to_t == "i8*":
                self.emit(f"  {tmp} = call i8* @unbox_s(%Box* {val})")
                return tmp
            self.emit(f"  {tmp} = call i8* @unbox_p(%Box* {val})")
            tmp2 = self.new_tmp()
            self.emit(f"  {tmp2} = bitcast i8* {tmp} to {to_t}")
            return tmp2
        if to_t == "%Box*": return self.coerce_to_box(val, from_t)
        int_types = {"i1","i4","i8","i16","i32","i64"}
        # i4 is stored as i8, so handle it specially
        if from_t == "i4" and to_t in int_types:
            if to_t == "i4":
                return val
            tmp2 = self.new_tmp()
            self.emit(f"  {tmp2} = sext i8 {val} to i64")  # i4 is stored as i8, sign-extend to i64
            if to_t == "i64":
                return tmp2
            tmp3 = self.new_tmp()
            self.emit(f"  {tmp3} = trunc i64 {tmp2} to {to_t}")
            return tmp3
        if to_t == "i4":
            # Converting to i4 - store as i8 (lower 4 bits)
            if from_t in int_types:
                tmp2 = self.new_tmp()
                self.emit(f"  {tmp2} = trunc {from_t} {val} to i8")  # Truncate to i8
                return tmp2
        if from_t in int_types and to_t in int_types:
            instr = "zext" if from_t == "i1" else ("sext" if int(to_t[1:]) > int(from_t[1:]) else "trunc")
            self.emit(f"  {tmp} = {instr} {from_t} {val} to {to_t}")
            return tmp
        if from_t in int_types and to_t in ("float","double"):
            self.emit(f"  {tmp} = sitofp {from_t} {val} to {to_t}"); return tmp
        if from_t == "float" and to_t == "double":
            self.emit(f"  {tmp} = fpext float {val} to double"); return tmp
        if from_t == "double" and to_t == "float":
            self.emit(f"  {tmp} = fptrunc double {val} to float"); return tmp
        if from_t in ("float","double") and to_t in int_types:
            self.emit(f"  {tmp} = fptosi {from_t} {val} to {to_t}"); return tmp
        return val