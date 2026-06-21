import re

TOKEN_SPEC = [
    ("NUMBER",   r"\d+\.\d+|\d+"),
    ("ISTRING_PLACEHOLDER", r"i\"PLACEHOLDER\""),  # placeholder — handled below
    ("STRING",   r'"[^"]*"'),
    ("BOOL",     r"True|False|Null|None"),

    ("LET",      r"let\b"),
    ("MUT",      r"mut\b"),
    ("FN",       r"fn\b"),
    ("CLASS",    r"class\b"),
    ("IF",       r"if\b"),
    ("ELSE",     r"else\b"),
    ("WHILE",    r"while\b"),
    ("FOR",      r"for\b"),
    ("IN",       r"in\b"),
    ("BREAK",    r"break\b"),
    ("CONTINUE", r"continue\b"),
    ("RETURN",   r"return\b"),
    ("PRINTLN",  r"println\b"),
    ("PRINT",    r"print\b"),
    ("RANGE",    r"range\b"),
    ("TRY",      r"try\b"),
    ("ON_ERROR", r"on_error\b"),
    ("IMPORT",   r"import\b"),
    ("USE",      r"use\b"),
    ("FILE",     r"\bfile\b"),

    ("AS",       r"as\b"),
    ("AND",      r"and\b"),
    ("OR",       r"or\b"),
    ("NOT",      r"not\b"),

    ("TYPE",     r"\b(?:i32|i64|i128|i256|i512|i1024|i2048|f32|f64|f128|f256|f512|f1024|f2048|str|bool|list|index|dict|Any)\b"),
    ("LOCAL",    r"\blocal\b"),
    ("OPEN",     r"\bopen\b"),

    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),

    ("OP",       r"==|!=|<=|>=|->|=|\+|-|\*\*|\*/|\*|/|<|>"),
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("COMMA",    r","),
    ("COLON",    r":"),
    ("DOT",      r"\."),

    ("COMMENT",  r"#[^\n]*"),
    ("SKIP",     r"[ \t]+"),
    ("NEWLINE",  r"\n"),
    ("MISMATCH", r"."),
]

token_regex = "|".join(f"(?P<{n}>{r})" for n, n_r in TOKEN_SPEC for n, r in [(n, n_r)] if n != "ISTRING_PLACEHOLDER")
# Re-build without the placeholder (it was never in TOKEN_SPEC under that name)
_REAL_SPEC = [(n, r) for n, r in TOKEN_SPEC if n != "ISTRING_PLACEHOLDER"]
token_regex = "|".join(f"(?P<{n}>{r})" for n, r in _REAL_SPEC)


def _scan_istring(code, start):
    """Scan a brace-aware i\"...\" token starting at position start (the 'i').
    Returns (full_token_text, end_pos) or raises SyntaxError."""
    assert code[start] == 'i' and code[start+1] == '"', "Not an ISTRING"
    i = start + 2  # skip i"
    depth = 0       # brace nesting depth
    in_str = False  # are we inside a nested " string inside {}?
    while i < len(code):
        c = code[i]
        if in_str:
            if c == '\\':
                i += 2; continue  # skip escaped char
            if c == '"':
                in_str = False
        else:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == '"':
                if depth == 0:
                    # Closing quote of the i-string
                    return code[start:i+1], i+1
                else:
                    in_str = True  # entering nested string inside {}
        i += 1
    raise SyntaxError("Unterminated interpolated string")


_token_re = re.compile(token_regex)

def tokenize(code):
    tokens = []
    line_no = 1
    pos = 0
    n = len(code)

    while pos < n:
        # Fast-path: detect i" at current position
        if code[pos] == 'i' and pos + 1 < n and code[pos+1] == '"':
            text, pos = _scan_istring(code, pos)
            tokens.append(("ISTRING", text, line_no))
            line_no += text.count('\n')
            continue

        m = _token_re.match(code, pos)
        if not m:
            raise SyntaxError(f"Line {line_no}: Unexpected character: {code[pos]!r}")

        kind = m.lastgroup
        value = m.group()
        pos = m.end()

        if kind == "NEWLINE":
            line_no += 1
            continue
        if kind in ("SKIP", "COMMENT"):
            continue
        if kind == "MISMATCH":
            raise SyntaxError(f"Line {line_no}: Unexpected character: {value!r}")

        tokens.append((kind, value, line_no))
    return tokens