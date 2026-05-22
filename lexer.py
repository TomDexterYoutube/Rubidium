import re

TOKEN_SPEC = [
    ("NUMBER",   r"\d+"),
    ("STRING",   r'"[^"]*"'),
    ("BOOL",     r"True|False"),

    ("LET",      r"let"),
    ("MUT",      r"mut"),
    ("FN",       r"fn"),
    ("CLASS",    r"class"),
    ("IF",       r"if"),
    ("ELSE",     r"else"),
    ("WHILE",    r"while"),
    ("FOR",      r"for"),
    ("IN",       r"in"),
    ("RETURN",   r"return"),
    ("PRINT",    r"print"),
    ("RANGE",    r"range"),
    ("TRY",      r"try"),
    ("ON_ERROR", r"on_error"),
    ("IMPORT",   r"import"),
    ("USE",      r"use"),

    ("AND",      r"and"),
    ("OR",       r"or"),
    ("NOT",      r"not"),

    ("TYPE",     r"i8|i16|i32|i64|i128|i256|f4|f8|f16|f32|f64|f128|f256|str|bool"),

    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),

    ("OP",       r"==|!=|<=|>=|->|=|\+|-|\*|/|<|>"),
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
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
    for m in re.finditer(token_regex, code):
        kind = m.lastgroup
        value = m.group()

        if kind in ("SKIP", "NEWLINE", "COMMENT"):
            continue
        if kind == "MISMATCH":
            raise SyntaxError(f"Unexpected character: {value!r}")

        tokens.append((kind, value))
    return tokens
