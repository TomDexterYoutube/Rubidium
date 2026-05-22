from ast import *

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self):
        self.pos += 1

    def match(self, kind):
        tok = self.peek()
        if tok and tok[0] == kind:
            self.advance()
            return tok[1]
        return None

    # ── top-level program ────────────────────────────────────────

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

    # ── import / use ─────────────────────────────────────────────

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

    # ── class ────────────────────────────────────────────────────

    def class_def(self):
        self.match("CLASS")
        name = self.match("IDENT")
        self.match("LPAREN")
        self.match("RPAREN")
        self.match("LBRACE")
        fields = []
        while self.peek() and self.peek()[0] != "RBRACE":
            if self.peek()[0] == "LET":
                fields.append(self.var_decl())
            else:
                self.advance()
        self.match("RBRACE")
        return ClassDef(name, fields)

    # ── function ─────────────────────────────────────────────────

    def fn_def(self):
        self.match("FN")
        name = self.match("IDENT")
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

    # ── block (inside braces) ─────────────────────────────────────

    def block(self):
        stmts = []
        while self.peek() and self.peek()[0] != "RBRACE":
            stmts += self.stmt_list_item()
        return stmts

    # ── single statement dispatcher ───────────────────────────────
    # Returns a LIST so for-range can expand cleanly if needed.

    def stmt_list_item(self):
        t = self.peek()
        if t is None:
            return []
        if   t[0] == "LET":      return [self.var_decl()]
        elif t[0] == "PRINT":    return [self.print_stmt()]
        elif t[0] == "IF":       return [self.if_stmt()]
        elif t[0] == "WHILE":    return [self.while_stmt()]
        elif t[0] == "FOR":      return [self.for_stmt()]
        elif t[0] == "RETURN":   return [self.return_stmt()]
        elif t[0] == "TRY":      return [self.try_stmt()]
        elif t[0] == "IDENT":    return [self.ident_stmt()]
        else:
            self.advance()
            return []

    # ── variable declaration ──────────────────────────────────────

    def var_decl(self):
        self.match("LET")
        mut = bool(self.match("MUT"))
        name = self.match("IDENT")
        vtype = None
        if self.peek() and self.peek()[0] == "TYPE":
            vtype = self.match("TYPE")
        self.match("OP")   # consumes '='
        value = self.expr()
        return VarDecl(name, mut, vtype, value)

    # ── print ─────────────────────────────────────────────────────

    def print_stmt(self):
        self.match("PRINT")
        self.match("LPAREN")
        val = self.expr()
        self.match("RPAREN")
        return Print(val)

    # ── if / else ─────────────────────────────────────────────────

    def if_stmt(self):
        self.match("IF")
        cond = self.expr()
        self.match("LBRACE")
        then_body = self.block()
        self.match("RBRACE")
        else_body = None
        if self.peek() and self.peek()[0] == "ELSE":
            self.match("ELSE")
            if self.peek() and self.peek()[0] == "IF":
                else_body = [self.if_stmt()]
            else:
                self.match("LBRACE")
                else_body = self.block()
                self.match("RBRACE")
        return If(cond, then_body, else_body)

    # ── while ─────────────────────────────────────────────────────

    def while_stmt(self):
        self.match("WHILE")
        cond = self.expr()
        self.match("LBRACE")
        body = self.block()
        self.match("RBRACE")
        return While(cond, body)

    # ── for ───────────────────────────────────────────────────────

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
        else:
            start = self.expr()
            if self.peek() and self.peek()[0] == "COMMA":
                self.match("COMMA")
            end = self.expr()
        self.match("LBRACE")
        body = self.block()
        self.match("RBRACE")
        return For(var, start, end, body)

    # ── return ────────────────────────────────────────────────────

    def return_stmt(self):
        self.match("RETURN")
        return Return(self.expr())

    # ── try / on_error ────────────────────────────────────────────

    def try_stmt(self):
        self.match("TRY")
        self.match("LBRACE")
        try_body = self.block()
        self.match("RBRACE")
        self.match("ON_ERROR")
        self.match("LBRACE")
        err_body = self.block()
        self.match("RBRACE")
        return Try(try_body, err_body)

    # ── ident-started statements ──────────────────────────────────
    # Handles: assign, field-assign, fn-call, method-call, thread, drop

    def ident_stmt(self):
        name = self.match("IDENT")

        # obj.method(...) or obj.field = ...
        if self.peek() and self.peek()[0] == "DOT":
            self.match("DOT")
            attr = self.match("IDENT")

            # x.drop()
            if attr == "drop":
                self.match("LPAREN")
                self.match("RPAREN")
                return Drop(name)

            # thread.wait(ids...)
            if name == "thread" and attr == "wait":
                self.match("LPAREN")
                ids = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    ids.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA":
                        self.match("COMMA")
                self.match("RPAREN")
                return ThreadWait([int(a.value) if isinstance(a, Number) else 0 for a in ids])

            # obj.field = value
            if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "=":
                self.match("OP")
                val = self.expr()
                return FieldAssign(name, attr, val)

            # obj.method(args)
            self.match("LPAREN")
            args = []
            while self.peek() and self.peek()[0] != "RPAREN":
                args.append(self.expr())
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RPAREN")
            return MethodCall(name, attr, args)

        # fn(args) or thread(fn(), id)
        if self.peek() and self.peek()[0] == "LPAREN":
            self.match("LPAREN")
            args = []
            while self.peek() and self.peek()[0] != "RPAREN":
                args.append(self.expr())
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RPAREN")
            if name == "thread" and len(args) == 2:
                fc = args[0]
                tid = args[1]
                return ThreadCall(fc, tid.value if isinstance(tid, Number) else 0)
            return FnCall(name, args)

        # name = expr  (plain assignment)
        if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "=":
            self.match("OP")
            return Assign(name, self.expr())

        # bare identifier as statement (shouldn't normally happen)
        return Var(name)

    # ── expressions ───────────────────────────────────────────────

    def expr(self):       return self.logical_or()

    def logical_or(self):
        left = self.logical_and()
        while self.peek() and self.peek()[0] == "OR":
            self.match("OR");  right = self.logical_and()
            left = BinOp(left, "or", right)
        return left

    def logical_and(self):
        left = self.comparison()
        while self.peek() and self.peek()[0] == "AND":
            self.match("AND");  right = self.comparison()
            left = BinOp(left, "and", right)
        return left

    def comparison(self):
        left = self.arithmetic()
        while self.peek() and self.peek()[1] in ("==","!=","<",">","<=",">="):
            op = self.match("OP");  right = self.arithmetic()
            left = Compare(left, op, right)
        return left

    def arithmetic(self):
        left = self.term()
        while self.peek() and self.peek()[1] in ("+","-"):
            op = self.match("OP");  right = self.term()
            left = BinOp(left, op, right)
        return left

    def term(self):
        left = self.factor()
        while self.peek() and self.peek()[1] in ("*","/"):
            op = self.match("OP");  right = self.factor()
            left = BinOp(left, op, right)
        return left

    def factor(self):
        tok = self.peek()
        if tok is None:
            return Number(0)

        if tok[0] == "NUMBER":
            self.advance();  return Number(int(tok[1]))

        if tok[0] == "BOOL":
            self.advance();  return Bool(tok[1] == "True")

        if tok[0] == "STRING":
            self.advance();  return Str(tok[1][1:-1])

        if tok[0] == "NOT":
            self.advance();  return UnaryOp("not", self.factor())

        if tok[0] == "LPAREN":
            self.advance();  e = self.expr();  self.match("RPAREN");  return e

        if tok[0] == "IDENT":
            self.advance()
            name = tok[1]

            # field access or method call in expression: obj.field  /  obj.method()
            if self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match("IDENT")
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA":
                            self.match("COMMA")
                    self.match("RPAREN")
                    return MethodCall(name, attr, args)
                # plain field access: stuff.x
                return FieldAccess(name, attr)

            # function call in expression
            if self.peek() and self.peek()[0] == "LPAREN":
                self.match("LPAREN")
                args = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    args.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA":
                        self.match("COMMA")
                self.match("RPAREN")
                # thread(fn(), id) in expression position
                if name == "thread" and len(args) == 2:
                    fc   = args[0]
                    tid  = args[1]
                    return ThreadCall(fc, tid.value if isinstance(tid, Number) else 0)
                return FnCall(name, args)

            return Var(name)

        # fallthrough
        self.advance()
        return Number(0)
