import re

TOKEN_SPEC = [
    ("NUMBER",   r"\d+\.\d+|\d+"),
    ("STRING",   r'"[^"]*"'),
    ("BOOL",     r"True|False|None"),

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
    ("RETURN",   r"return\b"),
    ("PRINTLN",  r"println\b"),
    ("PRINT",    r"print\b"),
    ("RANGE",    r"range\b"),
    ("TRY",      r"try\b"),
    ("ON_ERROR", r"on_error\b"),
    ("IMPORT",   r"import\b"),
    ("USE",      r"use\b"),

    ("AS",       r"as\b"),
    ("AND",      r"and\b"),
    ("OR",       r"or\b"),
    ("NOT",      r"not\b"),

    ("TYPE",     r"\b(?:i8|i16|i32|i64|i128|i256|f4|f8|f16|f32|f64|f128|f256|str|bool|list|index|dict)\b"),

    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),

    ("OP",       r"==|!=|<=|>=|->|=|\+|-|\*|/|<|>"),
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

token_regex = "|".join(f"(?P<{n}>{r})" for n, r in TOKEN_SPEC)

def tokenize(code):
    tokens = []
    line_no = 1
    for m in re.finditer(token_regex, code):
        kind = m.lastgroup
        value = m.group()

        if kind == "NEWLINE":
            line_no += 1
            continue
        if kind in ("SKIP", "COMMENT"):
            continue
        if kind == "MISMATCH":
            raise SyntaxError(f"Line {line_no}: Unexpected character: {value!r}")

        tokens.append((kind, value, line_no))
    return tokens