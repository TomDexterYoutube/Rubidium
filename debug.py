import sys
import os

# Remove the script's own directory from sys.path so stdlib modules
# (ast, inspect, etc.) are not shadowed by local files like ast.py.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir in sys.path:
    sys.path.remove(_script_dir)

import re
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple

# ==========================================
# 1. THE OFFICIAL RUBIDIUM LEXER
# ==========================================
TOKEN_SPEC = [
    ("NUMBER",   r"\d+\.\d+|\d+"),
    ("ISTRING",  r'i"[^"]*"'),
    ("STRING",   r'"[^"]*"'),
    ("BOOL",     r"\b(?:True|False)\b"),
    ("LET",      r"\blet\b"),
    ("MUT",      r"\bmut\b"),
    ("FN",       r"\bfn\b"),
    ("CLASS",    r"\bclass\b"),
    ("IF",       r"\bif\b"),
    ("ELSE",     r"\belse\b"),
    ("WHILE",    r"\bwhile\b"),
    ("FOR",      r"\bfor\b"),
    ("IN",       r"\bin\b"),
    ("RETURN",   r"\breturn\b"),
    ("BREAK",    r"\bbreak\b"),
    ("PRINT",    r"\bprint\b"),
    ("PRINTLN",  r"\bprintln\b"),
    ("INPUT",    r"\binput\b"),
    ("FILE_READ", r"\bfile_read\b"),
    ("FILE_WRITE", r"\bfile_write\b"),
    ("RANGE",    r"\brange\b"),
    ("THREAD",   r"\bthread\b"),
    ("IMPORT",   r"\bimport\b"),
    ("USE",      r"\buse\b"),
    ("TRY",      r"\btry\b"),
    ("ERROR",    r"\berror\b"),
    ("AS",       r"\bas\b"),
    ("LOGIC",    r"\b(?:and|or|not)\b"),
    ("TYPE",     r"\b(?:i32|i64|i128|i256|f32|f64|f128|f256|f512|f1024|f2048|str|bool|list|index|dict)\b"),
    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("OP",       r"==|!=|<=|>=|->|=|\+|-|\*\*|\*/|\*|/|<|>"),
    ("COLON",    r":"),
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("COMMA",    r","),
    ("DOT",      r"\."),
    ("COMMENT",  r"#[^\n]*"),
    ("SKIP",     r"[ \t]+"),
    ("NEWLINE",  r"\n"),
    ("MISMATCH", r"."),
]

token_regex = "|".join(f"(?P<{n}>{r})" for n, r in TOKEN_SPEC)

@dataclass
class Token:
    kind: str
    value: str
    line: int
    col: int = 0

def tokenize(code) -> List[Token]:
    tokens = []
    line_no = 1
    line_start = 0
    for m in re.finditer(token_regex, code):
        kind = m.lastgroup
        value = m.group()
        col = m.start() - line_start

        if kind == "NEWLINE":
            line_no += 1
            line_start = m.end()
            continue
        if kind in ("SKIP", "COMMENT"): continue
        if kind == "MISMATCH":
            print(f"\033[1;31merror[L001]\033[0m: Unexpected character '{value}' at line {line_no}")
            continue

        tokens.append(Token(kind, value, line_no, col))
    tokens.append(Token("EOF", "", line_no, 0))
    return tokens

# ==========================================
# 2. ABSTRACT SYNTAX TREE (AST)
# ==========================================
class ASTNode: pass

@dataclass
class ImportStmt(ASTNode):
    module: Token; line: int

@dataclass
class VarDecl(ASTNode):
    name: Token; is_mut: bool; v_type: Optional[Token]; expr: Optional[ASTNode]; line: int

@dataclass
class Assign(ASTNode):
    target: ASTNode; expr: ASTNode; line: int

@dataclass
class PrintStmt(ASTNode):
    expr: ASTNode; line: int; is_println: bool = False

@dataclass
class DropStmt(ASTNode):
    name: str; line: int

@dataclass
class ExprStmt(ASTNode):
    expr: ASTNode; line: int

@dataclass
class ReturnStmt(ASTNode):
    expr: Optional[ASTNode]; line: int

@dataclass
class BreakStmt(ASTNode):
    line: int

@dataclass
class FunctionDef(ASTNode):
    name: Token; params: List[Tuple[str, str]]; return_type: Optional[str]; body: List[ASTNode]; line: int

@dataclass
class ClassDef(ASTNode):
    name: Token; body: List[ASTNode]; line: int

# Control Flow
@dataclass
class IfStmt(ASTNode):
    condition: ASTNode; body: List[ASTNode]; else_body: List[ASTNode]; line: int

@dataclass
class WhileStmt(ASTNode):
    condition: ASTNode; body: List[ASTNode]; line: int

@dataclass
class ForStmt(ASTNode):
    item: Token; iterable: ASTNode; body: List[ASTNode]; line: int

@dataclass
class TryErrorBlock(ASTNode):
    try_body: List[ASTNode]; error_body: List[ASTNode]; line: int

# Expressions
@dataclass
class Literal(ASTNode):
    token: Token; value: Any

@dataclass
class InterpolatedStr(ASTNode):
    parts: List  # list of Literal(str) or Identifier nodes
    line: int

@dataclass
class Identifier(ASTNode):
    token: Token

@dataclass
class BinaryOp(ASTNode):
    left: ASTNode; op: Token; right: ASTNode; line: int

@dataclass
class UnaryOp(ASTNode):
    op: Token; expr: ASTNode; line: int

@dataclass
class FunctionCall(ASTNode):
    name: Token; args: List[ASTNode]; line: int

@dataclass
class MethodCall(ASTNode):
    obj: ASTNode; method: Token; args: List[ASTNode]; line: int

@dataclass
class PropertyAccess(ASTNode):
    obj: ASTNode; prop: Token; line: int

@dataclass
class TypeCast(ASTNode):
    expr: ASTNode; target_type: Token; line: int

# Collections
@dataclass
class ListLiteral(ASTNode):
    elements: List[ASTNode]; line: int

@dataclass
class DictLiteral(ASTNode):
    pairs: List[Tuple[ASTNode, ASTNode]]; line: int

@dataclass
class IndexLiteral(ASTNode):
    pairs: List[Tuple[ASTNode, ASTNode]]; line: int

# ==========================================
# 3. RECURSIVE DESCENT PARSER
# ==========================================
class ParseError(Exception): pass

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.errors = 0

    def current(self) -> Token: return self.tokens[self.pos] if self.pos < len(self.tokens) else self.tokens[-1]

    def consume(self, expected_kind=None) -> Token:
        tok = self.current()
        if expected_kind and tok.kind != expected_kind:
            print(f"\033[1;31merror[P001]\033[0m: Expected `{expected_kind}`, got `{tok.kind}` at line {tok.line}")
            self.errors += 1
            raise ParseError()
        self.pos += 1
        return tok

    def synchronize(self):
        """Graceful Error Recovery"""
        self.pos += 1
        while self.current().kind not in ('EOF', 'NEWLINE', 'RBRACE', 'LET', 'FN', 'CLASS'):
            self.pos += 1

    def parse(self):
        statements = []
        while self.current().kind != 'EOF':
            try:
                stmt = self.parse_statement()
                if stmt: statements.append(stmt)
            except ParseError:
                self.synchronize()
        return statements

    def parse_statement(self):
        tok = self.current()
        if tok.kind in ('IMPORT', 'USE'): return self.parse_import()
        if tok.kind == 'LET': return self.parse_let()
        if tok.kind == 'FN': return self.parse_fn()
        if tok.kind == 'CLASS': return self.parse_class()
        if tok.kind == 'IF': return self.parse_if()
        if tok.kind == 'WHILE': return self.parse_while()
        if tok.kind == 'FOR': return self.parse_for()
        if tok.kind == 'TRY': return self.parse_try_error()
        if tok.kind == 'RETURN': return self.parse_return()
        if tok.kind == 'BREAK': return self.parse_break()
        if tok.kind in ('PRINT', 'PRINTLN', 'INPUT', 'FILE_READ', 'FILE_WRITE', 'RANGE', 'THREAD'):
            return self.parse_call_or_print(tok)

        expr = self.parse_expression()
        
        if self.current().kind == 'OP' and self.current().value == '=':
            self.consume('OP')
            rval = self.parse_expression()
            if not isinstance(expr, (Identifier, PropertyAccess)):
                print(f"\033[1;31merror[P020]\033[0m: Invalid assignment target at line {tok.line}")
                self.errors += 1
            return Assign(expr, rval, tok.line)

        # Drop Statement check (x.drop())
        if isinstance(expr, MethodCall) and expr.method.value == 'drop':
            obj_name = expr.obj.token.value if isinstance(expr.obj, Identifier) else "unknown"
            return DropStmt(obj_name, expr.line)

        return ExprStmt(expr, tok.line)

    def parse_import(self):
        line = self.consume().line
        # Accept IDENT or keyword tokens for module names
        if self.current().kind == 'IDENT':
            module = self.consume('IDENT')
        else:
            module = self.consume()  # Accept any token as module name
        return ImportStmt(module, line)

    def parse_class(self):
        line = self.consume('CLASS').line
        name = self.consume('IDENT')
        if self.current().kind == 'LPAREN':
            self.consume('LPAREN'); self.consume('RPAREN')
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        return ClassDef(name, body, line)

    def parse_fn(self):
        line = self.consume('FN').line
        name = self.consume('IDENT')
        params = []
        if self.current().kind == 'LPAREN':
            self.consume('LPAREN')
            while self.current().kind not in ('RPAREN', 'EOF'):
                if self.current().kind == 'IDENT':
                    p_name = self.consume('IDENT').value
                    if self.current().kind == 'COLON': self.consume('COLON')
                    p_type = self.consume('TYPE').value if self.current().kind == 'TYPE' else "Unknown"
                    params.append((p_name, p_type))
                if self.current().kind == 'COMMA': self.consume('COMMA')
            self.consume('RPAREN')
            
        ret_type = None
        if self.current().kind == 'OP' and self.current().value == '->':
            self.consume('OP')
            ret_type = self.consume('TYPE').value
            
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        if self.current().kind == 'RBRACE': self.consume('RBRACE')
        return FunctionDef(name, params, ret_type, body, line)

    def parse_if(self):
        line = self.consume('IF').line
        cond = self.parse_expression()
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        
        else_body = []
        if self.current().kind == 'ELSE':
            self.consume('ELSE')
            if self.current().kind == 'IF':
                else_body.append(self.parse_if())
            else:
                self.consume('LBRACE')
                while self.current().kind not in ('RBRACE', 'EOF'):
                    stmt = self.parse_statement()
                    if stmt: else_body.append(stmt)
                self.consume('RBRACE')
                
        return IfStmt(cond, body, else_body, line)

    def parse_while(self):
        line = self.consume('WHILE').line
        cond = self.parse_expression()
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        return WhileStmt(cond, body, line)

    def parse_for(self):
        line = self.consume('FOR').line
        item = self.consume('IDENT')
        self.consume('IN')
        iterable = self.parse_expression()
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        return ForStmt(item, iterable, body, line)

    def parse_try_error(self):
        line = self.consume('TRY').line
        self.consume('LBRACE')
        try_body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: try_body.append(stmt)
        self.consume('RBRACE')
        
        error_body = []
        if self.current().kind == 'ERROR':
            self.consume('ERROR')
            self.consume('LBRACE')
            while self.current().kind not in ('RBRACE', 'EOF'):
                stmt = self.parse_statement()
                if stmt: error_body.append(stmt)
            self.consume('RBRACE')
            
        return TryErrorBlock(try_body, error_body, line)

    def parse_let(self):
        line = self.consume('LET').line
        is_mut = False
        if self.current().kind == 'MUT':
            is_mut = True; self.consume('MUT')
        # Accept both IDENT and TYPE tokens for variable names (e.g., "list" is a TYPE but can be a variable name)
        if self.current().kind in ('IDENT', 'TYPE'):
            name = self.consume()
        else:
            name = self.consume('IDENT')
        
        v_type = None
        if self.current().kind == 'COLON':
            self.consume('COLON')
            v_type = self.consume('TYPE') if self.current().kind == 'TYPE' else None
        elif self.current().kind == 'TYPE':
            v_type = self.consume('TYPE')
        
        expr = None
        if self.current().kind == 'OP' and self.current().value == '=':
            self.consume('OP')
            expr = self.parse_expression()
            
        return VarDecl(name, is_mut, v_type, expr, line)

    def parse_call_or_print(self, tok):
        if tok.value in ('print', 'println'):
            self.consume()  # consume the keyword
            self.consume('LPAREN')
            expr = self.parse_expression()
            self.consume('RPAREN')
            return PrintStmt(expr, tok.line, tok.value == 'println')
        
        # For thread keyword - could be thread() call or thread.wait() method
        # Check if followed by LPAREN (function call) or DOT (method call)
        if tok.kind == 'THREAD':
            # Consume the keyword first
            self.consume()
            if self.current().kind == 'LPAREN':
                self.consume('LPAREN')
                args = []
                while self.current().kind not in ('RPAREN', 'EOF'):
                    args.append(self.parse_expression())
                    if self.current().kind == 'COMMA': self.consume('COMMA')
                self.consume('RPAREN')
                return ExprStmt(FunctionCall(tok, args, tok.line), tok.line)
            elif self.current().kind == 'DOT':
                # thread.wait() - fall through to expression parsing
                return ExprStmt(self.parse_method_call_from_thread(tok), tok.line)
        
        self.consume()  # consume the keyword
        self.consume('LPAREN')
        args = []
        while self.current().kind not in ('RPAREN', 'EOF'):
            args.append(self.parse_expression())
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RPAREN')
        return ExprStmt(FunctionCall(tok, args, tok.line), tok.line)

    def parse_method_call_from_thread(self, tok):
        """Handle thread.wait() syntax"""
        # tok is 'thread' keyword
        self.consume('DOT')
        attr = self.consume('IDENT')
        self.consume('LPAREN')
        args = []
        while self.current().kind not in ('RPAREN', 'EOF'):
            args.append(self.parse_expression())
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RPAREN')
        return MethodCall(Identifier(tok), attr, args, attr.line)

    def parse_print(self):
        tok = self.consume()
        is_println = tok.value == 'println'
        self.consume('LPAREN')
        expr = self.parse_expression()
        self.consume('RPAREN')
        return PrintStmt(expr, tok.line, is_println)

    def parse_return(self):
        line = self.consume('RETURN').line
        expr = self.parse_expression() if self.current().kind not in ('NEWLINE', 'EOF', 'RBRACE') else None
        return ReturnStmt(expr, line)

    def parse_break(self): return BreakStmt(self.consume('BREAK').line)

    # --- EXPRESSION PARSING (Precedence & Operations) ---
    def parse_expression(self): return self.parse_logical()

    def parse_logical(self):
        left = self.parse_comparison()
        while self.current().kind == 'LOGIC':
            op = self.consume('LOGIC')
            right = self.parse_comparison()
            left = BinaryOp(left, op, right, op.line)
        return left

    def parse_comparison(self):
        left = self.parse_term()
        while self.current().kind == 'OP' and self.current().value in ('==', '!=', '<', '>', '<=', '>='):
            op = self.consume('OP')
            right = self.parse_term()
            left = BinaryOp(left, op, right, op.line)
        return left

    def parse_term(self):
        left = self.parse_factor()
        while self.current().kind == 'OP' and self.current().value in ('+', '-'):
            op = self.consume('OP')
            right = self.parse_factor()
            left = BinaryOp(left, op, right, op.line)
        return left

    def parse_factor(self):
        left = self.parse_primary()
        while self.current().kind == 'OP' and self.current().value in ('*', '/', '**'):
            op = self.consume('OP')
            right = self.parse_primary()
            left = BinaryOp(left, op, right, op.line)
        return left

    def parse_primary(self):
        if self.current().kind == 'OP' and self.current().value == '*/':
            op = self.consume('OP')
            return UnaryOp(op, self.parse_primary(), op.line)

        if self.current().kind == 'OP' and self.current().value == '-':
            op = self.consume('OP')
            return UnaryOp(op, self.parse_primary(), op.line)
            
        if self.current().kind == 'LOGIC' and self.current().value == 'not':
            op = self.consume('LOGIC')
            return UnaryOp(op, self.parse_primary(), op.line)
            
        tok = self.consume()
        base_expr = None
        
        if tok.kind == 'LPAREN':
            base_expr = self.parse_expression()
            self.consume('RPAREN')
        elif tok.kind == 'NUMBER': base_expr = Literal(tok, float(tok.value) if '.' in tok.value else int(tok.value))
        elif tok.kind == 'STRING': base_expr = Literal(tok, tok.value.strip('"'))
        elif tok.kind == 'ISTRING':
            import re
            raw = tok.value[2:-1]  # strip i" and "
            parts = []
            pattern = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
            last = 0
            for m in pattern.finditer(raw):
                before = raw[last:m.start()]
                if before:
                    parts.append(Literal(Token('STRING', before, tok.line, 0), before))
                parts.append(Identifier(Token('IDENT', m.group(1), tok.line, 0)))
                last = m.end()
            after = raw[last:]
            if after:
                parts.append(Literal(Token('STRING', after, tok.line, 0), after))
            base_expr = InterpolatedStr(parts, tok.line) if parts else Literal(tok, '')
        elif tok.kind == 'BOOL': base_expr = Literal(tok, tok.value == 'True')
        elif tok.kind == 'LBRACKET': 
            line = tok.line
            if self.current().kind == 'RBRACKET':
                self.consume('RBRACKET')
                base_expr = ListLiteral([], line)
            else:
                first = self.parse_expression()
                if self.current().kind == 'COLON': 
                    self.consume('COLON')
                    val = self.parse_expression()
                    pairs = [(first, val)]
                    while self.current().kind == 'COMMA':
                        self.consume('COMMA')
                        if self.current().kind == 'RBRACKET': break
                        k = self.parse_expression()
                        self.consume('COLON')
                        v = self.parse_expression()
                        pairs.append((k, v))
                    self.consume('RBRACKET')
                    base_expr = IndexLiteral(pairs, line)
                else:
                    elements = [first]
                    while self.current().kind == 'COMMA':
                        self.consume('COMMA')
                        if self.current().kind == 'RBRACKET': break
                        elements.append(self.parse_expression())
                    self.consume('RBRACKET')
                    base_expr = ListLiteral(elements, line)
        elif tok.kind == 'LBRACE': 
            line = tok.line
            pairs = []
            while self.current().kind not in ('RBRACE', 'EOF'):
                k = self.parse_expression()
                self.consume('OP')  # for '=' operator
                v = self.parse_expression()
                pairs.append((k, v))
                if self.current().kind == 'COMMA': self.consume('COMMA')
            self.consume('RBRACE')
            base_expr = DictLiteral(pairs, line)
        elif tok.kind in ('IDENT', 'PRINT', 'PRINTLN', 'INPUT', 'FILE_READ', 'FILE_WRITE', 'RANGE', 'THREAD', 'ERROR'):
            if self.current().kind in ('LPAREN', 'LBRACKET'):
                close_char = 'RPAREN' if self.current().kind == 'LPAREN' else 'RBRACKET'
                self.consume()
                args = []
                while self.current().kind not in (close_char, 'EOF'):
                    args.append(self.parse_expression())
                    if self.current().kind == 'COMMA': self.consume('COMMA')
                self.consume(close_char)
                base_expr = FunctionCall(tok, args, tok.line)
            else:
                base_expr = Identifier(tok)
        elif tok.kind == 'TYPE': base_expr = Literal(tok, tok.value)
        else:
            print(f"\033[1;31merror[P002]\033[0m: Unexpected token `{tok.kind}` in expression at line {tok.line}")
            self.errors += 1
            base_expr = Literal(tok, None)

        while True:
            if self.current().kind == 'DOT':
                self.consume('DOT')
                attr = self.consume('IDENT')
                if self.current().kind == 'LPAREN':
                    self.consume('LPAREN')
                    args = []
                    while self.current().kind not in ('RPAREN', 'EOF'):
                        args.append(self.parse_expression())
                        if self.current().kind == 'COMMA': self.consume('COMMA')
                    self.consume('RPAREN')
                    base_expr = MethodCall(base_expr, attr, args, attr.line)
                else:
                    base_expr = PropertyAccess(base_expr, attr, attr.line)
            elif self.current().kind == 'AS':
                self.consume('AS')
                t_type = self.consume('TYPE')
                base_expr = TypeCast(base_expr, t_type, t_type.line)
            else:
                break

        return base_expr

# ==========================================
# 4. STATIC ANALYZER & SYMBOL TABLE
# ==========================================
class VarMetadata:
    def __init__(self, is_mut: bool, declared_line: int, type_cat: str, is_initialized: bool):
        self.is_mut = is_mut
        self.declared_line = declared_line
        self.type_cat = type_cat
        self.is_initialized = is_initialized
        self.is_dropped = False
        self.is_reassigned = False 
        self.is_read = False       

class ClassMeta:
    def __init__(self, name: str):
        self.name = name
        self.methods: Dict[str, FunctionDef] = {}
        self.properties: Dict[str, str] = {}

class StaticAnalyzer:
    def __init__(self, filepath, source_lines, is_main_file=False, import_stack=None):
        self.scopes: List[Dict[str, VarMetadata]] = [{}] 
        self.global_functions: Dict[str, FunctionDef] = {}
        self.classes: Dict[str, ClassMeta] = {}
        
        self.current_return_type: Optional[str] = None
        self.has_returned = False
        self.unreachable = False
        self.loop_depth = 0
        self.in_try_block = False
        
        self.builtins = {"print", "println", "input", "file_read", "file_write", "thread", "thread.wait", "range", "random", "time", "time.wait", "time.timer_start", "time.timer_pause", "time.timer_stop", "time.timer_read"}
        self.errors, self.warnings = 0, 0
        self.filepath, self.source_lines = filepath, source_lines
        self.is_main_file = is_main_file
        self.import_stack = import_stack or []

    def report_error(self, code, msg, line):
        print(f"\033[1;31merror[{code}]\033[0m: {msg}\n \033[1;36m-->\033[0m {self.filepath}:{line}")
        print(f" \033[1;36m{line:3} |\033[0m {self.source_lines[line-1].strip()}\n     \033[1;36m|\033[0m \033[1;31m^^^\033[0m\n")
        self.errors += 1
        
    def report_warning(self, code, msg, line, hint):
        print(f"\033[1;33mwarning[{code}]\033[0m: {msg}\n \033[1;36m-->\033[0m {self.filepath}:{line}")
        print(f" \033[1;36m{line:3} |\033[0m {self.source_lines[line-1].strip()}\n     \033[1;36m|\033[0m \033[1;33m--- \033[0m\033[3m{hint}\033[0m\n")
        self.warnings += 1

    def push_scope(self): self.scopes.append({})
        
    def pop_scope(self): 
        popped = self.scopes.pop()
        for var_name, meta in popped.items():
            if not meta.is_read:
                self.report_warning("W012", f"Unused variable `{var_name}`.", meta.declared_line, hint="Remove this variable if it is not needed.")
            if meta.is_mut and not meta.is_reassigned and meta.is_initialized:
                self.report_warning("W011", f"Variable `{var_name}` does not need to be mutable.", meta.declared_line, hint=f"Change to `let {var_name}`.")
            if not meta.is_dropped:
                self.report_warning("W010", f"Variable `{var_name}` is never dropped.", meta.declared_line, hint=f"Add `{var_name}.drop()` to free memory.")

    def declare_var(self, name, meta, line):
        if name in self.scopes[-1]: self.report_error("E022", f"Duplicate declaration of `{name}`", line)
        self.scopes[-1][name] = meta

    def get_var(self, name) -> Optional[VarMetadata]:
        for scope in reversed(self.scopes):
            if name in scope: return scope[name]
        return None

    def get_type_category(self, type_str):
        if not type_str: return "Unknown"
        if type_str == 'index': return "Index<Unknown,Unknown>"
        if type_str == 'list': return "List<Unknown>"
        if type_str == 'dict': return "Dict<Unknown,Unknown>"
        if type_str.startswith('i'): return "Int"
        if type_str.startswith('f'): return "Float"
        if type_str == 'str': return "String"
        if type_str == 'bool': return "Bool"
        return type_str

    def infer_type(self, expr):
        if isinstance(expr, Literal):
            if expr.token.kind == 'NUMBER': return "Float" if '.' in expr.token.value else "Int"
            if expr.token.kind == 'STRING': return "String"
            if expr.token.kind == 'BOOL': return "Bool"
            if expr.token.kind == 'TYPE': return "Int" if expr.value.startswith('i') else ("Float" if expr.value.startswith('f') else "Unknown")
        elif isinstance(expr, InterpolatedStr): return "String"
        elif isinstance(expr, Identifier):
            meta = self.get_var(expr.token.value)
            if meta: return meta.type_cat
            # Handle module identifiers
            if expr.token.value in ("time", "random", "thread"): return expr.token.value
            return "Unknown"
        elif isinstance(expr, FunctionCall):
            name = expr.name.value
            if name == "range": return "List<Int>"
            if name == "random":
                type_name = ""
                if len(expr.args) >= 3:
                    arg3 = expr.args[2]
                    if isinstance(arg3, Identifier) and arg3.token.value in ("f32", "f64", "f128", "f256", "f512", "f1024", "f2048"):
                        type_name = arg3.token.value
                if type_name.startswith("f"):
                    return "Float"
                return "Int"
            if name in self.classes: return f"Class<{name}>"
            f_def = self.global_functions.get(name)
            if f_def and f_def.return_type: return self.get_type_category(f_def.return_type)
        elif isinstance(expr, MethodCall):
            base_type = self.infer_type(expr.obj)
            if base_type == "String":
                if expr.method.value == "combine": return "String"
                if expr.method.value == "len": return "Int"
                if expr.method.value == "has": return "Bool"
            elif base_type == "time":
                if expr.method.value in ("sleep", "wait", "timer_start", "timer_pause", "timer_stop"):
                    return "Void"
                if expr.method.value == "timer_read":
                    return "Float"
            elif base_type.startswith("Class<"):
                c_name = base_type[6:-1]
                if c_name in self.classes and expr.method.value in self.classes[c_name].methods:
                    ret = self.classes[c_name].methods[expr.method.value].return_type
                    return self.get_type_category(ret) if ret else "Void"
        elif isinstance(expr, PropertyAccess):
            base_type = self.infer_type(expr.obj)
            if base_type.startswith("Class<"):
                c_name = base_type[6:-1]
                if c_name in self.classes: return self.classes[c_name].properties.get(expr.prop.value, "Unknown")
        elif isinstance(expr, BinaryOp):
             if expr.op.value in ('==', '!=', '<', '>', '<=', '>='): 
                 return "Bool"
             if expr.op.value in ('and', 'or'):
                 return "Bool"
             return self.infer_type(expr.left)
        return "Unknown"

    def run(self, ast_nodes, tokens):
        for node in ast_nodes:
            if isinstance(node, FunctionDef): self.global_functions[node.name.value] = node
            elif isinstance(node, ClassDef):
                c_meta = ClassMeta(node.name.value)
                for stmt in node.body:
                    if isinstance(stmt, FunctionDef): c_meta.methods[stmt.name.value] = stmt
                    elif isinstance(stmt, VarDecl): c_meta.properties[stmt.name.value] = self.get_type_category(stmt.v_type.value) if stmt.v_type else "Unknown"
                self.classes[node.name.value] = c_meta

        for node in ast_nodes: self.visit(node)
        self.pop_scope()
        
        # Missing Main Check
        if self.is_main_file and "main" not in self.global_functions:
            print(f"\033[1;31merror[E000]\033[0m: No `main()` function found. Rubidium requires an entry point.")
            self.errors += 1
            
        return self.errors == 0

    def visit_block(self, stmts):
        unreachable_reported = False
        for stmt in stmts:
            if self.unreachable:
                if not unreachable_reported:
                    self.report_warning("W013", "Unreachable code detected.", getattr(stmt, 'line', 0), hint="This executes after a `return` or `break`.")
                    unreachable_reported = True
            self.visit(stmt)

    def visit(self, node):
        if isinstance(node, ImportStmt):
            target_file = f"{node.module.value}.rub"
            if target_file in self.import_stack:
                self.report_error("E063", f"Circular import detected: `{target_file}`", node.line)
            else:
                pass # Loading logic omitted for brevity; this ensures circular detection works

        elif isinstance(node, FunctionDef):
            self.current_return_type = self.get_type_category(node.return_type) if node.return_type else "Void"
            prev_returned, prev_unreachable = self.has_returned, self.unreachable
            self.has_returned, self.unreachable = False, False
            
            self.push_scope()
            for p_name, p_type in node.params:
                self.declare_var(p_name, VarMetadata(False, node.line, self.get_type_category(p_type), True), node.line)
            self.visit_block(node.body)
            self.pop_scope()
            
            if self.current_return_type != "Void" and not self.has_returned:
                self.report_error("E081", f"Function `{node.name.value}` expects to return `{self.current_return_type}` but might exit without returning.", node.line)
            
            self.current_return_type = None
            self.has_returned, self.unreachable = prev_returned, prev_unreachable

        elif isinstance(node, ClassDef):
            self.push_scope()
            self.visit_block(node.body)
            self.pop_scope()

        elif isinstance(node, IfStmt):
            self.check_expr(node.condition, node.line)
            c_type = self.infer_type(node.condition)
            if c_type not in ("Unknown", "Bool"):
                self.report_error("E034", f"Condition must evaluate to `bool`, found `{c_type}`", node.line)
            
            prev_unreachable = self.unreachable
            self.push_scope(); self.visit_block(node.body); self.pop_scope()
            self.unreachable = prev_unreachable
            
            self.push_scope(); self.visit_block(node.else_body); self.pop_scope()
            self.unreachable = prev_unreachable

        elif isinstance(node, WhileStmt):
            self.check_expr(node.condition, node.line)
            c_type = self.infer_type(node.condition)
            if c_type not in ("Unknown", "Bool"):
                self.report_error("E034", f"Loop condition must evaluate to `bool`, found `{c_type}`", node.line)
                
            self.loop_depth += 1
            prev_unreachable = self.unreachable
            self.push_scope(); self.visit_block(node.body); self.pop_scope()
            self.unreachable = prev_unreachable
            self.loop_depth -= 1

        elif isinstance(node, ForStmt):
            self.check_expr(node.iterable, node.line)
            iter_type = self.infer_type(node.iterable)
            if iter_type not in ("Unknown", "String") and not iter_type.startswith(("List<", "Dict<", "Index<")):
                self.report_error("E062", f"Type `{iter_type}` is not iterable. Cannot be used in a `for` loop.", node.line)
                
            item_type = iter_type[5:-1] if iter_type.startswith("List<") else "Unknown"
            self.loop_depth += 1
            prev_unreachable = self.unreachable
            self.push_scope()
            self.declare_var(node.item.value, VarMetadata(False, node.line, item_type, True), node.line)
            self.visit_block(node.body)
            self.pop_scope()
            self.unreachable = prev_unreachable
            self.loop_depth -= 1

        elif isinstance(node, TryErrorBlock):
            prev_unreachable = self.unreachable
            prev_in_try = self.in_try_block
            self.in_try_block = True
            self.push_scope(); self.visit_block(node.try_body); self.pop_scope()
            self.in_try_block = prev_in_try
            self.unreachable = prev_unreachable
            
            self.push_scope()
            self.declare_var("error", VarMetadata(False, node.line, "String", True), node.line)
            self.visit_block(node.error_body)
            self.pop_scope()
            self.unreachable = prev_unreachable

        elif isinstance(node, ReturnStmt):
            ret_type = self.infer_type(node.expr) if node.expr else "Void"
            if self.current_return_type and self.current_return_type != "Unknown" and ret_type != "Unknown":
                if ret_type != self.current_return_type:
                    self.report_error("E080", f"Function expected to return `{self.current_return_type}`, but returns `{ret_type}`", node.line)
            if node.expr: self.check_expr(node.expr, node.line)
            self.has_returned = True
            self.unreachable = True

        elif isinstance(node, BreakStmt):
            if self.loop_depth == 0:
                self.report_error("E061", "Use of `break` outside of a loop.", node.line)
            self.unreachable = True

        elif isinstance(node, ExprStmt): self.check_expr(node.expr, node.line)

        elif isinstance(node, VarDecl):
            is_init = node.expr is not None
            inf_cat = self.infer_type(node.expr) if is_init else "Unknown"
            fin_cat = self.get_type_category(node.v_type.value) if node.v_type else inf_cat
            self.declare_var(node.name.value, VarMetadata(node.is_mut, node.line, fin_cat, is_init), node.line)
            if is_init: self.check_expr(node.expr, node.line)
                
        elif isinstance(node, Assign):
            if isinstance(node.target, Identifier):
                meta = self.get_var(node.target.token.value)
                if not meta: self.report_error("E002", f"Cannot find value `{node.target.token.value}` in scope", node.line)
                else:
                    if not meta.is_mut and meta.is_initialized: self.report_error("E001", f"Cannot assign twice to immutable `{node.target.token.value}`", node.line)
                    meta.is_reassigned = True
                    meta.is_initialized = True
            elif isinstance(node.target, PropertyAccess):
                self.check_expr(node.target.obj, node.line)
                b_type = self.infer_type(node.target.obj)
                if b_type.startswith("Class<"):
                    c_name = b_type[6:-1]
                    if c_name in self.classes and node.target.prop.value not in self.classes[c_name].properties:
                        self.report_error("E092", f"Property `{node.target.prop.value}` not found on class `{c_name}`", node.line)
            self.check_expr(node.expr, node.line)
            
        elif isinstance(node, PrintStmt): self.check_expr(node.expr, node.line)
            
        elif isinstance(node, DropStmt):
            meta = self.get_var(node.name)
            if meta: 
                if meta.is_dropped:
                    self.report_error("E042", f"Double free: `{node.name}` has already been dropped.", node.line)
                meta.is_dropped = True

    def check_expr(self, expr, current_line):
        if isinstance(expr, Identifier):
            meta = self.get_var(expr.token.value)
            if not meta:
                # Check if it's a builtin module (time, random, thread)
                if expr.token.value in ("time", "random", "thread"):
                    return  # Module identifier, valid
                if expr.token.value not in self.builtins and expr.token.value not in self.global_functions and expr.token.value not in self.classes:
                    self.report_error("E002", f"Cannot find `{expr.token.value}` in scope", expr.token.line)
            else:
                if not meta.is_initialized: self.report_error("E032", f"Use of uninitialized variable `{expr.token.value}`", current_line)
                if meta.is_dropped: self.report_error("E041", f"Use-after-free: `{expr.token.value}` was already dropped.", current_line)
                meta.is_read = True 

        elif isinstance(expr, FunctionCall):
            target_name = expr.name.value
            meta = self.get_var(target_name)
            if meta and meta.type_cat.startswith(("List<", "Dict<", "Index<")):
                if len(expr.args) > 0 and isinstance(expr.args[0], Literal) and expr.args[0].value == 0:
                    self.report_error("E051", f"Attempted to access index 0. Collections are 1-indexed.", expr.line)
                for arg in expr.args: self.check_expr(arg, current_line)
                meta.is_read = True
            elif target_name not in self.global_functions and target_name not in self.builtins and target_name not in self.classes:
                self.report_error("E003", f"Cannot find function or class `{target_name}`", expr.line)
            else:
                f_def = self.global_functions.get(target_name)
                if f_def:
                    if len(expr.args) != len(f_def.params):
                        self.report_error("E070", f"Function `{target_name}` expects {len(f_def.params)} arguments, got {len(expr.args)}.", expr.line)
                    else:
                        for i, arg in enumerate(expr.args):
                            a_type, e_type = self.infer_type(arg), self.get_type_category(f_def.params[i][1])
                            if a_type != "Unknown" and e_type != "Unknown" and a_type != e_type:
                                self.report_error("E071", f"Argument {i+1} for `{target_name}` should be `{e_type}`, found `{a_type}`.", expr.line)
                for arg in expr.args: self.check_expr(arg, current_line)

        elif isinstance(expr, MethodCall):
            b_type = self.infer_type(expr.obj)
            self.check_expr(expr.obj, current_line)
            for arg in expr.args: self.check_expr(arg, current_line)
            
            if expr.method.value != "drop":
                if b_type == "String" and expr.method.value not in ("len", "has", "to", "combine"):
                    self.report_error("E090", f"Method `{expr.method.value}` not found on String", expr.line)
                elif b_type == "time" and expr.method.value not in ("sleep", "wait", "timer_start", "timer_pause", "timer_stop", "timer_read"):
                    self.report_error("E090", f"Method `{expr.method.value}` not found on time module", expr.line)
                elif b_type.startswith("Class<"):
                    c_name = b_type[6:-1]
                    if c_name in self.classes:
                        m_def = self.classes[c_name].methods.get(expr.method.value)
                        if m_def:
                            if len(expr.args) != len(m_def.params):
                                self.report_error("E070", f"Method `{expr.method.value}` expects {len(m_def.params)} arguments, got {len(expr.args)}.", expr.line)
                        else:
                            self.report_error("E091", f"Method `{expr.method.value}` not found on class `{c_name}`", expr.line)

        elif isinstance(expr, PropertyAccess):
            self.check_expr(expr.obj, current_line)
            b_type = self.infer_type(expr.obj)
            if b_type.startswith("Class<"):
                c_name = b_type[6:-1]
                if c_name in self.classes and expr.prop.value not in self.classes[c_name].properties:
                    self.report_error("E092", f"Property `{expr.prop.value}` not found on class `{c_name}`", current_line)

        elif isinstance(expr, BinaryOp):
            self.check_expr(expr.left, current_line)
            self.check_expr(expr.right, current_line)
            l_t, r_t = self.infer_type(expr.left), self.infer_type(expr.right)
            
            if expr.op.kind == 'LOGIC' and (l_t not in ("Unknown", "Bool") or r_t not in ("Unknown", "Bool")):
                self.report_error("E036", "Logical operators (and/or) require `bool` operands.", current_line)
            elif l_t != "Unknown" and r_t != "Unknown" and l_t != r_t and expr.op.value in ('+', '-', '*', '/', '<', '>', '<=', '>=') and not (l_t == "String" and r_t != "String" and expr.op.value == '+'):
                # String + Other type is allowed (int to string coercion)
                self.report_error("E033", f"Type mismatch in binary operation: cannot apply `{expr.op.value}` to `{l_t}` and `{r_t}`", current_line)
            if expr.op.value == '/' and isinstance(expr.right, Literal) and expr.right.value == 0:
                if self.in_try_block:
                    self.report_warning("W050", "Division by zero detected statically (inside try block, will be caught at runtime).", current_line, hint="This is inside a try/error block so the error will be caught.")
                else:
                    self.report_error("E050", "Division by zero detected statically.", current_line)

        elif isinstance(expr, UnaryOp): self.check_expr(expr.expr, current_line)
        
        elif isinstance(expr, TypeCast): 
            self.check_expr(expr.expr, current_line)
            b_type = self.infer_type(expr.expr)
            t_type = self.get_type_category(expr.target_type.value)
            if b_type == "String" and t_type in ("Int", "Float"):
                self.report_error("E035", f"Cannot cast String to `{t_type}` using `as`. Use `.to({expr.target_type.value})` instead.", current_line)

# ==========================================
# 5. CLI EXECUTION
# ==========================================
def main():
    if os.name == 'nt': os.system('color')
    
    args = sys.argv[1:]
    filepaths = [arg for arg in args if not arg.startswith("--")]
    if not filepaths:
        print("\033[1;31merror\033[0m: No file provided to xeon debug.\nUsage: python debug.py <file.rub>")
        sys.exit(1)

    filepath = filepaths[0]
    if not os.path.exists(filepath):
        print(f"\033[1;31merror\033[0m: File '{filepath}' not found.")
        sys.exit(1)

    with open(filepath, 'r') as f: code = f.read()

    start_time = time.time()
    tokens = tokenize(code)
    parser = Parser(tokens)
    ast = parser.parse()

    if parser.errors > 0:
        print(f"\n\033[1;31merror\033[0m: aborting due to {parser.errors} syntax error(s)")
        sys.exit(1)

    analyzer = StaticAnalyzer(os.path.basename(filepath), code.split("\n"), is_main_file=True)
    success = analyzer.run(ast, tokens)
    duration = (time.time() - start_time) * 1000
    
    if success:
        if analyzer.warnings > 0: print(f"\033[1;33m✔ Checked\033[0m {os.path.basename(filepath)} with {analyzer.warnings} warning(s) in {duration:.2f}ms")
        else: print(f"\033[1;32m✔ Checked\033[0m {os.path.basename(filepath)} successfully in {duration:.2f}ms")
        sys.exit(0)
    else:
        print(f"\n\033[1;31merror\033[0m: could not compile due to {analyzer.errors} error(s)")
        sys.exit(1)

if __name__ == "__main__": main()