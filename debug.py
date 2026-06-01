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
    ("BOOL",     r"\b(?:True|False|Null|None)\b"),
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
    ("OPEN",     r"\bopen\b"),
    ("FILE",     r"\bfile\b"),
    ("RANGE",    r"\brange\b"),
    ("THREAD",   r"\bthread\b"),
    ("OS",       r"\bos\b"),
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

@dataclass
class OsRun(ASTNode):
    id_expr: Optional[ASTNode]
    cmd_expr: Optional[ASTNode]
    input_expr: Optional[ASTNode] = None
    struct_args: Optional[Dict] = None
    line: int = 0

@dataclass
class OsStart(ASTNode):
    id_expr: ASTNode; line: int

@dataclass
class OsDrop(ASTNode):
    id_expr: ASTNode; line: int

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
        if tok.kind == 'OPEN': return self.parse_open_block()
        if tok.kind in ('PRINT', 'PRINTLN', 'INPUT', 'FILE_READ', 'FILE_WRITE', 'FILE', 'RANGE', 'THREAD', 'OS'):
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

    def parse_open_block(self):
        """Parse: open("path") as var_name { body } — file handle block"""
        line = self.consume('OPEN').line
        self.consume('LPAREN')
        path_expr = self.parse_expression()
        self.consume('RPAREN')
        if self.current().kind == 'AS':
            self.consume('AS')
        # var name after 'as' — may be IDENT or FILE keyword used as variable
        if self.current().kind in ('IDENT', 'FILE', 'TYPE'):
            var_tok = self.consume()
        else:
            var_tok = Token('IDENT', '_file_handle', line, 0)
        handle_name = var_tok.value
        # Register handle name so FILE.method() parses correctly inside body
        self._open_file_handles = getattr(self, '_open_file_handles', set())
        self._open_file_handles.add(handle_name)
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        self._open_file_handles.discard(handle_name)
        # Return a WhileStmt-like structure — debug.py only does static analysis,
        # so we wrap as a BlockStmt equivalent by returning an If with body
        # Use ForStmt(var_tok, path_expr, body) as a container node; warnings still fire
        return ForStmt(var_tok, path_expr, body, line)

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
        
        # For os keyword - could be os.start(), os.run(), or os(id).drop()
        if tok.kind == 'OS':
            # Consume the keyword first
            self.consume()
            if self.current().kind == 'DOT':
                # os.run() or os.start() - handle as method call
                saved_pos = self.pos
                self.consume('DOT')
                attr = self.consume('IDENT')
                if attr.value == 'run' and self.current().kind == 'LPAREN':
                    # os.run() - check for struct form or regular args
                    self.consume('LPAREN')
                    if self.current().kind == 'LBRACE':
                        # Struct form: os.run({ cmd: "...", input: "..." })
                        struct_args = self.parse_os_run_struct()
                        self.consume('RPAREN')
                        return ExprStmt(OsRun(None, None, struct_args=struct_args), tok.line)
                    else:
                        # Regular form: os.run(id, "cmd") - let the expression parser handle it
                        self.pos = saved_pos - 2  # Reset before DOT was consumed
                        return ExprStmt(self.parse_os_run_regular(tok), tok.line)
                elif attr.value == 'start':
                    # os.start(id)
                    self.consume('LPAREN')
                    id_expr = self.parse_expression()
                    self.consume('RPAREN')
                    return ExprStmt(OsStart(id_expr, attr.line), tok.line)
                else:
                    self.pos = saved_pos - 2
                    return ExprStmt(self.parse_os_run_regular(tok), tok.line)
            elif self.current().kind == 'LPAREN':
                # os(id) - could be os.run(id, cmd) or os(id).drop()
                saved_pos = self.pos
                self.consume('LPAREN')
                id_expr = self.parse_expression()
                self.consume('RPAREN')
                # Check if followed by .drop()
                if self.current().kind == 'DOT':
                    self.consume('DOT')
                    method = self.consume('IDENT')
                    if method.value == 'drop' and self.current().kind == 'LPAREN':
                        self.consume('LPAREN')
                        self.consume('RPAREN')
                        return ExprStmt(OsDrop(id_expr, method.line), tok.line)
                    self.pos = saved_pos
                return ExprStmt(FunctionCall(tok, [id_expr], tok.line), tok.line)
            return ExprStmt(Identifier(tok), tok.line)  # Just 'os' identifier

        # For file keyword — could be file.write(), file.read(), file.exists(), etc.
        if tok.kind == 'FILE':
            self.consume()  # consume 'file'
            if self.current().kind == 'DOT':
                self.consume('DOT')
                method = self.consume('IDENT')
                args = self._parse_method_args() if self.current().kind == 'LPAREN' else []
                return ExprStmt(MethodCall(Identifier(tok), method, args, tok.line), tok.line)
            return ExprStmt(Identifier(tok), tok.line)

        self.consume()  # consume the keyword
        self.consume('LPAREN')
        args = []
        while self.current().kind not in ('RPAREN', 'EOF'):
            args.append(self.parse_expression())
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RPAREN')