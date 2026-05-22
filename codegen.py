from ast import *

# ═══════════════════════════════════════════════════════════════
#  Rubidium → LLVM IR code generator
#
#  Scoping rules implemented here:
#    • All variables (global AND fn-local) go into one flat
#      shared table implemented as LLVM globals.  Every function
#      can read/write every variable by name.
#    • Class fields are isolated: accessed only via instance ptr.
#    • .drop() is a no-op at the IR level (memory is static).
# ═══════════════════════════════════════════════════════════════

class CodeGen:
    def __init__(self):
        self.fn_lines   = []     # IR for function bodies
        self.global_decls = []   # @var = global …  and string constants
        self.str_count  = 0
        self.tmp_count  = 0
        self.label_count = 0

        # Shared variable table: name -> ir_type
        # All variables, wherever declared, end up here as LLVM globals.
        self.shared_vars = {}    # name -> ir_type

        # Class definitions: name -> [VarDecl, ...]
        self.class_defs  = {}

        # Class instance table: var_name -> class_name
        # For each `let x = MyClass()` we track what type x is
        self.instances   = {}

        # Currently-emitting function (for tmp/label naming)
        self.cur_fn      = None
        self.functions   = {}    # name -> FnDef

    # ── utilities ────────────────────────────────────────────────

    def new_tmp(self):
        self.tmp_count += 1
        return f"%t{self.tmp_count}"

    def new_label(self, prefix="lbl"):
        self.label_count += 1
        return f"{prefix}{self.label_count}"

    def emit(self, line):
        self.fn_lines.append(line)

    def rubi_type_to_ir(self, t):
        if t in ("i8",):                          return "i8"
        if t in ("i16",):                         return "i16"
        if t in ("i32", None):                    return "i64"
        if t in ("i64","i128","i256"):            return "i64"
        if t in ("f32","f4"):                     return "float"
        if t in ("f64","f8","f128","f256"):       return "double"
        if t == "bool":                           return "i1"
        if t == "str":                            return "i8*"
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
        self.global_decls.append(
            f'{lbl} = private unnamed_addr constant [{byte_len} x i8] c"{escaped}"'
        )
        return lbl, byte_len

    # ── declare a shared variable (LLVM global) ──────────────────

    def declare_shared(self, name, ir_type, init_val=None):
        """Register a variable in the shared table and emit a global."""
        if name in self.shared_vars:
            return  # already declared
        self.shared_vars[name] = ir_type
        if ir_type.endswith("*"):
            self.global_decls.append(f"@{name} = global {ir_type} null")
        elif ir_type in ("float", "double"):
            self.global_decls.append(f"@{name} = global {ir_type} 0.0")
        else:
            self.global_decls.append(f"@{name} = global {ir_type} 0")

    # ── class struct type ─────────────────────────────────────────

    def class_ir_type(self, class_name):
        return f"%class_{class_name}"

    def emit_class_type(self, cls):
        """Emit LLVM named struct for a class."""
        field_types = ", ".join(
            self.rubi_type_to_ir(f.vtype) for f in cls.fields
        )
        self.global_decls.append(
            f"%class_{cls.name} = type {{ {field_types} }}"
        )

    def field_index(self, class_name, field_name):
        cls = self.class_defs[class_name]
        for i, f in enumerate(cls.fields):
            if f.name == field_name:
                return i, self.rubi_type_to_ir(f.vtype)
        raise NameError(f"Class '{class_name}' has no field '{field_name}'")

    # ── top-level codegen ─────────────────────────────────────────

    def gen(self, stmts):
        # ── pass 1: collect class defs and fn names ──
        for s in stmts:
            if isinstance(s, ClassDef):
                self.class_defs[s.name] = s
            elif isinstance(s, FnDef):
                self.functions[s.name] = s

        # ── pass 2: collect ALL variable declarations everywhere ──
        # This makes every variable a global so all fns share it.
        self.collect_all_vars(stmts)

        # ── pass 3: emit class struct types ──
        for cls in self.class_defs.values():
            self.emit_class_type(cls)

        # ── pass 4: emit libc declarations ──
        self.global_decls += [
            "",
            "declare i32 @printf(i8* noundef, ...)",
            "declare i32 @puts(i8* noundef)",
            "declare i8* @malloc(i64)",
            "declare void @free(i8*)",
            "",
        ]

        # ── pass 5: emit _rubidium_init for top-level initialisers ──
        top_init = [s for s in stmts
                    if not isinstance(s, (FnDef, ClassDef, Import, Use))]
        self.emit_fn(FnDef("_rubidium_init", [], None, top_init))

        # ── pass 6: emit user function bodies ──
        for s in stmts:
            if isinstance(s, FnDef):
                self.emit_fn(s)

        # ── pass 7: if no explicit main fn, create one ──
        if "main" not in self.functions:
            self.emit_fn(FnDef("main", [], "i32", []))

        # ── pass 8: inject _rubidium_init call at top of main ──
        self._inject_init_call()

        # ── assemble ──
        out = ["; Rubidium compiled output", 'source_filename = "rubidium"', ""]
        out += self.global_decls
        out += [""]
        out += self.fn_lines
        return "\n".join(out)

    # ── variable collection pass ──────────────────────────────────

    def collect_all_vars(self, stmts):
        """Walk the entire AST and pre-declare every VarDecl as a global."""
        for s in stmts:
            self._collect_stmt(s)

    def _collect_stmt(self, node):
        if isinstance(node, VarDecl):
            ir_t = self.rubi_type_to_ir(node.vtype)
            # If the value is a class instantiation, allocate a ptr instead
            if isinstance(node.value, (ClassInstantiate, FnCall)):
                cn = node.value.class_name if isinstance(node.value, ClassInstantiate) else node.value.name
                if cn in self.class_defs:
                    # It's an instance — store a pointer to the struct
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.declare_shared(node.name, f"{struct_t}*")
                    return
            self.declare_shared(node.name, ir_t)
        elif isinstance(node, FnDef):
            for s in node.body:
                self._collect_stmt(s)
        elif isinstance(node, If):
            for s in node.then_body: self._collect_stmt(s)
            for s in (node.else_body or []): self._collect_stmt(s)
        elif isinstance(node, (While,)):
            for s in node.body: self._collect_stmt(s)
        elif isinstance(node, For):
            # The loop variable itself
            self.declare_shared(node.var, "i64")
            for s in node.body: self._collect_stmt(s)
        elif isinstance(node, Try):
            for s in node.try_body: self._collect_stmt(s)
            for s in node.error_body: self._collect_stmt(s)

    # ── inject _rubidium_init call into main ────────────────────────

    def _inject_init_call(self):
        """Patch the emitted IR so main() calls _rubidium_init first."""
        patched = []
        in_main = False
        injected = False
        for line in self.fn_lines:
            patched.append(line)
            if line.strip() == "define i32 @main() {":
                in_main = True
            elif in_main and not injected and line.strip() == "entry:":
                patched.append("  call i64 @_rubidium_init()")
                injected = True
        self.fn_lines = patched

        # ── function emission ─────────────────────────────────────────

    def emit_fn(self, node, is_entry=False):
        self.tmp_count  = 0
        self.label_count = 0
        self.cur_fn     = node.name

        ret_ir = "i32" if node.name == "main" else (
            self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64"
        )

        param_ir = ", ".join(
            f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in node.params
        )
        self.emit(f"define {ret_ir} @{node.name}({param_ir}) {{")
        self.emit("entry:")

        # Copy params into shared globals so they're visible everywhere
        for pn, pt in node.params:
            ir_t = self.rubi_type_to_ir(pt)
            self.declare_shared(pn, ir_t)
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* @{pn}")

        returned = self.emit_body(node.body)

        if not returned:
            if node.name == "main":
                self.emit("  ret i32 0")
            elif ret_ir == "void":
                self.emit("  ret void")
            else:
                self.emit(f"  ret {ret_ir} 0")

        self.emit("}")
        self.emit("")

    def emit_body(self, stmts):
        returned = False
        for s in stmts:
            returned = self.emit_stmt(s)
        return returned

    # ── statements ───────────────────────────────────────────────

    def emit_stmt(self, node):

        # ── variable declaration ──
        if isinstance(node, VarDecl):
            # Class instantiation?
            if isinstance(node.value, (ClassInstantiate, FnCall)):
                cn = node.value.class_name if isinstance(node.value, ClassInstantiate) \
                     else node.value.name
                if cn in self.class_defs:
                    self.emit_class_init(node.name, cn)
                    return False

            ir_t = self.shared_vars.get(node.name, "i64")
            val, val_t = self.emit_expr(node.value)
            val = self.coerce(val, val_t, ir_t)
            self.emit(f"  store {ir_t} {val}, {ir_t}* @{node.name}")

        # ── assignment ──
        elif isinstance(node, Assign):
            if node.name in self.instances:
                # reassigning an instance pointer — skip for now
                pass
            elif node.name in self.shared_vars:
                ir_t = self.shared_vars[node.name]
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* @{node.name}")
            else:
                # auto-declare
                val, val_t = self.emit_expr(node.value)
                self.declare_shared(node.name, val_t)
                self.emit(f"  store {val_t} {val}, {val_t}* @{node.name}")

        # ── field assignment: obj.field = expr ──
        elif isinstance(node, FieldAssign):
            self.emit_field_assign(node)

        # ── print ──
        elif isinstance(node, Print):
            self.emit_print(node.value)

        # ── if ──
        elif isinstance(node, If):
            self.emit_if(node)

        # ── while ──
        elif isinstance(node, While):
            self.emit_while(node)

        # ── for ──
        elif isinstance(node, For):
            self.emit_for(node)

        # ── return ──
        elif isinstance(node, Return):
            val, val_t = self.emit_expr(node.value)
            self.emit(f"  ret {val_t} {val}")
            return True

        # ── fn call as statement ──
        elif isinstance(node, FnCall):
            self.emit_call_stmt(node)

        # ── method call as statement ──
        elif isinstance(node, MethodCall):
            pass  # no built-in methods other than drop

        # ── drop ── (no-op — memory is static globals)
        elif isinstance(node, Drop):
            pass

        # ── try / on_error ──
        elif isinstance(node, Try):
            self.emit_body(node.try_body)

        # ── thread ──
        elif isinstance(node, ThreadCall):
            if isinstance(node.func_call, FnCall):
                self.emit_call_stmt(node.func_call)

        elif isinstance(node, ThreadWait):
            pass

        elif isinstance(node, (Import, Use)):
            pass

        return False

    # ── class instantiation ───────────────────────────────────────

    def emit_class_init(self, var_name, class_name):
        """malloc a struct, store each field's default, save ptr into global."""
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)

        # sizeof struct via getelementptr trick
        size_ptr = self.new_tmp()
        size_int = self.new_tmp()
        raw_ptr  = self.new_tmp()
        typed_ptr = self.new_tmp()

        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {typed_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {typed_ptr}, {struct_t}** @{var_name}")

        # Initialise each field to its default value
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype)
            fptr = self.new_tmp()
            self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {typed_ptr}, i32 0, i32 {i}")
            val, val_t = self.emit_expr(field.value)
            val = self.coerce(val, val_t, ir_t)
            self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")

    # ── field assign: obj.field = expr ───────────────────────────

    def emit_field_assign(self, node):
        if node.obj not in self.instances:
            return
        class_name = self.instances[node.obj]
        idx, ir_t  = self.field_index(class_name, node.field)
        struct_t   = self.class_ir_type(class_name)

        inst_ptr = self.new_tmp()
        fptr     = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** @{node.obj}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        val, val_t = self.emit_expr(node.value)
        val = self.coerce(val, val_t, ir_t)
        self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")

    # ── print ─────────────────────────────────────────────────────

    def emit_print(self, value):
        if isinstance(value, Str):
            lbl, blen = self.intern_str(value.value)
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{blen} x i8], [{blen} x i8]* {lbl}, i64 0, i64 0')
            self.emit(f'  call i32 @puts(i8* {ptr})')
        else:
            val, val_t = self.emit_expr(value)
            if val_t in ("i64","i32","i16","i8","i1"):
                fmt, flen = self.intern_str("%lld\n")
                ptr = self.new_tmp()
                self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
                cv = self.coerce(val, val_t, "i64")
                self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            elif val_t in ("float","double"):
                fmt, flen = self.intern_str("%f\n")
                ptr = self.new_tmp()
                self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
                dv = self.coerce(val, val_t, "double")
                self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, double {dv})')
            elif val_t == "i8*":
                self.emit(f'  call i32 @puts(i8* {val})')

    # ── control flow ──────────────────────────────────────────────

    def to_bool(self, val, t):
        if t == "i1": return val
        tmp = self.new_tmp()
        if t in ("float","double"):
            self.emit(f"  {tmp} = fcmp une {t} {val}, 0.0")
        else:
            self.emit(f"  {tmp} = icmp ne {t} {val}, 0")
        return tmp

    def emit_if(self, node):
        cond, ct = self.emit_expr(node.cond)
        cond = self.to_bool(cond, ct)
        then_l = self.new_label("then")
        else_l = self.new_label("else")
        end_l  = self.new_label("endif")
        self.emit(f"  br i1 {cond}, label %{then_l}, label %{else_l}")
        self.emit(f"{then_l}:")
        self.emit_body(node.then_body)
        self.emit(f"  br label %{end_l}")
        self.emit(f"{else_l}:")
        if node.else_body:
            self.emit_body(node.else_body)
        self.emit(f"  br label %{end_l}")
        self.emit(f"{end_l}:")

    def emit_while(self, node):
        cond_l = self.new_label("wcond")
        body_l = self.new_label("wbody")
        end_l  = self.new_label("wend")
        self.emit(f"  br label %{cond_l}")
        self.emit(f"{cond_l}:")
        cond, ct = self.emit_expr(node.cond)
        cond = self.to_bool(cond, ct)
        self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}")
        self.emit(f"{body_l}:")
        self.emit_body(node.body)
        self.emit(f"  br label %{cond_l}")
        self.emit(f"{end_l}:")

    def emit_for(self, node):
        sv, st = self.emit_expr(node.start)
        ev, et = self.emit_expr(node.end)
        sv = self.coerce(sv, st, "i64")
        ev = self.coerce(ev, et, "i64")
        # loop var is a shared global
        self.declare_shared(node.var, "i64")
        self.emit(f"  store i64 {sv}, i64* @{node.var}")

        cond_l = self.new_label("fcond")
        body_l = self.new_label("fbody")
        end_l  = self.new_label("fend")
        self.emit(f"  br label %{cond_l}")
        self.emit(f"{cond_l}:")
        cur = self.new_tmp()
        cond = self.new_tmp()
        self.emit(f"  {cur} = load i64, i64* @{node.var}")
        self.emit(f"  {cond} = icmp slt i64 {cur}, {ev}")
        self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}")
        self.emit(f"{body_l}:")
        self.emit_body(node.body)
        inc = self.new_tmp()
        cur2 = self.new_tmp()
        self.emit(f"  {cur2} = load i64, i64* @{node.var}")
        self.emit(f"  {inc} = add i64 {cur2}, 1")
        self.emit(f"  store i64 {inc}, i64* @{node.var}")
        self.emit(f"  br label %{cond_l}")
        self.emit(f"{end_l}:")

    # ── fn call (statement) ───────────────────────────────────────

    def emit_call_stmt(self, node):
        if node.name == "print":
            for a in node.args: self.emit_print(a)
            return
        if node.name == "thread" and len(node.args) == 2:
            fc = node.args[0]
            if isinstance(fc, FnCall): self.emit_call_stmt(fc)
            return
        args_ir = []
        for a in node.args:
            v, t = self.emit_expr(a)
            args_ir.append(f"{t} {v}")
        ret_t = "i64"
        if node.name in self.functions:
            fn = self.functions[node.name]
            ret_t = self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
        tmp = self.new_tmp()
        if ret_t == "void":
            self.emit(f"  call void @{node.name}({', '.join(args_ir)})")
        else:
            self.emit(f"  {tmp} = call {ret_t} @{node.name}({', '.join(args_ir)})")

    # ── expressions ───────────────────────────────────────────────

    def emit_expr(self, node):
        """Returns (ir_val_str, ir_type_str)."""

        if isinstance(node, Number):
            return str(node.value), "i64"

        if isinstance(node, Bool):
            return ("1" if node.value else "0"), "i1"

        if isinstance(node, Str):
            lbl, blen = self.intern_str(node.value)
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{blen} x i8], [{blen} x i8]* {lbl}, i64 0, i64 0')
            return ptr, "i8*"

        if isinstance(node, Var):
            name = node.name
            if name in self.shared_vars:
                ir_t = self.shared_vars[name]
                tmp  = self.new_tmp()
                self.emit(f"  {tmp} = load {ir_t}, {ir_t}* @{name}")
                return tmp, ir_t
            return "0", "i64"

        # obj.field  →  FieldAccess
        if isinstance(node, FieldAccess):
            return self.emit_field_access(node.obj, node.field)

        if isinstance(node, BinOp):
            return self.emit_binop(node)

        if isinstance(node, Compare):
            return self.emit_compare(node)

        if isinstance(node, UnaryOp):
            val, t = self.emit_expr(node.value)
            if node.op == "not":
                v = self.to_bool(val, t)
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = xor i1 {v}, 1")
                return tmp, "i1"
            if node.op == "-":
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = sub {t} 0, {val}")
                return tmp, t
            return val, t

        if isinstance(node, FnCall):
            return self.emit_call_expr(node)

        if isinstance(node, (ClassInstantiate, ThreadCall)):
            return "0", "i64"

        return "0", "i64"

    # ── field access expression ───────────────────────────────────

    def emit_field_access(self, obj_name, field_name):
        if obj_name not in self.instances:
            return "0", "i64"
        class_name = self.instances[obj_name]
        idx, ir_t  = self.field_index(class_name, field_name)
        struct_t   = self.class_ir_type(class_name)

        inst_ptr = self.new_tmp()
        fptr     = self.new_tmp()
        val      = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** @{obj_name}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        self.emit(f"  {val} = load {ir_t}, {ir_t}* {fptr}")
        return val, ir_t

    # ── fn call (expression) ──────────────────────────────────────

    def emit_call_expr(self, node):
        if node.name == "print":
            for a in node.args: self.emit_print(a)
            return "0", "i64"
        if node.name == "thread" and len(node.args) == 2:
            fc = node.args[0]
            if isinstance(fc, FnCall):
                return self.emit_call_expr(fc)
            return "0", "i64"
        args_ir = []
        for a in node.args:
            v, t = self.emit_expr(a)
            args_ir.append(f"{t} {v}")
        ret_t = "i64"
        if node.name in self.functions:
            fn = self.functions[node.name]
            ret_t = self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
        tmp = self.new_tmp()
        if ret_t == "void":
            self.emit(f"  call void @{node.name}({', '.join(args_ir)})")
            return "0", "i64"
        self.emit(f"  {tmp} = call {ret_t} @{node.name}({', '.join(args_ir)})")
        return tmp, ret_t

    # ── binary ops ────────────────────────────────────────────────

    def emit_binop(self, node):
        l, lt = self.emit_expr(node.left)
        r, rt = self.emit_expr(node.right)
        if lt in ("float","double") or rt in ("float","double"):
            common = "double"
        else:
            common = "i64"
        l = self.coerce(l, lt, common)
        r = self.coerce(r, rt, common)
        tmp = self.new_tmp()
        op  = node.op
        if common == "i64":
            if op in ("and","or"):
                li = self.to_bool(l, "i64")
                ri = self.to_bool(r, "i64")
                instr = "and" if op == "and" else "or"
                self.emit(f"  {tmp} = {instr} i1 {li}, {ri}")
                return tmp, "i1"
            instr = {"+":"add","-":"sub","*":"mul","/":"sdiv","%":"srem"}.get(op)
            if instr:
                self.emit(f"  {tmp} = {instr} i64 {l}, {r}")
                return tmp, "i64"
        else:
            instr = {"+":"fadd","-":"fsub","*":"fmul","/":"fdiv"}.get(op)
            if instr:
                self.emit(f"  {tmp} = {instr} double {l}, {r}")
                return tmp, "double"
        return "0", "i64"

    def emit_compare(self, node):
        l, lt = self.emit_expr(node.left)
        r, rt = self.emit_expr(node.right)
        if lt in ("float","double") or rt in ("float","double"):
            common = "double"
            l = self.coerce(l, lt, common)
            r = self.coerce(r, rt, common)
            pred = {"==":"oeq","!=":"one","<":"olt",">":"ogt","<=":"ole",">=":"oge"}[node.op]
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = fcmp {pred} double {l}, {r}")
        else:
            common = "i64"
            l = self.coerce(l, lt, common)
            r = self.coerce(r, rt, common)
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = icmp {pred} i64 {l}, {r}")
        return tmp, "i1"

    # ── type coercion ─────────────────────────────────────────────

    def coerce(self, val, from_t, to_t):
        if from_t == to_t: return val
        tmp = self.new_tmp()
        int_types = {"i1","i8","i16","i32","i64"}
        if from_t in int_types and to_t in int_types:
            fb = int(from_t[1:]); tb = int(to_t[1:])
            instr = "sext" if tb > fb else "trunc"
            self.emit(f"  {tmp} = {instr} {from_t} {val} to {to_t}")
            return tmp
        if from_t in int_types and to_t in ("float","double"):
            self.emit(f"  {tmp} = sitofp {from_t} {val} to {to_t}")
            return tmp
        if from_t == "float" and to_t == "double":
            self.emit(f"  {tmp} = fpext float {val} to double")
            return tmp
        if from_t == "double" and to_t == "float":
            self.emit(f"  {tmp} = fptrunc double {val} to float")
            return tmp
        if from_t in ("float","double") and to_t in int_types:
            self.emit(f"  {tmp} = fptosi {from_t} {val} to {to_t}")
            return tmp
        return val
