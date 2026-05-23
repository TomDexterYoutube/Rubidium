from ast import *

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.line_no = 1

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
        elif t[0] == "IF":       return [self.if_stmt()]
        elif t[0] == "WHILE":    return [self.while_stmt()]
        elif t[0] == "FOR":      return [self.for_stmt()]
        elif t[0] == "BREAK":    self.match("BREAK"); return [Break()]
        elif t[0] == "RETURN":   return [self.return_stmt()]
        elif t[0] == "TRY":      return [self.try_stmt()]
        elif t[0] == "IDENT":    return [self.ident_stmt()]
        else:
            self.advance()
            return []

    def var_decl(self):
        self.match("LET")
        mut = bool(self.match("MUT"))
        name = self.match("IDENT")
        vtype = None
        if self.peek() and self.peek()[0] == "TYPE":
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
        self.match("ON_ERROR")
        self.match("LBRACE"); err_body = self.block(); self.match("RBRACE")
        return Try(try_body, err_body)

    def ident_stmt(self):
        name = self.match("IDENT")
        res = Var(name)
        while self.peek():
            if self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match("IDENT")
                if attr == "drop":
                    self.match("LPAREN"); self.match("RPAREN")
                    if isinstance(res, Var): return Drop(res.name)
                    return Drop(name)
                    
                if isinstance(res, Var) and res.name == "thread" and attr == "wait":
                    self.match("LPAREN")
                    ids = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        ids.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    return ThreadWait(ids)
                    
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = MethodCall(res, attr, args)
                else:
                    if self.peek() and self.peek()[0] == "OP" and self.peek()[1] == "=":
                        self.match("OP")
                        return FieldAssign(res.obj if isinstance(res, FieldAccess) else res, attr, self.expr())
                    res = FieldAccess(res, attr)
                    
            elif self.peek()[0] == "LPAREN":
                self.match("LPAREN")
                args = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    args.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                self.match("RPAREN")
                
                if isinstance(res, Var):
                    if res.name == "thread" and len(args) == 2: return ThreadCall(args[0], args[1])
                    if res.name == "file_write" and len(args) == 2: return FileWrite(args[0], args[1])
                    res = FnCall(res.name, args)
                else:
                    res = FnCall(res, args)
                    
            elif self.peek()[0] == "OP" and self.peek()[1] == "=":
                self.match("OP")
                if isinstance(res, Var): return Assign(res.name, self.expr())
                break
            else:
                break
        return res

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
        while self.peek() and self.peek()[1] in ("*","/"):
            op = self.match("OP"); left = BinOp(left, op, self.factor())
        return left

    def factor(self):
        tok = self.peek()
        if tok is None: return Number(0)

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
            self.advance(); return Str(tok[1][1:-1])
        if tok[0] == "NOT":
            self.advance(); return UnaryOp("not", self.factor())
        if tok[0] == "LPAREN":
            self.advance(); e = self.expr(); self.match("RPAREN"); return e

        if tok[0] == "IDENT":
            self.advance()
            name = tok[1]
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