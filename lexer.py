import re

TOKEN_SPEC = [
    # OPEN-8 root cause: hex/binary/octal literals must be matched BEFORE the
    # decimal rules, or `0x03` lexes as NUMBER `0` + NAME `x03` and the real
    # value is silently lost (everything hex read back as 0). Order in the
    # alternation matters: prefixed bases first, then float, then plain int.
    ("NUMBER",   r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+\.\d+|\d+"),
    ("ISTRING_PLACEHOLDER", r"i\"PLACEHOLDER\""),  # placeholder — handled below
    # BUGFIX (bugs.log #3): triple-quoted multi-line strings (used for the `str+`
    # type) must be matched BEFORE the single-quote STRING rule below, or a
    # `"""..."""` literal gets split into three separate STRING tokens (two
    # empty strings plus the real content), which previously produced a NULL
    # value at runtime and crashed. [\s\S]*? matches across newlines, non-greedy
    # so it stops at the first closing """.
    ("STRING3",  r'"""[\s\S]*?"""'),
    ("STRING",   r'"[^"]*"'),
    # BUGFIX (bugs.log #2): SY type literals are written with single quotes,
    # e.g. `let varable_name: SY = 'New varable'`. Previously unsupported —
    # a bare `'` fell through to "Unexpected character".
    ("SYSTRING", r"'[^']*'"),
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
    ("RAISE",    r"raise\b"),
    ("ON_ERROR", r"on_error\b"),
    ("IMPORT",   r"import\b"),
    ("XEON",     r"xeon\b"),
    ("USE",      r"use\b"),
    ("FILE",     r"\bfile\b"),

    ("AS",       r"as\b"),
    ("AND",      r"and\b"),
    ("OR",       r"or\b"),
    ("NOT",      r"not\b"),

    ("TYPE",     r"\bstr\+|\bdict\+|\bSY\b|\b(?:i32|i64|i128|i256|i512|i1024|i2048|f32|f64|f128|f256|f512|f1024|f2048|str|bool|list|index|dict|Any)\b"),
    ("LOCAL",    r"\blocal\b"),
    ("OPEN",     r"\bopen\b"),
    # syntax: FFI CALLBACKS — `fn callback name(...) { ... }`, a soft
    # keyword the same way local/open/file are (its own token type, matched
    # before the generic IDENT catch-all, so it can't double as a variable
    # or function name — but only meaningful right after `fn`, everywhere
    # else it's simply unavailable as an identifier).
    ("CALLBACK", r"\bcallback\b"),

    ("IDENT",    r"[a-zA-Z_][a-zA-Z0-9_]*"),

    ("OP",       r"==|!=|<=|>=|->|=|\+|-|\*\*|\*/|\*|/|%|<|>"),
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

        # BUGFIX (bugs.log OPEN-8 follow-up): 'self' inside a class method must
        # refer to the same instance-parameter name codegen registers
        # (method params are prepended with ("__self", class_name), and
        # _emit_class_method registers self.instances["__self"] = class_name).
        # The parser has no special-casing for the word "self" at all — it's
        # just tokenized as an ordinary IDENT — so every Var/FieldAccess/
        # FieldAssign/MethodCall built from a literal "self" reference never
        # matched "__self" in codegen's instance-name lookups: reads raised
        # "'self' is not an instance" and writes (self.field = value) were
        # silently dropped as a no-op. Canonicalizing at the token level fixes
        # every one of those call sites at once, instead of patching each
        # spot in the parser/codegen individually.
        if kind == "IDENT" and value == "self":
            value = "__self"

        tokens.append((kind, value, line_no))
        if kind == "STRING3":
            line_no += value.count("\n")
    return tokens