from ast import *

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.line_no = 1
        self._active_file_handles = set()  # var names bound via open() as varname

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self):
        if self.pos < len(self.tokens):
            self.line_no = self.tokens[self.pos][2] if len(self.tokens[self.pos]) > 2 else self.line_no
        self.pos += 1

    def match(self, kind):
        tok = self.peek()
        if tok and tok[0] == kind:
            self.advance()
            return tok[1]
        return None

    def parse(self):
        stmts = []
        while self.peek():
            t = self.peek()
            if   t[0] == "IMPORT": stmts.append(self.import_stmt())
            elif t[0] == "USE":    stmts.append(self.use_stmt())
            elif t[0] == "CLASS":  stmts.append(self.class_def())
            elif t[0] == "FN":     stmts.append(self.fn_def())
            else:                  stmts += self.stmt_list_item()
        return stmts

    # --- Unified Identifier Logic ---
    def parse_identifier_chain(self, name):
        """Unified method for parsing variable access, dots, and calls."""
        res = Var(name)
        while self.peek():
            t = self.peek()
            if t[0] == "DOT":
                self.advance()
                attr = self.match("IDENT")
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = MethodCall(res, attr, args)
                else:
                    res = FieldAccess(res, attr)
            elif t[0] == "LPAREN":
                self.match("LPAREN")
                args = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    args.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                self.match("RPAREN")
                
                # Intercept specialized built-ins
                if isinstance(res, Var):
                    if res.name == "thread" and len(args) == 2: res = ThreadCall(args[0], args[1])
                    elif res.name == "input": res = Input(args[0] if args else None)
                    elif res.name == "file_read" and len(args) == 1: res = FileRead(args[0])
                    else: res = FnCall(res.name, args)
                else:
                    res = FnCall(res, args)
            else:
                break
        return res

    def import_stmt(self):
        self.match("IMPORT")
        name = self.match("IDENT")
        while self.peek() and self.peek()[0] == "DOT":
            self.match("DOT")
            name += "." + self.match("IDENT")
        return Import(name)

    def use_stmt(self):
        self.match("USE")
        return Use(self.match("IDENT"))

    def class_def(self):
        self.match("CLASS")
        name = self.match("IDENT")
        self.match("LPAREN"); self.match("RPAREN")
        self.match("LBRACE")
        fields = []; methods = []
        while self.peek() and self.peek()[0] != "RBRACE":
            if self.peek()[0] == "LET": fields.append(self.var_decl())
            elif self.peek()[0] == "FN": methods.append(self.fn_def())
            else: self.advance()
        self.match("RBRACE")
        return ClassDef(name, fields, methods)

    def fn_def(self):
        self.match("FN")
        name = self.match("IDENT")
        # FFI binding: fn handle_name symbol_name(params) -> ret  (no body brace)
        # Detected when the token after `name` is another IDENT (not LPAREN)
        if self.peek() and self.peek()[0] == "IDENT":
            symbol_name = self.match("IDENT")
            self.match("LPAREN")
            params = []
            while self.peek() and self.peek()[0] != "RPAREN":
                pname = self.match("IDENT") or self.match("TYPE")
                self.match("COLON")
                ptype = self.match("TYPE")
                params.append((pname, ptype))
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RPAREN")
            ret_type = None
            if self.peek() and self.peek()[1] == "->":
                self.match("OP")
                ret_type = self.match("TYPE")
            return FFIBind(name, symbol_name, params, ret_type)
        # Normal function
        self.match("LPAREN")
        params = []
        while self.peek() and self.peek()[0] != "RPAREN":
            pname = self.match("IDENT")
            self.match("COLON")
            ptype = self.match("TYPE")
            params.append((pname, ptype))
            if self.peek() and self.peek()[0] == "COMMA":
                self.match("COMMA")
        self.match("RPAREN")
        ret_type = None
        if self.peek() and self.peek()[1] == "->":
            self.match("OP")
            ret_type = self.match("TYPE")
        self.match("LBRACE")
        body = self.block()
        self.match("RBRACE")
        return FnDef(name, params, ret_type, body)

    def block(self):
        stmts = []
        while self.peek() and self.peek()[0] != "RBRACE":
            stmts += self.stmt_list_item()
        return stmts

    def stmt_list_item(self):
        t = self.peek()
        if t is None: return []
        if   t[0] == "LET":      return [self.var_decl()]
        elif t[0] == "PRINT":    return [self.print_stmt()]
        elif t[0] == "PRINTLN":  return [self.println_stmt()]
        elif t[0] == "IF":       return [self.if_stmt()]
        elif t[0] == "WHILE":    return [self.while_stmt()]
        elif t[0] == "FOR":      return [self.for_stmt()]
        elif t[0] == "BREAK":    self.match("BREAK"); return [Break()]
        elif t[0] == "RETURN":   return [self.return_stmt()]
        elif t[0] == "TRY":      return [self.try_stmt()]
        elif t[0] == "FN":       return [self.fn_def()]
        elif t[0] == "OPEN":     return [self.open_stmt()]
        elif t[0] == "FILE":     return self.file_global_stmt_list()
        elif t[0] == "IDENT" or t[0] == "TYPE":    return [self.ident_stmt()]
        else:
            self.advance()
            return []

    def open_stmt(self):
        """Parse: open("path") as var_name { statements }"""
        self.match("OPEN")
        self.match("LPAREN")
        path = self.expr()
        self.match("RPAREN")
        self.match("AS")
        # "file" is tokenized as FILE keyword, but valid as a var name here
        var_name = self.match("IDENT") or self.match("FILE") or self.match("TYPE")
        self.match("LBRACE")
        # Register var_name so that FILE.method() inside body is parsed as handle method
        if var_name:
            self._active_file_handles.add(var_name)
        body = []
        while self.peek() and self.peek()[0] != "RBRACE":
            item = self.stmt_list_item()
            if item is not None:
                body += item if isinstance(item, list) else [item]
        self.match("RBRACE")
        if var_name:
            self._active_file_handles.discard(var_name)
        return FileOpen(path, var_name, body)

    def var_decl(self):
        self.match("LET")
        mut = bool(self.match("MUT"))
        # Accept both IDENT and TYPE tokens for variable names (e.g., "list" is a TYPE but can be a variable name)
        tok = self.peek()
        if tok and tok[0] in ("IDENT", "TYPE"):
            name = self.match(tok[0])  # match returns the value
        else:
            name = self.match("IDENT")
        vtype = None
        if self.peek() and self.peek()[0] == "COLON":
            self.match("COLON")
            vtype = self.match("TYPE")
        elif self.peek() and self.peek()[0] == "TYPE":
            vtype = self.match("TYPE")
        self.match("OP")
        value = self.expr()
        return VarDecl(name, mut, vtype, value)

    def print_stmt(self):
        self.match("PRINT")
        self.match("LPAREN")
        val = self.expr()
        self.match("RPAREN")
        return Print(val)

    def println_stmt(self):
        self.match("PRINTLN")
        self.match("LPAREN")
        val = self.expr()
        self.match("RPAREN")
        return Println(val)

    def if_stmt(self):
        self.match("IF")
        cond = self.expr()
        self.match("LBRACE"); then_body = self.block(); self.match("RBRACE")
        else_body = None
        if self.peek() and self.peek()[0] == "ELSE":
            self.match("ELSE")
            if self.peek() and self.peek()[0] == "IF": else_body = [self.if_stmt()]
            else:
                self.match("LBRACE"); else_body = self.block(); self.match("RBRACE")
        return If(cond, then_body, else_body)

    def while_stmt(self):
        self.match("WHILE")
        cond = self.expr(); self.match("LBRACE"); body = self.block(); self.match("RBRACE")
        return While(cond, body)

    def for_stmt(self):
        self.match("FOR")
        var = self.match("IDENT")
        self.match("IN")
        if self.peek() and self.peek()[0] == "RANGE":
            self.match("RANGE")
            self.match("LPAREN")
            start = self.expr()
            self.match("COMMA")
            end = self.expr()
            self.match("RPAREN")
            iterable = None
        else:
            iterable = self.expr()
            start = None
            end = None
        self.match("LBRACE")
        body = self.block()
        self.match("RBRACE")
        return For(var, start, end, body, iterable)

    def return_stmt(self):
        self.match("RETURN")
        return Return(self.expr())

    def try_stmt(self):
        self.match("TRY")
        self.match("LBRACE"); try_body = self.block(); self.match("RBRACE")
        # Accept both `error` (as IDENT) and `on_error` as the catch keyword
        tok = self.peek()
        if tok and tok[0] == "ON_ERROR":
            self.match("ON_ERROR")
        elif tok and tok[0] == "IDENT" and tok[1] == "error":
            self.advance()  # consume `error`
        self.match("LBRACE"); err_body = self.block(); self.match("RBRACE")
        return Try(try_body, err_body)

    def file_global_stmt_list(self):
        """Parse: file.write(...), file.append(...), file.read(...), file.exists(...), file.delete(...), file.rename(...), file.copy(...), file.new(...)"""
        # Peek at the FILE token value to check if it's an active file handle var
        file_tok = self.peek()
        file_var = file_tok[1] if file_tok else "file"
        self.match("FILE")
        self.match("DOT")
        method = self.match("IDENT")

        # If inside an open() block and this matches the handle var, parse as MethodCall
        if file_var in self._active_file_handles:
            self.match("LPAREN")
            args = []
            while self.peek() and self.peek()[0] != "RPAREN":
                args.append(self.expr())
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RPAREN")
            return [MethodCall(Var(file_var), method, args)]

        self.match("LPAREN")
        
        if method == "exists":
            path = self.expr()
            self.match("RPAREN")
            return FileExists(path)
        elif method == "delete":
            path = self.expr()
            self.match("RPAREN")
            return FileDelete(path)
        elif method == "rename":
            old_path = self.expr()
            self.match("COMMA")
            new_path = self.expr()
            self.match("RPAREN")
            return FileRename(old_path, new_path)
        elif method == "copy":
            src_path = self.expr()
            self.match("COMMA")
            dst_path = self.expr()
            self.match("RPAREN")
            return FileCopy(src_path, dst_path)
        elif method == "new":
            path = self.expr()
            self.match("RPAREN")
            self.match("LBRACE")
            body = []
            while self.peek() and self.peek()[0] != "RBRACE":
                body += self.stmt_list_item()
            self.match("RBRACE")
            return FileNew(path, body)
        else:
            return None  # Will be handled as file handle method

    def ident_stmt(self):
        # Accept both IDENT and TYPE tokens for variable names (e.g., "list" is a TYPE but can be a variable name)
        tok = self.peek()
        if tok and tok[0] in ("IDENT", "TYPE"):
            name = self.match(tok[0])
        else:
            name = self.match("IDENT")
        # Handle Assignment
        if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "=":
            self.match("OP")
            return Assign(name, self.expr())

        # Handle thread.wait(1, 2) -> ThreadWait
        if name == "thread" and self.peek() and self.peek()[0] == "DOT":
            self.match("DOT")
            attr = self.match("IDENT")
            if attr == "wait":
                self.match("LPAREN")
                ids = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    ids.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                self.match("RPAREN")
                return ThreadWait(ids)

        # Handle os.start(n), os.run(...), os(n).drop()
        if name == "os":
            return self._parse_os_stmt()

        # Handle Drop intercept
        res = self.parse_identifier_chain(name)
        # Handle field assignment: p.name = value
        if isinstance(res, FieldAccess) and self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "=":
            self.match("OP")
            return FieldAssign(res.obj, res.field, self.expr())
        if isinstance(res, FieldAccess) and res.field == "drop":
            return Drop(res.obj.name)
        if isinstance(res, MethodCall) and res.method == "drop":
            obj_name = res.obj.name if hasattr(res.obj, "name") else str(res.obj)
            return Drop(obj_name)
        return res

    def _parse_os_stmt(self):
        """Parse os.start(n), os.run(n, ...), os(n).drop()"""
        # os(n).drop()
        if self.peek() and self.peek()[0] == "LPAREN":
            self.match("LPAREN")
            id_expr = self.expr()
            self.match("RPAREN")
            self.match("DOT")
            method = self.match("IDENT")
            if method == "drop":
                return OsDrop(id_expr)
            raise SyntaxError(f"Unknown os() method: {method}")
        # os.start(...) or os.run(...)
        self.match("DOT")
        method = self.match("IDENT")
        if method == "start":
            self.match("LPAREN")
            id_expr = self.expr()
            self.match("RPAREN")
            return OsStart(id_expr)
        if method == "run":
            self.match("LPAREN")
            # Detect struct form: os.run({ cmd: ..., args: ..., input: ... })
            if self.peek() and self.peek()[0] == "LBRACE":
                struct_args = self._parse_os_run_struct()
                self.match("RPAREN")
                return OsRun(None, None, struct_args=struct_args)
            # Normal form: os.run(id, cmd) or os.run(id, cmd, input)
            id_expr = self.expr()
            self.match("COMMA")
            cmd_expr = self.expr()
            input_expr = None
            if self.peek() and self.peek()[0] == "COMMA":
                self.match("COMMA")
                input_expr = self.expr()
            self.match("RPAREN")
            return OsRun(id_expr, cmd_expr, input_expr=input_expr)
        raise SyntaxError(f"Unknown os module method: {method}")

    def _parse_os_run_struct(self):
        """Parse { cmd: expr, args: [...], input: expr }"""
        self.match("LBRACE")
        fields = {}
        while self.peek() and self.peek()[0] != "RBRACE":
            key = self.match("IDENT")
            self.match("COLON")
            val = self.expr()
            fields[key] = val
            if self.peek() and self.peek()[0] == "COMMA":
                self.match("COMMA")
        self.match("RBRACE")
        return fields

    def expr(self):       return self.logical_or()
    def logical_or(self):
        left = self.logical_and()
        while self.peek() and self.peek()[0] == "OR":
            self.match("OR");  left = BinOp(left, "or", self.logical_and())
        return left
    def logical_and(self):
        left = self.comparison()
        while self.peek() and self.peek()[0] == "AND":
            self.match("AND"); left = BinOp(left, "and", self.comparison())
        return left
    def comparison(self):
        left = self.cast_expr()
        while self.peek() and self.peek()[1] in ("==","!=","<",">","<=",">="):
            op = self.match("OP"); left = Compare(left, op, self.cast_expr())
        return left
    def cast_expr(self):
        left = self.arithmetic()
        while self.peek() and self.peek()[0] == "AS":
            self.match("AS"); left = TypeCast(left, self.match("TYPE"))
        return left
    def arithmetic(self):
        left = self.term()
        while self.peek() and self.peek()[1] in ("+","-"):
            op = self.match("OP"); left = BinOp(left, op, self.term())
        return left
    def term(self):
        left = self.factor()
        while self.peek() and self.peek()[1] in ("*","/","**"):
            op = self.match("OP")
            if op == "**":
                left = BinOp(left, "**", self.factor())
            else:
                left = BinOp(left, op, self.factor())
        return left

    def factor(self):
        tok = self.peek()
        if tok is None: return Number(0)

        # Handle square root operator (unary) - must be checked BEFORE general OP handling
        if tok[0] == "OP" and tok[1] == "*/":
            op = self.match("OP")
            return UnaryOp("*/", self.factor())

        # Unary minus: -expr
        if tok[0] == "OP" and tok[1] == "-":
            self.match("OP")
            return UnaryOp("-", self.factor())

        if tok[0] == "LBRACKET":
            self.advance()
            if self.peek() and self.peek()[0] == "RBRACKET":
                self.advance()
                return ListExpr([])
            first_expr = self.expr()
            if self.peek() and self.peek()[0] == "COLON":
                self.match("COLON")
                pairs = [(first_expr, self.expr())]
                while self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
                    if self.peek() and self.peek()[0] == "RBRACKET": break
                    k = self.expr(); self.match("COLON"); v = self.expr()
                    pairs.append((k, v))
                self.match("RBRACKET")
                return DictExpr(pairs)
            else:
                elements = [first_expr]
                while self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
                    if self.peek() and self.peek()[0] == "RBRACKET": break
                    elements.append(self.expr())
                self.match("RBRACKET")
                return ListExpr(elements)

        if tok[0] == "LBRACE":
            self.advance()
            if self.peek() and self.peek()[0] == "RBRACE":
                self.advance()
                return DictExpr([])
            pairs = []
            while self.peek() and self.peek()[0] != "RBRACE":
                k = self.expr()
                self.match("OP")
                v = self.expr()
                pairs.append((k, v))
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RBRACE")
            return DictExpr(pairs)

        if tok[0] == "NUMBER":
            self.advance()
            if '.' in tok[1]: return Number(float(tok[1]))
            return Number(int(tok[1]))
        if tok[0] == "BOOL":
            self.advance()
            if tok[1] == "None": return None_()
            return Bool(tok[1] == "True")
        if tok[0] == "STRING":
            self.advance()
            res = Str(tok[1][1:-1])
            # Allow method chaining on string literals: "foo".len(), "foo".combine(...)
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match("IDENT")
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = MethodCall(res, attr, args)
                else:
                    res = FieldAccess(res, attr)
            return res
        if tok[0] == "ISTRING":
            self.advance()
            # tok[1] is like i"Hello {x} world {y}"
            # Strip the leading i and the surrounding quotes
            raw = tok[1][2:-1]  # remove i" and "
            return self._parse_istring_parts(raw)

        if tok[0] == "NOT":
            self.advance(); return UnaryOp("not", self.factor())
        if tok[0] == "LPAREN":
            self.advance(); e = self.expr(); self.match("RPAREN"); return e

        # FILE token used as file handle variable inside open() block
        if tok[0] == "FILE" and tok[1] in self._active_file_handles:
            self.advance()
            var_name = tok[1]
            res = Var(var_name)
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match("IDENT")
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = MethodCall(res, attr, args)
                else:
                    res = FieldAccess(res, attr)
            return res
        # TYPE tokens used as arguments (e.g. "404".to(i32) — i32 appears as TYPE token)
        # Also handle TYPE tokens used as variable names (e.g., "list" is a type but can be a variable)
        if tok[0] == "TYPE":
            self.advance()
            name = tok[1]
            res = Var(name)
            # Check for collection access like list(a + 1) or method calls
            while self.peek():
                if self.peek()[0] == "DOT":
                    self.match("DOT")
                    attr = self.match("IDENT")
                    if self.peek() and self.peek()[0] == "LPAREN":
                        self.match("LPAREN")
                        args = []
                        while self.peek() and self.peek()[0] != "RPAREN":
                            args.append(self.expr())
                            if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                        self.match("RPAREN")
                        res = MethodCall(res, attr, args)
                    else:
                        res = FieldAccess(res, attr)
                elif self.peek()[0] == "LPAREN":
                    # TYPE as variable name followed by call - treat as FnCall for collection access
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = FnCall(name, args)
                    # Continue loop to allow method chaining on collection access
                    if self.peek() and self.peek()[0] == "DOT":
                        continue
                    break
                else:
                    break
            return res

        if tok[0] == "IDENT":
            self.advance()
            name = tok[1]
            # FFI("path") load expression
            if name == "FFI" and self.peek() and self.peek()[0] == "LPAREN":
                self.match("LPAREN")
                path_expr = self.expr()
                self.match("RPAREN")
                return FFILoad(path_expr)
            # os.run(...) in expression context
            if name == "os" and self.peek() and self.peek()[0] == "DOT":
                saved = self.pos
                self.match("DOT")
                method = self.match("IDENT")
                if method == "run":
                    self.match("LPAREN")
                    if self.peek() and self.peek()[0] == "LBRACE":
                        struct_args = self._parse_os_run_struct()
                        self.match("RPAREN")
                        return OsRun(None, None, struct_args=struct_args)
                    id_expr = self.expr()
                    self.match("COMMA")
                    cmd_expr = self.expr()
                    input_expr = None
                    if self.peek() and self.peek()[0] == "COMMA":
                        self.match("COMMA")
                        input_expr = self.expr()
                    self.match("RPAREN")
                    return OsRun(id_expr, cmd_expr, input_expr=input_expr)
                self.pos = saved
            # Handle thread.wait(...) in expression context
            if name == "thread" and self.peek() and self.peek()[0] == "DOT":
                saved_pos = self.pos
                self.match("DOT")
                attr = self.match("IDENT")
                if attr == "wait":
                    self.match("LPAREN")
                    ids = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        ids.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    return ThreadWait(ids)
                self.pos = saved_pos
            res = Var(name)
            while self.peek():
                if self.peek()[0] == "DOT":
                    self.match("DOT")
                    attr = self.match("IDENT")
                    if self.peek() and self.peek()[0] == "LPAREN":
                        self.match("LPAREN")
                        args = []
                        while self.peek() and self.peek()[0] != "RPAREN":
                            args.append(self.expr())
                            if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                        self.match("RPAREN")
                        res = MethodCall(res, attr, args)
                    else:
                        res = FieldAccess(res, attr)
                elif self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    
                    if isinstance(res, Var):
                        if res.name == "thread" and len(args) == 2: res = ThreadCall(args[0], args[1])
                        elif res.name == "input": res = Input(args[0] if args else None)
                        elif res.name == "file_read" and len(args) == 1: res = FileRead(args[0])
                        else: res = FnCall(res.name, args)
                    else:
                        res = FnCall(res, args)
                else:
                    break
            return res

        self.advance()
        return Number(0)

    def _parse_istring_parts(self, raw):
        """Parse an i"..." string body into an InterpolatedStr node.
        Splits on {identifier} patterns. Each piece is either a Str literal
        or a Var node (simple variable reference)."""
        import re
        parts = []
        # Split on {varname} tokens - support simple identifiers only
        pattern = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
        last = 0
        for m in pattern.finditer(raw):
            # text before this placeholder
            before = raw[last:m.start()]
            if before:
                parts.append(Str(before))
            parts.append(Var(m.group(1)))
            last = m.end()
        # trailing text
        after = raw[last:]
        if after:
            parts.append(Str(after))
        # If nothing was parsed (no interpolations), just return a plain Str
        if not parts:
            return Str(raw)
        # If only one part and it's a Str, return it directly
        if len(parts) == 1 and isinstance(parts[0], Str):
            return parts[0]
        return InterpolatedStr(parts)