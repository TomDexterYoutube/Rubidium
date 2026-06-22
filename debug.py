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
    ("CONTINUE", r"\bcontinue\b"),
    ("PRINT",    r"\bprint\b"),
    ("PRINTLN",  r"\bprintln\b"),
    ("INPUT",    r"\binput\b"),
    ("OPEN",     r"\bopen\b"),
    ("FILE",     r"\bfile\b"),
    ("RANGE",    r"\brange\b"),
    ("THREAD",   r"\bthread\b"),
    ("OS",       r"\bos\b"),
    ("IMPORT",   r"\bimport\b"),
    ("USE",      r"\buse\b"),
    ("TRY",      r"\btry\b"),
    ("ERROR",    r"\berror\b"),
    ("ON_ERROR", r"\bon_error\b"),
    ("AS",       r"\bas\b"),
    ("LOGIC",    r"\b(?:and|or|not)\b"),
    ("TYPE",     r"\b(?:i32|i64|i128|i256|f32|f64|f128|f256|f512|f1024|f2048|str|bool|list|index|dict|Any)\b"),
    ("LOCAL",    r"\blocal\b"),
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
    name: Token; is_mut: bool; is_local: bool; v_type: Optional[Token]; expr: Optional[ASTNode]; line: int

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
class ContinueStmt(ASTNode):
    line: int

@dataclass
class FunctionDef(ASTNode):
    name: Token; params: List[Tuple[str, str]]; return_type: Optional[str]; body: List[ASTNode]; line: int

@dataclass
class FFIBind(ASTNode):
    handle_name: Token; symbol_name: str; params: List[Tuple[str, str]]; return_type: Optional[str]; line: int

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

@dataclass
class FileOpenBlock(ASTNode):
    path_expr: ASTNode; var_name: Token; body: List[ASTNode]; line: int

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

# ──────────────────────────────────────────────────────────────────────────────
# Keyword typo detection
# ──────────────────────────────────────────────────────────────────────────────

_KEYWORDS = [
    'fn', 'let', 'class', 'if', 'else', 'while', 'for', 'in',
    'return', 'break', 'continue', 'use', 'import', 'try', 'error',
    'open', 'print', 'println', 'input', 'thread', 'mut', 'local',
    'as', 'and', 'or', 'not',
]

def _edit_distance(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    dp = list(range(lb + 1))
    for i in range(1, la + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, lb + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return dp[lb]

def _closest_keyword(word: str, max_dist: int = 2) -> Optional[str]:
    word_l = word.lower()
    best, best_d = None, max_dist + 1
    for kw in _KEYWORDS:
        d = _edit_distance(word_l, kw)
        if d < best_d:
            best, best_d = kw, d
    return best if best_d <= max_dist else None

_CONSTRUCT_HINTS = {
    'fn':      lambda rest: (
        # IDENT followed by LPAREN  → function definition (e.g. `def foo(`)
        # OR IDENT followed by LPAREN after optional TYPE  → covers `def add(a: i32`
        any(
            rest[i].kind == 'IDENT' and
            any(t.kind == 'LPAREN' for t in rest[i:i+4])
            for i in range(min(3, len(rest)))
        )
    ),
    'class':   lambda rest: any(t.kind == 'IDENT' for t in rest[:3]) and not any(t.kind == 'LPAREN' for t in rest[:2]),
    'let':     lambda rest: any(t.kind in ('IDENT', 'MUT') for t in rest[:3]) and not any(t.kind == 'LPAREN' for t in rest[:3]),
    'if':      lambda rest: len(rest) > 0 and rest[0].kind not in ('LPAREN',),
    'while':   lambda rest: len(rest) > 0,
    'for':     lambda rest: any(t.kind == 'IN' for t in rest[:6]),
    'return':  lambda rest: len(rest) > 0,
    'import':  lambda rest: any(t.kind == 'IDENT' for t in rest[:2]),
    'use':     lambda rest: any(t.kind == 'IDENT' for t in rest[:2]),
    'try':     lambda rest: any(t.kind == 'LBRACE' for t in rest[:2]),
    'print':   lambda rest: any(t.kind == 'LPAREN' for t in rest[:2]),
    'println': lambda rest: any(t.kind == 'LPAREN' for t in rest[:2]),
}

def _guess_construct(word: str, rest_tokens: list) -> Optional[str]:
    """Return the most likely intended keyword given the misspelled word and the tokens that follow."""
    # Common aliases from other languages that are too far for edit-distance alone
    _ALIASES = {
        'def': 'fn', 'func': 'fn', 'function': 'fn', 'fun': 'fn', 'proc': 'fn',
        'var': 'let', 'val': 'let', 'const': 'let',
        'elif': 'else if',
        'do': 'while',
        'foreach': 'for',
    }
    wl = word.lower()
    if wl in _ALIASES:
        return _ALIASES[wl]

    candidate = _closest_keyword(word)
    if candidate is None:
        return None
    # Refine using structural hints from the following tokens
    for kw, hint_fn in _CONSTRUCT_HINTS.items():
        if _edit_distance(wl, kw) <= 2 and hint_fn(rest_tokens):
            return kw
    return candidate


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
        if tok.kind == 'CONTINUE': return self.parse_continue()
        if tok.kind == 'OPEN': return self.parse_open_block()
        if tok.kind in ('PRINT', 'PRINTLN', 'INPUT', 'FILE_READ', 'FILE_WRITE', 'FILE', 'RANGE', 'THREAD', 'OS'):
            return self.parse_call_or_print(tok)

        # Typo / misspelled keyword detection.
        # If we have a bare IDENT at statement position, check whether it looks
        # like a misspelled keyword before falling through to expression parsing.
        if tok.kind == 'IDENT':
            rest = self.tokens[self.pos + 1:]
            suggestion = _guess_construct(tok.value, rest)
            if suggestion:
                R, B, DIM = '\033[0m', '\033[1;31m', '\033[2m'
                Y = '\033[1;33m'
                print(
                    f"{B}error[P100]{R}: Unknown keyword `{tok.value}` at line {tok.line}\n"
                    f" {DIM}hint:{R} did you mean `{Y}{suggestion}{R}`?\n"
                    f"      Replace `{tok.value}` with `{suggestion}`\n"
                )
                self.errors += 1
                raise ParseError()

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
        if self.current().kind == 'IDENT':
            symbol_name = self.consume('IDENT').value
            self.consume('LPAREN')
            params = []
            while self.current().kind not in ('RPAREN', 'EOF'):
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
            return FFIBind(name, symbol_name, params, ret_type, line)
        self.consume('LPAREN')
        params = []
        while self.current().kind not in ('RPAREN', 'EOF'):
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
        if self.current().kind in ('IDENT', 'FILE', 'TYPE'):
            var_tok = self.consume()
        else:
            var_tok = Token('IDENT', '_file_handle', line, 0)
        handle_name = var_tok.value
        self._open_file_handles = getattr(self, '_open_file_handles', set())
        self._open_file_handles.add(handle_name)
        self.consume('LBRACE')
        body = []
        while self.current().kind not in ('RBRACE', 'EOF'):
            stmt = self.parse_statement()
            if stmt: body.append(stmt)
        self.consume('RBRACE')
        self._open_file_handles.discard(handle_name)
        return ForStmt(var_tok, None, body, line)

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
        is_local = False
        if self.current().kind == 'MUT':
            is_mut = True; self.consume('MUT')
        if self.current().kind == 'LOCAL':
            is_local = True; self.consume('LOCAL')
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
            
        return VarDecl(name, is_mut, is_local, v_type, expr, line)

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
        return ExprStmt(FunctionCall(tok, args, tok.line), tok.line)

    def _parse_method_args(self):
        """Parse (arg, ...) returning list of args; assumes LPAREN already seen"""
        self.consume('LPAREN')
        args = []
        while self.current().kind not in ('RPAREN', 'EOF'):
            args.append(self.parse_expression())
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RPAREN')
        return args

    def parse_os_run_struct(self):
        """Parse { cmd: expr, args: [...], input: expr } for os.run() struct form"""
        self.consume('LBRACE')
        fields = {}
        while self.current().kind not in ('RBRACE', 'EOF'):
            key = self.consume('IDENT')
            # In struct form, the separator is COLON, not OP
            self.consume('COLON')
            val = self.parse_expression()
            fields[key.value] = val
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RBRACE')
        return fields

    def parse_os_run_regular(self, os_tok):
        """Parse os.run(id, cmd, input) regular form - returns MethodCall"""
        # We're positioned after LPAREN, need to parse: id, cmd, input
        args = []
        while self.current().kind not in ('RPAREN', 'EOF'):
            args.append(self.parse_expression())
            if self.current().kind == 'COMMA': self.consume('COMMA')
        self.consume('RPAREN')
        # Return as MethodCall with os identifier and run method
        return MethodCall(Identifier(os_tok), Token('IDENT', 'run', os_tok.line, 0), args, os_tok.line)

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

    def parse_continue(self): return ContinueStmt(self.consume('CONTINUE').line)

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
        elif tok.kind == 'BOOL': base_expr = Literal(tok, tok.value == 'True') if tok.value not in ('Null', 'None') else Literal(tok, None)
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
                # Support both COLON (for struct/os.run) and OP with '=' (for dict)
                if self.current().kind == 'COLON':
                    self.consume('COLON')
                else:
                    self.consume('OP')  # for '=' operator
                v = self.parse_expression()
                pairs.append((k, v))
                if self.current().kind == 'COMMA': self.consume('COMMA')
            self.consume('RBRACE')
            base_expr = DictLiteral(pairs, line)
        elif tok.kind in ('IDENT', 'PRINT', 'PRINTLN', 'INPUT', 'FILE_READ', 'FILE_WRITE', 'FILE', 'RANGE', 'THREAD', 'ERROR', 'OS'):
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
        self.is_auto_dropped = False  # True for loop vars, fn params, error vars — auto-freed by runtime
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
        self.cur_class: Optional[str] = None  # set while visiting a ClassDef body
        self.in_try_block = False
        
        self.builtins = {"print", "println", "input", "thread", "thread.wait", "thread.running", "range", "random", "time", "time.wait", "time.timer_start", "time.timer_pause", "time.timer_stop", "time.timer_read", "os", "os.start", "os.run", "file", "file.exists", "file.delete", "file.rename", "file.copy", "file.new", "FFI"}
        self.errors, self.warnings = 0, 0
        self.filepath, self.source_lines = filepath, source_lines
        self.is_main_file = is_main_file
        self.import_stack = import_stack or []
        self.known_import_modules: set = set()  # modules loaded via import/use statements

    def report_error(self, code, msg, line):
        print(f"\033[1;31merror[{code}]\033[0m: {msg}\n \033[1;36m-->\033[0m {self.filepath}:{line}")
        print(f" \033[1;36m{line:3} |\033[0m {self.source_lines[line-1].strip()}\n     \033[1;36m|\033[0m \033[1;31m^^^\033[0m\n")
        self.errors += 1
        
    def report_warning(self, code, msg, line, hint):
        print(f"\033[1;33mwarning[{code}]\033[0m: {msg}\n \033[1;36m-->\033[0m {self.filepath}:{line}")
        print(f" \033[1;36m{line:3} |\033[0m {self.source_lines[line-1].strip()}\n     \033[1;36m|\033[0m \033[1;33m--- \033[0m\033[3m{hint}\033[0m\n")
        self.warnings += 1

    def push_scope(self): self.scopes.append({})
        
    def pop_scope(self, is_class_scope=False, is_global_scope=False): 
        popped = self.scopes.pop()
        for var_name, meta in popped.items():
            if not meta.is_read and not is_class_scope:
                self.report_warning("W012", f"Unused variable `{var_name}`.", meta.declared_line, hint="Remove this variable if it is not needed.")
            if meta.is_mut and not meta.is_reassigned and meta.is_initialized and not is_class_scope:
                self.report_warning("W011", f"Variable `{var_name}` does not need to be mutable.", meta.declared_line, hint=f"Change to `let {var_name}`.")
            # W010 only fires for global-scope vars — function/loop locals are auto-freed on return/exit
            if not meta.is_dropped and not is_class_scope and not meta.is_auto_dropped and is_global_scope:
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
            if expr.token.kind == 'BOOL':
                if expr.value is None: return "Unknown"  # Null literal — valid for all types
                return "Bool"
            if expr.token.kind == 'TYPE': return "Int" if expr.value.startswith('i') else ("Float" if expr.value.startswith('f') else "Unknown")
        elif isinstance(expr, InterpolatedStr): return "String"
        elif isinstance(expr, Identifier):
            meta = self.get_var(expr.token.value)
            if meta: return meta.type_cat
            # Handle module identifiers
            if expr.token.value in ("time", "random", "thread", "os", "file"): return expr.token.value
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
                if expr.method.value == "char": return "String"
                if expr.method.value == "slice": return "List<String>"
            elif base_type == "time":
                if expr.method.value in ("sleep", "wait", "timer_start", "timer_pause", "timer_stop"):
                    return "Void"
                if expr.method.value == "timer_read":
                    return "Float"
            elif base_type == "os":
                if expr.method.value in ("start",):
                    return "Void"
                if expr.method.value in ("run",):
                    return "String"
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

    def _register_implicit_class_fields(self):
        """Mirrors codegen.py's _register_implicit_class_fields: per spec ('Classes
        create their own scope'), any non-local `let` inside a class method becomes
        an implicit instance field. Without this, E091/E092 would false-positive on
        valid external access like `model.input_w`."""
        def walk(body, c_meta):
            for stmt in body:
                if isinstance(stmt, VarDecl) and not stmt.is_local:
                    if stmt.name.value not in c_meta.properties:
                        c_meta.properties[stmt.name.value] = self.get_type_category(stmt.v_type.value) if stmt.v_type else "Unknown"
                elif isinstance(stmt, IfStmt):
                    walk(stmt.body, c_meta)
                    if stmt.else_body: walk(stmt.else_body, c_meta)
                elif isinstance(stmt, WhileStmt):
                    walk(stmt.body, c_meta)
                elif isinstance(stmt, ForStmt):
                    walk(stmt.body, c_meta)
                elif isinstance(stmt, TryErrorBlock):
                    walk(stmt.try_body, c_meta); walk(stmt.error_body, c_meta)
                elif isinstance(stmt, FileOpenBlock):
                    walk(stmt.body, c_meta)

        for c_meta in self.classes.values():
            for m in c_meta.methods.values():
                walk(m.body, c_meta)

    def run(self, ast_nodes, tokens):
        for node in ast_nodes:
            if isinstance(node, FunctionDef): self.global_functions[node.name.value] = node
            elif isinstance(node, ClassDef):
                c_meta = ClassMeta(node.name.value)
                for stmt in node.body:
                    if isinstance(stmt, FunctionDef): c_meta.methods[stmt.name.value] = stmt
                    elif isinstance(stmt, VarDecl): c_meta.properties[stmt.name.value] = self.get_type_category(stmt.v_type.value) if stmt.v_type else "Unknown"
                self.classes[node.name.value] = c_meta

        self._register_implicit_class_fields()
        for node in ast_nodes: self.visit(node)
        self.pop_scope(is_global_scope=True)
        
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
                # Register the module namespace in the known-modules set so that
                # module.func() calls don't produce false E002 "not in scope" errors.
                # We don't put it in the variable scope to avoid spurious W010/E041.
                mod_name = node.module.value
                self.known_import_modules.add(mod_name)

        elif isinstance(node, FunctionDef):
            self.current_return_type = self.get_type_category(node.return_type) if node.return_type else "Void"
            prev_returned, prev_unreachable = self.has_returned, self.unreachable
            self.has_returned, self.unreachable = False, False
            
            self.push_scope()
            for p_name, p_type in node.params:
                p_meta = VarMetadata(False, node.line, self.get_type_category(p_type), True)
                p_meta.is_auto_dropped = True  # params are auto-freed when function returns
                self.declare_var(p_name, p_meta, node.line)
            self.visit_block(node.body)
            self.pop_scope()
            
            if self.current_return_type != "Void" and not self.has_returned:
                self.report_error("E081", f"Function `{node.name.value}` expects to return `{self.current_return_type}` but might exit without returning.", node.line)
            
            self.current_return_type = None
            self.has_returned, self.unreachable = prev_returned, prev_unreachable

        elif isinstance(node, FFIBind):
            self.global_functions[node.symbol_name] = FunctionDef(
                Token('IDENT', node.symbol_name, node.line, 0),
                node.params,
                node.return_type,
                [],
                node.line
            )

        elif isinstance(node, ClassDef):
            self.cur_class = node.name.value
            self.push_scope()
            self.visit_block(node.body)
            self.pop_scope(is_class_scope=True)
            self.cur_class = None

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
            # Mark iterable as read (fixes W012 for vars only used in for loops)
            if node.iterable is not None:
                self.check_expr(node.iterable, node.line)
            self.loop_depth += 1
            prev_unreachable = self.unreachable
            self.push_scope()
            # Declare loop variable as local, immutable, initialized, auto-dropped at scope end
            if node.item:
                lv_meta = VarMetadata(False, node.line, "Unknown", True)
                lv_meta.is_auto_dropped = True  # loop vars are auto-freed at loop end
                lv_meta.is_read = True           # suppress W012 — loop vars are implicitly used
                self.declare_var(node.item.value, lv_meta, node.line)
            self.visit_block(node.body)
            self.pop_scope()
            self.unreachable = prev_unreachable
            self.loop_depth -= 1

        elif isinstance(node, FileOpenBlock):
            path_type = self.infer_type(node.path_expr)
            if path_type not in ("Unknown", "String"):
                self.report_error("E062", f"Type `{path_type}` is not a valid file path.", node.line)
            self.declare_var(node.var_name.value, VarMetadata(False, node.line, "String", True), node.line)
            prev_unreachable = self.unreachable
            for stmt in node.body:
                if self.unreachable:
                    self.report_warning("W013", "Unreachable code detected.", getattr(stmt, 'line', 0), hint="This executes after a `return` or `break`.")
                self.visit(stmt)
            self.unreachable = prev_unreachable

        elif isinstance(node, TryErrorBlock):
            prev_unreachable = self.unreachable
            prev_in_try = self.in_try_block
            self.in_try_block = True
            self.push_scope(); self.visit_block(node.try_body); self.pop_scope()
            self.in_try_block = prev_in_try
            self.unreachable = prev_unreachable
            
            self.push_scope()
            err_meta = VarMetadata(False, node.line, "String", True)
            err_meta.is_auto_dropped = True  # error var is auto-freed at end of error block
            self.declare_var("error", err_meta, node.line)
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

        elif isinstance(node, ContinueStmt):
            if self.loop_depth == 0:
                self.report_error("E062", "Use of `continue` outside of a loop.", node.line)
            self.unreachable = True

        elif isinstance(node, OsStart):
            self.check_expr(node.id_expr, node.line)

        elif isinstance(node, OsRun):
            if node.struct_args:
                for k, v in node.struct_args.items():
                    self.check_expr(v, node.line)
            else:
                self.check_expr(node.id_expr, node.line) if node.id_expr else None
                self.check_expr(node.cmd_expr, node.line) if node.cmd_expr else None
                self.check_expr(node.input_expr, node.line) if node.input_expr else None

        elif isinstance(node, OsDrop):
            self.check_expr(node.id_expr, node.line)

        elif isinstance(node, ExprStmt): self.check_expr(node.expr, node.line)
        elif isinstance(node, MethodCall): self.check_expr(node, node.line if hasattr(node, 'line') else 0)

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
                # Mark base object as reassigned (field assignment mutates the instance)
                if isinstance(node.target.obj, Identifier):
                    base_meta = self.get_var(node.target.obj.token.value)
                    if base_meta: base_meta.is_reassigned = True
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
                # Check if it's a builtin module (time, random, thread, os, file)
                if expr.token.value in ("time", "random", "thread", "os", "file"):
                    return  # Built-in module identifier, valid
                # Check if it's a module loaded via import/use
                if expr.token.value in self.known_import_modules:
                    return  # Imported module namespace, valid
                if expr.token.value not in self.builtins and expr.token.value not in self.global_functions and expr.token.value not in self.classes:
                    self.report_error("E002", f"Cannot find `{expr.token.value}` in scope", expr.token.line)
            else:
                if not meta.is_initialized: self.report_error("E032", f"Use of uninitialized variable `{expr.token.value}`", current_line)
                if meta.is_dropped: self.report_error("E041", f"Use-after-free: `{expr.token.value}` was already dropped.", current_line)
                meta.is_read = True 

        elif isinstance(expr, FunctionCall):
            target_name = expr.name.value
            meta = self.get_var(target_name)
            if meta:
                # Variable exists — calling with args is always collection access in Rubidium
                for arg in expr.args: self.check_expr(arg, current_line)
                meta.is_read = True
            elif self.cur_class and target_name in self.classes.get(self.cur_class, ClassMeta("")).methods:
                # Sibling method call (e.g. heal(40) inside use_potion — no 'self' keyword per spec)
                for arg in expr.args: self.check_expr(arg, current_line)
            elif target_name not in self.global_functions and target_name not in self.builtins and target_name not in self.classes:
                candidates = list(self.global_functions) + list(self.builtins) + list(self.classes)
                suggestion = _closest_name(target_name, candidates)
                msg = f"Cannot find function or class `{target_name}`"
                if suggestion:
                    msg += f" — did you mean `{suggestion}`?"
                self.report_error("E003", msg, expr.line)
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
            # Mutation methods mark the base variable as reassigned (suppress false W011)
            if expr.method.value in ("set", "add", "insert", "replace", "shuffle"):
                if isinstance(expr.obj, Identifier):
                    m = self.get_var(expr.obj.token.value)
                    if m: m.is_reassigned = True
                elif isinstance(expr.obj, FunctionCall):
                    # obj(key).set() or obj().add() — trace back to the collection variable
                    m = self.get_var(expr.obj.name.value)
                    if m: m.is_reassigned = True
            # random.shuffle(items) marks the first argument as mutated
            if expr.method.value == "shuffle" and expr.args:
                if isinstance(expr.args[0], Identifier):
                    m = self.get_var(expr.args[0].token.value)
                    if m: m.is_reassigned = True
            
            if expr.method.value != "drop":
                if b_type == "String" and expr.method.value not in ("len", "has", "to", "combine", "char", "set", "insert", "replace", "slice", "contains"):
                    self.report_error("E090", f"Method `{expr.method.value}` not found on String", expr.line)
                elif b_type == "time" and expr.method.value not in ("sleep", "wait", "timer_start", "timer_pause", "timer_stop", "timer_read"):
                    self.report_error("E090", f"Method `{expr.method.value}` not found on time module", expr.line)
                elif b_type.startswith("Class<"):
                    c_name = b_type[6:-1]
                    if c_name in self.classes:
                        # Mark all fields of this class as read — they may be used inside the method body
                        for field_name in self.classes[c_name].properties:
                            for scope in reversed(self.scopes):
                                if field_name in scope:
                                    scope[field_name].is_read = True
                                    break
                        # Mark the instance itself as reassigned if the method mutates state
                        # (any method call on an instance counts — we can't know statically)
                        # debug.py AST: obj is Identifier(token), so name is obj.token.value
                        if isinstance(expr.obj, Identifier):
                            obj_meta = self.get_var(expr.obj.token.value)
                            if obj_meta:
                                obj_meta.is_reassigned = True
                        if expr.method.value in self.classes[c_name].properties:
                            pass
                        else:
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
                if c_name in self.classes:
                    if expr.prop.value not in self.classes[c_name].properties:
                        self.report_error("E092", f"Property `{expr.prop.value}` not found on class `{c_name}`", current_line)
                    else:
                        # Mark the class field as read so W012 doesn't fire on it
                        for scope in reversed(self.scopes):
                            if expr.prop.value in scope:
                                scope[expr.prop.value].is_read = True
                                break

        elif isinstance(expr, InterpolatedStr):
            for part in expr.parts:
                if isinstance(part, Identifier):
                    self.check_expr(part, current_line)

        elif isinstance(expr, BinaryOp):
            self.check_expr(expr.left, current_line)
            self.check_expr(expr.right, current_line)
            l_t, r_t = self.infer_type(expr.left), self.infer_type(expr.right)
            
            if expr.op.kind == 'LOGIC' and (l_t not in ("Unknown", "Bool") or r_t not in ("Unknown", "Bool")):
                self.report_error("E036", "Logical operators (and/or) require `bool` operands.", current_line)
            elif l_t != "Unknown" and r_t != "Unknown" and l_t != r_t and expr.op.value in ('+', '-', '*', '/', '<', '>', '<=', '>=') and not (l_t == "String" and r_t != "String" and expr.op.value == '+') and not ({l_t, r_t} == {"Int", "Float"}):
                # String + Other type is allowed (int to string coercion).
                # Int/Float mixing is allowed (compiler promotes to float, matches `coerce`/`promote_type`).
                self.report_error("E033", f"Type mismatch in binary operation: cannot apply `{expr.op.value}` to `{l_t}` and `{r_t}`", current_line)
            if expr.op.value == '/' and isinstance(expr.right, Literal) and expr.right.value == 0:
                if self.in_try_block:
                    self.report_warning("W050", "Division by zero detected statically (inside try block, will be caught at runtime).", current_line, hint="This is inside a try/error block so the error will be caught.")
                else:
                    self.report_error("E050", "Division by zero detected statically.", current_line)

        elif isinstance(expr, OsRun):
            if expr.struct_args:
                for k, v in expr.struct_args.items():
                    self.check_expr(v, current_line)

        elif isinstance(expr, OsStart):
            self.check_expr(expr.id_expr, current_line)

        elif isinstance(expr, OsDrop):
            self.check_expr(expr.id_expr, current_line)

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
    
    # ──────────────────────────────────────────────────────────────────────────
    # PRE-PARSING KEYWORD / TYPO INTERCEPTOR
    # ──────────────────────────────────────────────────────────────────────────
    has_typo_errors = False
    for i, tok in enumerate(tokens):
        if tok.kind == 'IDENT':
            rest = tokens[i + 1:]
            suggestion = _guess_construct(tok.value, rest)
            if suggestion:
                R, B, DIM = '\033[0m', '\033[1;31m', '\033[2m'
                Y = '\033[1;33m'
                print(
                    f"{B}error[P100]{R}: Unknown keyword `{tok.value}` at line {tok.line}\n"
                    f" {DIM}hint:{R} did you mean `{Y}{suggestion}{R}`?\n"
                    f"      Replace `{tok.value}` with `{suggestion}`\n"
                )
                has_typo_errors = True
                
    if has_typo_errors:
        print(f"\n\033[1;31merror\033[0m: aborting due to syntax error(s)")
        sys.exit(1)

    # Now proceed to the main parser safely
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