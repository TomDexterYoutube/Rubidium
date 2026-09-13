import re

TOKEN_SPEC = [
    # Hex/binary/octal first (before decimal)
    ("NUMBER",   r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+\.\d+|\d+"),
    # str+ (triple-quoted) before single-quoted
    ("STRING3",  r'"""[\s\S]*?"""'),
    # String (double-quoted)
    ("STRING",   r'"[^"]*"'),
    # SY string (single-quoted)
    ("SYSTRING", r"'[^']*'"),
    # Bool/Null
    ("BOOL",     r"True|False|Null"),
    
    # Keywords
    ("LET",      r"let\b"),
    ("MUT",      r"mut\b"),
    ("FN",       r"fn\b"),
    ("CALLBACK", r"callback\b"),
    ("CLASS",    r"class\b"),
    ("STRUCT",   r"struct\b"),
    ("IMPL",     r"impl\b"),
    ("PUB",      r"pub\b"),
    ("IF",       r"if\b"),
    ("ELSE",     r"else\b"),
    ("WHILE",    r"while\b"),
    ("FOR",      r"for\b"),
    ("IN",       r"in\b"),
    ("BREAK",    r"break\b"),
    ("CONTINUE", r"continue\b"),
    ("RETURN",   r"return\b"),
    ("PRINT",    r"print\b"),
    ("PRINTLN",  r"println\b"),
    ("RANGE",    r"range\b"),
    ("TRY",      r"try\b"),
    ("ERROR",    r"error\b"),
    ("RAISE",    r"raise\b"),
    ("IMPORT",   r"import\b"),
    ("XEON",     r"xeon\b"),
    ("USE",      r"use\b"),
    ("FILE",     r"\bfile\b"),
    ("AS",       r"as\b"),
    ("AND",      r"and\b"),
    ("OR",       r"or\b"),
    ("NOT",      r"not\b"),
    ("TYPE",     r"\b(?:i32|i64|i128|i256|i512|i1024|i2048|f32|f64|f128|str|bool|list|index|Any|void|Clist|SY)\b"),
    ("LOCAL",    r"\blocal\b"),
    ("OPEN",     r"\bopen\b"),
    ("LINK",     r"link\b"),
    ("CAST",     r"\bcast\b"),
    ("PULL",     r"\bpull\b"),
    
    # Identifier
    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),
    
    # Punctuation
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("COMMA",    r","),
    ("DBL_COLON", r"::"),
    ("COLON",    r":"),
    ("SEMICOLON", r";"),
    ("DOT",      r"\."),
    
    # Comments (// to end of line) - MUST come before OP to avoid matching // as two / operators
    ("COMMENT",  r"//[^\n]*"),
    
    # Operators (multi-char first)
    ("OP",       r"==|!=|<=|>=|->|\+=|-=|\*=|/=|%=|\*\*|\*/|\+|-|\*|/|%|<|>|="),
    
    # Whitespace
    ("SKIP",     r"[ \t]+"),
    ("NEWLINE",  r"\n"),
    
    # Mismatch
    ("MISMATCH", r"."),
]

_REAL_SPEC = [(n, r) for n, r in TOKEN_SPEC]
token_regex = "|".join(f"(?P<{n}>{r})" for n, r in _REAL_SPEC)

_token_re = re.compile(token_regex)


def _scan_istring(code, start):
    """Scan a brace-aware i"..." token starting at position start (the 'i').
    Returns (full_token_text, end_pos) or raises SyntaxError."""
    assert code[start] == 'i' and code[start+1] == '"', "Not an ISTRING"
    i = start + 2  # skip i"
    depth = 0       # brace nesting depth
    in_str = False  # are we inside a nested " string inside {}?
    escaped = False # was the previous char an unescaped backslash?
    while i < len(code):
        c = code[i]
        if escaped:
            # Current char is escaped, treat it literally
            escaped = False
        elif c == '\\':
            # Backslash escapes the next character
            escaped = True
        elif in_str:
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

        # BUGFIX (bugs.log OPEN-8 follow-up): 'self' inside a class method must
        # refer to the same instance-parameter name codegen registers
        # (method params are prepended with ("__self", class_name), and
        if kind == "IDENT":
            tokens.append((kind, value, line_no))
        else:
            tokens.append((kind, value, line_no))

    tokens.append(("EOF", "", line_no))
    return tokens