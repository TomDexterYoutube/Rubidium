import sys
import os

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
    ("USE",      r"\buse\b"),
    ("IMPORT",   r"\bimport\b"),
    ("LOCAL",    r"\blocal\b"),
    ("TYPE",     r"\b(?:i32|i64|i128|i256|i512|i1024|i2048|f32|f64|f128|f256|f512|f1024|f2048|str|bool|list|dict|index|Any)\b"),
    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),
    ("ASSIGN",   r"="),
    ("OP",       r"\*\*|\+|-|\*|/|%"),
    ("COMP",     r"==|!=|<=|>=|<|>"),
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("LBRACK",   r"\["),
    ("RBRACK",   r"\]"),
    ("COMMA",    r","),
    ("COLON",    r":"),
    ("ARROW",    r"->"),
    ("DOT",      r"\."),
    ("COMMENT",  r"#[^\n]*"),
    ("NEWLINE",  r"\n"),
    ("SKIP",     r"[ \t\r]+"),
    ("MISMATCH", r"."),
]

@dataclass
class Token:
    kind: str
    value: str
    line: int
    col: int

def tokenize(code: str) -> List[Token]:
    tokens = []
    regex = "|".join(f"(?P<{name}>{pattern})" for name, pattern in TOKEN_SPEC)
    line_num = 1
    line_start = 0
    
    for mo in re.finditer(regex, code):
        kind = mo.lastgroup
        value = mo.group(kind)
        col = mo.start() - line_start
        
        if kind == "NEWLINE":
            line_num += 1
            line_start = mo.end()
            continue
        elif kind in ("SKIP", "COMMENT"):
            continue
        elif kind == "MISMATCH":
            print(f"\033[1;31merror[Lexer]\033[0m: Unexpected character '{value}' at line {line_num}:{col}")
            sys.exit(1)
            
        tokens.append(Token(kind, value, line_num, col))
    return tokens

# ──────────────────────────────────────────────────────────────────────────────
# Fuzzy helpers for Debugger Interception
# ──────────────────────────────────────────────────────────────────────────────

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

def _closest_name(word: str, candidates, max_dist: int = 1) -> str | None:
    best, best_d = None, max_dist + 1
    for c in candidates:
        d = _edit_distance(word.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    return best if best_d <= max_dist else None

# Placeholder definitions for Parser/StaticAnalyzer to allow execution structure
class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.errors = 0
    def parse(self):
        return []

class StaticAnalyzer:
    def __init__(self, filename: str, lines: List[str], is_main_file: bool):
        self.warnings = 0
    def run(self, ast, tokens):
        return True

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

    with open(filepath, 'r') as f: 
        code = f.read()

    source_lines = code.split('\n')

    start_time = time.time()
    tokens = tokenize(code)

    # ── Pre-parsing Keyword & Typo Interceptor ──
    KEYWORD_MAPPING = {
        'def': 'fn', 'function': 'fn', 'func': 'fn',
        'var': 'let', 'const': 'let',
        'elif': 'else if', 'elseif': 'else if',
        'nil': 'Null', 'none': 'Null', 'null': 'Null',
    }
    RUBIDIUM_KEYWORDS = [
        'let', 'mut', 'fn', 'class', 'drop', 'thread', 'while', 
        'for', 'if', 'else', 'return', 'print', 'println', 'use', 'import'
    ]

    has_typo_errors = False
    for tok in tokens:
        if tok.kind == 'IDENT':
            # Ignore words that are entirely uppercase (e.g. NN)
            if tok.value.isupper():
                continue

            if tok.value in KEYWORD_MAPPING:
                correct = KEYWORD_MAPPING[tok.value]
                error_line_str = source_lines[tok.line - 1] if 0 < tok.line <= len(source_lines) else ""
                padding = " " * tok.col
                underline = "^" * len(tok.value)

                print(f"\n\033[1;31merror[Syntax]\033[0m on Line {tok.line}: Invalid Keyword")
                print(f"Found '{tok.value}', but Rubidium uses '{correct}'.")
                print(f" \033[1;36m-->\033[0m line {tok.line}")
                print(f" \033[1;36m{tok.line:3} |\033[0m {error_line_str}")
                print(f"     | \033[1;31m{padding}{underline}\033[0m")
                print(f"Suggestion: Replace '{tok.value}' with '{correct}'.\n")
                has_typo_errors = True

            suggestion = _closest_name(tok.value, RUBIDIUM_KEYWORDS, max_dist=1)
            if suggestion and tok.value not in RUBIDIUM_KEYWORDS:
                error_line_str = source_lines[tok.line - 1] if 0 < tok.line <= len(source_lines) else ""
                padding = " " * tok.col
                underline = "^" * len(tok.value)

                print(f"\n\033[1;31merror[Syntax]\033[0m on Line {tok.line}: Misspelled Keyword")
                print(f"Found '{tok.value}'. Did you mean '{suggestion}'?")
                print(f" \033[1;36m-->\033[0m line {tok.line}")
                print(f" \033[1;36m{tok.line:3} |\033[0m {error_line_str}")
                print(f"     | \033[1;31m{padding}{underline}\033[0m")
                print(f"Suggestion: Change '{tok.value}' to '{suggestion}'.\n")
                has_typo_errors = True

    if has_typo_errors:
        print("\033[1;31merror\033[0m: aborting execution due to syntax typo errors.")
        sys.exit(1)

    parser = Parser(tokens)
    ast = parser.parse()

    if parser.errors > 0:
        print(f"\n\033[1;31merror\033[0m: aborting due to {parser.errors} syntax error(s)")
        sys.exit(1)

    analyzer = StaticAnalyzer(os.path.basename(filepath), source_lines, is_main_file=True)
    success = analyzer.run(ast, tokens)
    duration = (time.time() - start_time) * 1000
    
    if success:
        if analyzer.warnings > 0: 
            print(f"\033[1;33mwarning\033[0m: program passed check with warnings.")
    else:
        sys.exit(1)

if __name__ == '__main__':
    main()