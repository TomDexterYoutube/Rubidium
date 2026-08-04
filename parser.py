from rub_ast import *

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.line_no = 1
        self._active_file_handles = set()  # var names bound via open() as varname
        # BUGFIX/FEATURE (bugs.log #2): SY is a compile-time name-substitution
        # mechanism — `let x: SY = 'literal'` has no runtime existence; it just
        # records x -> "literal" here. `(x)` used anywhere a name is expected
        # (a `let`/`fn` declaration name, or a value/call-target reference)
        # is replaced with the recorded literal at parse time.
        self.sy_table = {}
        # BUGFIX/FEATURE (bugs.log #9): tracks EVERY name ever declared as
        # `SY` type, whether its value is a literal (also in sy_table) or a
        # computed/concatenated expression (only trackable here, since its
        # actual value isn't known until runtime). Variable-side reflection
        # (`let (x): T = ...` and later `x(...)` access) always routes through
        # this dynamic set now; sy_table is kept only for the still-static
        # `fn (name)() {...}` function-reflection case.
        self.sy_names = set()
        # BUGFIX/FEATURE (bugs.log #9): `(name)(...)` is used for BOTH calling
        # a reflected function (fn_def()'s static path — needs a compile-time
        # resolvable target) and accessing a reflected variable (the new
        # dynamic path). Same syntax, different meaning — disambiguated by
        # tracking which holder names were actually used in a `fn (name)()`
        # definition; those keep going through the old static substitution,
        # everything else in sy_names goes through the new dynamic path.
        self.sy_fn_names = set()

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self):
        if self.pos < len(self.tokens):
            self.line_no = self.tokens[self.pos][2] if len(self.tokens[self.pos]) > 2 else self.line_no
        self.pos += 1

    def _mark_dictplus(self, node):
        """FEATURE: dict+ — recursively mark a DictExpr (and any nested
        DictExpr values within its pairs, to unlimited depth) as dict+,
        so codegen knows to create each level with the dict+ magic number
        instead of a regular dict's. Only the declaration site
        (`let x: dict+ = {...}`) knows the intended type; nested `{...}`
        blocks written directly inside that same literal are statically
        known at parse time and marked here too."""
        if not isinstance(node, DictExpr):
            return
        node.is_dictplus = True
        for _, v in node.pairs:
            self._mark_dictplus(v)

    def _resolve_sy_name(self):
        """BUGFIX/FEATURE (bugs.log #2): if the parser is sitting on
        `( IDENT )` where IDENT is a known SY symbol, consume all three
        tokens and return the substituted literal name. Otherwise leaves
        the token stream untouched and returns None (so callers fall back
        to normal parsing — e.g. an ordinary parenthesized expression)."""
        if (self.peek() and self.peek()[0] == "LPAREN"
                and self.pos + 2 < len(self.tokens)
                and self.tokens[self.pos + 1][0] == "IDENT"
                and self.tokens[self.pos + 1][1] in self.sy_table
                and self.tokens[self.pos + 2][0] == "RPAREN"):
            sy_name = self.tokens[self.pos + 1][1]
            self.advance(); self.advance(); self.advance()  # LPAREN, IDENT, RPAREN
            literal = self.sy_table[sy_name]
            # The literal is used directly as a real variable/function name at
            # the LLVM level, which requires a valid bare identifier (letters,
            # digits, underscore). SY literals are free-form text (the spec's
            # own examples use names with spaces, e.g. 'New varable'), so
            # sanitize any non-identifier characters to underscores. This is
            # purely an internal-symbol concern — the raw literal was never
            # otherwise typeable/visible in source anyway, since it's only
            # ever reached again through another `(name)` substitution.
            import re as _re
            safe = _re.sub(r"[^a-zA-Z0-9_]", "_", literal)
            if safe and safe[0].isdigit():
                safe = "_" + safe
            return safe or "_sy_empty"
        return None

    def _check_sy_dynamic_name(self):
        """BUGFIX/FEATURE (bugs.log #9): if the parser is sitting on
        `( IDENT )` where IDENT is a known SY-typed variable (self.sy_names),
        consume the three tokens and return the raw IDENT (not a substituted
        literal — the actual value is only known at runtime). Otherwise
        leaves the token stream untouched and returns None."""
        if (self.peek() and self.peek()[0] == "LPAREN"
                and self.pos + 2 < len(self.tokens)
                and self.tokens[self.pos + 1][0] == "IDENT"
                and self.tokens[self.pos + 1][1] in self.sy_names
                and self.tokens[self.pos + 1][1] not in self.sy_fn_names
                and self.tokens[self.pos + 2][0] == "RPAREN"):
            holder = self.tokens[self.pos + 1][1]
            self.advance(); self.advance(); self.advance()  # LPAREN, IDENT, RPAREN
            return holder
        return None

    def match(self, kind):
        tok = self.peek()
        if tok and tok[0] == kind:
            self.advance()
            return tok[1]
        return None

    def match_attr(self):
        """Match any token as a method/field name after a dot.
        Keywords like 'range', 'int', 'float', 'type' are valid method names
        when they follow a dot (e.g. random.range, random.int, random.float)."""
        tok = self.peek()
        if tok:
            self.advance()
            return tok[1]
        return None

    def parse(self):
        stmts = []
        while self.peek():
            t = self.peek()
            if   t[0] == "IMPORT": stmts.append(self.import_stmt())
            elif t[0] == "XEON":   stmts.append(self.import_stmt(is_xeon_pkg=True))
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
            if self.peek()[0] == "DOT":
                self.advance()  # consume DOT
                attr = self.match_attr()  # consume the IDENT after the dot
                if self.peek() and self.peek()[0] == "LPAREN":
                    # Method call: DOT IDENT LPAREN args RPAREN
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    res = MethodCall(res, attr, args)
                else:
                    # Field access: DOT IDENT
                    res = FieldAccess(res, attr)
            elif self.peek()[0] == "LPAREN":
                # Call: LPAREN args RPAREN
                self.match("LPAREN")
                args = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    args.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                self.match("RPAREN")
                # Intercept specialized built-ins for direct calls
                if isinstance(res, Var):
                    if res.name == "thread" and len(args) == 2:
                        res = ThreadCall(args[0], args[1])
                    elif res.name == "input":
                        res = Input(args[0] if args else None)
                    elif res.name in self.sy_names and res.name not in self.sy_fn_names:
                        # BUGFIX/FEATURE (bugs.log #9): statement-level bare
                        # `tmp(...)` (e.g. `tmp().add("w")` as a full
                        # statement) goes through THIS function, not
                        # factor()'s IDENT branch — needs the same dynamic
                        # SY-reflection routing applied there.
                        res = FnCall(DynResolve(res.name), args)
                    else:
                        res = FnCall(res.name, args)
                else:
                    res = FnCall(res, args)
            else:
                break
        return res

    def import_stmt(self, is_xeon_pkg=False):
        self.match("XEON" if is_xeon_pkg else "IMPORT")
        # `import local X` — a private, per-importer instance of the module
        # (mirrors `let local`: global by default, `local` for your own copy).
        is_local = False
        if not is_xeon_pkg and self.peek() and self.peek()[0] == "LOCAL":
            self.match("LOCAL")
            is_local = True
        # For xeon packages, allow hyphens in package names (e.g. config-wiz)
        if is_xeon_pkg:
            # Build package name allowing hyphens
            parts = [self.match("IDENT")]
            while self.peek() and self.peek()[0] in ("DOT", "OP"):
                op = self.match(self.peek()[0])
                if op == "-":
                    parts.append(op + self.match("IDENT"))
                else:
                    parts.append(op + self.match("IDENT"))
            name = "".join(parts)
        else:
            name = self.match("IDENT")
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                name += "." + self.match("IDENT")
        alias = None
        if self.peek() and self.peek()[0] == "AS":
            self.match("AS")
            alias = self.match("IDENT")
        return Import(name, alias=alias, is_xeon_pkg=is_xeon_pkg, is_local=is_local)

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
        # BUGFIX/FEATURE (bugs.log #2): `fn (function_name) { ... }` — dynamic
        # function name substituted from a previously-declared SY symbol.
        # BUGFIX (bugs.log #9): record the holder name (before substitution)
        # so factor() knows this SY name means "reflected function", not
        # "reflected variable", when it's called later.
        if (self.peek() and self.peek()[0] == "LPAREN"
                and self.pos + 2 < len(self.tokens)
                and self.tokens[self.pos + 1][0] == "IDENT"
                and self.tokens[self.pos + 1][1] in self.sy_table):
            self.sy_fn_names.add(self.tokens[self.pos + 1][1])
        sy_name = self._resolve_sy_name()
        name = sy_name if sy_name is not None else self.match("IDENT")
        # FFI binding: fn handle_name symbol_name(params) -> ret  (no body brace)
        # Detected when the token after `name` is another IDENT (not LPAREN)
        if self.peek() and self.peek()[0] == "IDENT":
            symbol_name = self.match("IDENT")
            self.match("LPAREN")
            params = []
            while self.peek() and self.peek()[0] != "RPAREN":
                pname = self.match("IDENT") or self.match("TYPE")
                self.match("COLON")
                # BUGFIX (bugs.log #18): a class-typed parameter (e.g.
                # `x: Warehouse`) is tokenized as IDENT, not TYPE — TYPE only
                # covers built-in type keywords. self.match("TYPE") alone
                # would silently fail here (consuming nothing), leaving the
                # class-name token unconsumed; the next loop iteration would
                # then misparse it as an entirely separate bogus parameter,
                # corrupting the whole list and shifting every later
                # parameter's positional binding.
                ptype = self.match("TYPE") or self.match("IDENT")
                params.append((pname, ptype))
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RPAREN")
            ret_type = None
            if self.peek() and self.peek()[1] == "->":
                self.match("OP")
                ret_type = self.match("TYPE")
            alias = None
            if self.peek() and self.peek()[0] == "AS":
                self.match("AS")
                alias = self.match("IDENT")
            return FFIBind(name, symbol_name, params, ret_type, alias=alias)
        # Normal function
        # BUGFIX (bugs.log #2): the spec's `fn (function_name) { ... }` form
        # has NO parameter-list parentheses at all — the substituted name
        # occupies the name slot and the body brace follows directly. Without
        # this check, `self.match("LPAREN")` below would silently fail to
        # consume anything (next token is LBRACE), and the parameter-parsing
        # while-loop would then spin forever never seeing RPAREN — an
        # infinite loop/hang. Only require the parens when they're actually
        # there.
        params = []
        if self.peek() and self.peek()[0] == "LPAREN":
            self.match("LPAREN")
            while self.peek() and self.peek()[0] != "RPAREN":
                # Parameter name can be an identifier or a keyword (like file, var, etc.)
                pname = self.match("IDENT") or self.match("TYPE") or self.match("FILE") or self.match("VAR")
                self.match("COLON")
                # BUGFIX (bugs.log #18): see the identical fix in the FFI
                # binding branch above — class-typed parameters are
                # tokenized as IDENT, not TYPE.
                ptype = self.match("TYPE") or self.match("IDENT")
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
        if t[0] == "RBRACE": return []
        if   t[0] == "IMPORT":  return [self.import_stmt()]
        elif t[0] == "XEON":    return [self.import_stmt(is_xeon_pkg=True)]
        elif t[0] == "USE":     return [self.use_stmt()]
        elif t[0] == "LET":     return [self.var_decl()]
        elif t[0] == "PRINT":   return [self.print_stmt()]
        elif t[0] == "PRINTLN": return [self.println_stmt()]
        elif t[0] == "IF":      return [self.if_stmt()]
        elif t[0] == "WHILE":   return [self.while_stmt()]
        elif t[0] == "FOR":     return [self.for_stmt()]
        elif t[0] == "BREAK":   self.match("BREAK"); return [Break()]
        elif t[0] == "CONTINUE":self.match("CONTINUE"); return [Continue()]
        elif t[0] == "RETURN":  return [self.return_stmt()]
        elif t[0] == "TRY":     return [self.try_stmt()]
        elif t[0] == "RAISE":
            self.match("RAISE")
            return [Raise(self.expr())]
        elif t[0] == "FN":      return [self.fn_def()]
        elif t[0] == "OPEN":    return [self.open_stmt()]
        elif t[0] == "FILE":    
            result = self.file_global_stmt_list()
            if result is not None:
                return [result]
            return []
        elif t[0] == "IDENT" or t[0] == "TYPE":   return [self.ident_stmt()]
        elif t[0] == "LPAREN":
            # BUGFIX/FEATURE (bugs.log #2): a statement starting with `(` is a
            # dynamic SY call/reference, e.g. `(function_name)()`. Previously
            # unhandled here, so it silently fell into the generic "unknown
            # token, skip one and continue" branch below, one token at a time
            # — the call was never actually parsed or emitted.
            return [self.expr()]
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
        is_local = bool(self.match("LOCAL"))
        # BUGFIX/FEATURE (bugs.log #9): `let (varname): TYPE = ...` where
        # varname is a runtime-dynamic SY variable — the identity of the
        # declared thing is only known at runtime (it's whatever varname's
        # current string value is), so this can't be resolved to a plain
        # VarDecl at parse time at all. Produces a DynVarDecl instead.
        dyn_holder = self._check_sy_dynamic_name()
        if dyn_holder is not None:
            vtype = None
            if self.peek() and self.peek()[0] == "COLON":
                self.match("COLON")
                vtype = self.match("TYPE")
            elif self.peek() and self.peek()[0] == "TYPE":
                vtype = self.match("TYPE")
            if vtype in ("list", "index", "dict") and self.peek() and self.peek()[0] == "COLON":
                self.match("COLON")
                self.match("TYPE")
            self.match("OP")
            value = self.expr()
            # BUGFIX (bugs.log #13): `let (x): index = []` — an empty `[]`
            # literal is syntactically indistinguishable from an empty list
            # (no `key: value` pairs to disambiguate), so factor() always
            # returns a plain ListExpr([]) for it. Real codegen then makes
            # this an actual runtime LIST (make_list()), completely ignoring
            # the explicit `index` type annotation — so `d().add("k", 5)`
            # later crashed with "list index 0 out of bounds", since a 2-arg
            # add on a list tries to SET index 5 at position "k", not do a
            # key-value insert. Only the declaration site knows the intended
            # type, so the substitution has to happen here.
            if vtype == "index" and isinstance(value, ListExpr) and not value.elements:
                value = DictExpr([], is_index=True)
            if vtype == "dict+":
                self._mark_dictplus(value)
            return DynVarDecl(dyn_holder, mut, is_local, vtype, value)
        # BUGFIX/FEATURE (bugs.log #2): `let (function_name)...` — this is
        # only still reachable for the STATIC (literal-valued) SY case here,
        # since any SY name is now also in sy_names and would have been
        # caught above; kept as a defensive fallback, not the primary path.
        sy_name = self._resolve_sy_name()
        if sy_name is not None:
            name = sy_name
        else:
            # Accept both IDENT and TYPE tokens for variable names (e.g., "list" is a TYPE but can be a variable name)
            # Also check if this is a file handle variable inside an open() block
            tok = self.peek()
            if tok and tok[0] in ("IDENT", "TYPE"):
                name = self.match(tok[0])  # match returns the value
            elif tok and tok[0] == "FILE":
                name = self.match("FILE")
            else:
                name = self.match("IDENT")
        vtype = None
        if self.peek() and self.peek()[0] == "COLON":
            self.match("COLON")
            vtype = self.match("TYPE")
        elif self.peek() and self.peek()[0] == "TYPE":
            vtype = self.match("TYPE")
        # Optional element-type annotation for collections: let x: list: i32 = [0,10,32]
        element_type = None
        if vtype in ("list", "index", "dict") and self.peek() and self.peek()[0] == "COLON":
            self.match("COLON")
            element_type = self.match("TYPE")
        self.match("OP")
        value = self.expr()
        # BUGFIX (bugs.log #13): see the identical fix in the DynVarDecl path
        # above — `let x: index = []` must become an actual index (DictExpr
        # with is_index=True), not a plain empty list, or later `x().add(k,
        # v)`-style key-value inserts crash at runtime.
        if vtype == "index" and isinstance(value, ListExpr) and not value.elements:
            value = DictExpr([], is_index=True)
        if vtype == "dict+":
            self._mark_dictplus(value)
        # BUGFIX/FEATURE (bugs.log #9): `let x: SY = <expr>` used to be a
        # pure compile-time mapping with NO runtime existence at all — fine
        # for a literal string, but any non-literal expression (e.g.
        # concatenation with a loop variable, the whole point of generating
        # a fresh name per iteration) was never evaluated; `str(value)` was
        # called on the raw AST node object itself, producing garbage like
        # "<rub_ast.BinOp object at 0x...>" instead of an actual computed
        # string. x is now a REAL runtime string variable (so concatenation,
        # .to(SY), loop variables etc. all just work like any other str
        # expression) and is tracked in sy_names for the dynamic reflection
        # path above / in factor(). sy_table is still populated for literal
        # values, preserving the static `fn (name)() {...}` case unchanged.
        if vtype == "SY":
            self.sy_names.add(name)
            if isinstance(value, Str):
                self.sy_table[name] = value.value
            return VarDecl(name, mut, is_local, "str", value, element_type=element_type)
        return VarDecl(name, mut, is_local, vtype, value, element_type=element_type)

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
        # Allow TYPE keywords (index, dict, str, etc.) as loop variable names
        if self.peek() and self.peek()[0] == "IDENT":
            var = self.match("IDENT")
        elif self.peek() and self.peek()[0] == "TYPE":
            var = self.match("TYPE")
        else:
            var = self.match("IDENT")  # will return None and bubble up as parse error
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

    # Token kinds that can never start an expression — seeing one of these
    # right after 'return' means it's a bare `return` with no value, not the
    # start of an expression that happens to be missing.
    _STMT_BOUNDARY_TOKENS = {
        "RBRACE", "IMPORT", "XEON", "USE", "LET", "PRINT", "PRINTLN", "IF", "WHILE",
        "FOR", "BREAK", "CONTINUE", "RETURN", "TRY", "FN", "OPEN", "CLASS",
        "DROP", "RAISE",
    }

    def return_stmt(self):
        self.match("RETURN")
        nxt = self.peek()
        if nxt is None or nxt[0] in self._STMT_BOUNDARY_TOKENS:
            return Return(None)
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
        # BUGFIX: use match_attr() (accepts any token kind), not match("IDENT").
        # Method names after a dot can collide with reserved keywords — e.g.
        # `file.list(...)` tokenizes "list" as a TYPE token (it's also a
        # collection-type keyword), not IDENT, so match("IDENT") would fail
        # silently (no error, no advance) and leave the parser stuck reading
        # the same TYPE("list") token as if 'file.list(...)' had never been
        # consumed at all — producing garbage downstream.
        method = self.match_attr()

        # If inside an open() block and this matches the handle var, parse as handle method
        # Also parse as handle method if file_var is "file" (default handle for file.new)
        if (file_var in self._active_file_handles or file_var == "file") and method in ("write", "append", "add", "read", "readln", "writeln"):
            self.match("LPAREN")
            args = []
            while self.peek() and self.peek()[0] != "RPAREN":
                args.append(self.expr())
                if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
            self.match("RPAREN")
            return FileHandleStmt(file_var, method, args)

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
        elif method == "list":
            path = self.expr()
            self.match("RPAREN")
            return FileList(path)
        elif method == "new":
            path = self.expr()
            self.match("RPAREN")
            body = []
            if self.peek() and self.peek()[0] == "LBRACE":
                self.match("LBRACE")
                self._active_file_handles.add("file")
                while self.peek() and self.peek()[0] != "RBRACE":
                    body += self.stmt_list_item()
                self.match("RBRACE")
                self._active_file_handles.discard("file")
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

        # Handle thread.wait(1, 2) -> ThreadWait / thread.running(1) -> ThreadRunning
        if name == "thread" and self.peek() and self.peek()[0] == "DOT":
            self.match("DOT")
            attr = self.match_attr()
            if attr == "wait":
                self.match("LPAREN")
                ids = []
                while self.peek() and self.peek()[0] != "RPAREN":
                    ids.append(self.expr())
                    if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                self.match("RPAREN")
                return ThreadWait(ids)
            if attr == "running":
                self.match("LPAREN")
                tid = self.expr()
                self.match("RPAREN")
                return ThreadRunning(tid)

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
            # items(1).drop() — obj is an indexing FnCall/MethodCall chain with
            # args (a collection element/key) — remove-and-shift, NOT a whole-
            # variable drop. items.drop() (obj is a bare Var) drops the whole
            # variable as before.
            if isinstance(res.obj, (FnCall, MethodCall)) and getattr(res.obj, "args", None):
                return ElementDrop(res.obj)
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
            method = self.match_attr()
            if method == "drop":
                return OsDrop(id_expr)
            raise SyntaxError(f"Unknown os() method: {method}")
        # os.start(...) or os.run(...)
        self.match("DOT")
        method = self.match_attr()
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
        left = self.arithmetic()
        while self.peek() and self.peek()[1] in ("==","!=","<",">","<=",">="):
            op = self.match("OP"); left = Compare(left, op, self.arithmetic())
        return left
    def cast_expr(self):
        left = self.factor()
        while self.peek() and self.peek()[0] == "AS":
            self.match("AS"); left = TypeCast(left, self.match("TYPE"))
        return left
    def arithmetic(self):
        left = self.term()
        while self.peek() and self.peek()[1] in ("+","-"):
            op = self.match("OP"); left = BinOp(left, op, self.term())
        return left
    def term(self):
        left = self.cast_expr()
        while self.peek() and self.peek()[1] in ("*","/","**","*/","%"):
            op = self.match("OP")
            if op == "**":
                left = BinOp(left, "**", self.cast_expr())
            elif op == "*/":
                left = BinOp(left, "*/", self.cast_expr())
            else:
                left = BinOp(left, op, self.cast_expr())
        return left

    def factor(self):
        tok = self.peek()
        if tok is None: return Number(0)

        # The Link Rule: `link expr` — pass-by-reference marker, valid
        # anywhere an expression is (in practice, only meaningful as a
        # function-call argument). Handled here (not in every individual
        # call-argument loop) since "link" isn't otherwise a valid term.
        if tok[0] == "IDENT" and tok[1] == "link":
            self.match("IDENT")
            return LinkArg(self.factor())

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
                return DictExpr(pairs, is_index=True)
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
                # Match either OP (=) or COLON (:) as key-value separator
                if self.peek() and self.peek()[0] in ("OP", "COLON"):
                    self.advance()
                else:
                    raise SyntaxError("Expected '=' or ':' in dict literal")
                v = self.expr()
                pairs.append((k, v))
                if self.peek() and self.peek()[0] == "COMMA":
                    self.match("COMMA")
            self.match("RBRACE")
            return DictExpr(pairs)

        if tok[0] == "NUMBER":
            self.advance()
            # OPEN-5: preserve the raw literal text for floats so codegen can
            # emit it directly at fp128 precision when it has more
            # significant digits than a double can hold exactly.
            if '.' in tok[1]: res = Number(float(tok[1]), raw=tok[1])
            # OPEN-8: hex/binary/octal literals (0x.., 0b.., 0o..) — int()
            # with base 0 auto-detects the prefix; plain decimal keeps base 10
            # (base 0 would reject a leading-zero decimal like `03`).
            elif len(tok[1]) > 1 and tok[1][0] == '0' and tok[1][1] in 'xXbBoO':
                res = Number(int(tok[1], 0))
            else: res = Number(int(tok[1]))
            # BUGFIX (bugs.log #10): allow method chaining on number literals
            # (e.g. `0.to(SY)`), matching what STRING literals already
            # support just below. Real code found this via `0.to(SY)`.
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match_attr()
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
        if tok[0] == "BOOL":
            self.advance()
            if tok[1] in ("None", "Null"): return None_()
            return Bool(tok[1] == "True")
        if tok[0] in ("STRING", "STRING3", "SYSTRING"):
            self.advance()
            # BUGFIX (bugs.log #3/#2): STRING3 (triple-quoted `"""..."""`) strips 3
            # quote characters from each side instead of 1; SYSTRING (single-quoted
            # SY literal, e.g. 'New varable') strips 1 single-quote from each side.
            strip_n = 3 if tok[0] == "STRING3" else 1
            res = Str(tok[1][strip_n:-strip_n])
            # Allow method chaining on string literals: "foo".len(), "foo".combine(...)
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match_attr()
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
            # BUGFIX/FEATURE (bugs.log #9): `(varname)` / `(varname)(...)` for
            # a runtime-dynamic SY variable — resolved via the runtime
            # hash-map (DynResolve), not a compile-time literal substitution,
            # since varname's value may differ every time this is reached
            # (e.g. inside a loop). Checked before the old static path.
            dyn_holder = self._check_sy_dynamic_name()
            if dyn_holder is not None:
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    return FnCall(DynResolve(dyn_holder), args)
                return DynResolve(dyn_holder)
            # BUGFIX/FEATURE (bugs.log #2): `(function_name)()` — static
            # reference to a previously-declared LITERAL SY symbol. Only
            # still reachable for names that never went through the dynamic
            # check above (i.e. names not in sy_names at all), so in
            # practice this now mainly serves defensively.
            sy_name = self._resolve_sy_name()
            if sy_name is not None:
                if self.peek() and self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    return FnCall(sy_name, args)
                return Var(sy_name)
            self.advance(); e = self.expr(); self.match("RPAREN")
            # FEATURE (typed math block): `(expr): TYPE` computes the whole
            # bracketed expression at TYPE's precision (see MathBlock / codegen's
            # _math_block_type). Only triggers when a COLON+TYPE immediately
            # follows the closing paren — normal grouped exprs are unaffected.
            _nxt = self.tokens[self.pos + 1] if self.pos + 1 < len(self.tokens) else None
            if (self.peek() and self.peek()[0] == "COLON"
                    and _nxt and _nxt[0] == "TYPE"):
                self.match("COLON")
                block_type = self.match("TYPE")
                e = MathBlock(e, block_type)
            # BUGFIX (bugs.log #10): postfix `.method()`/field chaining was
            # only wired up for specific primary-expression shapes (bare
            # IDENT, SY reflection, TYPE-as-varname) individually, each with
            # their own copy of this loop — a parenthesized/grouped
            # expression like `(layers + 1)` fell straight through this
            # branch with no chaining at all, so `(layers + 1).to(SY)`
            # silently mis-parsed (".to" ended up read as a bare, undefined
            # reference) instead of being treated as a method call. Real
            # code found this via `(layers + 1).to(SY)` inside a loop.
            while self.peek():
                if self.peek()[0] == "DOT":
                    self.match("DOT")
                    attr = self.match_attr()
                    if self.peek() and self.peek()[0] == "LPAREN":
                        self.match("LPAREN")
                        args = []
                        while self.peek() and self.peek()[0] != "RPAREN":
                            args.append(self.expr())
                            if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                        self.match("RPAREN")
                        e = MethodCall(e, attr, args)
                    else:
                        e = FieldAccess(e, attr)
                elif self.peek()[0] == "LPAREN":
                    self.match("LPAREN")
                    args = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        args.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    e = FnCall(e, args)
                else:
                    break
            return e

        # FILE token used as file handle variable inside open() block
        if tok[0] == "FILE" and tok[1] in self._active_file_handles:
            self.advance()
            var_name = tok[1]
            res = Var(var_name)
            while self.peek() and self.peek()[0] == "DOT":
                self.match("DOT")
                attr = self.match_attr()
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
        # FILE token used as file utility in expression context (e.g. let x = file.exists("f"))
        if tok[0] == "FILE":
            node = self.file_global_stmt_list()
            if node is not None:
                return node
            # Not a file module operation — treat as regular variable name
            self.advance()
            name = tok[1]
            res = Var(name)
            # Check for method calls on the variable
            while self.peek():
                if self.peek()[0] == "DOT":
                    self.match("DOT")
                    attr = self.match_attr()
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
                    res = FnCall(name, args)
                    if self.peek() and self.peek()[0] == "DOT":
                        continue
                    break
                else:
                    break
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
                    attr = self.match_attr()
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
                method = self.match_attr()
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
            # Handle thread.wait(...) / thread.running(...) in expression context
            if name == "thread" and self.peek() and self.peek()[0] == "DOT":
                saved_pos = self.pos
                self.match("DOT")
                attr = self.match_attr()
                if attr == "wait":
                    self.match("LPAREN")
                    ids = []
                    while self.peek() and self.peek()[0] != "RPAREN":
                        ids.append(self.expr())
                        if self.peek() and self.peek()[0] == "COMMA": self.match("COMMA")
                    self.match("RPAREN")
                    return ThreadWait(ids)
                if attr == "running":
                    self.match("LPAREN")
                    tid = self.expr()
                    self.match("RPAREN")
                    return ThreadRunning(tid)
                self.pos = saved_pos
            res = Var(name)
            while self.peek():
                if self.peek()[0] == "DOT":
                    self.match("DOT")
                    attr = self.match_attr()
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
                        elif res.name in self.sy_names and res.name not in self.sy_fn_names:
                            # BUGFIX/FEATURE (bugs.log #9): bare `tmp(...)`
                            # after a reflective declaration (no explicit
                            # `(tmp)` parens on this later access — the
                            # pattern real code actually uses) must also
                            # route through the dynamic runtime lookup, not
                            # be treated as calling a literal function/
                            # collection named "tmp" (which doesn't exist).
                            res = FnCall(DynResolve(res.name), args)
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
        Each {...} segment is parsed as a full Rubidium expression (supports
        variables, field/method chains, indexing like {items(0)}, arithmetic
        like {age + 10}, comparisons like {age > 3}, etc.). If the contents of
        a {...} don't form a single valid expression, it's left as literal text."""
        from lexer import tokenize
        parts = []
        i, last, n = 0, 0, len(raw)
        while i < n:
            if raw[i] == '{':
                # Find the matching '}', tracking () [] nesting so calls/indexing work
                depth, j = 0, i + 1
                while j < n:
                    c = raw[j]
                    if c in '([': depth += 1
                    elif c in ')]':
                        if depth == 0: break
                        depth -= 1
                    elif c == '{': depth += 1
                    elif c == '}':
                        if depth == 0: break
                        depth -= 1
                    j += 1
                if j < n and raw[j] == '}':
                    inner = raw[i+1:j]
                    node = None
                    if inner.strip():
                        try:
                            sub_tokens = tokenize(inner)
                            sub_parser = Parser(sub_tokens)
                            candidate = sub_parser.expr()
                            if sub_parser.pos == len(sub_tokens):
                                node = candidate
                        except Exception:
                            node = None
                    if node is not None:
                        before = raw[last:i]
                        if before:
                            parts.append(Str(before))
                        parts.append(node)
                        last = j + 1
                        i = j + 1
                        continue
            i += 1
        after = raw[last:]
        if after:
            parts.append(Str(after))
        if not parts:
            return Str(raw)
        if len(parts) == 1 and isinstance(parts[0], Str):
            return parts[0]
        return InterpolatedStr(parts)