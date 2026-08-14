from rub_ast import *
from decimal import Decimal
from fractions import Fraction
import re

extern_decls = '''
declare i32 @pthread_create(i64*, i64*, i8* (i8*)*, i8*)
declare i32 @pthread_join(i64, i8**)
declare i32 @pthread_tryjoin_np(i64, i8**)
declare i1 @_thread_is_running(i64)
declare double @strtod(i8*, i8**)
declare double @_rubidium_pow(double, double)
declare double @sin(double)
declare double @cos(double)
declare double @tan(double)
declare double @sqrt(double)
declare double @log(double)
declare double @log10(double)
declare double @exp(double)
declare double @fabs(double)
declare double @floor(double)
declare double @ceil(double)
declare double @round(double)
%Box = type { i32, i64, double, i8*, i8* }
declare %Box* @box_i(i64)
declare %Box* @box_f(double)
declare %Box* @box_s(i8*)
declare %Box* @box_p(i8*)
declare %Box* @box_b(i64)
declare %Box* @box_null()
declare i64 @unbox_i(%Box*)
declare double @unbox_f(%Box*)
declare i8* @unbox_s(%Box*)
declare i8* @unbox_p(%Box*)
declare %Box* @make_list()
declare void @list_append(%Box*, %Box*)
declare void @list_append_raw(%Box*, %Box*)
declare void @list_swap(%Box*, i32, i32)
declare %Box* @make_dict()
declare %Box* @make_dictplus()
declare void @dict_set(%Box*, %Box*, %Box*)
declare %Box* @collection_get(%Box*, %Box*)
declare void @collection_set(%Box*, %Box*, %Box*)
declare void @print_boxed(%Box*)
declare i8* @box_to_cstr(%Box*)
declare i32 @collection_len(%Box*)
declare i32 @box_equal(%Box*, %Box*)
declare i32 @box_compare_num(%Box*, %Box*)
declare %Box* @box_class(i8*, i64)
declare i64 @unbox_class_id(%Box*)
declare %Box* @str_split(i8*, i8*)
declare %Box* @try_collection_get(%Box*, %Box*)
declare void @collection_add1(%Box*, %Box*)
; BUG-4: element reads return an independent deep copy (see the runtime).
declare %Box* @collection_get_copy(%Box*, %Box*)
declare %Box* @try_collection_get_copy(%Box*, %Box*)
declare i8* @unbox_s_dup(%Box*)
; BUG-3: scope-owned temporary arena.
declare %Box* @rub_temp_track(%Box*)
declare i8* @rub_temp_track_str(i8*)
declare %Box* @rub_temp_untrack(%Box*)
declare i8* @rub_temp_untrack_str(i8*)
declare i64 @rub_temp_mark()
declare void @rub_temp_release_to(i64)
@_rub_error_msg = global i8* null
@_rub_error_flag = global i1 0
declare %Box* @collection_get_at(%Box*, i32)
declare void @collection_set_at(%Box*, i32, %Box*)
declare void @box_drop(%Box*)
declare void @collection_drop(%Box*, %Box*)
declare void @rub_throw(i8*)
declare void @rub_overflow_check(i32, i8*)
; BUG (found via syntax sweep): the ONLY existing overflow check lived in the
; NARROWING half of coerce() (e.g. i64 -> i32), which only ever ran when a
; literal happened to force implicit widening followed by narrowing back down
; at an assignment. Ordinary same-typed arithmetic (`x + y` for two i32
; variables, or arithmetic already at the widest type in play, e.g.
; i128 + i128) had NO overflow protection at all and silently WRAPPED —
; confirmed empirically, directly contradicting the spec's explicit "does not
; wrap around" guarantee. These give emit_binop's arithmetic path (not just
; coerce()'s narrowing path) real overflow detection for +, -, *.
declare { i32, i1 } @llvm.sadd.with.overflow.i32(i32, i32)
declare { i32, i1 } @llvm.ssub.with.overflow.i32(i32, i32)
declare { i32, i1 } @llvm.smul.with.overflow.i32(i32, i32)
declare { i64, i1 } @llvm.sadd.with.overflow.i64(i64, i64)
declare { i64, i1 } @llvm.ssub.with.overflow.i64(i64, i64)
declare { i64, i1 } @llvm.smul.with.overflow.i64(i64, i64)
declare { i128, i1 } @llvm.sadd.with.overflow.i128(i128, i128)
declare { i128, i1 } @llvm.ssub.with.overflow.i128(i128, i128)
declare { i128, i1 } @llvm.smul.with.overflow.i128(i128, i128)
declare { i256, i1 } @llvm.sadd.with.overflow.i256(i256, i256)
declare { i256, i1 } @llvm.ssub.with.overflow.i256(i256, i256)
declare { i256, i1 } @llvm.smul.with.overflow.i256(i256, i256)
declare { i512, i1 } @llvm.sadd.with.overflow.i512(i512, i512)
declare { i512, i1 } @llvm.ssub.with.overflow.i512(i512, i512)
declare { i512, i1 } @llvm.smul.with.overflow.i512(i512, i512)
declare { i1024, i1 } @llvm.sadd.with.overflow.i1024(i1024, i1024)
declare { i1024, i1 } @llvm.ssub.with.overflow.i1024(i1024, i1024)
declare { i1024, i1 } @llvm.smul.with.overflow.i1024(i1024, i1024)
declare { i2048, i1 } @llvm.sadd.with.overflow.i2048(i2048, i2048)
declare { i2048, i1 } @llvm.ssub.with.overflow.i2048(i2048, i2048)
declare { i2048, i1 } @llvm.smul.with.overflow.i2048(i2048, i2048)
declare %Box* @rub_dynvar_get(i8*)
declare void @rub_dynvar_set(i8*, %Box*)
declare %Box* @box_add(%Box*, %Box*)
declare %Box* @box_copy(%Box*)
declare i1 @collection_has(%Box*, %Box*)
declare i32 @_rub_str_hash(i8*)
declare i64 @time(i64*)
declare i64 @random()
declare void @srandom(i32)
declare i32 @usleep(i32)
declare i32 @sleep(i32)
declare void @time_timer_start(i64, double)
declare void @time_timer_pause(i64)
declare void @time_timer_stop(i64)
declare double @time_timer_read(i64)
declare void @_thread_smart_wait(i64)
declare i64 @file_open(i64, i8*, i32)
declare void @file_close(i64)
declare void @file_append_all(i64, i8*)
declare void @file_write_all(i64, i8*)
declare i8* @file_read_all(i64)
declare i8* @file_readln(i64, i64)
declare void @file_writeln(i64, i64, i8*)
declare i32 @file_exists(i8*)
declare i32 @file_delete(i8*)
declare i32 @file_rename_file(i8*, i8*)
declare i32 @file_copy_file(i8*, i8*)
declare %Box* @file_list_dir(i8*)
'''


class RubidiumTypeError(Exception): pass
class RubidiumNameError(Exception): pass

def _decimal_str_to_fp128_hex(raw):
    """OPEN-5: encode a decimal literal's EXACT value as LLVM's `0xL<32 hex
    digits>` IEEE-754 binary128 constant syntax. Needed because LLVM's plain
    decimal float-constant syntax is only ever parsed at double precision
    (regardless of the declared target type) — a literal with more
    significant digits than a double holds would silently lose precision
    the same way it already did going through Python's float() in the
    parser, unless encoded exactly like this instead. Uses Decimal (exact
    string parsing) + Fraction (exact rational arithmetic) throughout, so
    the only rounding that happens is the intentional final round-to-
    nearest-even down to binary128's 112 mantissa bits — the best
    precision this architecture's float type can represent.
    """
    def _pack(bits):
        # LLVM's 0xL takes the LOW 64 bits first, then the HIGH 64 bits —
        # the reverse of a naive big-endian reading of the 128-bit value
        # (confirmed empirically: round-tripping 1.0 through 0xL and back
        # only gave the right answer with the halves swapped this way).
        lo = bits & 0xFFFFFFFFFFFFFFFF
        hi = bits >> 64
        return "0xL" + format(lo, "016X") + format(hi, "016X")

    d = Decimal(raw)
    sign = 1 if d.is_signed() else 0
    if d == 0:
        return _pack(sign << 127)
    f = Fraction(abs(d))
    e = f.numerator.bit_length() - f.denominator.bit_length()
    while Fraction(2) ** e > f:
        e -= 1
    while Fraction(2) ** (e + 1) <= f:
        e += 1
    mantissa_frac = f / Fraction(2) ** e - 1  # in [0, 1)
    scaled = mantissa_frac * (1 << 112)
    mantissa = scaled.numerator // scaled.denominator
    remainder = scaled - mantissa
    if remainder * 2 > 1 or (remainder * 2 == 1 and mantissa % 2 == 1):
        mantissa += 1
    if mantissa >= (1 << 112):
        mantissa = 0
        e += 1
    biased_exp = e + 16383
    bits = (sign << 127) | (biased_exp << 112) | mantissa
    return _pack(bits)

class CodeGen:
    def __init__(self, import_aliases=None, shared_lib=False):
        self.shared_lib  = shared_lib  # -s flag: compile as a shared library, no main entry
        self.fn_lines    = []
        self.global_decls = []
        self.str_count   = 0
        self.tmp_count   = 0
        self.label_count = 0
        self.global_vars  = {}
        self.local_vars_stack = [[]]  # Stack of scopes, each scope is a dict of variable names to types
        self.mutable_vars = set()
        self.dropped_vars = set()
        self.class_defs   = {}
        self.class_ids    = {}  # bugs.log OPEN-9: class_name -> stable int id for runtime dispatch
        self.instances    = {}
        # bugs.log #12: see _gather_vardecl_types — pre-scan-only instance
        # tracking, kept separate from self.instances (see that comment).
        self._prescan_instances = {}
        self.cur_fn       = None
        self.functions    = {}
        self.ffi_functions = set()  # names of FFI-bound functions — their calls must use _ffi_type_to_ir (Any->i64), matching emit_ffi_bind's define, not rubi_type_to_ir (Any->%Box*)
        self.loop_end_stack = []
        self.loop_cond_stack = []  # continue target labels
        self.linked_to = {}  # bugs.log #5: name -> target name for scalar `link` aliasing
        self.indexed_links = {}  # bugs.log OPEN-1: name -> (collection_var, index_expr) for indexed `link` aliasing
        self.element_types = {}  # bugs.log #2: var_name -> element_type for collection type enforcement
        # OPEN-4 (scalar Null): names of scalar-int variables whose CURRENT value
        # is an explicit Null literal. A raw i32/i64 stores Null as INT32_MIN, which
        # is indistinguishable at runtime from a genuine value that computed/clamped
        # to the type's true minimum — so `print` can't decide "Null" vs the real
        # min from the bits alone. Per the language owner's rule ("a value at the
        # bottom limit is just the bottom limit, it does not become Null"), only a
        # value KNOWN at compile time to be an explicit Null prints as "Null";
        # everything else prints its real number. This set tracks that knowledge,
        # updated on every scalar VarDecl/Assign.
        self.null_valued = set()
        self.index_typed_vars = set()  # names declared `let x: index = ...` — see _check_index_values_are_scalar
        self.cur_class    = None
        self._alloca_emitted = set()  # local var names that already have an alloca in current fn
        self._math_block_type = None  # typed math block `(expr): TYPE` — forces every arithmetic op inside to compute at this IR type
        self._try_error_label = None  # set while inside a try body for div-by-zero guards
        self._fn_error_exit_label = None  # OPEN-7: per-function block that returns a default value to propagate an uncaught error to the caller
        self._pending_trampolines = []  # trampoline fn_lines to emit after current function
        self._file_slot_counter = 0     # unique slot number for each open() block
        self._file_handle_vars = {}     # var_name -> slot_int while inside an open() block
        # import alias map: alias_name -> module_prefix (e.g. "mt" -> "math_tools")
        self.import_aliases = import_aliases or {}
        self._all_global_names = set()   # BUG-21: filled at the top of gen()
        # Tracks the IR return type of the function currently being emitted;
        # used by the Return handler to avoid type mismatches when ret_type is None.
        self._cur_fn_ret_ir: str = "i64"
        # BUGFIX (bugs.log #1): the generated runtime C file (compiler.py) includes
        # <math.h>/<stdio.h>/<stdlib.h>/<string.h>/<time.h>/<pthread.h>, which already
        # declare these C-library symbol names. A Rubidium function or FFI alias that
        # shares one of these names (e.g. `fn sqrt(...)` or `fn lib sin(...) as sin`,
        # both used verbatim in the syntax file's own examples) previously caused
        # clang to fail with "invalid redefinition of function". We keep the
        # Rubidium-visible name (used as the self.functions dict key, so calls still
        # resolve normally) but emit the actual LLVM symbol under a safe alias when it
        # collides with one of these reserved names.
        self._RESERVED_C_SYMBOLS = frozenset({
            # math.h
            "sin","cos","tan","asin","acos","atan","atan2","sinh","cosh","tanh",
            "asinh","acosh","atanh","exp","exp2","expm1","log","log2","log10","log1p",
            "pow","sqrt","cbrt","hypot","fabs","floor","ceil","round","trunc","fmod",
            "remainder","ldexp","frexp","modf","copysign","nan","erf","erfc","gamma",
            "lgamma","tgamma","j0","j1","jn","y0","y1","yn",
            # stdio.h / stdlib.h / string.h
            "printf","fprintf","sprintf","snprintf","scanf","fopen","fclose","fread",
            "fwrite","fgets","fputs","fflush","malloc","calloc","realloc","free",
            "exit","abort","atoi","atol","atof","rand","srand","system","getenv",
            "strlen","strcmp","strncmp","strcpy","strncpy","strcat","strncat","strchr",
            "strstr","strdup","memcpy","memmove","memset","memcmp","abs","labs","qsort",
            # time.h / pthread.h / unistd.h / dlfcn.h
            "time","clock","difftime","mktime","localtime","gmtime","sleep","usleep",
            "pthread_create","pthread_join","pthread_exit","pthread_mutex_lock",
            "pthread_mutex_unlock","fork","exec","wait","waitpid","dlopen","dlsym",
            "dlclose","dlerror",
        })
        # Maps a Rubidium-visible function name -> the safe LLVM symbol actually
        # emitted for it, ONLY when the two differ (i.e. only for reserved-name
        # collisions). Populated at function/FFI-bind registration time.
        self._fn_symbol_override = {}

    def _safe_fn_symbol(self, name):
        """Return an LLVM-safe symbol for a Rubidium function name, avoiding
        collisions with C library symbols already declared via the runtime's
        #include headers (see bugs.log #1). No-op for non-colliding names."""
        if name in self._RESERVED_C_SYMBOLS and name != "main":
            return f"__rub_{name}"
        return name

    def is_class_field(self, name):
        if not self.cur_class: return False
        cls = self.class_defs.get(self.cur_class)
        if not cls: return False
        for f in cls.fields:
            if f.name == name: return True
        return False

    def _emit_input_line_helper(self):
        self.fn_lines += [
            "define i8* @_rubidium_input_line() {",
            "  %buf = call i8* @malloc(i64 1024)",
            "  %stdin = load i8*, i8** @_stdin_ptr", # Changed from @.stdin_ptr
            "  %res = call i8* @fgets(i8* %buf, i32 1024, i8* %stdin)",
            # BUG-19: fgets' result used to be ignored. At EOF (or a read
            # error) fgets leaves the buffer UNTOUCHED, so strlen below ran
            # over uninitialized malloc memory — it only looked like "" because
            # a fresh heap chunk happens to be zeroed. On a dirty heap this
            # returns stale garbage from whatever last used that chunk. Detect
            # the failure and return a real empty string instead.
            "  %read_ok = icmp ne i8* %res, null",
            "  br i1 %read_ok, label %have_line, label %at_eof",
            "at_eof:",
            "  store i8 0, i8* %buf",
            "  br label %done",
            "have_line:",
            "  %len = call i64 @strlen(i8* %buf)",
            "  %is_empty = icmp eq i64 %len, 0",
            "  br i1 %is_empty, label %done, label %check_nl",
            "check_nl:",
            "  %last_char_idx = sub i64 %len, 1",
            "  %last_char_ptr = getelementptr i8, i8* %buf, i64 %last_char_idx",
            "  %last_char = load i8, i8* %last_char_ptr",
            "  %is_nl = icmp eq i8 %last_char, 10",
            "  br i1 %is_nl, label %strip, label %done",
            "strip:",
            "  store i8 0, i8* %last_char_ptr",
            "  br label %done",
            "done:",
            "  ret i8* %buf",
            "}"
        ]

    def new_tmp(self):
        self.tmp_count += 1
        return f"%t{self.tmp_count}"

    def new_label(self, prefix="lbl"):
        self.label_count += 1
        return f"{prefix}{self.label_count}"

    def emit(self, line):
        self.fn_lines.append(line)

    # -------------------------------------------------------
    # BUG-3: scope-owned temporaries
    # -------------------------------------------------------
    def _block_is_terminated(self):
        """True when the last thing emitted ends the current basic block, so
        no further instruction may be appended to it (LLVM requires exactly
        one terminator, at the end). Guards the release calls below, which are
        emitted after a statement that may have been `break`/`continue`/
        `raise`/`return`."""
        for line in reversed(self.fn_lines):
            s = line.strip()
            if not s or s.startswith(";"):
                continue
            if s.endswith(":"):        # a fresh label — block is open and empty
                return False
            return (s.startswith("br ") or s.startswith("ret ")
                    or s.startswith("unreachable") or s.startswith("switch "))
        return False

    def _emit_temp_mark(self):
        """Remember the arena high-water mark on entry to a scope."""
        mark = self.new_tmp()
        self.emit(f"  {mark} = call i64 @rub_temp_mark()")
        return mark

    def _emit_temp_release(self, mark):
        """Free every temporary allocated since `mark`. Per spec, 'temporary
        scoped values are dropped automatically' and locals are released when
        their block ends — this is what actually performs that."""
        if mark is not None and not self._block_is_terminated():
            self.emit(f"  call void @rub_temp_release_to(i64 {mark})")

    def _track_temp(self, val, ir_t):
        """Register a freshly allocated value with the arena so it is freed
        when the enclosing block ends."""
        out = self.new_tmp()
        if ir_t == "%Box*":
            self.emit(f"  {out} = call %Box* @rub_temp_track(%Box* {val})")
            return out
        if ir_t == "i8*":
            self.emit(f"  {out} = call i8* @rub_temp_track_str(i8* {val})")
            return out
        return val

    def _null_safe_str(self, val):
        """BUGFIX (found reviewing user code, same root cause as the str-vs-
        Null comparison crash): a `str` value can genuinely be a real null
        i8* pointer at runtime (that IS what Null means for str — see
        coerce()'s "null" handling), but nearly every string method
        (_emit_string_method_impl below) hands the receiver AND its string-
        typed arguments straight to a raw libc call — strlen/strstr/atol/
        strtod/strcpy/strcat/strncpy — with no null check. Any of those on a
        NULL argument is undefined behavior; confirmed segfaulting on
        .len()/.has()/.to()/.set()/.insert() called on (or with) a Null
        string, in ordinary spec-legal code, not just a contrived case.
        Substitutes a pointer to an interned empty string whenever the
        value is null, via a `select` (no branching/control-flow needed) —
        matches the "Null treated as empty string" convention str_replace/
        str_slice already use internally, just applied uniformly instead of
        ad hoc per function."""
        empty_lbl, empty_len = self.intern_str("")
        empty_ptr = self.new_tmp()
        self.emit(f"  {empty_ptr} = getelementptr [{empty_len} x i8], [{empty_len} x i8]* {empty_lbl}, i64 0, i64 0")
        is_null = self.new_tmp()
        self.emit(f"  {is_null} = icmp eq i8* {val}, null")
        safe = self.new_tmp()
        self.emit(f"  {safe} = select i1 {is_null}, i8* {empty_ptr}, i8* {val}")
        return safe

    def _emit_str_index_bounds_check(self, safe_ptr, idx_i, allow_at_end=False):
        """BUGFIX (found investigating the Null-safety fixes above — a
        SEPARATE, non-Null-specific bug): .char()/.set()/.insert() compute
        a raw `getelementptr` into the string's buffer from a caller-
        supplied index with NO bounds check at all — `text.set(50, "Z")`
        on a 2-character string silently writes 50 bytes past a 3-byte
        malloc'd buffer. Confirmed real heap corruption: a moderate
        out-of-bounds offset landed inside the allocator's own bookkeeping
        without AddressSanitizer flagging it as a use-after/heap-overflow
        (the corrupted bytes just weren't read back in that particular
        repro), and a large offset segfaults outright — undefined behavior
        either way, not something to leave silently reachable. Raises the
        spec's own documented "Invalid index access" runtime error (see
        syntax's Runtime Error Examples) instead, catchable via try/error
        like any other. `allow_at_end`: insert()'s "before an index" can
        legally target the position just past the last character (spec:
        `text.insert(5, "!")` on 5-char "Hello" inserts at the very end);
        char()/set() read/overwrite an existing character, so they require
        a STRICTLY in-bounds index."""
        len_v = self.new_tmp()
        self.emit(f"  {len_v} = call i64 @strlen(i8* {safe_ptr})")
        too_low = self.new_tmp()
        self.emit(f"  {too_low} = icmp slt i64 {idx_i}, 0")
        too_high = self.new_tmp()
        pred = "sgt" if allow_at_end else "sge"
        self.emit(f"  {too_high} = icmp {pred} i64 {idx_i}, {len_v}")
        oob = self.new_tmp()
        self.emit(f"  {oob} = or i1 {too_low}, {too_high}")
        ok_l, bad_l = self.new_label("stridx_ok"), self.new_label("stridx_oob")
        self.emit(f"  br i1 {oob}, label %{bad_l}, label %{ok_l}")
        self.emit(f"{bad_l}:")
        err_lbl, err_len = self.intern_str("Invalid index access")
        err_ptr = self.new_tmp()
        self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
        self._emit_raise_or_propagate(err_ptr)
        self.emit(f"{ok_l}:")

    def _escape_temp(self, val, ir_t):
        """Move a value OUT of the temporary arena because it will outlive the
        block that produced it — it is being stored in a global / class field,
        or returned. Without this the arena would free memory the program
        still owns. A no-op for scalars and for values that were never
        tracked."""
        if val in ("null", "0", None):
            return val
        if ir_t == "%Box*":
            out = self.new_tmp()
            self.emit(f"  {out} = call %Box* @rub_temp_untrack(%Box* {val})")
            return out
        if ir_t == "i8*":
            out = self.new_tmp()
            self.emit(f"  {out} = call i8* @rub_temp_untrack_str(i8* {val})")
            return out
        return val

    # -------------------------------------------------------
    # Type system: rank-based promotion for mixed-width math
    # -------------------------------------------------------
    # Integer IR types are exact LLVM widths (iN).
    # Float types: f32→float, f64→double, f128+→fp128
    # (LLVM only has native float/double/fp128; f256+ map to fp128)
    # Null sentinel: INT32_MIN (-2147483648). Chosen because sign-extension/truncation
    # preserves this exact value across ALL int widths (i32..i2048), so a Null stored
    # in an i32 var compares equal to a Null stored in an i64 var. Per spec, Null
    # behaves as -infinity (smaller than any non-null value) — see syntax NULL BEHAVIOR.
    _NULL_SENTINEL = "-2147483648"
    _NULL_SENTINEL_FLOAT = "0xFFF0000000000000"  # IEEE -infinity, exact across float/double

    _INT_IR = {"i32": "i32", "i64": "i64", "i128": "i128",
               "i256": "i256", "i512": "i512", "i1024": "i1024", "i2048": "i2048"}
    _FLT_IR = {"f32": "float", "f64": "double",
               "f128": "fp128", "f256": "fp128", "f512": "fp128",
               "f1024": "fp128", "f2048": "fp128"}
    # Rank: higher = wider. Floats outrank all ints.
    _TYPE_RANK = {
        "i1": 0, "i32": 1, "i64": 2, "i128": 3,
        "i256": 4, "i512": 5, "i1024": 6, "i2048": 7,
        "float": 10, "double": 11, "fp128": 12,
    }
    # Which IR types are integers vs floats
    _INT_IR_SET  = {"i1", "i32", "i64", "i128", "i256", "i512", "i1024", "i2048"}
    _FLOAT_IR_SET = {"float", "double", "fp128"}

    # BUGFIX (bugs.log #4): bit widths for each signed integer IR type, used to
    # compute the min/max representable value so narrowing conversions can be
    # clamped (per syntax file's "Integer Overflow" section) instead of being
    # silently truncated. i1 (bool) is excluded — it has no overflow concept.
    _INT_BITS = {"i32": 32, "i64": 64, "i128": 128,
                 "i256": 256, "i512": 512, "i1024": 1024, "i2048": 2048}

    # syntax: FFI C TYPES — FFI.type(list) only supports the widths a real C
    # ABI actually uses; wider Rubidium int/float types (i128+/f128+) already
    # can't round-trip through the boxed-scalar 64-bit `i`/`f` fields (same
    # existing limitation noted for print/coerce_to_box).
    _FFI_BUFFER_KINDS = {"i32": 0, "i64": 1, "f32": 2, "f64": 3}

    def _int_bounds(self, ir_type):
        """Return (min, max) representable signed values for an integer IR type."""
        bits = self._INT_BITS[ir_type]
        max_v = (1 << (bits - 1)) - 1
        min_v = -(1 << (bits - 1))
        return min_v, max_v

    def rubi_type_to_ir(self, t):
        if t in ("list", "index", "dict", "dict+", "Any"): return "%Box*"
        if t == "bool":  return "i1"
        if t == "str":   return "i8*"
        if t == "str+":  return "i8*"  # bugs.log #3: str+ (big/multi-line string) uses the same representation as str
        if t in self._INT_IR:  return self._INT_IR[t]
        if t in self._FLT_IR:  return self._FLT_IR[t]
        # Fallback: already an IR type (e.g. "i64" from internal use)
        if t in self._TYPE_RANK: return t
        return "i64"

    def _is_ffi_type_call(self, t):
        """True if t LOOKS LIKE an attempted `FFI.type(...)` call — valid or
        not. Used to give a clear error for a malformed one (a string
        argument, e.g. FFI.type("list"), the old removed spelling; zero/
        extra args) instead of letting it silently fall through to whatever
        generic-type handling treats an unrecognized value as."""
        return isinstance(t, MethodCall) and t.method == "type" \
            and isinstance(t.obj, Var) and t.obj.name == "FFI"

    def _c_type_name(self, t):
        """If t is an `FFI.type(name)` marker parsed at an FFI-bind
        parameter-type position (the argument is a bare Rubidium type
        keyword, e.g. `list` — TYPE tokens parse as Var(name), same as any
        other "TYPE token used as an argument" spot), return its name
        string, else None (including when it LOOKS like an attempted
        FFI.type(...) call but is malformed — see _is_ffi_type_call)."""
        if self._is_ffi_type_call(t) and t.args and isinstance(t.args[0], Var):
            return t.args[0].name
        return None

    def _ffi_type_to_ir(self, t):
        """Like rubi_type_to_ir, but for FFI signatures. 'Any' here means an opaque
        C pointer or int-sized value (e.g. GLFWwindow*, int) — mapping it to
        Rubidium's %Box* (an internal boxed-value struct pointer) would be an ABI
        mismatch with the foreign function, which knows nothing about Boxes."""
        c_type_name = self._c_type_name(t)
        if c_type_name == "list": return "i8*"
        # syntax: FFI C TYPES — FFI.type(X) for any other X (a scalar type
        # with an already-well-defined raw ABI shape, e.g. FFI.type(i32)) is
        # just an explicit spelling of that same type — same IR either way.
        if c_type_name is not None: return self._ffi_type_to_ir(c_type_name)
        if t == "Any": return "i64"
        if t == "void": return "void"
        return self.rubi_type_to_ir(t)

    # syntax: FFI C TYPES — a Rubidium type with no defined raw-ABI layout
    # (Rubidium's own boxed list/index/dict/dict+ collections, or a bare
    # user-defined class/struct instance) can't cross an FFI call as-is —
    # no foreign C-ABI-based language has a matching shape for an internal
    # %Box*/struct. Without this check the type silently fell back to a
    # plain i64 (see rubi_type_to_ir's fallback), passing a garbage
    # pointer-sized integer instead of erroring. FFI.type(...) (recognized
    # via _c_type_name above) is the documented escape hatch for the one
    # shape that IS convertible today (list).
    _FFI_ABI_ILLEGAL_BARE_TYPES = {"list", "index", "dict", "dict+"}

    # syntax: FFI C TYPES — index/dict/dict+ have no defined raw-ABI layout
    # for FFI.type(...) to convert them into. Unlike `list` (homogeneous,
    # ordered, a forced element type + count maps directly onto a C array),
    # index has arbitrary-typed keys, dict's values are themselves variable-
    # length lists, and dict+ nests to unbounded depth — none of that fits
    # the flat "pointer + count" shape list gets. Not implemented; explicit
    # rejection instead of the silent %Box*/garbage-i64 fallback these used
    # to get by slipping past this check inside an FFI.type(...) wrapper.
    _FFI_TYPE_UNSUPPORTED_COLLECTIONS = {"index", "dict", "dict+"}

    def _check_ffi_param_types(self, fn_display_name, params):
        for pname, ptype in params:
            c_type_name = self._c_type_name(ptype)
            if c_type_name is not None:
                # FFI.type(...) only actually CONVERTS `list` (see
                # _ffi_type_to_ir) — everything else it accepts is a
                # passthrough of an already-ABI-safe scalar. A class
                # instance wrapped in it is still illegal: wrapping doesn't
                # give it a raw-ABI layout it doesn't otherwise have.
                if c_type_name in self.class_defs:
                    raise RubidiumTypeError(
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type({c_type_name})', but '{c_type_name}' is "
                        f"a class instance with no defined raw-ABI layout — "
                        f"FFI.type(...) converts a list, not a class instance."
                    )
                if c_type_name in self._FFI_TYPE_UNSUPPORTED_COLLECTIONS:
                    raise RubidiumTypeError(
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type({c_type_name})' — {c_type_name} isn't "
                        f"supported by FFI.type(...); only list is convertible today."
                    )
                # syntax: void means "no value at all" — meaningful only as
                # an FFI binding's RETURN type (a real C function with no
                # return value slot). A parameter always receives a real
                # argument on every call, so "a parameter of no value" has
                # no meaning here either, same as it has none in C itself.
                if c_type_name == "void":
                    raise RubidiumTypeError(
                        f"FFI binding '{fn_display_name}': parameter '{pname}' "
                        f"has type 'FFI.type(void)' — void isn't a valid parameter "
                        f"type (only a return type). Remove this parameter if the "
                        f"foreign function doesn't take it, or declare it 'Any' and "
                        f"pass Null if it expects a real argument that just carries "
                        f"no value."
                    )
                continue
            if self._is_ffi_type_call(ptype):
                raise RubidiumTypeError(
                    f"FFI binding '{fn_display_name}': parameter '{pname}' has a "
                    f"malformed FFI.type(...) — it takes exactly one bare type "
                    f"keyword argument (e.g. FFI.type(list), FFI.type(i32)), not "
                    f"a string or a missing/extra argument."
                )
            if ptype in self._FFI_ABI_ILLEGAL_BARE_TYPES or ptype in self.class_defs:
                raise RubidiumTypeError(
                    f"FFI binding '{fn_display_name}': parameter '{pname}' "
                    f"has type '{ptype}', which can't cross an FFI call directly "
                    f"— there's no defined raw-ABI layout for a Rubidium "
                    f"{'collection' if ptype in self._FFI_ABI_ILLEGAL_BARE_TYPES else 'class instance'}. "
                    f"Use a primitive/pointer type, or FFI.type(list) for a list."
                )

    def promote_type(self, a, b):
        """Return the higher-ranked IR type for mixed-width arithmetic."""
        ra = self._TYPE_RANK.get(a, 2)   # default to i64 rank
        rb = self._TYPE_RANK.get(b, 2)
        # If one is float, result is always float
        if a in self._FLOAT_IR_SET or b in self._FLOAT_IR_SET:
            if a in self._FLOAT_IR_SET and b in self._FLOAT_IR_SET:
                return a if ra >= rb else b
            return a if a in self._FLOAT_IR_SET else b
        # Both ints
        return a if ra >= rb else b

    def intern_str(self, text):
        raw = text.replace("\\n", "\n").replace("\\t", "\t")
        byte_len = len(raw.encode("utf-8")) + 1
        escaped = ""
        for ch in raw:
            for b in ch.encode("utf-8"):
                escaped += f"\\{b:02X}"
        escaped += "\\00"
        lbl = f"@.str{self.str_count}"
        self.str_count += 1
        self.global_decls.append(f'{lbl} = private unnamed_addr constant [{byte_len} x i8] c"{escaped}"')
        return lbl, byte_len

    def _unlink_var(self, name):
        """BUGFIX (bugs.log #5): give a variable that was `link`-aliased its own
        independent storage, the moment it's reassigned directly (per spec:
        "if b changes without a then they become unlinked ... then become
        their own variable in memory"). Returns (ptr_str, ir_t) for the now-
        independent storage, ready for a normal store."""
        del self.linked_to[name]
        ir_t = None
        for scope in reversed(self.local_vars_stack):
            if name in scope:
                ir_t = scope[name]; break
        if ir_t is not None:
            ptr_str = f"%ptr_{name}"
            if name not in self._alloca_emitted:
                self.emit(f"  {ptr_str} = alloca {ir_t}")
                self._alloca_emitted.add(name)
                # bugs.log OPEN-J: now that it has real storage of its own
                # (no longer aliasing the link target's flag), give it a
                # companion is-null cell like any other freshly-declared
                # scalar — get_null_flag_ptr() would otherwise report this
                # name as tagged (linked_to no longer excludes it, just
                # removed above) without a flag ever having been allocated.
                self._emit_null_flag_decl(name, ir_t, is_local=True)
            return ptr_str, ir_t
        # Global scope — emit the `@name = global ...` decl directly (can't
        # use declare_global(), which no-ops because we already registered
        # the name's type in self.global_vars when the link was created).
        ir_t = self.global_vars[name]
        if not any(d.startswith(f"@{name} =") for d in self.global_decls):
            if ir_t.endswith("*"):
                self.global_decls.append(f"@{name} = global {ir_t} null")
            elif ir_t == "fp128":
                self.global_decls.append(f"@{name} = global {ir_t} 0xL00000000000000000000000000000000")
            elif ir_t in ("float", "double"):
                self.global_decls.append(f"@{name} = global {ir_t} 0.0")
            else:
                self.global_decls.append(f"@{name} = global {ir_t} 0")
            self._emit_null_flag_decl(name, ir_t, is_local=False)  # bugs.log OPEN-J, see note above
        return f"@{name}", ir_t

    def declare_global(self, name, ir_type):
        if name in self.global_vars: return
        # Mangle names that conflict with C library functions
        c_lib_funcs = {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        if name in c_lib_funcs:
            name = f"_var_{name}"
        if name in self.global_vars: return
        self.global_vars[name] = ir_type
        # Prefix with _var_ to avoid conflicts with C library functions
        ir_name = f"_var_{name}" if name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else name
        if ir_type.endswith("*"):
            self.global_decls.append(f"@{ir_name} = global {ir_type} null")
        elif ir_type == "fp128":
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0xL00000000000000000000000000000000")
        elif ir_type in ("float", "double"):
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0.0")
        else:
            self.global_decls.append(f"@{ir_name} = global {ir_type} 0")

    def get_var_ptr(self, name):
        # BUGFIX (bugs.log #5): scalar variable-level `link` (e.g. `let b = link a`)
        # previously fell through LinkArg's no-op passthrough in emit_expr, which
        # only gives real shared-reference behavior for %Box* collections (already
        # pointers) — for scalars it just copied the current value once, so `b`
        # never reflected later changes to `a`. VarDecl now registers linked scalar
        # names in self.linked_to instead of giving them their own storage; any
        # lookup of a linked name transparently resolves to its target's real
        # pointer here, so reads always see the target's live value. Assign
        # handles "unlinking" (see emit_stmt's Assign branch) by giving the name
        # its own storage the moment it's reassigned directly.
        if name in self.linked_to:
            return self.get_var_ptr(self.linked_to[name])
        # Indexed links don't have real storage - they're handled in emit_expr Var branch
        if name in self.indexed_links:
            raise RuntimeError(f"get_var_ptr called on indexed link '{name}' - should be handled in emit_expr")
        if "." in name:
            mangled = name.replace(".", "_")
            if mangled in self.global_vars:
                return f"@{mangled}", self.global_vars[mangled]
        
        # Look for variable in the innermost scope first
        for scope in reversed(self.local_vars_stack):
            if name in scope:
                return f"%ptr_{name}", scope[name]
        # FEATURE: class methods cannot see global variables — they only see
        # their own instance fields (handled earlier, before get_var_ptr is
        # even called — see is_class_field()/emit_field_access in the Var
        # branch of emit_expr) and their own locals/parameters (the scope
        # check just above). Globals are treated as if they don't exist,
        # unless explicitly passed in as a parameter.
        #
        # BUGFIX (bugs.log #14): "error" is the exception — it's the
        # implicit binding inside a `try { } error { }` block's error body,
        # scoped (semantically) to that block, not a real user-level global.
        # It's implemented as a global purely as an internal mechanism (see
        # emit_try — @error is just where the error message gets stashed),
        # so it must be exempt from this restriction or try/error simply
        # couldn't be used inside any class method at all.
        if self.cur_class and name != "error":
            raise RubidiumNameError(
                f"Class '{self.cur_class}' cannot access global variable '{name}': "
                f"globals are not accessible from inside a class (pass it in as "
                f"a parameter instead)"
            )
        # Check for mangled name if original conflicts with C lib func
        c_lib_funcs = {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        mangled = f"_var_{name}" if name in c_lib_funcs else name
        if mangled in self.global_vars: return f"@{mangled}", self.global_vars[mangled]
        if name in self.global_vars: return f"@{name}", self.global_vars[name]
        raise RubidiumNameError(f"Undefined variable '{name}'")

    def class_ir_type(self, class_name): return f"%class_{class_name}"

    def _class_instantiate_candidate(self, value_node):
        """bugs.log OPEN-O: the class name a VarDecl's RHS MIGHT be
        instantiating, for any of the three syntactic shapes that can mean
        "construct an instance" — a same-file bare call (`player()`, parsed
        as a plain FnCall), an already-recognized ClassInstantiate node, or
        an IMPORTED, namespaced call (`shapes.circle()`, parsed as
        MethodCall(Var("shapes"), "circle", args)). For the namespaced
        case, resolves "shapes" through import_aliases to the real module
        name and combines it with the module prefix compiler.py's
        multi-file merge already gives every class — the exact same
        convention functions/variables already use for cross-file
        namespaced access, just never wired up for classes: the merge pass
        renames `circle` to `shapes_circle` internally, but nothing
        previously rewrote the CALL SITE to match, so an imported class
        was unreachable by any name. Returns None if value_node isn't any
        of these shapes; the caller still needs to check the result
        against self.class_defs, same as before — this only resolves the
        NAME to check."""
        if isinstance(value_node, ClassInstantiate):
            return value_node.class_name
        if isinstance(value_node, FnCall) and isinstance(value_node.name, str):
            return value_node.name
        if isinstance(value_node, MethodCall) and isinstance(value_node.obj, Var):
            real_mod = self.import_aliases.get(value_node.obj.name, value_node.obj.name)
            return f"{real_mod}_{value_node.method}"
        return None

    def emit_class_type(self, cls):
        field_types = []
        for f in cls.fields:
            ir_t = self.rubi_type_to_ir(f.vtype) if f.vtype else self._infer_type(f.value)
            field_types.append(ir_t)
        field_types_str = ", ".join(field_types)
        if not field_types_str: field_types_str = "i8"
        self.global_decls.append(f"%class_{cls.name} = type {{ {field_types_str} }}")

    def field_index(self, class_name, field_name):
        cls = self.class_defs[class_name]
        for i, f in enumerate(cls.fields):
            if f.name == field_name:
                ir_t = self.rubi_type_to_ir(f.vtype) if f.vtype else self._infer_type(f.value)
                return i, ir_t
        raise RubidiumNameError(f"Class '{class_name}' has no field '{field_name}'")

    def method_ir_name(self, class_name, method_name):
        return f"{class_name}__{method_name}"

    def _register_implicit_class_fields(self):
        """Per spec (Scope: 'Classes create their own scope'): any `let` declared
        inside a class method WITHOUT `local` becomes an implicit instance field —
        persistent across method calls on that instance, accessible externally as
        `instance.name`. Only `let local x = ...` stays a true per-call stack local.
        This walks every method body (recursively through if/while/for/try blocks)
        collecting non-local VarDecl names not already declared as explicit fields,
        and appends them to cls.fields so the existing field-access machinery
        (is_class_field, field_index, emit_field_access) picks them up automatically."""
        def walk(body, cls, seen_names):
            for stmt in body:
                if isinstance(stmt, VarDecl) and not getattr(stmt, "is_local", False):
                    if stmt.name not in seen_names and not any(f.name == stmt.name for f in cls.fields):
                        stmt._is_implicit_field = True  # zero-init at construction, real value set when method runs
                        cls.fields.append(stmt)
                        seen_names.add(stmt.name)
                elif isinstance(stmt, If):
                    walk(stmt.then_body, cls, seen_names)
                    if getattr(stmt, "else_body", None): walk(stmt.else_body, cls, seen_names)
                elif isinstance(stmt, While):
                    walk(stmt.body, cls, seen_names)
                elif isinstance(stmt, For):
                    walk(stmt.body, cls, seen_names)
                elif isinstance(stmt, Try):
                    walk(stmt.try_body, cls, seen_names)
                    walk(stmt.error_body, cls, seen_names)
                elif isinstance(stmt, FileOpen):
                    walk(stmt.body, cls, seen_names)

        for cls in self.class_defs.values():
            seen = {f.name for f in cls.fields}
            for m in cls.methods:
                walk(m.body, cls, seen)

    def gen(self, stmts):
        # BUG-21: the set of every top-level variable name in the merged
        # program, collected BEFORE anything else runs. Type inference happens
        # in a prescan, at which point self.global_vars is still empty — so
        # _ns_global_name could not tell that `helper.shared_list` names an
        # imported module's VARIABLE rather than a method, and indexed access
        # into a module collection failed to compile.
        self._all_global_names = {
            s2.name for s2 in stmts
            if isinstance(s2, VarDecl) and not getattr(s2, 'is_local', False)
        }

        # BUGFIX (bugs.log #2): top-level functions and classes were being
        # registered by blindly overwriting self.functions[name] /
        # self.class_defs[name], so a duplicate `fn foo()` or `class Foo()`
        # was never caught by Rubidium itself — it silently kept only the
        # last definition and then (for functions) failed at the LLVM/clang
        # stage with a raw "invalid redefinition of function" IR error
        # instead of a proper Rubidium-level compile error. Duplicate
        # symbol definitions must be a compile-time RubidiumNameError
        # (per syntax file: "Function names must be unique within a scope",
        # "Class names must be unique within a scope").
        for s in stmts:
            if isinstance(s, ClassDef):
                if s.name in self.class_defs:
                    raise RubidiumNameError(f"Duplicate class definition: '{s.name}'")
                self.class_defs[s.name] = s
                # bugs.log OPEN-9: stable numeric id for this class, used to
                # tag boxed instances (box_class) so a value retrieved from
                # a generic collection can still be dispatched to the right
                # class's method at runtime.
                self.class_ids[s.name] = len(self.class_ids)
                for m in s.methods:
                    mangled_name = self.method_ir_name(s.name, m.name)
                    mfn = FnDef(mangled_name, [("__self", s.name)] + m.params, m.ret_type, m.body,
                                defaults=getattr(m, "defaults", {}))
                    mfn.class_name = s.name
                    self.functions[mangled_name] = mfn
            elif isinstance(s, FnDef):
                # BUGFIX (bugs.log #1): keep the dict key as the original,
                # Rubidium-visible name (so calls elsewhere that look it up by
                # source name keep working), but if it collides with a reserved
                # C symbol, emit the actual LLVM define/call under a safe mangled
                # name (stored on the FnDef object's .name, used by emit_fn and
                # by call sites via fn_obj.name rather than the dict key).
                original_name = s.name
                if original_name in self.functions:
                    raise RubidiumNameError(f"Duplicate function definition: '{original_name}'")
                safe_name = self._safe_fn_symbol(original_name)
                if safe_name != original_name:
                    self._fn_symbol_override[original_name] = safe_name
                    s.name = safe_name
                self.functions[original_name] = s

        self._register_implicit_class_fields()
        # Initialize local_vars_stack for _collect_global which uses it for global vars
        self.local_vars_stack = [{}]
        self.collect_globals(stmts)
        for cls in self.class_defs.values(): self.emit_class_type(cls)

        self.global_decls += extern_decls.split("\n")
        self.global_decls += [
            "", "declare i32 @printf(i8* noundef, ...)", "declare i32 @puts(i8* noundef)", "declare i32 @fflush(i8*)",
            "declare void @print_int_or_null(i64)",
            "declare void @print_i128_or_null(i128)",
            "declare i8* @i128_to_str(i128)",
            "declare void @print_bignum_or_null(i64*, i32)",
            "declare i8* @bignum_to_str(i64*, i32)",
            "declare void @print_int_plain(i64)",       # OPEN-4: no Null-sentinel check
            "declare void @rub_clear_screen()",         # syntax: PRINT & INPUT — clear()
            "declare void @print_i128_plain(i128)",
            "declare void @print_bignum_plain(i64*, i32)",
            "declare void @print_fp128_exact(i64, i64)",  # float precision fix: raw fp128 bits (lo, hi)
            "declare i8* @fp128_to_exact_decimal_str(i64, i64)",
            "declare i8* @malloc(i64)", "declare void @free(i8*)", "declare i64 @strlen(i8*)",
            "declare i32 @scanf(i8*, ...)", "declare i8* @fgets(i8*, i32, i8*)",
            "declare i8* @strcpy(i8*, i8*)", "declare i32 @strcmp(i8*, i8*)",
            "declare i32 @rub_strcmp_null_safe(i8*, i8*)",
            "declare i8* @strcat(i8*, i8*)", "declare i8* @strstr(i8*, i8*)",
            "declare i8* @rub_strdup_safe(i8*)",  # BUG: str variable-to-variable assignment aliasing
            "declare i8* @strncpy(i8*, i8*, i64)",
            "declare i8* @str_replace(i8*, i8*, i8*)",
            "declare %Box* @str_slice(i8*)",
            "declare i64 @strtol(i8*, i8**, i32)", "declare i64 @atol(i8*)",
            "declare i8* @strndup(i8*, i64)", "declare i32 @fclose(i8*)",
            "declare i8* @list_combine(%Box*)",
            "declare i8* @list_to_flat_buffer(%Box*, i32)",   # syntax: FFI C TYPES — FFI.type(list)
            "declare %Box* @box_deep_copy(%Box*)",
            "declare i8* @fopen(i8*, i8*)", "declare i64 @fread(i8*, i64, i64, i8*)",
            "declare i64 @fwrite(i8*, i64, i64, i8*)", "declare i64 @fseek(i8*, i64, i32)",
            "declare i64 @ftell(i8*)", "declare void @rewind(i8*)",
            "declare i32 @sprintf(i8*, i8*, ...)",
            "@_stdin_ptr = external global i8*", # Changed from @.stdin_ptr
            "@_thread_handles = global [1024 x i64] zeroinitializer",
            "@_thread_results = external global [1024 x %Box*]", # <--- ADD THIS LINE
            "declare void @os_start(i64)",
            "declare i8* @os_run(i64, i8*, i8*, i64)",  # OPEN-13: 4th arg = absolute timeout in ms (<=0 => default)
            "declare void @os_terminal_drop(i64)",
            "declare i64 @ffi_load(i8*)",
            "declare i64 @ffi_sym(i64, i8*)",
            ""
        ]

        self._emit_input_line_helper()
        
        # Inject RNG seed
        self.fn_lines += [
            "define void @_rubidium_init_rng() {",
            "  %t_seed = call i64 @time(i64* null)",
            "  %t_seed_i32 = trunc i64 %t_seed to i32",
            "  call void @srandom(i32 %t_seed_i32)",
            "  ret void", "}", ""
        ]
        
        top_init = [s for s in stmts if not isinstance(s, (FnDef, ClassDef, Import, Use, FFIBind))]
        self.cur_fn = None
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.emit_fn(FnDef("_rubidium_init", [], None, top_init))

        for s in stmts:
            if isinstance(s, FnDef): self.emit_fn(s)
            elif isinstance(s, FFIBind): self.emit_ffi_bind(s)

        for cls in self.class_defs.values():
            for m in cls.methods:
                self._emit_class_method(self.functions[self.method_ir_name(cls.name, m.name)], cls.name)

        if not self.shared_lib and "main" not in self.functions:
            self.emit_fn(FnDef("main", [], "i32", []))

        if self.shared_lib:
            self._emit_global_ctor_init()
        else:
            self._inject_init_call()

        out = ["; Rubidium compiled output", 'source_filename = "rubidium"', ""]
        out += self.global_decls + [""] + self.fn_lines
        return "\n".join(out)

    def _emit_global_ctor_init(self):
        """-s (shared lib) mode: no main() is guaranteed to exist/run, so
        global variable init (_rubidium_init/_rubidium_init_rng) can't be
        patched into main's entry block like the executable path does.
        Instead, register a constructor via @llvm.global_ctors, which the
        C runtime loader (glibc/ld.so) guarantees runs at load time —
        i.e. as soon as the .so is dlopen'd — before any exported fn can
        be called from the host language."""
        self.fn_lines += [
            "define void @_rubidium_ctor() {",
            "  call void @_rubidium_init_rng()",
            "  call i64 @_rubidium_init()",
            # OPEN-7: same uncaught-top-level-init-error check as the
            # executable entry path (_inject_init_call) — there is no main()
            # here to eventually catch/report it otherwise.
            "  %_rub_ctor_err = load i1, i1* @_rub_error_flag",
            "  br i1 %_rub_ctor_err, label %_rub_ctor_err_l, label %_rub_ctor_ok_l",
            "_rub_ctor_err_l:",
            "  %_rub_ctor_err_msg = load i8*, i8** @_rub_error_msg",
            "  call void @rub_throw(i8* %_rub_ctor_err_msg)",
            "  unreachable",
            "_rub_ctor_ok_l:",
            "  ret void",
            "}", ""
        ]
        self.global_decls.append(
            "@llvm.global_ctors = appending global [1 x { i32, void ()*, i8* }] "
            "[{ i32, void ()*, i8* } { i32 65535, void ()* @_rubidium_ctor, i8* null }]"
        )

    def _inject_init_call(self):
        # BUG (reported via an agent's investigation, and independently
        # confirmed to have nothing to do with either bare-bool operands or
        # loops — the minimal repro is `fn main() { print(True and False) }`,
        # no variables, no loop, first statement in the file): this patch
        # retroactively inserts a conditional-branch block right after
        # main()'s `entry:` label, splicing it between `entry` and whatever
        # was ALREADY generated as the rest of the body. Any `phi` already
        # emitted in that body — `and`/`or`'s short-circuit codegen
        # (_emit_short_circuit) is the only thing that emits one this early
        # — was built by _cur_block_label(), which correctly found "entry"
        # as the current block AT THE TIME it ran (before this patch
        # existed). Once this patch runs, `entry` no longer falls straight
        # through into the body — it now branches to `_rub_init_ok_l` first
        # — so a phi still claiming `%entry` as a predecessor names a block
        # that doesn't actually branch directly to it anymore: invalid IR
        # (clang's parser crashed outright on it rather than a clean
        # diagnostic). Confirmed happening for ANY and/or — literal, bare
        # variable, or comparison operands all equally trigger it — the
        # actual condition is just "and/or is the first branch-introducing
        # construct anywhere in main() before this patch runs to fix it up."
        # Fix: rewrite any phi predecessor that says `%entry` to the block
        # that is now the REAL direct predecessor, `_rub_init_ok_l`, once
        # the injection point has been passed. Scoped to inside main() only
        # (via in_main/injected) — a phi's own literal `%entry` in any OTHER
        # function refers to THAT function's own untouched entry block and
        # must not be touched.
        #
        # BUGFIX (found immediately after the fix above, via my own
        # regression testing — and/or as the first statement of a plain
        # function or class method defined anywhere in the SAME file):
        # self.fn_lines holds every function's IR concatenated together in
        # one list, not just main()'s — in_main/injected are plain booleans
        # that, once set True while scanning through main(), never got
        # reset back to False on exiting it. Any OTHER function compiled
        # after main() (in emission order) that ALSO happens to have and/or
        # as its first branching statement has its own, entirely legitimate
        # `%entry` phi reference (that function's own untouched entry block
        # — _inject_init_call only ever patches main()) — but the stale
        # still-True flags made the substitution above fire on it anyway,
        # rewriting a correct reference into `%_rub_init_ok_l`, a label
        # that doesn't exist in that function at all ("use of undefined
        # value '%_rub_init_ok_l'"). Reset in_main the moment a bare `}`
        # closes whatever `define` is currently open — LLVM IR never nests
        # braces within a function body, so the first standalone `}` after
        # `define i32 @main() {` reliably marks leaving it.
        patched = []; in_main = False; injected = False
        for line in self.fn_lines:
            if in_main and injected and " phi " in line and "%entry" in line:
                line = re.sub(r"%entry(?=\s*\])", "%_rub_init_ok_l", line)
            patched.append(line)
            if line.strip() == "define i32 @main() {": in_main = True
            elif in_main and line.strip() == "}":
                in_main = False; injected = False
            elif in_main and not injected and line.strip() == "entry:":
                patched.append("  call void @_rubidium_init_rng()")
                patched.append("  call i64 @_rubidium_init()")
                # OPEN-7: top-level init code (_rubidium_init) runs before any of
                # main's own body, so nothing else will ever check whether it
                # propagated an uncaught raise/division-by-zero — check right
                # here and report+exit immediately if so, same as an uncaught
                # error anywhere else.
                patched.append("  %_rub_init_err = load i1, i1* @_rub_error_flag")
                patched.append("  br i1 %_rub_init_err, label %_rub_init_err_l, label %_rub_init_ok_l")
                patched.append("_rub_init_err_l:")
                patched.append("  %_rub_init_err_msg = load i8*, i8** @_rub_error_msg")
                patched.append("  call void @rub_throw(i8* %_rub_init_err_msg)")
                patched.append("  unreachable")
                patched.append("_rub_init_ok_l:")
                injected = True
        self.fn_lines = patched

    def _is_known_null(self, node):
        """OPEN-4 (scalar Null): True iff `node` is known AT COMPILE TIME to be an
        explicit Null — either the `Null` literal itself, or a scalar variable
        whose current value was tracked as Null (see self.null_valued). Used so
        `print` shows "Null" only for a genuine Null, and shows the real number
        for any value that merely computed/clamped to the type's minimum."""
        if isinstance(node, None_):
            return True
        if isinstance(node, Var) and node.name in self.null_valued:
            return True
        return False

    def _track_null_valued(self, name, value_node):
        """OPEN-4 (scalar Null): keep self.null_valued in sync on scalar
        VarDecl/Assign — a name becomes Null-valued only when assigned an explicit
        Null (directly, or copied from another currently-Null scalar), and loses
        that status on any other assignment. Conservative by design: anything the
        compiler can't prove is Null (a function return, a param, a conditionally
        assigned value) is treated as a normal number, matching the rule that a
        bottom-limit value is just the bottom limit, not Null."""
        if self._is_known_null(value_node):
            self.null_valued.add(name)
        else:
            self.null_valued.discard(name)

    # ---------------------------------------------------------------
    # bugs.log OPEN-J: real RUNTIME Null tagging for local/global scalars
    # ---------------------------------------------------------------
    # self.null_valued (above) is compile-time-only and provably-null-only —
    # it can't tell a genuinely-Null value apart from one that merely
    # overflow-clamped to the same sentinel bit pattern in every case (that
    # collision is OPEN-J itself), and it loses track of a var's Null-ness
    # across any branch (confirmed by testing: a var set to Null on only one
    # side of an if/else is never in null_valued afterward, regardless of
    # which side actually ran). Phase 1 gives every plain `let`-declared
    # local/global SCALAR variable a real companion `i1` cell alongside its
    # value storage, updated on every write and consulted by comparisons/
    # print/`as str` instead of the sentinel bits. Deliberately NOT extended
    # to function parameters, return values, class fields, or `link`ed/
    # indexed names — propagating the tag across a call boundary means
    # widening every user function's LLVM signature, a separate, much
    # larger change (see bugs.log OPEN-J Phase 2). Untagged names simply
    # return None here and every call site below falls back to exactly
    # today's behavior for them — this is strictly additive, never a
    # regression.
    _NULL_TAG_C_LIB_FUNCS = {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}

    def _is_null_taggable_ir(self, ir_t):
        return ir_t in self._INT_IR_SET or ir_t in self._FLOAT_IR_SET

    def get_null_flag_ptr(self, name):
        """LLVM pointer string for `name`'s companion is-null i1 cell, or
        None if `name` isn't a currently-in-scope scalar local/global that
        actually has one declared (untagged: params, class fields, loop
        vars, linked/indexed names — see the note above). Checking ONLY
        scope membership + a taggable IR type is not enough: loop variables
        and function parameters are ALSO registered straight into
        local_vars_stack with a scalar type, by code paths that never call
        _emit_null_flag_decl — so this must check the flag was actually
        emitted (via the same dedup bookkeeping _emit_null_flag_decl uses:
        self._alloca_emitted for locals, self.global_decls for globals),
        not just infer eligibility from type."""
        if not isinstance(name, str) or "." in name:
            return None
        if name in self.linked_to or name in self.indexed_links:
            return None
        for scope in reversed(self.local_vars_stack):
            if name in scope:
                flag_key = f"{name}__isnull"
                return f"%ptr_{name}__isnull" if flag_key in self._alloca_emitted else None
        if self.cur_class and name != "error":
            return None
        mangled = f"_var_{name}" if name in self._NULL_TAG_C_LIB_FUNCS else name
        gname = f"@{mangled}__isnull"
        return gname if any(d.startswith(f"{gname} =") for d in self.global_decls) else None

    def _emit_null_flag_decl(self, name, ir_t, is_local):
        """Emit the companion `i1` alloca/global for a scalar VarDecl,
        exactly once per its normal storage's own lifetime (mirrors the
        alloca/declare_global dedup already used for the main value)."""
        if not self._is_null_taggable_ir(ir_t):
            return
        flag_key = f"{name}__isnull"
        if is_local:
            if flag_key not in self._alloca_emitted:
                self.emit(f"  %ptr_{name}__isnull = alloca i1")
                self._alloca_emitted.add(flag_key)
        else:
            mangled = f"_var_{name}" if name in self._NULL_TAG_C_LIB_FUNCS else name
            gname = f"@{mangled}__isnull"
            if not any(d.startswith(f"{gname} =") for d in self.global_decls):
                self.global_decls.append(f"{gname} = global i1 0")

    def _emit_null_flag_store(self, name, value_node):
        """Store this write's Null-ness into `name`'s companion flag. No-op
        if `name` isn't tagged (untagged categories, or a non-scalar type)."""
        ptr = self.get_null_flag_ptr(name)
        if ptr is None:
            return
        if isinstance(value_node, None_):
            flag_val = "1"
        elif isinstance(value_node, Var):
            src_ptr = self.get_null_flag_ptr(value_node.name)
            if src_ptr is not None:
                flag_val = self.new_tmp()
                self.emit(f"  {flag_val} = load i1, i1* {src_ptr}")
            else:
                flag_val = "0"
        else:
            flag_val = "0"
        self.emit(f"  store i1 {flag_val}, i1* {ptr}")

    def _emit_null_flag_load(self, name):
        """Load a tagged var's current is-null flag as a fresh i1 SSA temp.
        Caller must already know get_null_flag_ptr(name) is not None."""
        ptr = self.get_null_flag_ptr(name)
        tmp = self.new_tmp()
        self.emit(f"  {tmp} = load i1, i1* {ptr}")
        return tmp

    def _check_index_values_are_scalar(self, dict_expr_node, var_name):
        """`index` is a key -> SINGLE SCALAR VALUE map (per the language
        owner) — never a list/index/dict/dict+. Rejects any pair whose value
        is written DIRECTLY as a collection literal (`"A": [...]`,
        `"A": {...}`). Scoped to literal values only — a variable that holds
        a collection isn't (yet) traced back to its declared type here, so
        `"A": some_list_var` isn't caught by this check."""
        for k, v in dict_expr_node.pairs:
            if isinstance(v, (ListExpr, DictExpr)):
                key_desc = f"'{k.value}'" if isinstance(k, Str) else "a key"
                kind = "list" if isinstance(v, ListExpr) else ("index" if getattr(v, "is_index", False) else "dict")
                raise RubidiumTypeError(
                    f"index '{var_name}': value for {key_desc} is a {kind}, not a scalar — "
                    f"`index` holds exactly one scalar value per key. Use `dict` instead if a "
                    f"key needs to hold a collection of values."
                )

    def _check_index_add_value_scalar(self, base_var_name, value_node):
        """Same rule as _check_index_values_are_scalar, applied to a runtime
        `.add(key, value)` / `.set(value)` call on a variable declared
        `index`. Only fires for a literal collection value, same scoping
        caveat as the literal check."""
        if base_var_name in self.index_typed_vars and isinstance(value_node, (ListExpr, DictExpr)):
            kind = "list" if isinstance(value_node, ListExpr) else ("index" if getattr(value_node, "is_index", False) else "dict")
            raise RubidiumTypeError(
                f"index '{base_var_name}': value is a {kind}, not a scalar — "
                f"`index` holds exactly one scalar value per key. Use `dict` instead if a "
                f"key needs to hold a collection of values."
            )

    def _deep_copy_if_var(self, val, val_t, source_node):
        """Deep copy a value when the source is an existing variable (not a
        fresh allocation). Called on VarDecl/Assign to implement the spec's
        'every assignment creates a full deep copy' / 'Rubidium does not use
        references, borrowing, or shared ownership' rule (THE DEEP COPY RULE,
        stated as a GENERAL rule, not scoped to collections)."""
        if val_t == "%Box*" and isinstance(source_node, Var):
            copied = self.new_tmp()
            tracked = self.new_tmp()
            self.emit(f"  {copied} = call %Box* @box_deep_copy(%Box* {val})")
            # BUG-3: the fresh copy belongs to the current block until it is
            # bound somewhere longer-lived (which calls _escape_temp).
            self.emit(f"  {tracked} = call %Box* @rub_temp_track(%Box* {copied})")
            return tracked
        # BUG (found via syntax sweep): a `str`-typed variable's slot must
        # always hold a buffer IT owns, because .drop() unconditionally
        # frees whatever is in the slot. Three ways that used to not be true:
        #
        #  1. `let t = s` (Var source) — s and t ended up ALIASED, sharing
        #     the exact same heap buffer. Directly violates THE DEEP COPY
        #     RULE ("a and b are completely independent... does not use
        #     references, borrowing, or shared ownership"), and is a real
        #     heap-use-after-free the moment either side is .drop()'d:
        #     confirmed under AddressSanitizer — `let t = s; s.drop();
        #     print(t)` reads freed memory.
        #
        #  2. `let s = "literal"` (bare Str source) — emit_expr for a Str
        #     node returns a pointer straight into the LITERAL'S STATIC
        #     STORAGE (an LLVM global constant), never a heap allocation.
        #     Confirmed under AddressSanitizer: even a SINGLE `.drop()` of a
        #     variable still holding its original literal crashed with
        #     "free(): invalid pointer" — free() on read-only static memory.
        #
        #  3. `let y = i"just text"` / `let y = i"{x}"` (InterpolatedStr
        #     source) — the 0-part and 1-part cases take a shortcut that
        #     returns a part's raw pointer directly (same static-storage
        #     issue as #2 for a literal part, same aliasing issue as #1 if
        #     the one part is itself a plain string variable read).
        #
        # strdup() in all three cases gives the target its own independent,
        # owned buffer, exactly mirroring the %Box* branch above. Multi-part
        # interpolation and string concatenation (BinOp `+`) are NOT covered
        # here — both already build a fresh malloc'd buffer of their own, so
        # copying again would just leak the original for no safety benefit.
        if val_t == "i8*" and isinstance(source_node, (Var, Str, InterpolatedStr)):
            copied = self.new_tmp()
            tracked = self.new_tmp()
            self.emit(f"  {copied} = call i8* @rub_strdup_safe(i8* {val})")
            self.emit(f"  {tracked} = call i8* @rub_temp_track_str(i8* {copied})")
            return tracked
        return val

    def _infer_type(self, node):
        if isinstance(node, FileList): return "%Box*"
        if isinstance(node, LinkArg):
            # BUGFIX (bugs.log #4): `link expr` is a pass-by-reference marker,
            # not a distinct type — it must infer as whatever `expr` itself is
            # (e.g. `link a_list` is still %Box*). Previously unhandled, so it
            # fell through to the generic "i64" default, causing `let y = link x`
            # (with no explicit type annotation) to allocate/coerce as i64 and
            # silently produce a wrong value instead of the linked collection.
            return self._infer_type(node.expr)
        if isinstance(node, Number):
            if isinstance(node.value, float): return "double"
            v = int(node.value)
            for ir_t in ("i64", "i128", "i256", "i512", "i1024", "i2048"):
                lo, hi = self._int_bounds(ir_t)
                if lo <= v <= hi:
                    return ir_t
            return "i2048"
        if isinstance(node, Bool): return "i1"
        if isinstance(node, None_): return "i64"
        if isinstance(node, Str): return "i8*"
        if isinstance(node, InterpolatedStr): return "i8*"
        if isinstance(node, (ListExpr, DictExpr)): return "%Box*"
        if isinstance(node, Input): return "i8*"
        if isinstance(node, FileHandleStmt):
            if node.method in ("read", "readln"): return "i8*"
            return "i64"

        if isinstance(node, FieldAccess):
            obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)
            resolved_obj = self.import_aliases.get(obj_name, obj_name)
            mangled_var = f"{resolved_obj}_{node.field}"
            if obj_name not in self.instances and mangled_var in self.global_vars:
                return self.global_vars[mangled_var]
            if obj_name in self.instances:
                _, ir_t = self.field_index(self.instances[obj_name], node.field)
                return ir_t
            return "i64"
        
        if isinstance(node, MethodCall):
            obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)
            
            # Check for module function call (e.g., math_tools.sin -> math_tools_sin)
            # Also resolves import aliases (e.g. 'mt' -> 'math_tools' -> math_tools_sin)
            resolved_obj = self.import_aliases.get(obj_name, obj_name)
            if isinstance(node.obj, Var) and f"{resolved_obj}_{node.method}" in self.functions:
                fn = self.functions[f"{resolved_obj}_{node.method}"]
                return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"

            # Object is a known import alias but the method isn't registered yet
            # (e.g. imported .rub file wasn't found, or will be linked separately).
            # Return a sensible default so infer_type doesn't raise.
            if isinstance(node.obj, Var) and obj_name in self.import_aliases:
                return "i64"

            # FFI bindings: c_lib.my_c_func -> method name alone is the bound symbol
            # (also covers chained namespace access, e.g. wrap.glfw.glfwInit())
            if node.method in self.functions and obj_name not in self.instances:
                fn = self.functions[node.method]
                return self._ffi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
            
            if obj_name in self.instances:
                class_name = self.instances[obj_name]
                mangled = self.method_ir_name(class_name, node.method)
                if mangled in self.functions:
                    fn = self.functions[mangled]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                # node.method might actually be a FIELD name being accessed via call
                # syntax (e.g. model.input_w() reads the field, model.input_w(x) is
                # collection access on it) — not a real method.
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == node.method for f in cls.fields):
                    return "%Box*" if node.args else self.field_index(class_name, node.method)[1]
            elif obj_name in self._prescan_instances:
                # BUGFIX (bugs.log #12): pre-scan-only fallback — see
                # _gather_vardecl_types. Mirrors the full self.instances
                # logic above (method lookup, then field-vs-method fallback)
                # but for a class instance that's only known via the
                # pre-scan pass at this point (real codegen hasn't reached
                # its declaration yet).
                class_name = self._prescan_instances[obj_name]
                mangled = self.method_ir_name(class_name, node.method)
                if mangled in self.functions:
                    fn = self.functions[mangled]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == node.method for f in cls.fields):
                    return "%Box*" if node.args else self.field_index(class_name, node.method)[1]
            
            if node.method == "len": return "i64"
            if node.method == "char": return "i8*"
            if node.method in ("contains", "has"): return "i1"
            if node.method == "combine": return "i8*"
            if node.method in ("concat", "set", "insert", "replace"): return "i8*"
            if node.method in ("to_int", "to_str"): return "i64"
            if node.method == "split": return "%Box*"
            if node.method == "slice":
                if len(node.args) == 0:
                    return "%Box*"
                return "i8*"
            if node.method == "to": 
                if len(node.args) == 1:
                    arg = node.args[0]
                    if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                        return self.rubi_type_to_ir(arg.name)
                    elif hasattr(arg, 'value'):
                        return self.rubi_type_to_ir(arg.value)
                return "i64"
# Built-in module methods
            if obj_name == "time":
                if node.method == "timer_read":
                    return "double"
                return "i64"
            if obj_name == "random":
                if node.method == "choice": return "%Box*"
                if node.method == "float": return "double"
                return "i64"  # int, range, shuffle, seed all return i64/void
            # File handle methods (inside open() block)
            if obj_name in self._file_handle_vars:
                if node.method in ("read", "readln"): return "i8*"
                return "i64"
            # Check import aliases (e.g., xeon config-wiz as cf -> cf.read maps to config-wiz_read)
            if obj_name in self.import_aliases:
                resolved_name = self.import_aliases[obj_name]
                target_name = f"{resolved_name}_{node.method}"
                if target_name in self.functions:
                    fn_obj = self.functions[target_name]
                    return self.rubi_type_to_ir(fn_obj.ret_type) if fn_obj.ret_type else "i64"

            # BUG-21: `helper.shared_list(2)` — an ELEMENT of an imported
            # module's collection. Not a method call at all; the module's
            # variable is the merged global, and an element read out of it is
            # a boxed value like any other collection element.
            if self._ns_global_name(node) is not None:
                return "%Box*"

            raise RubidiumNameError(f"Cannot infer type for method call '{node.method}' on object '{obj_name}'")

        if isinstance(node, OsRun):
            return "i8*"
        if isinstance(node, FnCall):
            # BUGFIX/FEATURE (bugs.log #9): a non-string node.name means this
            # is either the existing "chained collection access" pattern
            # (nested(0)(1) → FnCall(FnCall("nested",[0]),[1])) or the new
            # DynResolve-based dynamic SY reflection (FnCall(DynResolve(...),
            # args)) — both ALWAYS evaluate to a %Box*, per emit_call_expr's
            # `not isinstance(node.name, str)` branch. Falling through to the
            # generic "i64" default below was wrong and broke method dispatch
            # (e.g. .add()/.set()) for these, since it made the target look
            # like a scalar instead of a collection.
            if not isinstance(node.name, str):
                return "%Box*"
            # Normalization check
            name = node.name.replace(".", "_") if isinstance(node.name, str) and "." in node.name else node.name
            if isinstance(name, str):
                # Check if name is a collection variable — emit_call_expr priority 4 returns %Box*
                for scope in reversed(self.local_vars_stack):
                    if name in scope:
                        if scope[name] == "%Box*":
                            return "%Box*"
                        break
                if name in self.global_vars and self.global_vars[name] == "%Box*":
                    return "%Box*"
                if name == "random":
                    # Check if third arg is a float type (TYPE token parsed as Var)
                    type_name = ""
                    if len(node.args) >= 3:
                        arg3 = node.args[2]
                        if isinstance(arg3, Var) and arg3.name in ("f32", "f64", "f128", "f256", "f512", "f1024", "f2048"):
                            type_name = arg3.name
                        elif isinstance(arg3, Str):
                            type_name = arg3.value
                    is_float = type_name.startswith("f")
                    if is_float:
                        return "double"
                    return "i64"
                if name in self.functions:
                    fn = self.functions[name]
                    return self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                # Check for class instantiation
                if name in self.class_defs:
                    return f"{self.class_ir_type(name)}*"
            return "i64"
            
        if isinstance(node, BinOp):
            lt = self._infer_type(node.left)
            rt = self._infer_type(node.right)
            if lt == "i8*" or rt == "i8*": return "i8*"
            if lt == "double" or rt == "double": return "double"
            return lt
        if isinstance(node, Compare): return "i1"
        if isinstance(node, UnaryOp):
            if node.op == "not": return "i1"
            return self._infer_type(node.value)
        if isinstance(node, TypeCast): return self.rubi_type_to_ir(node.target_type)
        if isinstance(node, MathBlock): return self.rubi_type_to_ir(node.vtype)
        if isinstance(node, Var):
            # Look for variable in the innermost scope first
            for scope in reversed(self.local_vars_stack):
                if node.name in scope:
                    return scope[node.name]
            if node.name in self.global_vars: return self.global_vars[node.name]
            return "i64"
        return "i64"

    def collect_globals(self, stmts):
        # Phase 1: a variable declared at global scope with `let` more than once
        # using DIFFERENT types (spec's "drop and recreate" overwrite rule) can't
        # be stored as a single fixed-type LLVM global — so first detect any name
        # that's declared with conflicting types and force it to the dynamic
        # %Box* representation (same mechanism already used for `Any`). Without
        # this, a later store used the *new* type against a slot sized/typed for
        # the *first* declaration, corrupting memory.
        type_map = {}
        self._link_aliases = []  # (alias_name, target_name) pairs from `let a = link b`
        self._gather_vardecl_types(stmts, type_map)
        # BUGFIX (bugs.log OPEN-10 follow-up): `let y = link x` was inferred
        # via _infer_type(LinkArg) -> _infer_type(Var("x")), but at THIS
        # prescan point self.global_vars/local_vars_stack don't hold x's type
        # yet (declare_global only runs in the LATER _collect_global pass) —
        # so it silently fell back to "i64" regardless of x's real/eventual
        # type(s). If x is itself polymorphic (or just a different type than
        # y's own prior declarations), y's polymorphism went undetected and y
        # kept a fixed-type global while actually aliasing a %Box* value,
        # corrupting reads/writes through the link. Union in the target's
        # OWN gathered types (now that the full prescan pass has finished, so
        # even target declarations appearing after the `link` line count).
        for alias_name, target_name in self._link_aliases:
            if target_name in type_map:
                type_map.setdefault(alias_name, set()).update(type_map[target_name])
        self._polymorphic_globals = {n for n, ts in type_map.items() if len(ts) > 1}
        for s in stmts: self._collect_global(s)

    def _gather_vardecl_types(self, stmts, type_map):
        for node in stmts:
            if isinstance(node, VarDecl):
                if node.is_local: continue
                if isinstance(node.value, (ClassInstantiate, FnCall, MethodCall)):
                    raw_cn = self._class_instantiate_candidate(node.value)  # bugs.log OPEN-O
                    if raw_cn and raw_cn in self.class_defs:
                        # BUGFIX (bugs.log #12): this pre-scan pass runs BEFORE
                        # real codegen and recurses into every function body
                        # (including main()). Real codegen registers
                        # self.instances[name]=class incrementally as it goes —
                        # but this pass previously didn't track that at all, so
                        # a LATER statement in the same body/pre-scan (e.g.
                        # `let v = b.items(key)`, indexing a collection FIELD
                        # via call syntax) hit _infer_type's MethodCall handler
                        # with no way to resolve "b" — a spurious "Cannot infer
                        # type for method call" error. Uses a SEPARATE dict
                        # (not self.instances itself) since real codegen's own
                        # incremental self.instances population is order-
                        # sensitive elsewhere; _infer_type's fallback checks
                        # both.
                        self._prescan_instances.setdefault(node.name, raw_cn)
                        # Still track the class type for polymorphism detection
                        ir_t = f"{self.class_ir_type(raw_cn)}*"
                        type_map.setdefault(node.name, set()).add(ir_t)
                        continue
                    if f"main_{raw_cn}" in self.class_defs:
                        self._prescan_instances.setdefault(node.name, f"main_{raw_cn}")
                        ir_t = f"{self.class_ir_type(f'main_{raw_cn}')}*"
                        type_map.setdefault(node.name, set()).add(ir_t)
                        continue
                if isinstance(node.value, LinkArg) and isinstance(node.value.expr, Var):
                    self._link_aliases.append((node.name, node.value.expr.name))
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                type_map.setdefault(node.name, set()).add(ir_t)
            elif isinstance(node, If):
                self._gather_vardecl_types(node.then_body, type_map)
                self._gather_vardecl_types(node.else_body or [], type_map)
            elif isinstance(node, While):
                self._gather_vardecl_types(node.body, type_map)
            elif isinstance(node, For):
                self._gather_vardecl_types(node.body, type_map)
            elif isinstance(node, Try):
                self._gather_vardecl_types(node.try_body, type_map)
                self._gather_vardecl_types(node.error_body, type_map)
            elif isinstance(node, FnDef):
                self._gather_vardecl_types(node.body, type_map)
            elif isinstance(node, FileOpen):
                if node.var_name:
                    self._file_handle_vars[node.var_name] = -1  # sentinel during gather pass
                self._gather_vardecl_types(node.body, type_map)
                if node.var_name:
                    self._file_handle_vars.pop(node.var_name, None)

    def _collect_global(self, node):
        if isinstance(node, NoOp):
            return  # SY declarations (bugs.log #2) — nothing to collect
        if isinstance(node, DynVarDecl):
            return  # bugs.log #9: dynamic hash-map entry, no named global to pre-declare
        if isinstance(node, VarDecl):
# Local variables are function-scoped, not global
            if node.is_local:
                return
            cn = None
            if isinstance(node.value, ClassInstantiate):
                cn = node.value.class_name
            elif isinstance(node.value, FnCall) and node.value.name in self.class_defs:
                cn = node.value.name
            elif isinstance(node.value, MethodCall):
                # bugs.log OPEN-O: imported, namespaced class instantiation
                # (`shapes.circle()`) — see _class_instantiate_candidate.
                candidate = self._class_instantiate_candidate(node.value)
                if candidate and candidate in self.class_defs:
                    cn = candidate
            
            if cn:
                ir_t = f"{self.class_ir_type(cn)}*"
                # Check if this name was declared with different types (polymorphic)
                if node.name in getattr(self, "_polymorphic_globals", ()):
                    ir_t = "%Box*"
                self.declare_global(node.name, ir_t)
                self.instances[node.name] = cn
            else:
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                # BUGFIX (bugs.log OPEN-10): this branch handles GLOBAL-pool
                # VarDecls (is_local was already handled above), so it must
                # check the GLOBAL polymorphism set and actually register the
                # LLVM global via declare_global — it previously checked
                # _local_polymorphic (wrong set, always empty here) and wrote
                # into local_vars_stack (a no-op for global storage), so a
                # name redeclared with a conflicting type never got promoted
                # to %Box* and never got declared, leaving the FIRST
                # declaration's type/global permanently in effect.
                if node.name in getattr(self, "_polymorphic_globals", ()):
                    ir_t = "%Box*"
                self.declare_global(node.name, ir_t)
                # Track element type for collection type enforcement (bugs.log #2)
                if node.element_type:
                    self.element_types[node.name] = node.element_type
                if node.mutable: self.mutable_vars.add(node.name)
        elif isinstance(node, FFIBind):
            # Pre-register the bound symbol so calls resolve in collect pass.
            # Use the 'as' alias when provided (e.g. fn lib rb_sin(...) -> f64 as sin  → "sin")
            fn_name = node.alias if node.alias else node.symbol_name
            # BUGFIX (bugs.log #1): avoid colliding with reserved C symbols
            # (e.g. `as sin` shadowing libm's sin) — see _safe_fn_symbol.
            safe_name = self._safe_fn_symbol(fn_name)
            if safe_name != fn_name:
                self._fn_symbol_override[fn_name] = safe_name
            self.functions[fn_name] = FnDef(safe_name, node.params, node.ret_type, [])
        elif isinstance(node, If):
            for s in node.then_body: self._collect_global(s)
            for s in (node.else_body or []): self._collect_global(s)
        elif isinstance(node, While):
            for s in node.body: self._collect_global(s)
        elif isinstance(node, For):
            if not node.iterable:
                ir_t = "i64"
            elif isinstance(node.iterable, MethodCall) and node.iterable.method == "len":
                ir_t = "i64"
            else:
                ir_t = "%Box*"
            self.declare_global(node.var, ir_t)
            for s in node.body: self._collect_global(s)
        elif isinstance(node, Try):
            for s in node.try_body: self._collect_global(s)
            for s in node.error_body: self._collect_global(s)
        elif isinstance(node, FnDef):
            # Variable pool: let declarations inside regular functions go to the global pool
            for s in node.body: self._collect_global(s)
        elif isinstance(node, FileOpen):
            if node.var_name:
                self._file_handle_vars[node.var_name] = -1  # sentinel during collect pass
            for s in node.body: self._collect_global(s)
            if node.var_name:
                self._file_handle_vars.pop(node.var_name, None)

    def _gather_local_types(self, stmts, type_map, only_local=False):
        """Scan a function/method body for `let` redeclarations of the same
        local variable name with different types (same corruption risk as
        globals, but for stack-allocated locals — an alloca is only
        sized/typed once). Does NOT recurse into nested FnDef (separate
        function scope). If only_local=True (class methods, where non-local
        `let` becomes an instance field instead), only is_local nodes count."""
        for node in stmts:
            if isinstance(node, VarDecl):
                if only_local and not node.is_local: continue
                if isinstance(node.value, (ClassInstantiate, FnCall, MethodCall)):
                    raw_cn = self._class_instantiate_candidate(node.value)  # bugs.log OPEN-O
                    if raw_cn and (raw_cn in self.class_defs or f"main_{raw_cn}" in self.class_defs):
                        continue
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                type_map.setdefault(node.name, set()).add(ir_t)
            elif isinstance(node, If):
                self._gather_local_types(node.then_body, type_map, only_local)
                self._gather_local_types(node.else_body or [], type_map, only_local)
            elif isinstance(node, While):
                self._gather_local_types(node.body, type_map, only_local)
            elif isinstance(node, For):
                # BUGFIX (bugs.log #5): a for-loop implicitly (re)declares its
                # loop variable each time the block runs. If that name was
                # already used (before or after, textually) for a `let` of a
                # different underlying LLVM type — or for another for-loop
                # with a different natural type — the two allocas collide,
                # since alloca is only sized/typed once per name. Register the
                # loop var's "natural" type here so the same polymorphism
                # detection used for `let` redeclarations also catches this,
                # forcing a shared %Box* representation instead of silently
                # reusing a mistyped alloca (previously caused a segfault).
                if getattr(node, "var", None):
                    if node.iterable is None:
                        nat_t = "i64"  # for i in a..b (range)
                    elif isinstance(node.iterable, Var) and node.iterable.name in self._file_handle_vars:
                        nat_t = "%Box*"  # for line in file_handle
                    else:
                        try:
                            it_t = self._infer_type(node.iterable)
                        except Exception:
                            it_t = "%Box*"
                        nat_t = "i64" if it_t in self._INT_IR_SET else "%Box*"
                    type_map.setdefault(node.var, set()).add(nat_t)
                self._gather_local_types(node.body, type_map, only_local)
            elif isinstance(node, Try):
                self._gather_local_types(node.try_body, type_map, only_local)
                self._gather_local_types(node.error_body, type_map, only_local)
            elif isinstance(node, FileOpen):
                if node.var_name:
                    self._file_handle_vars[node.var_name] = -1  # sentinel during gather pass
                self._gather_local_types(node.body, type_map, only_local)
                if node.var_name:
                    self._file_handle_vars.pop(node.var_name, None)

    def _emit_default_return(self, ret_ir, is_main):
        if is_main: self.emit("  ret i32 0")
        elif ret_ir == "i64": self.emit("  ret i64 0")
        elif ret_ir == "i1": self.emit("  ret i1 0")
        elif ret_ir in ("float","double"): self.emit(f"  ret {ret_ir} 0.0")
        elif ret_ir == "i8*": self.emit("  ret i8* null")
        elif ret_ir in self._INT_IR_SET: self.emit(f"  ret {ret_ir} 0")
        else: self.emit(f"  ret {ret_ir} null")

    def emit_fn(self, node):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, node.name, None
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.dropped_vars = set()
        self._alloca_emitted = set()  # track which local names have had alloca emitted
        _type_map = {}
        self._gather_local_types(node.body, _type_map, only_local=False)
        self._local_polymorphic = {n for n, ts in _type_map.items() if len(ts) > 1}

        ret_ir = "i32" if node.name == "main" else (self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64")
        self._cur_fn_ret_ir = ret_ir   # Return handler uses this when fn has no declared ret type
        param_ir = ", ".join(f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in node.params)
        self.emit(f"define {ret_ir} @{node.name}({param_ir}) {{")
        self.emit("entry:")

        # OPEN-7: block this function branches to (instead of crashing/silently
        # continuing) when a raise or division-by-zero happens with no enclosing
        # try in THIS function — returns a default value so the caller's
        # _emit_error_propagation_check can notice @_rub_error_flag and either
        # catch it (if the call site is itself inside a try) or keep propagating.
        self._fn_error_exit_label = self.new_label("fn_err_exit")

        for pn, pt in node.params:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars_stack[-1][pn] = ir_t
            self.mutable_vars.add(pn)  # params have no `mut` syntax; spec examples mutate them directly
            self.emit(f"  %ptr_{pn} = alloca {ir_t}")
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* %ptr_{pn}")

        if not self.emit_body(node.body):
            self._emit_default_return(ret_ir, node.name == "main")
        self.emit(f"{self._fn_error_exit_label}:")
        if node.name == "main":
            # OPEN-7: an error propagated all the way up through main's own
            # body uncaught — this is the top of the call stack, so report and
            # exit now instead of silently returning 0 as if nothing happened.
            msg_ptr = self.new_tmp()
            self.emit(f"  {msg_ptr} = load i8*, i8** @_rub_error_msg")
            self.emit(f"  call void @rub_throw(i8* {msg_ptr})")
            self.emit("  unreachable")
        else:
            self._emit_default_return(ret_ir, False)
        self._fn_error_exit_label = None
        self.emit("}\n")
        # syntax: FFI CALLBACKS — the real function above is done; now also
        # generate its C-ABI trampoline, buffered into the same
        # _pending_trampolines mechanism so it flushes right below.
        if node.is_callback:
            self._emit_callback_trampoline(node)
        # Flush any trampolines generated during this function
        if self._pending_trampolines:
            self.fn_lines += self._pending_trampolines
            self._pending_trampolines = []

    def _emit_callback_trampoline(self, node):
        """syntax: FFI CALLBACKS. Emits `@{name}_c_trampoline` — a plain-C-ABI
        wrapper C can call directly. Its entire body just marshals arguments
        and makes a normal internal call to the real function (@name, already
        compiled just above), reusing exactly the same call-emission a
        Rubidium-to-Rubidium call site already uses — so none of Rubidium's
        own per-call setup (temp-arena marks, error propagation, all handled
        inside @name's own emit_fn-generated body) needs to be duplicated
        here. Mirrors emit_ffi_bind's trampoline, which does the same thing
        in the opposite direction (Rubidium calling into C)."""
        c_param_types = [self._ffi_type_to_ir(pt) for _, pt in node.params]
        internal_param_types = [self.rubi_type_to_ir(pt) for _, pt in node.params]
        c_ret_t = self._ffi_type_to_ir(node.ret_type) if node.ret_type else "i64"
        internal_ret_t = self.rubi_type_to_ir(node.ret_type) if node.ret_type else "i64"

        tramp_name = f"{node.name}_c_trampoline"
        params_ir = ", ".join(f"{t} %p{i}" for i, t in enumerate(c_param_types))
        pending = [f"\ndefine {c_ret_t} @{tramp_name}({params_ir}) {{", "entry:"]

        # BUGFIX (found under AddressSanitizer testing this feature): every
        # other function's body gets a temp-arena mark/release pair from
        # emit_body (see _emit_temp_mark/_emit_temp_release) — this
        # trampoline builds its IR into a local `pending` list rather than
        # through self.emit(), which is exactly why emit_ffi_bind's own
        # trampoline does the same manual replication rather than calling
        # those self.emit()-based helpers (they'd inject into the OUTER
        # function currently being compiled, not this one). Without a mark
        # here, the box_i() call below (for an Any-typed param) was
        # confirmed leaking 48 bytes per invocation — nothing ever freed it.
        mark_tmp = self.new_tmp()
        pending.append(f"  {mark_tmp} = call i64 @rub_temp_mark()")

        # Marshal each C-typed incoming arg into what the real function
        # expects. Every declared type maps identically under
        # _ffi_type_to_ir/rubi_type_to_ir EXCEPT Any (i64 at the C boundary,
        # %Box* internally, same as any other Any-typed value) — box it the
        # same way box_i() already boxes any other raw integer/handle, and
        # track it the same way _track_temp() tracks any other freshly
        # boxed value so the release below actually finds it.
        call_args = []
        for i, (c_t, internal_t) in enumerate(zip(c_param_types, internal_param_types)):
            if c_t == internal_t:
                call_args.append(f"{internal_t} %p{i}")
            else:
                box_tmp = self.new_tmp()
                pending.append(f"  {box_tmp} = call %Box* @box_i(i64 %p{i})")
                tracked_tmp = self.new_tmp()
                pending.append(f"  {tracked_tmp} = call %Box* @rub_temp_track(%Box* {box_tmp})")
                call_args.append(f"{internal_t} {tracked_tmp}")

        ret_tmp = self.new_tmp()
        pending.append(f"  {ret_tmp} = call {internal_ret_t} @{node.name}({', '.join(call_args)})")

        # If the real function propagated an uncaught error, there is no
        # Rubidium caller to hand it further up to — C called this directly.
        # Clear the flag and return a plain default rather than crashing the
        # whole program from inside what may be a minor event-handler.
        err_flag = self.new_tmp()
        pending.append(f"  {err_flag} = load i1, i1* @_rub_error_flag")
        err_l, ok_l = self.new_label("ctramp_err"), self.new_label("ctramp_ok")
        pending.append(f"  br i1 {err_flag}, label %{err_l}, label %{ok_l}")
        pending.append(f"{err_l}:")
        pending.append(f"  store i1 0, i1* @_rub_error_flag")
        pending.append(f"  call void @rub_temp_release_to(i64 {mark_tmp})")
        if c_ret_t in ("float", "double"):
            default_ret = "0.0"
        elif c_ret_t == "fp128":
            default_ret = "0xL00000000000000000000000000000000"
        elif c_ret_t.endswith("*"):
            default_ret = "null"
        else:
            default_ret = "0"
        pending.append(f"  ret {c_ret_t} {default_ret}")
        pending.append(f"{ok_l}:")

        # BUG-3 note (same reasoning as any other caller of a Rubidium
        # function): a returned %Box* is deliberately left tracked by the
        # callee, sitting above THIS trampoline's own mark — it's this
        # trampoline's job, as the caller, to free it. Unbox first (when the
        # C return type differs), then release everything back to the mark
        # — the boxed args above and, for an Any return, the now-copied-out
        # returned box itself.
        if internal_ret_t == c_ret_t:
            pending.append(f"  call void @rub_temp_release_to(i64 {mark_tmp})")
            pending.append(f"  ret {c_ret_t} {ret_tmp}")
        else:
            unbox_tmp = self.new_tmp()
            pending.append(f"  {unbox_tmp} = call i64 @unbox_i(%Box* {ret_tmp})")
            pending.append(f"  call void @rub_temp_release_to(i64 {mark_tmp})")
            pending.append(f"  ret {c_ret_t} {unbox_tmp}")
        pending.append("}")
        self._pending_trampolines += pending

    def emit_body(self, stmts):
        # Push a new scope for this block
        self.local_vars_stack.append({})
        # BUG-3: everything the block allocates above this mark is released
        # when the block ends — the spec's "locals and temporaries are dropped
        # at scope exit" rule. Anything that must outlive the block (a global
        # binding, a class field, a return value) is taken out of the arena by
        # _escape_temp at the point it escapes.
        mark = self._emit_temp_mark()
        returned = False
        for s in stmts:
            if returned: break
            if self.emit_stmt(s): returned = True
        if not returned:
            self._emit_temp_release(mark)
        # Pop the scope when leaving the block
        self.local_vars_stack.pop()
        return returned

    def _emit_class_method(self, mfn, class_name):
        self.tmp_count, self.label_count, self.cur_fn, self.cur_class = 0, 0, mfn.name, class_name
        self.local_vars_stack = [{}]  # Stack of scopes, each scope is a dict of variable names to types
        self.dropped_vars = set()
        self._alloca_emitted = set()
        _type_map = {}
        self._gather_local_types(mfn.body, _type_map, only_local=True)
        self._local_polymorphic = {n for n, ts in _type_map.items() if len(ts) > 1}
        
        struct_t, ret_ir = self.class_ir_type(class_name), (self.rubi_type_to_ir(mfn.ret_type) if mfn.ret_type else "i64")
        self._cur_fn_ret_ir = ret_ir   # Return handler uses this when method has no declared ret type
        param_str = ", ".join([f"{struct_t}* %param___self"] + [f"{self.rubi_type_to_ir(pt)} %param_{pn}" for pn, pt in mfn.params[1:]])
        self.emit(f"define {ret_ir} @{mfn.name}({param_str}) {{"  )
        self.emit("entry:")

        # OPEN-7: see emit_fn — same per-callable propagate-on-uncaught-error block.
        self._fn_error_exit_label = self.new_label("fn_err_exit")

        # Use ptr___self naming to match get_var_ptr convention for local_vars
        self.emit(f"  %ptr___self = alloca {struct_t}*")
        self.emit(f"  store {struct_t}* %param___self, {struct_t}** %ptr___self")
        # Register __self so field access (Var("__self")) works inside method bodies
        self.local_vars_stack[-1]["__self"] = f"{struct_t}*"
        self.instances["__self"] = class_name
        for pn, pt in mfn.params[1:]:
            ir_t = self.rubi_type_to_ir(pt)
            self.local_vars_stack[-1][pn] = ir_t
            self.mutable_vars.add(pn)
            self.emit(f"  %ptr_{pn} = alloca {ir_t}")
            self.emit(f"  store {ir_t} %param_{pn}, {ir_t}* %ptr_{pn}")

        if not self.emit_body(mfn.body):
            self._emit_default_return(ret_ir, False)
        self.emit(f"{self._fn_error_exit_label}:")
        self._emit_default_return(ret_ir, False)
        self._fn_error_exit_label = None
        self.emit("}\n")

    def emit_stmt(self, node):
        if isinstance(node, NoOp):
            return  # SY declarations (bugs.log #2) — compile-time only, nothing to emit
        if isinstance(node, Raise):
            # FEATURE: raise <expr> — same error-propagation path already used
            # by builtin runtime errors (division by zero, missing file, etc.):
            # store the message in the global error buffer and jump to the
            # nearest LEXICALLY enclosing try's error handler in this function,
            # or (OPEN-7) propagate it to this function's caller — which itself
            # either catches it (if its call site is inside a try) or keeps
            # propagating — all the way up to the entry point if truly uncaught.
            msg_v, msg_t = self.emit_expr(node.message)
            msg_s = self.coerce(msg_v, msg_t, "i8*")
            self._emit_raise_or_propagate(msg_s)
            cont_l = self.new_label("after_raise")
            self.emit(f"{cont_l}:")
            return
        if isinstance(node, DynVarDecl):
            # BUGFIX/FEATURE (bugs.log #9): `let (x): TYPE = value` where x is
            # a runtime-dynamic SY variable. The value codegens exactly like
            # any normal declaration's value (so dict/list/index literals,
            # scalars, etc. all just work); it's boxed and stored into the
            # runtime hash-map under x's CURRENT string value, rather than
            # given a fixed compile-time name.
            key_v, key_t = self.emit_expr(Var(node.holder_name))
            key_s = self.coerce(key_v, key_t, "i8*")
            val, val_t = self.emit_expr(node.value)
            box_v = self.coerce_to_box(val, val_t)
            self.emit(f"  call void @rub_dynvar_set(i8* {key_s}, %Box* {box_v})")
            return
        if isinstance(node, VarDecl):
            if node.name in self.dropped_vars: self.dropped_vars.discard(node.name)
            self._track_null_valued(node.name, node.value)  # OPEN-4 scalar Null
            # `index` must hold exactly one SCALAR value per key (never a
            # list/index/dict/dict+) — see _check_index_values_are_scalar.
            if node.vtype == "index":
                self.index_typed_vars.add(node.name)
                if isinstance(node.value, DictExpr):
                    self._check_index_values_are_scalar(node.value, node.name)
            else:
                self.index_typed_vars.discard(node.name)

            # BUGFIX (bugs.log #5): `let [mut] b = link a` for a SCALAR source
            # (collections already work via shared %Box* pointers, see
            # _deep_copy_if_var). Register b as an alias of a instead of giving
            # it its own storage, so get_var_ptr("b") transparently resolves to
            # a's real pointer and every future read of b sees a's live value.
            # BUG-22: rewrite a namespaced link target into the merged global
            # it actually refers to, so the two branches below recognise it.
            if isinstance(node.value, LinkArg):
                node.value.expr = self._ns_link_target(node.value.expr)

            if isinstance(node.value, LinkArg) and isinstance(node.value.expr, Var):
                target_name = node.value.expr.name
                target_t = self._infer_type(node.value.expr)
                if target_t != "%Box*":
                    if node.is_local or (self.cur_fn is not None and self.cur_fn != "_rubidium_init"):
                        self.local_vars_stack[-1][node.name] = target_t
                    else:
                        self.global_vars[node.name] = target_t
                    if node.mutable: self.mutable_vars.add(node.name)
                    self.linked_to[node.name] = target_name
                    return False

            # BUGFIX (bugs.log OPEN-1): `let [mut] b = link a(i)` for indexed
            # access on a collection. Create a persistent reference to the
            # element at index i, so reads/writes to b go through the collection.
            if isinstance(node.value, LinkArg) and isinstance(node.value.expr, FnCall):
                # Check if this is a collection index access: var(index)
                inner = node.value.expr
                # FnCall has .name (the variable) and .args (the indices)
                if isinstance(inner.name, str) and len(inner.args) == 1:
                    coll_name = inner.name
                    index_expr = inner.args[0]
                    # Verify the source is a collection type
                    coll_type = self._infer_type(Var(coll_name))
                    if coll_type == "%Box*":
                        # Store the indexed link reference
                        self.indexed_links[node.name] = (coll_name, index_expr)
                        # The linked variable has the same type as the collection element
                        # For now, infer as %Box* (boxed element) since we don't know element type statically
                        elem_type = "%Box*"
                        if node.is_local or (self.cur_fn is not None and self.cur_fn != "_rubidium_init"):
                            self.local_vars_stack[-1][node.name] = elem_type
                        else:
                            # For globals, track in global_vars for type info but DON'T declare storage
                            # Reads/writes are handled specially in emit_expr Var branch and Assign branch
                            self.global_vars[node.name] = elem_type
                        if node.mutable: self.mutable_vars.add(node.name)
                        return False

            is_class = False
            cn = ""
            is_class_copy_src = None  # set when source is an existing instance var
            if isinstance(node.value, (ClassInstantiate, FnCall, MethodCall)):
                raw_cn = self._class_instantiate_candidate(node.value)  # bugs.log OPEN-O
                if raw_cn and raw_cn in self.class_defs:
                    cn = raw_cn
                    is_class = True
                elif raw_cn and f"main_{raw_cn}" in self.class_defs:
                    cn = f"main_{raw_cn}"
                    is_class = True
            elif isinstance(node.value, Var) and node.value.name in self.instances:
                # let p2 = p1 where p1 is a class instance — deep copy semantics
                cn = self.instances[node.value.name]
                is_class = True
                is_class_copy_src = node.value.name  # source var to copy from

            # Local variables are function-scoped (not global)
            if node.is_local:
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                if node.name in getattr(self, "_local_polymorphic", ()):
                    ir_t = "%Box*"
                self.local_vars_stack[-1][node.name] = ir_t
                # Track element type for collection type enforcement (bugs.log #2)
                if node.element_type:
                    self.element_types[node.name] = node.element_type
                if node.mutable: self.mutable_vars.add(node.name)
                ptr_str = f"%ptr_{node.name}"
                # BUG-3: a FIRST declaration owns its storage for exactly this
                # block, so its value can stay arena-tracked and be auto-dropped
                # at scope exit (the spec's local-variable rule). A REPEAT `let`
                # of the same name reuses an alloca that may belong to an
                # enclosing block, so that value must escape instead.
                first_decl = node.name not in self._alloca_emitted
                if first_decl:
                    self.emit(f"  {ptr_str} = alloca {ir_t}")
                    self._alloca_emitted.add(node.name)
                    self._emit_null_flag_decl(node.name, ir_t, is_local=True)  # bugs.log OPEN-J
                val, val_t = self.emit_expr(node.value)
                # OPEN-10: deep-copy the %Box* BEFORE coercing, not after. The
                # old order coerced first (e.g. %Box* -> i8* via unbox_s, which
                # REASSIGNS val to a raw string pointer) and THEN called
                # _deep_copy_if_var with the STALE val_t="%Box*", so it ran
                # box_deep_copy on what was now an i8* — treating a char* as a
                # Box* and producing garbage (`let w: str = <loop var>` lost its
                # value entirely). Copying the box first also makes the unboxed
                # string an independent strdup'd buffer, so it survives the
                # source box being dropped (e.g. a for-loop variable freed at
                # end of iteration).
                val = self._deep_copy_if_var(val, val_t, node.value)
                val = self.coerce(val, val_t, ir_t)
                if not (first_decl and node.is_local):
                    val = self._escape_temp(val, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                self._emit_null_flag_store(node.name, node.value)  # bugs.log OPEN-J
                return False
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init" and self.cur_class is not None:
                # Inside a class method — keep variables local (class instances isolate memory)
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.local_vars_stack[-1][node.name] = f"{struct_t}*"
                    if node.mutable: self.mutable_vars.add(node.name)
                    ptr_str = f"%ptr_{node.name}"
                    self.emit(f"  {ptr_str} = alloca {struct_t}*")
                    if is_class_copy_src:
                        self.emit_class_copy(ptr_str, cn, is_class_copy_src)
                    else:
                        init_args = node.value.args if isinstance(node.value, (FnCall, MethodCall)) else []  # bugs.log OPEN-O
                        self.emit_class_init(ptr_str, cn, init_args)
                    return False

                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                if node.mutable: self.mutable_vars.add(node.name)
                # Per spec: non-local `let` inside a class method is an IMPLICIT
                # INSTANCE FIELD (registered by _register_implicit_class_fields),
                # not a stack-local — it persists across method calls on the same
                # instance and is readable externally as `instance.name`.
                idx, field_ir_t = self.field_index(self.cur_class, node.name)
                struct_t = self.class_ir_type(self.cur_class)
                self_ptr_str, _ = self.get_var_ptr("__self")
                inst_ptr = self.new_tmp(); fptr = self.new_tmp()
                self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {self_ptr_str}")
                self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
                val, val_t = self.emit_expr(node.value)
                # OPEN-10: copy the %Box* before coercing (see the detailed note
                # at the is_local branch above — same stale-val_t bug otherwise).
                val = self._deep_copy_if_var(val, val_t, node.value)
                val = self.coerce(val, val_t, field_ir_t)
                # BUG-3: a class field outlives this block — take ownership.
                val = self._escape_temp(val, field_ir_t)
                self.emit(f"  store {field_ir_t} {val}, {field_ir_t}* {fptr}")
                if isinstance(node.value, FFILoad):
                    slot = f"@_ffi_slot_{node.name}"
                    # BUG-1 (RIG report): `d.startswith(slot)` is a substring
                    # check against the WHOLE declaration line, not an exact
                    # global-name check — "@_ffi_slot_glfw = global i64 -1"
                    # starts with "@_ffi_slot_gl" too, so declaring `gl` after
                    # `glfw` false-positived as "already declared" and skipped
                    # emitting @_ffi_slot_gl entirely, leaving a later `store`
                    # into it referencing an undefined global (LLVM codegen
                    # error). Match the exact declaration prefix ("slot =")
                    # instead, so a name that happens to be a prefix of an
                    # earlier one is never mistaken for it.
                    if not any(d.startswith(f"{slot} =") for d in self.global_decls):
                        self.global_decls.append(f"{slot} = global i64 -1")
                    self.emit(f"  store i64 {val}, i64* {slot}")
            else:
                # BUGFIX: a non-local `let` inside an ordinary (non-class-
                # method) function used to hit a separate branch here that
                # allocated it as a plain function-local (a stack `alloca`,
                # gone the moment the function returns) — silently
                # contradicting the spec's GLOBAL BY DEFAULT rule ("Variables
                # created normally are placed into the global memory pool...
                # Global variables can be accessed anywhere"), which draws no
                # distinction between a `let` at true top level and one
                # inside a regular function. Confirmed: `fn set_it() { let
                # mut counter = 42 }` then `set_it(); print(counter)` from
                # `main()` printed 0, not 42 — every one of a real program's
                # helper-function-initialized globals (a shuffled deck, dealt
                # hands, starting coin totals, ...) silently reset to their
                # type's zero value the moment the initializing function
                # returned, while still LOOKING like ordinary global state
                # everywhere else in the source. This branch is now removed
                # entirely so those declarations fall through to the same
                # `else` below as a true top-level `let` — the exact same
                # real-global codegen path, regardless of which function
                # first declares the name.
                if is_class:
                    struct_t = self.class_ir_type(cn)
                    self.instances[node.name] = cn
                    self.declare_global(node.name, f"{struct_t}*")
                    if node.mutable: self.mutable_vars.add(node.name)
                    init_args = node.value.args if isinstance(node.value, (FnCall, MethodCall)) else []  # bugs.log OPEN-O
                    ir_name = f"_var_{node.name}" if node.name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else node.name
                    if is_class_copy_src:
                        self.emit_class_copy(f"@{ir_name}", cn, is_class_copy_src)
                    else:
                        self.emit_class_init(f"@{ir_name}", cn, init_args)
                    return False
                ir_t = self.rubi_type_to_ir(node.vtype) if node.vtype else self._infer_type(node.value)
                if node.mutable: self.mutable_vars.add(node.mutable)
                self.declare_global(node.name, ir_t)
                # Track element type for collection type enforcement (bugs.log #2)
                if node.element_type:
                    self.element_types[node.name] = node.element_type
                # Use the AUTHORITATIVE type the global was actually declared with
                # (may be %Box* if this name was re-declared elsewhere with a
                # different type — see collect_globals's polymorphism detection).
                # Using a freshly-recomputed ir_t here instead would store using
                # the wrong type/size against the real global, corrupting memory.
                actual_t = self.global_vars.get(node.name, ir_t)
                self._emit_null_flag_decl(node.name, actual_t, is_local=False)  # bugs.log OPEN-J
                val, val_t = self.emit_expr(node.value)
                # OPEN-10: copy the %Box* before coercing (see the detailed note
                # at the is_local branch above — same stale-val_t bug otherwise).
                val = self._deep_copy_if_var(val, val_t, node.value)
                val = self.coerce(val, val_t, actual_t)
                # BUG-3: globals live until an explicit .drop(), so this value
                # must leave the block-scoped arena.
                val = self._escape_temp(val, actual_t)
                ir_name = f"_var_{node.name}" if node.name in ("pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round") else node.name
                self.emit(f"  store {actual_t} {val}, {actual_t}* @{ir_name}")
                self._emit_null_flag_store(node.name, node.value)  # bugs.log OPEN-J
                if isinstance(node.value, FFILoad):
                    slot = f"@_ffi_slot_{node.name}"
                    # BUG-1 (RIG report): `d.startswith(slot)` is a substring
                    # check against the WHOLE declaration line, not an exact
                    # global-name check — "@_ffi_slot_glfw = global i64 -1"
                    # starts with "@_ffi_slot_gl" too, so declaring `gl` after
                    # `glfw` false-positived as "already declared" and skipped
                    # emitting @_ffi_slot_gl entirely, leaving a later `store`
                    # into it referencing an undefined global (LLVM codegen
                    # error). Match the exact declaration prefix ("slot =")
                    # instead, so a name that happens to be a prefix of an
                    # earlier one is never mistaken for it.
                    if not any(d.startswith(f"{slot} =") for d in self.global_decls):
                        self.global_decls.append(f"{slot} = global i64 -1")
                    self.emit(f"  store i64 {val}, i64* {slot}")
                return False
 
        elif isinstance(node, Assign):
            if node.name in self.dropped_vars: raise RubidiumNameError(f"Var '{node.name}'")
            if isinstance(node.name, str):
                self._track_null_valued(node.name, node.value)  # OPEN-4 scalar Null

            # Handle indexed link assignment: let b = link a(i); b = val -> a(i).set(val)
            if node.name in self.indexed_links:
                coll_name, index_expr = self.indexed_links[node.name]
                # Check mutability
                if node.name not in self.mutable_vars and node.name not in self.linked_to:
                    raise RubidiumTypeError(f"Immutable '{node.name}'")
                # Emit collection pointer
                coll_ptr, coll_t = self.emit_expr(Var(coll_name))
                coll_box = self.coerce_to_box(coll_ptr, coll_t)
                # Emit index as i64 (collection_set_at takes int, not Box*)
                idx_v, idx_t = self.emit_expr(index_expr)
                idx_i64 = self.coerce(idx_v, idx_t, "i64")
                # Emit value
                val, val_t = self.emit_expr(node.value)
                val_box = self.coerce_to_box(val, val_t)
                # Deep copy the value (assignment creates deep copy per spec)
                val_copy = self.new_tmp()
                self.emit(f"  {val_copy} = call %Box* @box_deep_copy(%Box* {val_box})")
                # Set the element
                self.emit(f"  call void @collection_set_at(%Box* {coll_box}, i32 {idx_i64}, %Box* {val_copy})")
                return
            
            # --- FIX: Check for Field Assignment ---
            # BUGFIX (bugs.log #15): same precedence fix as the Var read path
            # above — a local/parameter must shadow a same-named implicit
            # class field, or assigning to a parameter could silently write
            # to an unrelated field from a different method instead.
            in_local_scope = any(node.name in scope for scope in self.local_vars_stack)
            if not in_local_scope and self.is_class_field(node.name):
                self.emit_field_assign(FieldAssign(Var("__self"), node.name, node.value))
            # ----------------------------------------
            elif node.name in self.instances: pass
            else:
                if node.name not in self.mutable_vars: raise RubidiumTypeError(f"Immutable '{node.name}'")
                if node.name in self.linked_to:
                    ptr_str, ir_t = self._unlink_var(node.name)
                else:
                    ptr_str, ir_t = self.get_var_ptr(node.name)
                val, val_t = self.emit_expr(node.value)
                val = self.coerce(val, val_t, ir_t)
                # Deep copy only if the COERCED value is a Box* (collection), not the original value
                val = self._deep_copy_if_var(val, ir_t, node.value)
                # BUG-3: reassignment always takes the value out of the arena.
                # The target variable may have been DECLARED in an enclosing
                # block, which outlives this one, so leaving the value tracked
                # here could free it while the variable still points at it.
                # (Only a variable's own `let` declaration keeps its value
                # block-scoped — that is what auto-drops locals at scope exit.)
                val = self._escape_temp(val, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {ptr_str}")
                self._emit_null_flag_store(node.name, node.value)  # bugs.log OPEN-J

        elif isinstance(node, FieldAssign): self.emit_field_assign(node)
        elif isinstance(node, Print): self.emit_print(node.value)
        elif isinstance(node, Println): self.emit_println(node.value)
        elif isinstance(node, If): self.emit_if(node)
        elif isinstance(node, While): self.emit_while(node)
        elif isinstance(node, For): self.emit_for(node)
        elif isinstance(node, Return):
            val, val_t = self.emit_expr(node.value)
            # Determine the expected return type:
            # 1. Use the registered function's declared return type if available
            # 2. Fall back to the current function's IR return type (_cur_fn_ret_ir)
            # 3. Only use the expression's type (val_t) as a last resort
            # This prevents `ret %Box* %t75` inside a function declared as `i64`.
            if self.cur_fn in self.functions and self.functions[self.cur_fn].ret_type:
                expected = self.rubi_type_to_ir(self.functions[self.cur_fn].ret_type)
            else:
                expected = getattr(self, '_cur_fn_ret_ir', val_t)
            val = self.coerce(val, val_t, expected)
            # BUG-3: deliberately NOT escaped. A returning block skips its own
            # release (see emit_body), so a tracked return value simply stays
            # in the arena at a level above the CALLER's block mark — the
            # caller's block-end release is what frees it. That is also why a
            # returned temporary is never double-freed: it is only ever one
            # arena entry, no matter how many frames it is passed back through.
            self.emit(f"  ret {expected} {val}")
            return True
        elif isinstance(node, FnCall): self.emit_call_expr(node)
        elif isinstance(node, MethodCall): 
            # Handle indexed link: y = link x(i); y.set(val) -> x(i).set(val)
            if isinstance(node.obj, Var) and node.obj.name in self.indexed_links:
                coll_name, index_expr = self.indexed_links[node.obj.name]
                if node.method == "set" and len(node.args) == 1:
                    # Emit collection pointer
                    coll_ptr, coll_t = self.emit_expr(Var(coll_name))
                    coll_box = self.coerce_to_box(coll_ptr, coll_t)
                    # Emit index
                    idx_v, idx_t = self.emit_expr(index_expr)
                    idx_box = self.coerce_to_box(idx_v, idx_t)
                    # Emit value
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_box = self.coerce_to_box(val_v, val_t)
                    val_copy = self.new_tmp()
                    self.emit(f"  {val_copy} = call %Box* @box_deep_copy(%Box* {val_box})")
                    self.emit(f"  call void @collection_set(%Box* {coll_box}, %Box* {idx_box}, %Box* {val_copy})")
                    return False
                elif node.method == "add" and len(node.args) == 1:
                    # For indexed link, add is not well-defined (it appends to collection)
                    # But we can treat it as set for the element
                    # Actually, per spec, add on a collection element doesn't make sense
                    # For now, let's fall through and let it error or handle as set
                    pass
            
            if node.method == "set" and isinstance(node.obj, (FnCall, MethodCall)):
                self.emit_collection_set(node)
                return False
            if node.method == "add" and isinstance(node.obj, (FnCall, MethodCall)):
                self.emit_collection_add(node)
                return False
            # String mutation: s.set(idx, char) / s.insert(idx, str) / s.replace(old, new)
            # These return a new string that must be stored back to the variable.
            if node.method in ("set", "insert", "replace") and isinstance(node.obj, Var):
                obj_t = self._infer_type(node.obj)
                if obj_t == "i8*":
                    obj_val, _ = self.emit_expr(node.obj)
                    result, _ = self.emit_string_method(obj_val, node.method, node.args)
                    ptr_str, _ = self.get_var_ptr(node.obj.name)
                    self.emit(f"  store i8* {result}, i8** {ptr_str}")
                    # BUGFIX (found reviewing user code): .set()/.insert()/
                    # .replace() always produce a real, non-null buffer (even
                    # an empty one) — but this mutation path never updates
                    # self.null_valued, so a variable that started as Null
                    # still shows "Null" from print()/`as str` afterward
                    # (both keyed off that compile-time set), even though
                    # its actual value is now a real string. Confirmed:
                    # `let mut n: str = Null; n.set(0, "x"); print(n)`
                    # printed "Null" despite n == Null correctly being False.
                    self.null_valued.discard(node.obj.name)
                    return False
            self.emit_method_call_expr(node)
        elif isinstance(node, ElementDrop):
            self.emit_element_drop(node)
        elif isinstance(node, Drop):
            self.dropped_vars.add(node.name)
            # Look for variable in the innermost scope first
            ir_t = None
            for scope in reversed(self.local_vars_stack):
                if node.name in scope:
                    ir_t = scope[node.name]
                    break
            if ir_t is None:
                ir_t = self.global_vars.get(node.name)
            if ir_t:
                ptr_str = f"%ptr_{node.name}" if any(node.name in scope for scope in self.local_vars_stack) else f"@_var_{node.name}" if node.name in {"pow", "sin", "cos", "tan", "sqrt", "log", "log10", "exp", "fabs", "floor", "ceil", "round"} else f"@{node.name}"
                val = self.new_tmp()
                self.emit(f"  {val} = load {ir_t}, {ir_t}* {ptr_str}")
                # BUG-3: an explicit .drop() frees the value NOW, so the arena
                # must forget it — otherwise the block-end release would free
                # the same allocation a second time.
                val = self._escape_temp(val, ir_t)
                if ir_t in ("%Box*", "i8*"):
                    # BUG (found via syntax sweep): dropping the SAME variable
                    # a SECOND time used to call box_drop()/free() again on
                    # the already-freed pointer — a real double-free /
                    # use-after-free, confirmed under AddressSanitizer (a
                    # `str` double-drop SEGV'd inside the allocator; a `list`
                    # double-drop aborted with "double free detected"). Spec
                    # already documents "Accessing dropped memory" as a
                    # runtime error, so raise THAT (catchable via try/error)
                    # instead of corrupting the heap: null the slot right
                    # after freeing, and treat a null slot on entry as
                    # already-dropped rather than re-freeing it.
                    is_dropped = self.new_tmp()
                    ok_l = self.new_label("dropok")
                    err_l = self.new_label("dropped_err")
                    err_lbl, err_len = self.intern_str(f"Accessing dropped memory: '{node.name}'")
                    err_ptr = self.new_tmp()
                    self.emit(f"  {is_dropped} = icmp eq {ir_t} {val}, null")
                    self.emit(f"  br i1 {is_dropped}, label %{err_l}, label %{ok_l}")
                    self.emit(f"{err_l}:")
                    self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
                    self._emit_raise_or_propagate(err_ptr)
                    self.emit(f"{ok_l}:")
                    if ir_t == "%Box*": self.emit(f"  call void @box_drop(%Box* {val})")
                    else: self.emit(f"  call void @free(i8* {val})")
                    self.emit(f"  store {ir_t} null, {ir_t}* {ptr_str}")
        elif isinstance(node, Break):
            # BUG (found via syntax sweep): `break` outside any loop used to
            # silently emit NOTHING (the `if self.loop_end_stack:` guard just
            # skipped it) while still telling emit_body this statement
            # terminated the block (the unconditional `return True` below).
            # That left the current LLVM basic block with no terminator at
            # all — invalid IR, so the whole compile failed with an opaque
            # clang-level error ("expected instruction opcode") instead of a
            # clean Rubidium compile error. The debugger already treats this
            # as invalid (an unhandled `_Break` when there's no enclosing
            # loop); the compiler should reject it just as cleanly, and
            # before ever reaching codegen for it.
            if not self.loop_end_stack:
                raise RubidiumTypeError("'break' used outside of a loop")
            self.emit(f"  br label %{self.loop_end_stack[-1]}")
            return True
        elif isinstance(node, Continue):
            # Same reasoning as Break above.
            if not self.loop_cond_stack:
                raise RubidiumTypeError("'continue' used outside of a loop")
            # Continue: branch to the loop condition (skip to end of current iteration)
            self.emit(f"  br label %{self.loop_cond_stack[-1]}")
            return False
        elif isinstance(node, Try): self.emit_try(node)
        elif isinstance(node, ThreadCall):
            self.emit_call_expr(FnCall("thread", [node.func_call, node.thread_id]))
        elif isinstance(node, ThreadWait):
            for texpr in node.thread_ids:
                tid_v, tid_t = self.emit_expr(texpr)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
        elif isinstance(node, ThreadRunning):
            self.emit_thread_running_stmt(node)
#         elif isinstance(node, FileWrite): self.emit_file_write(node)
        elif isinstance(node, FileHandleStmt):
            self.emit_file_handle_method(node.var_name, node.method, node.args)
        elif isinstance(node, FileOpen): self.emit_file_open(node)
        elif isinstance(node, FileExists): self.emit_file_exists(node)
        elif isinstance(node, FileDelete): self.emit_file_delete(node)
        elif isinstance(node, FileRename): self.emit_file_rename(node)
        elif isinstance(node, FileCopy): self.emit_file_copy(node)
        elif isinstance(node, FileList): self.emit_file_list(node)
        elif isinstance(node, FileNew): self.emit_file_new(node)
        elif isinstance(node, OsStart):
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            self.emit(f"  call void @os_start(i64 {id_v})")
        elif isinstance(node, OsRun):
            self.emit_os_run(node)
        elif isinstance(node, OsDrop):
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            self.emit(f"  call void @os_terminal_drop(i64 {id_v})")
        elif isinstance(node, FFIBind):
            self.emit_ffi_bind(node)
        elif isinstance(node, Use):
            pass  # module activation — tracked at parse time, no IR needed
        return False

    def emit_element_drop(self, node):
        """items(1).drop() — remove element/key at the given index/key and
        shift, per spec (does NOT replace with Null, unlike .set(Null))."""
        access_node = self._normalize_ns_access(node.access_node)        # BUG-21
        keys = []
        curr = access_node
        while isinstance(curr, (FnCall, MethodCall)):
            if curr.args:
                keys = curr.args + keys
            curr = curr.obj if isinstance(curr, MethodCall) else curr.name

        self._check_collection_mutable(curr)
        if isinstance(curr, Var) and self.is_class_field(curr.name):
            col_v, col_t = self.emit_field_access(Var("__self"), curr.name)
        elif isinstance(curr, str) and self.is_class_field(curr):
            col_v, col_t = self.emit_field_access(Var("__self"), curr)
        elif isinstance(curr, str):
            col_v, col_t = self.emit_expr(Var(curr))
        else:
            col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)

        for i in range(len(keys) - 1):
            k_v, k_t = self.emit_expr(keys[i])
            k_b = self.coerce_to_box(k_v, k_t)
            next_col = self.new_tmp()
            self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {k_b})")
            col_b = next_col

        last_key = keys[-1]
        k_v, k_t = self.emit_expr(last_key)
        k_b = self.coerce_to_box(k_v, k_t)
        self.emit(f"  call void @collection_drop(%Box* {col_b}, %Box* {k_b})")

    def _check_collection_mutable(self, curr):
        """Per spec (NOTES/Collections): 'Collections require mut to be modified'.
        `curr` is the resolved base of a chained collection access (a Var, a plain
        variable-name string, or an inner expression node for class-field/nested
        cases, which are checked elsewhere — e.g. BUG-018 for class fields)."""
        name = curr.name if isinstance(curr, Var) else (curr if isinstance(curr, str) else None)
        if name is not None and not self.is_class_field(name):
            if name not in self.mutable_vars:
                raise RubidiumTypeError(f"Cannot modify '{name}': collection is not declared 'mut'")

    def _check_element_type(self, base_var_name, val_b):
        """Check if the value being added to a collection matches the declared element type (bugs.log #2)."""
        if base_var_name and base_var_name in self.element_types:
            expected_type = self.element_types[base_var_name]
            # Get the actual type of the value being added
            # We need to unbox and check the type at runtime
            # For now, we'll emit a runtime check
            self._emit_element_type_check(base_var_name, expected_type, val_b)

    def _emit_element_type_check(self, base_var_name, expected_type, val_b):
        """Emit IR to check if the value being added matches the expected element type."""
        # Map Rubidium element types to Box type tags
        # Box type tags: 0=int, 1=float, 2=string, 3=collection, 4=bool
        type_tag_map = {
            "i32": 0, "i64": 0, "i128": 0, "i256": 0, "i512": 0, "i1024": 0, "i2048": 0,
            "f32": 1, "f64": 1, "f128": 1, "f256": 1, "f512": 1, "f1024": 1, "f2048": 1,
            "str": 2,
            "bool": 4,
        }
        expected_tag = type_tag_map.get(expected_type)
        if expected_tag is None:
            return  # Unknown type, skip check
        
        # Get the box's type field (field 0 of Box struct)
        type_field = self.new_tmp()
        self.emit(f"  {type_field} = getelementptr inbounds %Box, %Box* {val_b}, i32 0, i32 0")
        actual_tag = self.new_tmp()
        self.emit(f"  {actual_tag} = load i32, i32* {type_field}")
        
        # Compare with expected tag
        cmp_result = self.new_tmp()
        self.emit(f"  {cmp_result} = icmp eq i32 {actual_tag}, {expected_tag}")
        
        # Branch to error or continue
        error_label = self.new_label("type_err")
        ok_label = self.new_label("type_ok")
        self.emit(f"  br i1 {cmp_result}, label %{ok_label}, label %{error_label}")
        
        # Error block: throw type error
        self.emit(f"{error_label}:")
        error_lbl, error_len = self.intern_str(f"Type error: expected {expected_type} for collection '{base_var_name}', got different type")
        error_ptr = self.new_tmp()
        self.emit(f"  {error_ptr} = getelementptr [{error_len} x i8], [{error_len} x i8]* {error_lbl}, i64 0, i64 0")
        self.emit(f"  store i8* {error_ptr}, i8** @_rub_error_msg")
        self.emit(f"  call void @rub_throw(i8* {error_ptr})")
        self.emit(f"  unreachable")
        
        # OK block: continue
        self.emit(f"{ok_label}:")
    
    def _check_os_run_error(self, res):
        """Check if os_run returned NULL (command failed); catch-or-propagate (OPEN-7)."""
        is_null = self.new_tmp()
        self.emit(f"  {is_null} = icmp eq i8* {res}, null")
        ok_l = self.new_label("osrunok")
        err_l = self.new_label("osrunerr")
        self.emit(f"  br i1 {is_null}, label %{err_l}, label %{ok_l}")
        self.emit(f"{err_l}:")
        # The error message is already in _rub_error_msg set by os_run C function
        err_ptr = self.new_tmp()
        self.emit(f"  {err_ptr} = load i8*, i8** @_rub_error_msg")
        self._emit_raise_or_propagate(err_ptr)
        self.emit(f"{ok_l}:")

    def _ns_global_name(self, node):
        """BUG-21: `helper.shared_list` names an imported module's VARIABLE, but
        with arguments it parses as a MethodCall — indistinguishable from a
        method call. Return the merged global name (`helper_shared_list`) when
        that is what this really is, else None. Functions win: a module
        function of the same name is resolved before this is consulted."""
        if not isinstance(node, MethodCall) or not isinstance(node.obj, Var):
            return None
        ns = self.import_aliases.get(node.obj.name, node.obj.name)
        merged = f"{ns}_{node.method}"
        if merged in self.functions:
            return None
        if merged in self.global_vars:
            return merged
        # Prescan (type inference) runs before global_vars is filled — fall
        # back to the names gathered up front in gen().
        if merged in getattr(self, '_all_global_names', ()):
            return merged
        return None

    def _ns_link_target(self, expr):
        """BUG-22: resolve the target of a `link` that points into an imported
        module, so cross-file links register the same way same-file ones do.

          link helper.shared_num      -> Var("helper_shared_num")
          link helper.shared_list(2)  -> FnCall("helper_shared_list", [2])

        The link machinery only recognised Var / FnCall targets, so these
        namespaced forms silently fell through to a plain copy: the linked
        name held the value at link time and never tracked later changes."""
        if isinstance(expr, FieldAccess) and isinstance(expr.obj, Var):
            ns = self.import_aliases.get(expr.obj.name, expr.obj.name)
            merged = f"{ns}_{expr.field}"
            if merged not in self.functions and (
                    merged in self.global_vars or merged in getattr(self, '_all_global_names', ())):
                return Var(merged)
        if isinstance(expr, MethodCall):
            merged = self._ns_global_name(expr)
            if merged is not None:
                return FnCall(merged, expr.args)
        return expr

    def _normalize_ns_access(self, node):
        """Rewrite `<ns>.<var>(...)` into a plain collection access on the
        merged global, so every existing read/mutate path handles a module's
        collection exactly like a local one. Without this, indexed reads
        reported "Undefined variable 'helper'" and mutations reported
        "Cannot modify 'helper'" — both because the walk that finds the base of
        a chained access stopped at the NAMESPACE (`helper`) instead of the
        variable it qualifies."""
        merged = self._ns_global_name(node)
        if merged is not None:
            return FnCall(merged, node.args)
        return node

    def _emit_slot_bounds_check(self, id_v, slot_count, kind):
        """BUG (found via syntax sweep): thread/timer/OS-session/FFI-handle
        IDs all index directly into fixed `[N]`-element C arrays. Most of
        those access sites already bounds-check (time_timer_*, os_start/
        os_run/os(id).drop, ffi_sym — all guard `0 <= id < N` before
        touching the array), but two thread-related ones did not:
        `thread(fn(), id)`'s pthread_create write into _thread_handles[id],
        and thread_result(id)'s read from _thread_results[id]. Confirmed
        under AddressSanitizer: a large out-of-range id (e.g. 1000000)
        segfaults outright; a moderately out-of-range one (e.g. 5000)
        silently corrupts whatever OTHER global happens to sit past the end
        of the array, with no crash and no diagnostic — worse than the
        crash, since it fails silently. This guard makes those two sites
        match every other slot-indexed subsystem: an out-of-range id raises
        a clean, catchable runtime error instead of touching memory outside
        the array."""
        in_range = self.new_tmp()
        ok_l = self.new_label("slotok")
        err_l = self.new_label("slotrange")
        self.emit(f"  {in_range} = icmp ult i64 {id_v}, {slot_count}")
        self.emit(f"  br i1 {in_range}, label %{ok_l}, label %{err_l}")
        self.emit(f"{err_l}:")
        err_lbl, err_len = self.intern_str(f"{kind} ID out of range (must be 0-{slot_count - 1})")
        err_ptr = self.new_tmp()
        self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
        self._emit_raise_or_propagate(err_ptr)
        self.emit(f"{ok_l}:")

    def emit_guarded_collection_get(self, col_b, key_b, err_msg="collection access error",
                                    copy=False):
        """Emit a collection_get with a null-check guard; catch-or-propagate (OPEN-7).

        copy=True (BUG-4) returns an independent deep copy of the element
        instead of the collection's own interior Box. Use it for the FINAL
        step of a value READ; leave it False while navigating toward a
        mutation (.set()/.add()/.drop()), which must reach the real object."""
        res = self.new_tmp()
        getter = "try_collection_get_copy" if copy else "try_collection_get"
        self.emit(f"  {res} = call %Box* @{getter}(%Box* {col_b}, %Box* {key_b})")
        # Check for null — null means out-of-bounds or missing key
        is_null = self.new_tmp(); ok_l = self.new_label("cgok"); err_l = self.new_label("cgerr")
        err_lbl, err_len = self.intern_str(err_msg)
        err_ptr = self.new_tmp()
        self.emit(f"  {is_null} = icmp eq %Box* {res}, null")
        self.emit(f"  br i1 {is_null}, label %{err_l}, label %{ok_l}")
        self.emit(f"{err_l}:")
        self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
        self._emit_raise_or_propagate(err_ptr)
        self.emit(f"{ok_l}:")
        return res

    def emit_collection_set(self, method_call_node):
        access_node = self._normalize_ns_access(method_call_node.obj)   # BUG-21
        val_node = method_call_node.args[0]
        
        # Handle MethodCall on class instances where method is actually a field (e.g., p.scores(0).set(99))
        if isinstance(access_node, MethodCall):
            inner = access_node
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) or p.dict_field("key", 0).set(99)
                    # inner.args holds N navigation indices; chain all but the last as
                    # collection_get, then collection_set with the final index.
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)

                    nav_args = inner.args[:-1]
                    final_idx_arg = inner.args[-1]
                    for nav in nav_args:
                        nv, nt = self.emit_expr(nav)
                        nb = self.coerce_to_box(nv, nt)
                        col_b = self.emit_guarded_collection_get(col_b, nb)

                    idx_v, idx_t = self.emit_expr(final_idx_arg)
                    idx_b = self.coerce_to_box(idx_v, idx_t)

                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(val_node)
                    val_b = self.coerce_to_box(val_v, val_t)

                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"
        
        keys = []
        curr = access_node
        while isinstance(curr, (FnCall, MethodCall)):
            if curr.args:
                keys = curr.args + keys
            curr = curr.obj if isinstance(curr, MethodCall) else curr.name

        # index(...).set(value): value must be a scalar (see
        # _check_index_values_are_scalar's docstring for the rule).
        set_base_name = curr.name if isinstance(curr, Var) else (curr if isinstance(curr, str) else None)
        self._check_index_add_value_scalar(set_base_name, val_node)

        self._check_collection_mutable(curr)
        # Handle Var that is a class field (e.g., scores inside a class method)
        if isinstance(curr, Var) and self.is_class_field(curr.name):
            col_v, col_t = self.emit_field_access(Var("__self"), curr.name)
        elif isinstance(curr, str) and self.is_class_field(curr):
            col_v, col_t = self.emit_field_access(Var("__self"), curr)
        elif isinstance(curr, str):
            col_v, col_t = self.emit_expr(Var(curr))
        else:
            col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)
        
        for i in range(len(keys) - 1):
            arg = keys[i]
            if isinstance(arg, FnCall) and isinstance(arg.name, str) and arg.name not in self.functions and arg.name not in self.global_vars and not any(arg.name in scope for scope in self.local_vars_stack):
                key_str = arg.name
                key_lbl, key_len = self.intern_str(key_str)
                key_ptr = self.new_tmp(); key_b = self.new_tmp()
                self.emit(f"  {key_ptr} = getelementptr [{key_len} x i8], [{key_len} x i8]* {key_lbl}, i64 0, i64 0")
                self.emit(f"  {key_b} = call %Box* @box_s(i8* {key_ptr})")
                next_col = self.new_tmp()
                self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {key_b})")
                col_b = next_col
                
                idx_val, idx_t = self.emit_expr(arg.args[0])
                idx_b = self.coerce_to_box(idx_val, idx_t)
                next_col2 = self.new_tmp()
                self.emit(f"  {next_col2} = call %Box* @collection_get(%Box* {col_b}, %Box* {idx_b})")
                col_b = next_col2
            else:
                k_v, k_t = self.emit_expr(arg)
                k_b = self.coerce_to_box(k_v, k_t)
                next_col = self.new_tmp()
                self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {k_b})")
                col_b = next_col

        last_arg = keys[-1]
        val_v, val_t = self.emit_expr(val_node)
        val_b = self.coerce_to_box(val_v, val_t)
        
        if isinstance(last_arg, FnCall) and isinstance(last_arg.name, str) and last_arg.name not in self.functions and last_arg.name not in self.global_vars and last_arg.name not in self.local_vars:
            key_str = last_arg.name
            key_lbl, key_len = self.intern_str(key_str)
            key_ptr = self.new_tmp(); key_b = self.new_tmp()
            self.emit(f"  {key_ptr} = getelementptr [{key_len} x i8], [{key_len} x i8]* {key_lbl}, i64 0, i64 0")
            self.emit(f"  {key_b} = call %Box* @box_s(i8* {key_ptr})")
            next_col = self.new_tmp()
            self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {key_b})")
            col_b = next_col
            
            idx_val, idx_t = self.emit_expr(last_arg.args[0])
            idx_b = self.coerce_to_box(idx_val, idx_t)
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        else:
            k_v, k_t = self.emit_expr(last_arg)
            k_b = self.coerce_to_box(k_v, k_t)
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {k_b}, %Box* {val_b})")
            
        return "0", "i64"

    def emit_collection_add(self, method_call_node):
        access_node = self._normalize_ns_access(method_call_node.obj)    # BUG-21
        val_nodes = method_call_node.args
        
        # Handle MethodCall on class instances where method is actually a field (e.g., p.scores().add(50))
        if isinstance(access_node, MethodCall):
            inner = access_node
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    if len(val_nodes) == 1:
                        val_v, val_t = self.emit_expr(val_nodes[0])
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
                    elif len(val_nodes) == 2:
                        key_v, key_t = self.emit_expr(val_nodes[0])
                        val_v, val_t = self.emit_expr(val_nodes[1])
                        key_b = self.coerce_to_box(key_v, key_t)
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
                    return "0", "i64"
        
        keys = []
        curr = access_node
        base_var_name = None
        while isinstance(curr, (FnCall, MethodCall)):
            if curr.args:
                keys = curr.args + keys
            curr = curr.obj if isinstance(curr, MethodCall) else curr.name
        # Get the base variable name for element type checking
        if isinstance(curr, Var):
            base_var_name = curr.name
        elif isinstance(curr, str):
            base_var_name = curr
        
        self._check_collection_mutable(curr)
        # Handle Var that is a class field (e.g., scores inside a class method)
        if isinstance(curr, Var) and self.is_class_field(curr.name):
            col_v, col_t = self.emit_field_access(Var("__self"), curr.name)
        elif isinstance(curr, str) and self.is_class_field(curr):
            col_v, col_t = self.emit_field_access(Var("__self"), curr)
        elif isinstance(curr, str): 
            col_v, col_t = self.emit_expr(Var(curr))
        else: 
            col_v, col_t = self.emit_expr(curr)
        col_b = self.coerce_to_box(col_v, col_t)
        
        for arg in keys:
            k_v, k_t = self.emit_expr(arg)
            k_b = self.coerce_to_box(k_v, k_t)
            next_col = self.new_tmp()
            self.emit(f"  {next_col} = call %Box* @collection_get(%Box* {col_b}, %Box* {k_b})")
            col_b = next_col
        
        if method_call_node.method == "add":
            if len(val_nodes) == 1:
                val_v, val_t = self.emit_expr(val_nodes[0])
                val_b = self.coerce_to_box(val_v, val_t)
                # Check element type constraint if declared (bugs.log #2)
                self._check_element_type(base_var_name, val_b)
                self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
            elif len(val_nodes) == 2:
                # index().add(key, value): value must be a scalar (see
                # _check_index_values_are_scalar's docstring for the rule).
                self._check_index_add_value_scalar(base_var_name, val_nodes[1])
                key_v, key_t = self.emit_expr(val_nodes[0])
                val_v, val_t = self.emit_expr(val_nodes[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                # Check element type constraint for the value (bugs.log #2)
                self._check_element_type(base_var_name, val_b)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")

        return "0", "i64"

    def emit_collection_set_on_field(self, method_call_node, field_val, field_t):
        """Handle p.scores(0).set(99) - collection set on a class field value."""
        # The method_call_node.obj is a FieldAccess, and we already have the field value
        # The args are the index/key and value
        val_node = method_call_node.args[0]
        
        # For p.scores(0).set(99), the args are [0, 99]
        # We need to get the field value, then do collection_set
        col_b = self.coerce_to_box(field_val, field_t)
        
        # Get the index from the first arg
        idx_v, idx_t = self.emit_expr(method_call_node.args[0])
        idx_b = self.coerce_to_box(idx_v, idx_t)
        
        val_v, val_t = self.emit_expr(val_node)
        val_b = self.coerce_to_box(val_v, val_t)
        
        self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_set_on_field_nested(self, outer_method_call, field_val, field_t):
        """Handle p.scores(0).set(99) where p.scores(0) is parsed as MethodCall(Var('p'), 'scores', [0])."""
        # outer_method_call is MethodCall(Var('p'), 'scores', [MethodCall(Var('p'), 0, [99])])
        # Actually it's MethodCall(Var('p'), 'scores', [MethodCall(Var('p'), 0, [99])])
        # Wait, let me re-examine: p.scores(0).set(99)
        # This is parsed as: MethodCall(MethodCall(Var('p'), 'scores', [Number(0)]), 'set', [Number(99)])
        # So outer_method_call.obj is MethodCall(Var('p'), 'scores', [Number(0)])
        # And outer_method_call.args is [Number(99)]
        
        # We already have field_val and field_t from the field access
        col_b = self.coerce_to_box(field_val, field_t)
        
        # The inner MethodCall has the index
        inner = outer_method_call.args[0] if outer_method_call.args else None
        if isinstance(inner, MethodCall):
            # inner.args[0] is the index
            idx_v, idx_t = self.emit_expr(inner.args[0])
            idx_b = self.coerce_to_box(idx_v, idx_t)
            
            val_v, val_t = self.emit_expr(outer_method_call.args[0] if len(outer_method_call.args) > 1 else Number(0))
            val_b = self.coerce_to_box(val_v, val_t)
            
            self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_add_on_field(self, method_call_node, field_val, field_t):
        """Handle p.scores().add(50) - collection add on a class field value."""
        col_b = self.coerce_to_box(field_val, field_t)
        
        if len(method_call_node.args) == 1:
            val_v, val_t = self.emit_expr(method_call_node.args[0])
            val_b = self.coerce_to_box(val_v, val_t)
            self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
        elif len(method_call_node.args) == 2:
            key_v, key_t = self.emit_expr(method_call_node.args[0])
            val_v, val_t = self.emit_expr(method_call_node.args[1])
            key_b = self.coerce_to_box(key_v, key_t)
            val_b = self.coerce_to_box(val_v, val_t)
            self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_collection_add_on_field_nested(self, outer_method_call, field_val, field_t):
        """Handle p.scores().add(50) where p.scores() is parsed as MethodCall(Var('p'), 'scores', [])."""
        col_b = self.coerce_to_box(field_val, field_t)
        
        inner = outer_method_call.args[0] if outer_method_call.args else None
        if isinstance(inner, MethodCall):
            # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
            # outer_method_call.args[0] is the add MethodCall
            # Actually: p.scores().add(50) is MethodCall(MethodCall(Var('p'), 'scores', []), 'add', [Number(50)])
            # So outer_method_call.args is [Number(50)]
            if len(outer_method_call.args) == 1:
                val_v, val_t = self.emit_expr(outer_method_call.args[0])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
            elif len(outer_method_call.args) == 2:
                key_v, key_t = self.emit_expr(outer_method_call.args[0])
                val_v, val_t = self.emit_expr(outer_method_call.args[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
        return "0", "i64"

    def emit_class_init(self, ptr_str, class_name, init_args=None):
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)
        size_ptr, size_int, raw_ptr, typed_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {typed_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {typed_ptr}, {struct_t}** {ptr_str}")
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype) if field.vtype else self._infer_type(field.value)
            fptr = self.new_tmp()
            self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {typed_ptr}, i32 0, i32 {i}")
            if getattr(field, "_is_implicit_field", False):
                # Implicit field (from a non-local `let` inside a method body): the
                # declaration's value expression may reference other fields/params
                # only valid when the owning method actually runs — so zero-init
                # here instead of evaluating it now. The real value gets stored the
                # first time that VarDecl statement executes during a method call.
                zero_val = {"i1": "0", "i8*": "null", "%Box*": "null",
                            "float": "0.0", "double": "0.0"}.get(ir_t, "0" if ir_t in self._INT_IR_SET else "null")
                self.emit(f"  store {ir_t} {zero_val}, {ir_t}* {fptr}")
            else:
                val, val_t = self.emit_expr(field.value)
                val = self.coerce(val, val_t, ir_t)
                self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")
        # Call __init__ if it exists and we have init_args
        # BUGFIX (bugs.log OPEN-8): every class method is registered under
        # method_ir_name(class_name, method_name) = f"{class_name}__{method_name}".
        # For a method literally named "__init__" that's f"{class_name}____init__"
        # (4 underscores) — this used to hardcode f"{class_name}__init__" (2
        # underscores), which never matched, so __init__ was silently never called.
        init_key = self.method_ir_name(class_name, "__init__")
        if init_args and init_key in self.functions:
            mangled = init_key
            fn = self.functions[mangled]
            args_ir = [f"{struct_t}* {typed_ptr}"]
            for i, arg_node in enumerate(init_args):
                v, t = self.emit_expr(arg_node)
                if i + 1 < len(fn.params):
                    expected_t = self.rubi_type_to_ir(fn.params[i + 1][1])
                    v = self.coerce(v, t, expected_t)
                    args_ir.append(f"{expected_t} {v}")
                else:
                    args_ir.append(f"{t} {v}")
            self.emit(f"  call i64 @{mangled}({', '.join(args_ir)})")

    def emit_class_copy(self, ptr_str, class_name, src_var_name):
        """Deep copy a class instance into ptr_str. Allocates a new struct and copies all fields."""
        cls = self.class_defs[class_name]
        struct_t = self.class_ir_type(class_name)
        src_ptr_ptr, _ = self.get_var_ptr(src_var_name)
        src_ptr = self.new_tmp()
        self.emit(f"  {src_ptr} = load {struct_t}*, {struct_t}** {src_ptr_ptr}")
        size_ptr, size_int, raw_ptr, dst_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
        self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
        self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
        self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
        self.emit(f"  {dst_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
        self.emit(f"  store {struct_t}* {dst_ptr}, {struct_t}** {ptr_str}")
        for i, field in enumerate(cls.fields):
            ir_t = self.rubi_type_to_ir(field.vtype) if field.vtype else self._infer_type(field.value)
            src_fptr, dst_fptr, fval = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {src_fptr} = getelementptr {struct_t}, {struct_t}* {src_ptr}, i32 0, i32 {i}")
            self.emit(f"  {fval} = load {ir_t}, {ir_t}* {src_fptr}")
            if ir_t == "%Box*":
                copied = self.new_tmp()
                self.emit(f"  {copied} = call %Box* @box_deep_copy(%Box* {fval})")
                fval = copied
            self.emit(f"  {dst_fptr} = getelementptr {struct_t}, {struct_t}* {dst_ptr}, i32 0, i32 {i}")
            self.emit(f"  store {ir_t} {fval}, {ir_t}* {dst_fptr}")

    def emit_field_assign(self, node):
        obj_name = node.obj.name if hasattr(node.obj, 'name') else node.obj
        # BUG-23: `helper.shared_num = 42` — assigning to an imported module's
        # variable. `helper` is a namespace, not a class instance, so this fell
        # straight through the `not in self.instances` return below and the
        # assignment was SILENTLY DISCARDED: no error, no write, the module's
        # value simply never changed. Route it through the normal assignment
        # path on the merged global, which brings the mutability check,
        # deep-copy semantics and link-unlinking with it.
        if obj_name not in self.instances and isinstance(node.obj, Var):
            ns = self.import_aliases.get(obj_name, obj_name)
            merged = f"{ns}_{node.field}"
            if merged not in self.functions and (
                    merged in self.global_vars or merged in getattr(self, '_all_global_names', ())):
                self.emit_stmt(Assign(merged, node.value))
                return
        if obj_name not in self.instances: return
        class_name = self.instances[obj_name]

        # Mutation rules (see syntax CLASSES section):
        # - If the instance itself is not declared 'mut', nothing can be changed.
        # - If the instance is 'mut', only fields declared 'mut' inside the class can change.
        if obj_name != "__self" and obj_name not in self.mutable_vars:
            raise RubidiumTypeError(f"Cannot assign to field '{node.field}': instance '{obj_name}' is not mutable")
        cls = self.class_defs[class_name]
        field_def = next((f for f in cls.fields if f.name == node.field), None)
        if field_def is not None and not field_def.mutable:
            raise RubidiumTypeError(f"Cannot assign to field '{node.field}': field is not declared 'mut' in class '{class_name}'")

        idx, ir_t  = self.field_index(class_name, node.field)
        struct_t   = self.class_ir_type(class_name)
        
        ptr_str, _ = self.get_var_ptr(obj_name)
        inst_ptr = self.new_tmp(); fptr = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        val, val_t = self.emit_expr(node.value)
        val = self.coerce(val, val_t, ir_t)
        self.emit(f"  store {ir_t} {val}, {ir_t}* {fptr}")

    def _emit_bignum_ptr(self, val, val_t):
        """OPEN-6: store an i256/i512/i1024/i2048 SSA value to a fresh alloca
        and bitcast the pointer to i64* — the calling convention shared by
        print_bignum_or_null/bignum_to_str, which read the raw bits directly
        as a little-endian array of 64-bit limbs (matches how x86 lays out a
        wide integer in memory, so no repacking is needed). Returns
        (i64ptr_ssa, num_limbs)."""
        bits = int(val_t[1:])
        n = bits // 64
        slot = self.new_tmp()
        self.emit(f"  {slot} = alloca {val_t}")
        self.emit(f"  store {val_t} {val}, {val_t}* {slot}")
        ptr64 = self.new_tmp()
        self.emit(f"  {ptr64} = bitcast {val_t}* {slot} to i64*")
        return ptr64, n

    def _emit_fp128_halves(self, val):
        """Float-precision fix: store an fp128 SSA value to a fresh alloca and
        read back its raw 128 bits as two i64 halves (lo, hi) — the calling
        convention shared by print_fp128_exact/fp128_to_exact_decimal_str,
        which extract the IEEE-754 binary128 sign/exponent/mantissa directly
        from those bits (no double intermediate, so no precision is lost).
        Returns (lo_ssa, hi_ssa)."""
        slot = self.new_tmp()
        self.emit(f"  {slot} = alloca fp128")
        self.emit(f"  store fp128 {val}, fp128* {slot}")
        ptr64 = self.new_tmp()
        self.emit(f"  {ptr64} = bitcast fp128* {slot} to i64*")
        lo = self.new_tmp()
        self.emit(f"  {lo} = load i64, i64* {ptr64}")
        hi_ptr = self.new_tmp()
        self.emit(f"  {hi_ptr} = getelementptr i64, i64* {ptr64}, i64 1")
        hi = self.new_tmp()
        self.emit(f"  {hi} = load i64, i64* {hi_ptr}")
        return lo, hi

    def emit_print(self, value):
         # If printing a dropped variable, emit error message only
        if isinstance(value, Var) and value.name in self.dropped_vars:
            err_lbl, err_len = self.intern_str("ERROR: variable does not exist\\n")
            ptr = self.new_tmp()
            self.emit(f"  {ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
            self.emit(f"  call i32 (i8*, ...) @printf(i8* {ptr})")
            return
        # OPEN-4 (scalar Null): only a value the compiler KNOWS is an explicit
        # Null prints as "Null". A raw scalar that merely computed/clamped to the
        # type's minimum prints its real number (handled by the plain-int print
        # paths below), per the rule "a bottom-limit value is just the bottom
        # limit, not Null". Boxed Null (type==6) still prints "Null" via
        # print_boxed and is unaffected by this scalar-only path.
        if self._is_known_null(value):
            self._emit_print_null_literal()
            return
        # bugs.log OPEN-J: a runtime-tagged scalar local/global (one whose
        # Null-ness the compiler can't prove at compile time — e.g. set on
        # only one branch of an if/else) needs a REAL runtime check here,
        # not just the compile-time _is_known_null() above. Untagged names
        # (params/class fields/loop vars — Phase 2) fall through unchanged.
        if isinstance(value, Var):
            flag_ptr = self.get_null_flag_ptr(value.name)
            if flag_ptr is not None:
                flag = self.new_tmp()
                self.emit(f"  {flag} = load i1, i1* {flag_ptr}")
                null_l, real_l, done_l = self.new_label("pnull"), self.new_label("pnum"), self.new_label("pdone")
                self.emit(f"  br i1 {flag}, label %{null_l}, label %{real_l}")
                self.emit(f"{null_l}:")
                self._emit_print_null_literal()
                self.emit(f"  br label %{done_l}")
                self.emit(f"{real_l}:")
                self._emit_print_value(value)
                self.emit(f"  br label %{done_l}")
                self.emit(f"{done_l}:")
                return
        self._emit_print_value(value)

    def _emit_print_null_literal(self):
        nl_lbl, nl_len = self.intern_str("Null\n")
        nptr = self.new_tmp()
        self.emit(f"  {nptr} = getelementptr [{nl_len} x i8], [{nl_len} x i8]* {nl_lbl}, i64 0, i64 0")
        self.emit(f"  call i32 (i8*, ...) @printf(i8* {nptr})")
        self.emit(f"  call i32 @fflush(i8* null)")

    def _emit_print_value(self, value):
        val, val_t = self.emit_expr(value)
        if val_t == "%Box*":
            self.emit(f"  call void @print_boxed(%Box* {val})")  # print_boxed already calls fflush
        elif val_t == "i1":
            # Print True or False
            true_lbl, true_len = self.intern_str("True\n")
            false_lbl, false_len = self.intern_str("False\n")
            true_ptr = self.new_tmp(); false_ptr = self.new_tmp(); sel_ptr = self.new_tmp()
            self.emit(f"  {true_ptr} = getelementptr [{true_len} x i8], [{true_len} x i8]* {true_lbl}, i64 0, i64 0")
            self.emit(f"  {false_ptr} = getelementptr [{false_len} x i8], [{false_len} x i8]* {false_lbl}, i64 0, i64 0")
            self.emit(f"  {sel_ptr} = select i1 {val}, i8* {true_ptr}, i8* {false_ptr}")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {sel_ptr})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "i128":
            # BUGFIX (bugs.log #17): was narrowed to i64 first (clamping
            # anything outside i64's range), so print() couldn't verify
            # correctly-computed i128 arithmetic beyond 64 bits.
            # OPEN-4: plain (no sentinel check) — an explicit Null was already
            # handled above; a raw min value prints as its real number.
            self.emit(f'  call void @print_i128_plain(i128 {val})')
        elif val_t in ("i256", "i512", "i1024", "i2048"):
            # OPEN-6: true bignum printing — was narrowed to i64 (clamping
            # anything outside i64's range) before this fix.
            # OPEN-4: plain (no sentinel check) — see i128 note above.
            ptr64, n = self._emit_bignum_ptr(val, val_t)
            self.emit(f'  call void @print_bignum_plain(i64* {ptr64}, i32 {n})')
        elif val_t in ("i32", "i64"):
            # OPEN-4: plain number print (no sentinel/Null check) — an explicit
            # Null was already handled above; a value at the type minimum prints
            # as its real number, not "Null".
            cv = self.coerce(val, val_t, "i64")
            self.emit(f'  call void @print_int_plain(i64 {cv})')
        elif val_t == "fp128":
            # Float-precision fix: fp128 (f128/f256/.../f2048's IR type — see
            # a typed math block `(expr): TYPE`) used to be narrowed to
            # double and printed with printf's default 6-sig-fig "%g",
            # throwing away all the extra precision that was actually
            # computed. Print the exact decimal value directly from the raw
            # 128-bit bit pattern instead (no double intermediate).
            lo, hi = self._emit_fp128_halves(val)
            self.emit(f'  call void @print_fp128_exact(i64 {lo}, i64 {hi})')
        elif val_t in ("float", "double"):
            fmt, flen = self.intern_str("%g\n")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            dv = self.coerce(val, val_t, "double")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, double {dv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "i8*":
            self.emit(f'  call i32 @puts(i8* {val})')
            self.emit(f'  call i32 @fflush(i8* null)')

    def emit_println(self, value):
        # println prints without newline; subsequent calls overwrite same
        # line via \r.
        # BUGFIX: \r alone only moves the cursor back to column 0 — it does
        # NOT erase anything already on that terminal line. When a println
        # call's text is SHORTER than the previous call's (e.g. the first
        # frame of a new typewriter loop, right after the long final frame
        # of the previous one), the previous line's leftover trailing
        # characters stayed on screen, visually overlapping with the new,
        # shorter text instead of being replaced by it — confirmed garbled
        # output at exactly that kind of loop boundary. Append the ANSI
        # "clear from cursor to end of line" sequence (\x1b[K) after the
        # text so every call fully replaces the previous one, matching the
        # spec's own description: "Calling it again replaces the previous
        # output."
        val, val_t = self.emit_expr(value)
        if val_t in ("i32", "i64", "i1", "i128", "i256", "i512", "i1024", "i2048"):
            fmt, flen = self.intern_str("\r%lld\x1b[K")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            cv = self.coerce(val, val_t, "i64")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i64 {cv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t in ("float", "double", "fp128"):
            fmt, flen = self.intern_str("\r%g\x1b[K")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            dv = self.coerce(val, val_t, "double")
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, double {dv})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "i8*":
            fmt, flen = self.intern_str("\r%s\x1b[K")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i8* {val})')
            self.emit(f'  call i32 @fflush(i8* null)')
        elif val_t == "%Box*":
            # Box printing for println — use printf with \r instead of puts
            fmt, flen = self.intern_str("\r<value>\x1b[K")
            ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
            self.emit(f'  call i32 @printf(i8* {ptr})')
            self.emit(f'  call i32 @fflush(i8* null)')

    def to_bool(self, val, t):
        if t == "i1": return val
        tmp = self.new_tmp()
        if t in ("float","double"): self.emit(f"  {tmp} = fcmp une {t} {val}, 0.0")
        elif t == "i8*": self.emit(f"  {tmp} = icmp ne i8* {val}, null")
        elif t == "%Box*":
            c_int = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = icmp ne i64 {c_int}, 0")
        else: self.emit(f"  {tmp} = icmp ne {t} {val}, 0")
        return tmp

    def emit_if(self, node):
        cond, ct = self.emit_expr(node.cond); cond = self.to_bool(cond, ct)
        then_l, else_l, end_l = self.new_label("then"), self.new_label("else"), self.new_label("endif")
        self.emit(f"  br i1 {cond}, label %{then_l}, label %{else_l}")
        self.emit(f"{then_l}:")
        if not self.emit_body(node.then_body): self.emit(f"  br label %{end_l}")
        self.emit(f"{else_l}:")
        if not (node.else_body and self.emit_body(node.else_body)): self.emit(f"  br label %{end_l}")
        self.emit(f"{end_l}:")

    def emit_while(self, node):
        cond_l, body_l, end_l = self.new_label("wcond"), self.new_label("wbody"), self.new_label("wend")
        # BUG-3: the CONDITION is re-evaluated every iteration but lives in the
        # enclosing block, so a condition that reads a collection
        # (`while items(0) < n`) would pile up one temporary per iteration.
        # Release back to this mark at the top of each condition evaluation;
        # the body has its own (higher) mark, so nothing the body still needs
        # is ever in range.
        loop_mark = self._emit_temp_mark()
        self.emit(f"  br label %{cond_l}\n{cond_l}:")
        self._emit_temp_release(loop_mark)
        cond, ct = self.emit_expr(node.cond); cond = self.to_bool(cond, ct)
        self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
        self.loop_end_stack.append(end_l)
        self.loop_cond_stack.append(cond_l)
        self.emit_body(node.body)
        self.loop_cond_stack.pop()
        self.loop_end_stack.pop()
        self.emit(f"  br label %{cond_l}\n{end_l}:")

    def emit_for(self, node):
        if node.iterable:
            # Check if the iterable is a file handle var
            if isinstance(node.iterable, Var) and node.iterable.name in self._file_handle_vars:
                self._emit_for_file(node)
                return
            iter_v, iter_t = self.emit_expr(node.iterable)

            # Integer iterable: for i in N → loop from 1 to N (inclusive)
            if iter_t in self._INT_IR_SET:
                iter_v_64 = self.coerce(iter_v, iter_t, "i64")
                # BUGFIX (bugs.log #5): if node.var was also used elsewhere with a
                # different underlying type (another `let`/for-loop), keep the loop
                # mechanics on a hidden i64 counter and box the value into the
                # user-visible %Box* variable each iteration, instead of reusing a
                # stale/mistyped alloca for node.var directly (previously corrupted
                # memory / segfaulted).
                is_poly = node.var in getattr(self, "_local_polymorphic", ())
                if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                    if is_poly:
                        ctr_ptr = f"%ptr_{node.var}_ctr_{self.new_tmp()[1:]}"
                        self.emit(f"  {ctr_ptr} = alloca i64")
                        var_ptr = f"%ptr_{node.var}"
                        if node.var not in self._alloca_emitted:
                            self.emit(f"  {var_ptr} = alloca %Box*")
                            self._alloca_emitted.add(node.var)
                        self.local_vars_stack[-1][node.var] = "%Box*"
                    else:
                        ctr_ptr = f"%ptr_{node.var}"
                        if node.var not in self._alloca_emitted:
                            self.emit(f"  {ctr_ptr} = alloca i64")
                            self._alloca_emitted.add(node.var)
                        self.local_vars_stack[-1][node.var] = "i64"
                else:
                    self.declare_global(node.var, "i64")
                    ctr_ptr = f"@{node.var}"
                self.emit(f"  store i64 0, i64* {ctr_ptr}")
                cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
                self.emit(f"  br label %{cond_l}\n{cond_l}:")
                cur, cond = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur} = load i64, i64* {ctr_ptr}\n  {cond} = icmp slt i64 {cur}, {iter_v_64}")
                self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
                if is_poly:
                    cur_v = self.new_tmp()
                    self.emit(f"  {cur_v} = load i64, i64* {ctr_ptr}")
                    boxed = self.coerce_to_box(cur_v, "i64")
                    self.emit(f"  store %Box* {boxed}, %Box** {var_ptr}")
                self.loop_end_stack.append(end_l)
                self.loop_cond_stack.append(cont_l)
                self.emit_body(node.body)
                self.loop_cond_stack.pop()
                self.loop_end_stack.pop()
                self.emit(f"  br label %{cont_l}\n{cont_l}:")
                inc, cur2 = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur2} = load i64, i64* {ctr_ptr}\n  {inc} = add i64 {cur2}, 1\n  store i64 {inc}, i64* {ctr_ptr}")
                self.emit(f"  br label %{cond_l}\n{end_l}:")
                return

            iter_b = self.coerce_to_box(iter_v, iter_t)
            
            idx_ptr = self.new_tmp()
            self.emit(f"  {idx_ptr} = alloca i32")
            self.emit(f"  store i32 0, i32* {idx_ptr}")
            
            item_t = "%Box*"
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                var_ptr = f"%ptr_{node.var}"
                if node.var not in self._alloca_emitted:
                    self.local_vars_stack[-1][node.var] = item_t
                    self.emit(f"  {var_ptr} = alloca {item_t}")
                    self._alloca_emitted.add(node.var)
                else:
                    self.local_vars_stack[-1][node.var] = item_t
                    # Already alloca'd — just update the type in case it changed
                    self.local_vars_stack[-1][node.var] = item_t
            else:
                self.declare_global(node.var, item_t)
                var_ptr = f"@{node.var}"
                
            len_val = self.new_tmp()
            self.emit(f"  {len_val} = call i32 @collection_len(%Box* {iter_b})")
            
            cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            
            cur_idx, cond = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur_idx} = load i32, i32* {idx_ptr}")
            self.emit(f"  {cond} = icmp slt i32 {cur_idx}, {len_val}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            
            item_val = self.new_tmp()
            item_copy = self.new_tmp()
            self.emit(f"  {item_val} = call %Box* @collection_get_at(%Box* {iter_b}, i32 {cur_idx})")
            self.emit(f"  {item_copy} = call %Box* @box_copy(%Box* {item_val})")
            self.emit(f"  store %Box* {item_copy}, %Box** {var_ptr}")
            
            self.loop_end_stack.append(end_l)
            self.loop_cond_stack.append(cont_l)
            self.emit_body(node.body)
            self.loop_cond_stack.pop()
            self.loop_end_stack.pop()
            
            # Continue target: drop loop variable, increment, and go to condition
            self.emit(f"  br label %{cont_l}\n{cont_l}:")
            drop_val = self.new_tmp()
            self.emit(f"  {drop_val} = load %Box*, %Box** {var_ptr}")
            self.emit(f"  call void @box_drop(%Box* {drop_val})")
            inc_idx = self.new_tmp()
            self.emit(f"  {inc_idx} = add i32 {cur_idx}, 1")
            self.emit(f"  store i32 {inc_idx}, i32* {idx_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")
            
        else:
            sv, st = self.emit_expr(node.start); ev, et = self.emit_expr(node.end)
            sv = self.coerce(sv, st, "i64"); ev = self.coerce(ev, et, "i64")

            # BUGFIX (bugs.log #5): same fix as the integer-iterable branch above —
            # keep loop mechanics on a hidden i64 counter and box into node.var's
            # own %Box* alloca each iteration if the name is polymorphic (reused
            # elsewhere with a different type), instead of reusing a stale alloca.
            is_poly = node.var in getattr(self, "_local_polymorphic", ())
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                if is_poly:
                    ctr_ptr = f"%ptr_{node.var}_ctr_{self.new_tmp()[1:]}"
                    self.emit(f"  {ctr_ptr} = alloca i64")
                    var_ptr = f"%ptr_{node.var}"
                    if node.var not in self._alloca_emitted:
                        self.emit(f"  {var_ptr} = alloca %Box*")
                        self._alloca_emitted.add(node.var)
                    self.local_vars_stack[-1][node.var] = "%Box*"
                else:
                    ctr_ptr = f"%ptr_{node.var}"
                    if node.var not in self._alloca_emitted:
                        self.emit(f"  {ctr_ptr} = alloca i64")
                        self._alloca_emitted.add(node.var)
                    self.local_vars_stack[-1][node.var] = "i64"
            else:
                self.declare_global(node.var, "i64")
                ctr_ptr = f"@{node.var}"

            # Determine direction at codegen time: is_up = (start < end)
            is_up = self.new_tmp()
            self.emit(f"  {is_up} = icmp slt i64 {sv}, {ev}")
            self.emit(f"  store i64 {sv}, i64* {ctr_ptr}")
            cond_l, body_l, cont_l, end_l = self.new_label("fcond"), self.new_label("fbody"), self.new_label("fcont"), self.new_label("fend")
            self.emit(f"  br label %{cond_l}\n{cond_l}:")
            cur, cur_lt, cur_gt, cond = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {cur} = load i64, i64* {ctr_ptr}")
            self.emit(f"  {cur_lt} = icmp slt i64 {cur}, {ev}")
            self.emit(f"  {cur_gt} = icmp sgt i64 {cur}, {ev}")
            self.emit(f"  {cond} = select i1 {is_up}, i1 {cur_lt}, i1 {cur_gt}")
            self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
            if is_poly:
                cur_v = self.new_tmp()
                self.emit(f"  {cur_v} = load i64, i64* {ctr_ptr}")
                boxed = self.coerce_to_box(cur_v, "i64")
                self.emit(f"  store %Box* {boxed}, %Box** {var_ptr}")
            self.loop_end_stack.append(end_l)
            self.loop_cond_stack.append(cont_l)
            self.emit_body(node.body)
            self.loop_cond_stack.pop()
            self.loop_end_stack.pop()
            self.emit(f"  br label %{cont_l}\n{cont_l}:")
            step, inc, cur2 = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {step} = select i1 {is_up}, i64 1, i64 -1")
            self.emit(f"  {cur2} = load i64, i64* {ctr_ptr}\n  {inc} = add i64 {cur2}, {step}\n  store i64 {inc}, i64* {ctr_ptr}")
            self.emit(f"  br label %{cond_l}\n{end_l}:")

    def _emit_for_file(self, node):
        """for line in file_handle { body } — iterate file lines one-by-one."""
        slot = self._file_handle_vars[node.iterable.name]
        # BUGFIX (bugs.log #8): the loop variable used to be declared/stored
        # directly under its source name (e.g. "line") as %Box*. If that name
        # was already in use elsewhere as a *different* IR type (e.g. a plain
        # `let line = "..."` string, which is i8* not %Box*), declare_global's
        # "already registered, no-op" guard left the OLD i8* declaration in
        # place while this loop stored a %Box* pointer into it — silent type
        # confusion, producing garbage on print (readers saw the Box struct's
        # raw bytes instead of a string). Fix: always give the loop variable
        # its own uniquely-named %Box* storage, and alias the user-facing name
        # to it (via the same linked_to mechanism used for scalar `link`) only
        # for the loop's duration, restoring the old binding afterward.
        self._file_loop_ctr = getattr(self, "_file_loop_ctr", 0) + 1
        storage_name = f"__fileline_{self._file_loop_ctr}_{node.var}"
        had_prior_link = node.var in self.linked_to
        prior_link = self.linked_to.get(node.var)
        self.linked_to[node.var] = storage_name

        if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
            var_ptr = f"%ptr_{storage_name}"
            self.local_vars_stack[-1][storage_name] = "%Box*"
            if storage_name not in self._alloca_emitted:
                self.emit(f"  {var_ptr} = alloca %Box*")
                self._alloca_emitted.add(storage_name)
        else:
            self.declare_global(storage_name, "%Box*")
            var_ptr = f"@{storage_name}"

        # Line counter (0-based — file_readln/file_writeln are 0-based;
        # bugs.log #7: this previously started at 1, silently skipping the
        # file's first line on every `for line in file` iteration)
        line_ptr = self.new_tmp()
        self.emit(f"  {line_ptr} = alloca i64")
        self.emit(f"  store i64 0, i64* {line_ptr}")

        cond_l = self.new_label("ffilecond")
        body_l = self.new_label("ffilebody")
        cont_l = self.new_label("ffilecont")
        end_l  = self.new_label("ffileend")
        self.emit(f"  br label %{cond_l}\n{cond_l}:")

        cur_line = self.new_tmp()
        line_s   = self.new_tmp()
        cond     = self.new_tmp()
        self.emit(f"  {cur_line} = load i64, i64* {line_ptr}")
        self.emit(f"  {line_s} = call i8* @file_readln(i64 {slot}, i64 {cur_line})")
        # Empty string → end of file
        empty_lbl, elen = self.intern_str("")
        empty_ptr = self.new_tmp()
        self.emit(f"  {empty_ptr} = getelementptr [{elen} x i8], [{elen} x i8]* {empty_lbl}, i64 0, i64 0")
        cmp_res = self.new_tmp()
        # Defensive: rub_strcmp_null_safe (not plain strcmp) in case
        # file_readln ever returns NULL rather than an empty string — see
        # the identical reasoning at emit_compare's i8*-vs-i8* case.
        self.emit(f"  {cmp_res} = call i32 @rub_strcmp_null_safe(i8* {line_s}, i8* {empty_ptr})")
        self.emit(f"  {cond} = icmp eq i32 {cmp_res}, 0")
        self.emit(f"  br i1 {cond}, label %{end_l}, label %{body_l}\n{body_l}:")

        # Box the line string and store as loop variable
        boxed = self.new_tmp()
        self.emit(f"  {boxed} = call %Box* @box_s(i8* {line_s})")
        self.emit(f"  store %Box* {boxed}, %Box** {var_ptr}")

        self.loop_end_stack.append(end_l)
        self.loop_cond_stack.append(cont_l)
        self.emit_body(node.body)
        self.loop_cond_stack.pop()
        self.loop_end_stack.pop()

        # Continue target: increment and go to condition
        self.emit(f"  br label %{cont_l}\n{cont_l}:")
        inc_v = self.new_tmp()
        cur2  = self.new_tmp()
        self.emit(f"  {cur2} = load i64, i64* {line_ptr}")
        self.emit(f"  {inc_v} = add i64 {cur2}, 1")
        self.emit(f"  store i64 {inc_v}, i64* {line_ptr}")
        self.emit(f"  br label %{cond_l}\n{end_l}:")

        # Restore whatever "node.var" resolved to before this loop (bugs.log #8)
        if had_prior_link:
            self.linked_to[node.var] = prior_link
        else:
            self.linked_to.pop(node.var, None)

    def emit_thread_running(self, node):
        """thread.running(id) -> bool: True if thread is still running, False if done."""
        # Lookup thread handle in _thread_handles array
        tid_v, tid_t = self.emit_expr(node.thread_id)
        tid_v = self.coerce(tid_v, tid_t, "i64")
        result = self.new_tmp()
        # pthread_tryjoin_np returns 0 (EBUSY) if thread is still running, ESRCH/0 if done
        # We call it with null retval; if it returns EBUSY (16) → still running
        # For simplicity use our runtime helper to check via non-destructive probe
        self.emit(f"  {result} = call i1 @_thread_is_running(i64 {tid_v})")
        return result, "i1"

    def emit_thread_running_stmt(self, node):
        self.emit_thread_running(node)

    def emit_file_new(self, node):
        """file.new("path") { body } - create a new file with handle and auto-close"""
        path_val, path_t = self.emit_expr(node.path_expr)
        path_s = self.coerce(path_val, path_t, "i8*")
        mode_lbl, mlen = self.intern_str("w")
        mode_ptr = self.new_tmp()
        self.emit(f"  {mode_ptr} = getelementptr [{mlen} x i8], [{mlen} x i8]* {mode_lbl}, i64 0, i64 0")
        slot = self._file_slot_counter
        self._file_slot_counter += 1
        self.emit(f"  call i64 @file_open(i64 {slot}, i8* {path_s}, i32 1)")
        handle_name = f"_file_new_{slot}"
        self._file_handle_vars["file"] = slot
        if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
            self.local_vars_stack[-1][handle_name] = "i64"
            ptr = f"%ptr_{handle_name}"
            self.emit(f"  {ptr} = alloca i64")
            self._alloca_emitted.add(handle_name)
            self.emit(f"  store i64 {slot}, i64* {ptr}")
        else:
            self.declare_global(handle_name, "i64")
            self.emit(f"  store i64 {slot}, i64* @{handle_name}")
        saved_loop = self.loop_end_stack[:]
        self.emit_body(node.body)
        self.loop_end_stack = saved_loop
        del self._file_handle_vars["file"]
        self.emit(f"  call void @file_close(i64 {slot})")

    def emit_try(self, node):
        # Rubidium try/error uses a global error buffer + explicit branch guards.
        # What IS catchable: division-by-zero, collection out-of-bounds, missing key,
        #   raise, and (OPEN-7) any of those same errors surfacing from a function
        #   called from within this try, via the cross-call propagation mechanism
        #   (see _emit_raise_or_propagate / _emit_error_propagation_check).
        # What is NOT catchable: segfaults, C-level aborts, FFI crashes.
        ok_l, err_l, end_l = self.new_label("tok"), self.new_label("terr"), self.new_label("tend")
        outer_try_label = self._try_error_label
        self._try_error_label = err_l
        self.emit(f"  br label %{ok_l}\n{ok_l}:")
        self.emit_body(node.try_body)
        self._try_error_label = outer_try_label
        self.emit(f"  br label %{end_l}\n{err_l}:")
        # OPEN-7: this try just caught the error — clear the propagation flag so
        # it isn't mistaken for still-live further up the call/lexical stack.
        self.emit(f"  store i1 0, i1* @_rub_error_flag")
        # Load the error message from the global _rub_error_msg buffer
        err_ptr = self.new_tmp()
        self.emit(f"  {err_ptr} = load i8*, i8** @_rub_error_msg")
        self.declare_global("error", "i8*")
        self.emit(f"  store i8* {err_ptr}, i8** @error")
        self.emit_body(node.error_body)
        self.emit(f"  br label %{end_l}\n{end_l}:")

    def _emit_raise_or_propagate(self, err_ptr_ssa):
        """OPEN-7: report a runtime error, given its i8* message already computed
        as `err_ptr_ssa`. Store the message + set the global propagation flag, then
        either jump straight to the nearest LEXICALLY enclosing try's error handler
        (fast path, same as before), or — if there is none in this function — jump
        to this function's error-exit block, which returns a default value so the
        caller (checked via _emit_error_propagation_check right after every call)
        can itself either catch it or keep propagating it further up."""
        self.emit(f"  store i8* {err_ptr_ssa}, i8** @_rub_error_msg")
        self.emit(f"  store i1 1, i1* @_rub_error_flag")
        target = self._try_error_label if self._try_error_label else self._fn_error_exit_label
        self.emit(f"  br label %{target}")

    def _emit_error_propagation_check(self):
        """OPEN-7: call right after emitting a `call` to a user-defined Rubidium
        function/method. If that call (transitively) hit an uncaught raise or
        division-by-zero, @_rub_error_flag is now set — branch to the nearest
        lexically enclosing try's error handler if there is one at this call site,
        else propagate further by jumping to this function's own error-exit block."""
        flag = self.new_tmp()
        self.emit(f"  {flag} = load i1, i1* @_rub_error_flag")
        cont_l = self.new_label("errchk_ok")
        target = self._try_error_label if self._try_error_label else self._fn_error_exit_label
        self.emit(f"  br i1 {flag}, label %{target}, label %{cont_l}")
        self.emit(f"{cont_l}:")

    def emit_file_write(self, node):
        path_val, path_t  = self.emit_expr(node.path_expr)
        cont_val, cont_t  = self.emit_expr(node.content_expr)
        mode_lbl, mlen = self.intern_str("w"); mode_ptr = self.new_tmp()
        self.emit(f"  {mode_ptr} = getelementptr [{mlen} x i8], [{mlen} x i8]* {mode_lbl}, i64 0, i64 0")
        fp, clen = self.new_tmp(), self.new_tmp()
        self.emit(f"  {fp} = call i8* @fopen(i8* {path_val}, i8* {mode_ptr})")
        self.emit(f"  {clen} = call i64 @strlen(i8* {cont_val})")
        self.emit(f"  call i64 @fwrite(i8* {cont_val}, i64 1, i64 {clen}, i8* {fp})")
        self.emit(f"  call i32 @fclose(i8* {fp})")

    def emit_file_open(self, node):
        """open("path") as var { body } - file handle with automatic close"""
        path_val, path_t = self.emit_expr(node.path_expr)
        path_s = self.coerce(path_val, path_t, "i8*")
        # Assign a unique slot for this open() block
        slot = self._file_slot_counter
        self._file_slot_counter += 1
        rc = self.new_tmp()
        self.emit(f"  {rc} = call i64 @file_open(i64 {slot}, i8* {path_s}, i32 0)")
        # BUGFIX (bugs.log #6): per syntax file, a missing file is reported as
        # a runtime error (catchable via try/error) but must NOT crash the
        # program when uncaught — file_open() has already auto-created it, so
        # the uncaught case just continues normally. A truly unrecoverable
        # open (rc == -1, e.g. bad path/permissions) still crashes when
        # uncaught, same as before.
        is_missing = self.new_tmp()
        self.emit(f"  {is_missing} = icmp eq i64 {rc}, -2")
        is_fatal = self.new_tmp()
        self.emit(f"  {is_fatal} = icmp eq i64 {rc}, -1")
        ok_l = self.new_label("fopenok")
        missing_l = self.new_label("fopenmissing")
        checked_l = self.new_label("fopenchecked")
        err_lbl, err_len = self.intern_str("file not found")
        err_ptr = self.new_tmp()
        self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
        self.emit(f"  br i1 {is_missing}, label %{missing_l}, label %{checked_l}")
        self.emit(f"{missing_l}:")
        self.emit(f"  store i8* {err_ptr}, i8** @_rub_error_msg")
        if self._try_error_label:
            self.emit(f"  br label %{self._try_error_label}")
        else:
            self.emit(f"  br label %{ok_l}")
        self.emit(f"{checked_l}:")
        if self._try_error_label:
            self.emit(f"  br i1 {is_fatal}, label %{self._try_error_label}, label %{ok_l}")
        else:
            # OPEN-7: a truly fatal open error now catches-or-propagates like any
            # other runtime error, instead of always calling rub_throw() directly
            # (which would ignore a try in a CALLER of this function).
            fail_l = self.new_label("fopenfatal")
            self.emit(f"  br i1 {is_fatal}, label %{fail_l}, label %{ok_l}")
            self.emit(f"{fail_l}:")
            self._emit_raise_or_propagate(err_ptr)
        self.emit(f"{ok_l}:")
        # Register this var_name as a file handle pointing to this slot
        self._file_handle_vars[node.var_name] = slot
        # Allocate the variable in local/global scope so Var(f) can be resolved
        slot_val = str(slot)
        # Check if variable already exists in any local scope
        var_exists = any(node.var_name in scope for scope in self.local_vars_stack)
        if not var_exists and node.var_name not in self.global_vars:
            if self.cur_fn is not None and self.cur_fn != "_rubidium_init":
                self.local_vars_stack[-1][node.var_name] = "i64"
                ptr = f"%ptr_{node.var_name}"
                self.emit(f"  {ptr} = alloca i64")
                self._alloca_emitted.add(node.var_name)
                self.emit(f"  store i64 {slot_val}, i64* {ptr}")
            else:
                self.declare_global(node.var_name, "i64")
                self.emit(f"  store i64 {slot_val}, i64* @{node.var_name}")
        # Emit body — method calls on node.var_name will be intercepted
        saved_loop = self.loop_end_stack[:]
        self.emit_body(node.body)
        self.loop_end_stack = saved_loop
        # Unregister the handle and auto-close
        del self._file_handle_vars[node.var_name]
        self.emit(f"  call void @file_close(i64 {slot})")

    def emit_file_handle_method(self, var_name, method, args):
        """Emit a method call on a file handle variable (inside open() block)"""
        slot = self._file_handle_vars[var_name]
        if method == "write":
            data_v, data_t = self.emit_expr(args[0])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_write_all(i64 {slot}, i8* {data_s})")
            return "0", "i64"
        elif method in ("append", "add"):
            data_v, data_t = self.emit_expr(args[0])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_append_all(i64 {slot}, i8* {data_s})")
            return "0", "i64"
        elif method == "read":
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @file_read_all(i64 {slot})")
            return result, "i8*"
        elif method == "readln":
            line_v, line_t = self.emit_expr(args[0])
            line_i = self.coerce(line_v, line_t, "i64")
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @file_readln(i64 {slot}, i64 {line_i})")
            return result, "i8*"
        elif method == "writeln":
            line_v, line_t = self.emit_expr(args[0])
            line_i = self.coerce(line_v, line_t, "i64")
            data_v, data_t = self.emit_expr(args[1])
            data_s = self.coerce(data_v, data_t, "i8*")
            self.emit(f"  call void @file_writeln(i64 {slot}, i64 {line_i}, i8* {data_s})")
            return "0", "i64"
        else:
            raise RubidiumNameError(f"Unknown file handle method '.{method}()'")



    def emit_file_exists(self, node):
        path_val, path_t = self.emit_expr(node.path_expr)
        raw = self.new_tmp()
        cmp = self.new_tmp()
        self.emit(f"  {raw} = call i32 @file_exists(i8* {path_val})")
        self.emit(f"  {cmp} = icmp ne i32 {raw}, 0")
        return cmp, "i1"

    def emit_file_delete(self, node):
        path_val, path_t = self.emit_expr(node.path_expr)
        self.emit(f"  call i32 @file_delete(i8* {path_val})")

    def emit_file_rename(self, node):
        old_val, old_t = self.emit_expr(node.old_path)
        new_val, new_t = self.emit_expr(node.new_path)
        self.emit(f"  call i32 @file_rename_file(i8* {old_val}, i8* {new_val})")

    def emit_file_copy(self, node):
        src_val, src_t = self.emit_expr(node.src_path)
        dst_val, dst_t = self.emit_expr(node.dst_path)
        self.emit(f"  call i32 @file_copy_file(i8* {src_val}, i8* {dst_val})")

    def emit_file_list(self, node):
        path_val, path_t = self.emit_expr(node.path_expr)
        path_s = self.coerce(path_val, path_t, "i8*")
        result = self.new_tmp()
        self.emit(f"  {result} = call %Box* @file_list_dir(i8* {path_s})")
        return result, "%Box*"

    def _os_run_core(self, node):
        """Shared logic: emit os_run() call, return (result_tmp, "i8*")"""
        null_lbl, null_len = self.intern_str("")
        null_ptr = self.new_tmp()
        self.emit(f"  {null_ptr} = getelementptr [{null_len} x i8], [{null_len} x i8]* {null_lbl}, i64 0, i64 0")
        if node.struct_args is not None:
            # os.run({ cmd: "...", args: [...], input: "..." })
            # Auto-start terminal 0 if not already started
            self.emit(f"  call void @os_start(i64 0)")
            # Build the command string: cmd + " " + joined args
            fields = node.struct_args
            cmd_v, cmd_t = self.emit_expr(fields["cmd"])
            cmd_s = self.coerce(cmd_v, cmd_t, "i8*")
            # If args present, build "cmd arg1 arg2 ..."
            if "args" in fields:
                args_node = fields["args"]
                args_v, args_t = self.emit_expr(args_node)
                args_b = self.coerce_to_box(args_v, args_t)
                # Build full cmd string by concatenating: cmd + " " + each arg
                sp_lbl, sp_len = self.intern_str(" ")
                sp_ptr = self.new_tmp()
                self.emit(f"  {sp_ptr} = getelementptr [{sp_len} x i8], [{sp_len} x i8]* {sp_lbl}, i64 0, i64 0")
                len_cmd = self.new_tmp(); len_args = self.new_tmp()
                len_v = self.new_tmp(); buf_sz = self.new_tmp(); buf = self.new_tmp()
                buf_ptr = self.new_tmp()
                self.emit(f"  {buf_ptr} = alloca i8*")
                # BUGFIX (found via syntax sweep): idx_tmp/len_v were used as
                # if they were mutable loop-carried pointers (`store ... , i32*
                # {idx_tmp}` / `store ..., i64* {len_v}`) but neither ever got
                # an `alloca` — {len_v} in particular is the RESULT of an `add`
                # (a plain SSA value, not storage), so storing through it is
                # invalid IR ("defined with type i64 but expected ptr"),
                # confirmed failing to compile a `os.run({cmd:.., args:[...]})`
                # call with 2+ args. idx_ptr fixes that half — real storage,
                # loaded/stored every iteration, the same pattern buf_ptr
                # already used correctly.
                #
                # The length side needed more than just an alloca: an EARLIER
                # attempt gave len_v its own storage too and loaded it back
                # each iteration, but the arithmetic itself was wrong at any
                # scale beyond 2 args — `len_cmd + len_args` (and each
                # iteration's `+= arg_len`) only ever counts RAW TEXT length,
                # never the separator spaces, so the flat "+2" (one space +
                # null) added to size each malloc undercounts by one byte per
                # PRIOR separator once 3+ pieces have been joined. Confirmed
                # under AddressSanitizer: `os.run({cmd:"echo", args:["one",
                # "two","three","four"]})` (5 total pieces, 4 separators
                # needed) heap-buffer-overflowed in strcat. Fixed by dropping
                # the separate counter entirely and computing the CURRENT
                # length via `strlen(cur_buf)` fresh each iteration — the
                # actual buffer already contains every separator inserted so
                # far, so this can never miscount regardless of how many
                # pieces have accumulated.
                idx_ptr = self.new_tmp()
                self.emit(f"  {idx_ptr} = alloca i32")
                self.emit(f"  {len_cmd} = call i64 @strlen(i8* {cmd_s})")
                # Concatenate all args: cmd + " " + arg0 + " " + arg1 + ...
                first_arg_b = self.new_tmp()
                self.emit(f"  {first_arg_b} = call %Box* @collection_get_at(%Box* {args_b}, i32 0)")
                first_arg_s = self.new_tmp()
                self.emit(f"  {first_arg_s} = call i8* @unbox_s(%Box* {first_arg_b})")
                self.emit(f"  {len_args} = call i64 @strlen(i8* {first_arg_s})")
                self.emit(f"  {len_v} = add i64 {len_cmd}, {len_args}")
                self.emit(f"  {buf_sz} = add i64 {len_v}, 2")  # space + null
                self.emit(f"  {buf} = call i8* @malloc(i64 {buf_sz})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {cmd_s})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {sp_ptr})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {first_arg_s})")
                self.emit(f"  store i8* {buf}, i8** {buf_ptr}")
                # Loop over remaining args and append each with a space
                self.emit(f"  store i32 1, i32* {idx_ptr}")
                loop_lbl = self.new_label("osrun_args")
                loop_end = self.new_label("osrun_args_end")
                self.emit(f"  br label %{loop_lbl}")
                self.emit(f"{loop_lbl}:")
                i_val = self.new_tmp()
                self.emit(f"  {i_val} = load i32, i32* {idx_ptr}")
                i64_val = self.new_tmp()
                self.emit(f"  {i64_val} = sext i32 {i_val} to i64")
                len_box = self.new_tmp()
                self.emit(f"  {len_box} = call i64 @collection_len(%Box* {args_b})")
                cond = self.new_tmp()
                self.emit(f"  {cond} = icmp slt i64 {i64_val}, {len_box}")
                self.emit(f"  br i1 {cond}, label %{loop_lbl}_body, label %{loop_lbl}_end")
                self.emit(f"{loop_lbl}_body:")
                arg_b = self.new_tmp()
                self.emit(f"  {arg_b} = call %Box* @collection_get_at(%Box* {args_b}, i32 {i_val})")
                arg_s = self.new_tmp()
                self.emit(f"  {arg_s} = call i8* @unbox_s(%Box* {arg_b})")
                arg_len = self.new_tmp()
                self.emit(f"  {arg_len} = call i64 @strlen(i8* {arg_s})")
                cur_buf = self.new_tmp()
                self.emit(f"  {cur_buf} = load i8*, i8** {buf_ptr}")
                cur_len = self.new_tmp()
                self.emit(f"  {cur_len} = call i64 @strlen(i8* {cur_buf})")
                new_len = self.new_tmp()
                self.emit(f"  {new_len} = add i64 {cur_len}, {arg_len}")
                new_sz = self.new_tmp()
                self.emit(f"  {new_sz} = add i64 {new_len}, 2")  # space + null
                new_buf = self.new_tmp()
                self.emit(f"  {new_buf} = call i8* @malloc(i64 {new_sz})")
                self.emit(f"  call i8* @strcpy(i8* {new_buf}, i8* {cur_buf})")
                self.emit(f"  call i8* @strcat(i8* {new_buf}, i8* {sp_ptr})")
                self.emit(f"  call i8* @strcat(i8* {new_buf}, i8* {arg_s})")
                self.emit(f"  call void @free(i8* {cur_buf})")
                self.emit(f"  store i8* {new_buf}, i8** {buf_ptr}")
                next_i = self.new_tmp()
                self.emit(f"  {next_i} = add i32 {i_val}, 1")
                self.emit(f"  store i32 {next_i}, i32* {idx_ptr}")
                self.emit(f"  br label %{loop_lbl}")
                self.emit(f"{loop_lbl}_end:")
                self.emit(f"  br label %{loop_end}")
                self.emit(f"{loop_end}:")
                final_buf = self.new_tmp()
                self.emit(f"  {final_buf} = load i8*, i8** {buf_ptr}")
                cmd_s = final_buf
            input_s = null_ptr
            if "input" in fields:
                inp_v, inp_t = self.emit_expr(fields["input"])
                input_s = self.coerce(inp_v, inp_t, "i8*")
            # OPEN-13: optional `timeout` field (in SECONDS) → absolute ms ceiling.
            # Absent → 0, which os_run() maps to its generous default.
            if "timeout" in fields:
                to_v, to_t = self.emit_expr(fields["timeout"])
                to_s = self.coerce(to_v, to_t, "i64")
                to_ms = self.new_tmp()
                self.emit(f"  {to_ms} = mul i64 {to_s}, 1000")
            else:
                to_ms = "0"
            # id is not needed for struct form — use terminal 0 by default
            res = self.new_tmp()
            self.emit(f"  {res} = call i8* @os_run(i64 0, i8* {cmd_s}, i8* {input_s}, i64 {to_ms})")
            # Check for NULL return (command failed) and branch to try error handler
            self._check_os_run_error(res)
            return res, "i8*"
        else:
            id_v, id_t = self.emit_expr(node.id_expr)
            id_v = self.coerce(id_v, id_t, "i64")
            cmd_v, cmd_t = self.emit_expr(node.cmd_expr)
            cmd_s = self.coerce(cmd_v, cmd_t, "i8*")
            if node.input_expr is not None:
                inp_v, inp_t = self.emit_expr(node.input_expr)
                inp_s = self.coerce(inp_v, inp_t, "i8*")
            else:
                inp_s = null_ptr
            # OPEN-13: positional form has no timeout slot (3rd arg is `input`);
            # pass 0 so os_run() uses its generous default absolute ceiling.
            res = self.new_tmp()
            self.emit(f"  {res} = call i8* @os_run(i64 {id_v}, i8* {cmd_s}, i8* {inp_s}, i64 0)")
            # Check for NULL return (command failed) and branch to try error handler
            self._check_os_run_error(res)
            return res, "i8*"

    def emit_os_run(self, node):
        """Statement form: os.run(...) — result discarded"""
        self._os_run_core(node)

    def emit_os_run_expr(self, node):
        """Expression form: let data = os.run(...) — result is string"""
        return self._os_run_core(node)

    def emit_ffi_bind(self, node):
        """
        FFI binding: fn lib symbol(params) -> ret
        Emits an LLVM function that:
          1. Calls ffi_sym(handle_idx, "symbol") to get the function pointer
          2. Bitcasts it to the right function type
          3. Calls it with the provided args
        We store the binding so calling symbol(args) works normally afterwards.
        """
        # syntax: FFI C TYPES — reject any parameter type with no defined
        # raw-ABI layout before emitting anything against it.
        self._check_ffi_param_types(node.alias or node.symbol_name, node.params)

        # Build param IR types
        param_ir_types = [self._ffi_type_to_ir(pt) for _, pt in node.params]
        # bugs.log: an FFI binding with no `-> ret` was previously assumed
        # to return i64 here, but debug.py's ctypes-based FFI call already
        # assumed void (`fn.restype = None if not bind['ret'] else ...`,
        # debug.py) for the identical omitted-return-type case — the
        # compiler and debugger disagreed on what an omitted return type
        # means. `void` (explicit or omitted) now means what it means in C:
        # a real foreign function that returns nothing, matching the vast
        # majority of OpenGL/GLFW-style APIs (glClear, glBindBuffer,
        # glfwPollEvents, ...) which were previously unusable without
        # reading a garbage i64 out of the return register.
        ret_ir = self._ffi_type_to_ir(node.ret_type) if node.ret_type else "void"
        fn_ptr_t = f"{ret_ir} ({', '.join(param_ir_types)})*" if param_ir_types else f"{ret_ir} ()*"
        # Rubidium-callable name: use 'as' alias when provided, else fall back to C symbol name
        fn_name = node.alias if node.alias else node.symbol_name
        # BUGFIX (bugs.log #1): don't emit the LLVM define under a name that
        # collides with a reserved C symbol (e.g. `as sin`/`as sqrt`, both used
        # verbatim in the syntax file's FFI examples) — keep `fn_name` as the
        # Rubidium-visible dict key, but emit/define under `safe_name`.
        safe_name = self._safe_fn_symbol(fn_name)
        if safe_name != fn_name:
            self._fn_symbol_override[fn_name] = safe_name
        params_ir = ", ".join(f"{pt} %p{i}" for i, pt in enumerate(param_ir_types))

        # Register immediately so calls in the same scope resolve
        self.functions[fn_name] = FnDef(safe_name, node.params, node.ret_type, [])
        self.ffi_functions.add(fn_name)

        # Buffer the wrapper — flush after current function closes (like trampolines)
        # so we don't emit a define inside another define
        pending = []
        pending.append(f"\ndefine {ret_ir} @{safe_name}({params_ir}) {{")
        pending.append("entry:")

        # Get handle index from the dedicated global slot written when FFI("path") ran.
        # Using the slot (not a local alloca) means the wrapper always finds it.
        slot_name = f"@_ffi_slot_{node.handle_name}"
        # Declare the slot if not yet declared (collect pass may run before emit_ffi_bind)
        # BUG-1 (RIG report): exact-match the declaration prefix, not a bare
        # substring check — see the matching fix/comment at the VarDecl sites
        # above for the full explanation (a name that's a prefix of an
        # earlier-declared one, e.g. "gl" after "glfw", false-positived as
        # already-declared and silently skipped emitting its own global).
        if not any(d.startswith(f"{slot_name} =") for d in self.global_decls):
            self.global_decls.append(f"@_ffi_slot_{node.handle_name} = global i64 -1")
        handle_var_v = f"%ffi_h_{self.new_tmp()[1:]}"
        pending.append(f"  {handle_var_v} = load i64, i64* {slot_name}")

        # Intern the symbol name string constant
        sym_lbl, sym_len = self.intern_str(node.symbol_name)
        sym_ptr_t = f"%ffi_sp_{self.new_tmp()[1:]}"
        pending.append(f"  {sym_ptr_t} = getelementptr [{sym_len} x i8], [{sym_len} x i8]* {sym_lbl}, i64 0, i64 0")

        # Get raw fn pointer as i64 via ffi_sym
        raw_fp = f"%ffi_raw_{self.new_tmp()[1:]}"
        pending.append(f"  {raw_fp} = call i64 @ffi_sym(i64 {handle_var_v}, i8* {sym_ptr_t})")

        # Null check — if symbol not resolved, skip the call and return zero/null
        ok_lbl  = f"ffi_ok_{self.new_tmp()[1:]}"
        bad_lbl = f"ffi_bad_{self.new_tmp()[1:]}"
        is_null = f"%ffi_null_{self.new_tmp()[1:]}"
        pending.append(f"  {is_null} = icmp eq i64 {raw_fp}, 0")
        pending.append(f"  br i1 {is_null}, label %{bad_lbl}, label %{ok_lbl}")
        pending.append(f"{bad_lbl}:")
        if ret_ir == "void":
            # `ret void 0`/`ret void null` are invalid IR — a void return
            # takes no value at all.
            pending.append("  ret void")
        elif ret_ir in ("float", "double"):
            pending.append(f"  ret {ret_ir} 0.0")
        elif ret_ir in ("i8*", "%Box*") or ret_ir.endswith("*"):
            pending.append(f"  ret {ret_ir} null")
        else:
            pending.append(f"  ret {ret_ir} 0")
        pending.append(f"{ok_lbl}:")

        # Bitcast i64 → fn ptr type
        fp_cast = f"%ffi_fp_{self.new_tmp()[1:]}"
        pending.append(f"  {fp_cast} = inttoptr i64 {raw_fp} to {fn_ptr_t}")

        # Call the foreign function
        args_str = ", ".join(f"{pt} %p{i}" for i, pt in enumerate(param_ir_types))
        if ret_ir == "void":
            # `%tmp = call void ...` is invalid IR — a void call's result
            # can't be assigned to a register at all, unlike every other
            # return type.
            pending.append(f"  call void {fp_cast}({args_str})")
            pending.append("  ret void")
        else:
            call_tmp = f"%ffi_ret_{self.new_tmp()[1:]}"
            pending.append(f"  {call_tmp} = call {ret_ir} {fp_cast}({args_str})")
            pending.append(f"  ret {ret_ir} {call_tmp}")
        pending.append("}")

        self._pending_trampolines += pending

    def emit_expr(self, node):
        if isinstance(node, KwArg):
            # A named argument (`x = value`) only has meaning inside a call's
            # parentheses, where _resolve_call_args() matches it to a
            # parameter and strips the wrapper before codegen ever sees the
            # inner value. Reaching here means one survived into a context
            # that doesn't resolve named arguments at all (a method call, a
            # collection index, ...) — a clear compile error instead of
            # emit_expr silently trying to evaluate a KwArg as if it were an
            # ordinary value.
            raise RubidiumTypeError(
                f"Named argument '{node.name} = ...' is not valid here — "
                f"named/default arguments are only supported for plain function calls"
            )
        if isinstance(node, LinkArg):
            # Link Rule: pass-by-reference. Collections/Any are already
            # %Box* (pointer/reference) and function calls currently never
            # deep-copy arguments (see bugs.log BUG #1 note), so evaluating
            # the inner expression normally already gives the shared-
            # reference behavior the spec describes.
            return self.emit_expr(node.expr)
        if isinstance(node, Number):
            if isinstance(node.value, float):
                # OPEN-5: a literal with MORE significant digits than a
                # double can hold (17 is enough to round-trip any double
                # exactly) has already lost precision the moment the parser
                # converted it via Python's float() — that happened before
                # codegen ever saw it. If the original source text is
                # available and genuinely needs more precision, encode it
                # as an exact IEEE-754 binary128 hex constant (LLVM's plain
                # decimal float syntax is only ever parsed at DOUBLE
                # precision regardless of the target type — confirmed by
                # testing, contrary to this fix's original investigation
                # note — so the raw decimal text can't be emitted directly;
                # the `0xL<32 hex digits>` form is required for full
                # precision). Ordinary literals (<=17 significant digits,
                # the overwhelming majority) are completely unaffected —
                # same "double" output as before, avoiding any behavior/perf
                # change for code that never asked for f128+.
                raw = node.raw
                if raw and sum(ch.isdigit() for ch in raw) > 17:
                    return _decimal_str_to_fp128_hex(raw), "fp128"
                return f"{node.value:.17e}", "double"
            v = int(node.value)
            # BUGFIX (bugs.log #17): a literal was always typed "i64",
            # regardless of its actual magnitude. A value that doesn't fit
            # in 64 bits (e.g. i128's max, 2**127-1) got emitted as an
            # invalid i64 constant, which silently truncated/wrapped —
            # concretely, 2**127-1's low 64 bits are all 1s, which as a
            # signed i64 is exactly -1, matching the exact bug that was
            # reported. Pick the smallest IR integer type that actually
            # fits the value instead; coerce()/promote_type() take it from
            # there for arithmetic and narrowing/widening as before.
            for ir_t in ("i64", "i128", "i256", "i512", "i1024", "i2048"):
                lo, hi = self._int_bounds(ir_t)
                if lo <= v <= hi:
                    return str(v), ir_t
            return str(v), "i2048"
        if isinstance(node, Bool): return ("1" if node.value else "0"), "i1"
        # OPEN-4: return a distinguishable pseudo-type ("null") rather than
        # "i64" so boxing (coerce_to_box) can tell "this specific value IS
        # the Null literal" apart from "this is a real int literal that
        # happens to equal the sentinel" (e.g. `[-2147483648, 1, 2]`) — see
        # coerce()/coerce_to_box() handling of from_t/t == "null".
        if isinstance(node, None_): return self._NULL_SENTINEL, "null"
        if isinstance(node, Str):
            lbl, blen = self.intern_str(node.value); ptr = self.new_tmp()
            self.emit(f'  {ptr} = getelementptr [{blen} x i8], [{blen} x i8]* {lbl}, i64 0, i64 0')
            return ptr, "i8*"
        if isinstance(node, InterpolatedStr):
            # Build result by concatenating all parts
            # Start with an empty string, then strcat each part
            # Pre-compute total length at runtime and malloc once
            # For simplicity: build iteratively with malloc+strcpy+strcat
            # First convert all parts to i8* strings
            part_vals = []
            for part in node.parts:
                pv, pt = self.emit_expr(part)
                pv = self.coerce_to_string(pv, pt)
                part_vals.append(pv)
            # Sum all lengths
            if len(part_vals) == 0:
                empty_lbl, elen = self.intern_str("")
                ep = self.new_tmp()
                self.emit(f'  {ep} = getelementptr [{elen} x i8], [{elen} x i8]* {empty_lbl}, i64 0, i64 0')
                return ep, "i8*"
            if len(part_vals) == 1:
                return part_vals[0], "i8*"
            # Sum lengths of all parts
            total = self.new_tmp()
            lens = [self.new_tmp() for _ in part_vals]
            for i, pv in enumerate(part_vals):
                self.emit(f"  {lens[i]} = call i64 @strlen(i8* {pv})")
            # Add them up
            acc = lens[0]
            for i in range(1, len(lens)):
                nacc = self.new_tmp()
                self.emit(f"  {nacc} = add i64 {acc}, {lens[i]}")
                acc = nacc
            total_plus1 = self.new_tmp()
            self.emit(f"  {total_plus1} = add i64 {acc}, 1")
            buf = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 {total_plus1})")
            # Copy first part
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {part_vals[0]})")
            # Concatenate remaining parts
            for pv in part_vals[1:]:
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {pv})")
            return self._track_temp(buf, "i8*"), "i8*"   # BUG-3

        if isinstance(node, ListExpr):
            lst = self.new_tmp()
            self.emit(f"  {lst} = call %Box* @make_list()")
            for e in node.elements:
                ev, et = self.emit_expr(e)
                eb = self.coerce_to_box(ev, et)
                # OPEN-4 follow-up: list_append_raw, not list_append — a
                # literal must keep every element verbatim, including a
                # leading Null, without .add()'s singleton-replace rule.
                self.emit(f"  call void @list_append_raw(%Box* {lst}, %Box* {eb})")
            # BUG-3: a literal is a fresh allocation owned by this block until
            # something longer-lived (a global, a field, a first-declaration
            # local) takes it over via _escape_temp.
            return self._track_temp(lst, "%Box*"), "%Box*"
        if isinstance(node, DictExpr):
            # FEATURE: dict+ — same underlying RDict layout as dict, just a
            # different magic number (see IS_DICT_MAGIC in the C runtime),
            # used solely to distinguish what `.add(newkey)` should create
            # as a new key's default value (dict: Null, dict+: an empty
            # nested dict+). node.is_dictplus is set recursively by the
            # parser for every level of a `let x: dict+ = {...}` literal.
            ctor = "make_dictplus" if node.is_dictplus else "make_dict"
            dct = self.new_tmp()
            self.emit(f"  {dct} = call %Box* @{ctor}()")
            for k, v in node.pairs:
                kv, kt = self.emit_expr(k); vv, vt = self.emit_expr(v)
                kb = self.coerce_to_box(kv, kt); vb = self.coerce_to_box(vv, vt)
                self.emit(f"  call void @dict_set(%Box* {dct}, %Box* {kb}, %Box* {vb})")
            return self._track_temp(dct, "%Box*"), "%Box*"   # BUG-3
        if isinstance(node, Input):
            if node.prompt is not None:
                pv, pt = self.emit_expr(node.prompt)
                if pt == "i8*":
                    fmt, flen = self.intern_str("%s"); ptr = self.new_tmp()
                    self.emit(f'  {ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt}, i64 0, i64 0')
                    self.emit(f'  call i32 (i8*, ...) @printf(i8* {ptr}, i8* {pv})')
                    self.emit(f'  call i32 @fflush(i8* null)')
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @_rubidium_input_line()")
            return result, "i8*"

        if isinstance(node, ThreadRunning):
            return self.emit_thread_running(node)
        if isinstance(node, FileHandleStmt):
            return self.emit_file_handle_method(node.var_name, node.method, node.args)
        if isinstance(node, FileExists):
            return self.emit_file_exists(node)
        if isinstance(node, FileList):
            return self.emit_file_list(node)
        if isinstance(node, DynResolve):
            # BUGFIX/FEATURE (bugs.log #9): resolve the dynamic variable
            # currently named by holder_name's CURRENT runtime value (a
            # normal i8* string load), via the runtime hash-map. Returns a
            # %Box* — from here, the existing chained-collection-access
            # codegen (emit_call_expr's `not isinstance(node.name, str)`
            # branch, and normal MethodCall handling) treats it exactly like
            # any other collection value, with no further changes needed.
            key_v, key_t = self.emit_expr(Var(node.holder_name))
            key_s = self.coerce(key_v, key_t, "i8*")
            result = self.new_tmp()
            self.emit(f"  {result} = call %Box* @rub_dynvar_get(i8* {key_s})")
            return result, "%Box*"
        if isinstance(node, Var):
            if node.name in self.dropped_vars:
                # Runtime behavior: print error message instead of crash
                err_lbl, err_len = self.intern_str("ERROR: variable does not exist\\n")
                fmt_ptr = self.new_tmp()
                self.emit(f"  {fmt_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
                self.emit(f"  call i32 (i8*, ...) @printf(i8* {fmt_ptr})")
                return "0", "i64"
            
            # --- FIX: Implicitly access class field if it exists ---
            # BUGFIX (bugs.log #15): a LOCAL variable or PARAMETER must
            # always shadow a class field of the same name — this is
            # standard scoping in every OO language, and matters a lot here
            # because _register_implicit_class_fields() turns ANY un-"local"
            # `let` in ANY method into a class-wide field. Before this fix,
            # is_class_field() was checked first, so e.g. a parameter named
            # "qty" in one method would get silently replaced by an
            # unrelated implicit field also named "qty" from a totally
            # different method (very easy to hit with common names like
            # qty/total/value/temp), reading 0/uninitialized instead of the
            # real parameter value.
            in_local_scope = any(node.name in scope for scope in self.local_vars_stack)
            if not in_local_scope and self.is_class_field(node.name):
                return self.emit_field_access(Var("__self"), node.name)
            # -------------------------------------------------------
            
            # Handle indexed link: let b = link a(i) -> reading b does a(i)
            if node.name in self.indexed_links:
                coll_name, index_expr = self.indexed_links[node.name]
                # Emit collection pointer
                coll_ptr, coll_t = self.emit_expr(Var(coll_name))
                coll_box = self.coerce_to_box(coll_ptr, coll_t)
                # Emit index as i64 (collection_get_at takes int, not Box*)
                idx_v, idx_t = self.emit_expr(index_expr)
                idx_i64 = self.coerce(idx_v, idx_t, "i64")
                # Get the element at index
                elem = self.new_tmp()
                self.emit(f"  {elem} = call %Box* @collection_get_at(%Box* {coll_box}, i64 {idx_i64})")
                # Return a copy so the caller owns it (deep copy semantics for reads)
                elem_copy = self.new_tmp()
                self.emit(f"  {elem_copy} = call %Box* @box_copy(%Box* {elem})")
                return elem_copy, "%Box*"
            
            # syntax: FFI CALLBACKS — a bare function name (no call
            # parentheses) referencing a `fn callback` means "give me its
            # trampoline's address," not "read a variable" — checked only
            # once name resolution as an actual variable has failed (a
            # variable of the same name, if one legitimately existed here,
            # must still win — matches the language's own "names must be
            # unique within a scope" rule, so the two can never genuinely
            # collide anyway).
            is_var = (any(node.name in scope for scope in self.local_vars_stack)
                      or node.name in self.global_vars)
            if not is_var:
                fn_obj = self.functions.get(node.name)
                if fn_obj is not None and getattr(fn_obj, "is_callback", False):
                    # A function value in LLVM IR is a strongly-typed function
                    # pointer (return type + full param list), not i8* — build
                    # that exact type string, matching how emit_ffi_bind
                    # builds fn_ptr_t for the same reason.
                    c_param_types = [self._ffi_type_to_ir(pt) for _, pt in fn_obj.params]
                    c_ret_t = self._ffi_type_to_ir(fn_obj.ret_type) if fn_obj.ret_type else "i64"
                    fn_ptr_t = f"{c_ret_t} ({', '.join(c_param_types)})*" if c_param_types else f"{c_ret_t} ()*"
                    addr = self.new_tmp()
                    self.emit(f"  {addr} = ptrtoint {fn_ptr_t} @{fn_obj.name}_c_trampoline to i64")
                    return addr, "i64"
            ptr_str, ir_t = self.get_var_ptr(node.name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = load {ir_t}, {ir_t}* {ptr_str}")
            return tmp, ir_t

        if isinstance(node, FieldAccess): return self.emit_field_access(node.obj, node.field)
        if isinstance(node, MathBlock): return self.emit_math_block(node)
        if isinstance(node, BinOp): return self.emit_binop(node)
        if isinstance(node, Compare): return self.emit_compare(node)
        if isinstance(node, UnaryOp):
            val, t = self.emit_expr(node.value)
            if node.op == "-":
                tmp = self.new_tmp()
                if t in ("float","double"): self.emit(f"  {tmp} = fneg {t} {val}")
                else: self.emit(f"  {tmp} = sub {t} 0, {val}")
                return tmp, t
            if node.op == "*/":
                # Square root operator - call sqrt() from math library
                val_d = self.coerce(val, t, "double")
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call double @sqrt(double {val_d})")
                return tmp, "double"
            if node.op == "not":
                v = self.to_bool(val, t); tmp = self.new_tmp()
                self.emit(f"  {tmp} = xor i1 {v}, 1")
                return tmp, "i1"
            return val, t
        if isinstance(node, FnCall): return self.emit_call_expr(node)
        if isinstance(node, ClassInstantiate):
            struct_t = self.class_ir_type(node.class_name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call {struct_t}* @_{node.class_name}_new()")
            return tmp, f"{struct_t}*"
        if isinstance(node, MethodCall):
            if node.method == "set" and isinstance(node.obj, FnCall):
                self.emit_collection_set(node)
                return "0", "i64"
            if node.method == "add" and isinstance(node.obj, FnCall):
                self.emit_collection_add(node)
                return "0", "i64"
            return self.emit_method_call_expr(node)
        if isinstance(node, TypeCast): return self.emit_type_cast(node)
        if isinstance(node, FFILoad):
            path_v, path_t = self.emit_expr(node.path_expr)
            path_s = self.coerce(path_v, path_t, "i8*")
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @ffi_load(i8* {path_s})")
            return tmp, "i64"
        if isinstance(node, OsRun):
            return self.emit_os_run_expr(node)
        return "0", "i64"

    def emit_field_access(self, obj, field_name):
        # Extract the name if it's an ast.Var object
        obj_name = obj.name if hasattr(obj, 'name') else str(obj)

        # Module-qualified variable access (e.g. `xeon math_tools` / `import
        # math_tools` then `math_tools.pi` -> the global `math_tools_pi`).
        # Resolves aliases too (e.g. `import math_tools as mt` -> `mt.pi`).
        resolved_obj = self.import_aliases.get(obj_name, obj_name)
        mangled_var = f"{resolved_obj}_{field_name}"
        if obj_name not in self.instances and mangled_var in self.global_vars:
            ir_t = self.global_vars[mangled_var]
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = load {ir_t}, {ir_t}* @{mangled_var}")
            return tmp, ir_t

        if obj_name not in self.instances: 
            raise RubidiumNameError(f"'{obj_name}' is not an instance")
            
        class_name = self.instances[obj_name]
        idx, ir_t  = self.field_index(class_name, field_name)
        struct_t   = self.class_ir_type(class_name)
        
        ptr_str, _ = self.get_var_ptr(obj_name)
        inst_ptr = self.new_tmp(); fptr = self.new_tmp(); val = self.new_tmp()
        self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")
        self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {inst_ptr}, i32 0, i32 {idx}")
        self.emit(f"  {val} = load {ir_t}, {ir_t}* {fptr}")
        return val, ir_t

    def _resolve_call_args(self, fn_name, params, defaults, args):
        """DEFAULT / NAMED ARGUMENTS: `fn test(x: i32 = 10)` declares a
        default, and a call site can bind by name instead of position —
        `test(x = 100, y = 4, b = 9)`. Matches `args` (a mix of plain expr
        nodes and KwArg('name', expr) nodes, straight from the parser)
        against `params` ([(name, type), ...], in declaration order),
        filling any parameter the call didn't supply from `defaults`
        ({name: expr}). Returns a plain positional list the same length as
        `params`, so every existing call-emission site downstream (which
        zips args 1:1 against params) needs no changes at all — it never
        sees a KwArg or a missing argument, only the resolved result.
        """
        has_kwargs = any(isinstance(a, KwArg) for a in args)
        if not has_kwargs and len(args) == len(params):
            return args  # fast path: the overwhelming common case, untouched

        param_names = [pn for pn, _ in params]
        resolved = [None] * len(params)
        filled = [False] * len(params)

        pos_i = 0
        seen_named = False
        for a in args:
            if isinstance(a, KwArg):
                seen_named = True
                if a.name not in param_names:
                    raise RubidiumTypeError(f"Function '{fn_name}' has no parameter named '{a.name}'")
                idx = param_names.index(a.name)
                if filled[idx]:
                    raise RubidiumTypeError(f"Function '{fn_name}' got multiple values for parameter '{a.name}'")
                resolved[idx] = a.value
                filled[idx] = True
            else:
                if seen_named:
                    raise RubidiumTypeError(f"Function '{fn_name}': positional argument follows a named one")
                if pos_i >= len(params):
                    raise RubidiumTypeError(
                        f"Function '{fn_name}' expects {len(params)} argument(s), got more"
                    )
                resolved[pos_i] = a
                filled[pos_i] = True
                pos_i += 1

        for idx, pn in enumerate(param_names):
            if not filled[idx]:
                if pn in defaults:
                    resolved[idx] = defaults[pn]
                    filled[idx] = True
                else:
                    raise RubidiumTypeError(f"Function '{fn_name}' missing required argument '{pn}'")

        return resolved

    def _check_arg_count(self, fn_name, params, args):
        """BUG (found via syntax sweep): calling a function with the wrong
        number of arguments silently succeeded, on the REAL compiler — extra
        arguments were dropped, missing ones defaulted to zero, with NO
        diagnostic. Confirmed: `add(a: i32, b: i32)` called as `add(1,2,3)`
        silently computed 1+2 (dropped the 3); called as `add(1)` silently
        computed 1+0. The debugger's SEPARATE static-analyzer pass already
        catches this correctly — but `python3 compiler.py foo.rub` (and
        `xeon build --no-debug`, an explicitly documented, legitimate way to
        skip that analyzer) has zero protection, so a silently-wrong binary
        was fully reachable. Matches the analyzer's own message wording."""
        if len(args) != len(params):
            raise RubidiumTypeError(
                f"Function '{fn_name}' expects {len(params)} argument(s), got {len(args)}."
            )

    def emit_call_expr(self, node):
        # Chained collection access: nested(0)(1) parses as FnCall(FnCall("nested",[0]),[1])
        # Evaluate the inner call first, then do collection_get for the outer args.
        if not isinstance(node.name, str):
            col_v, col_t = self.emit_expr(node.name)
            col_b = self.coerce_to_box(col_v, col_t)
            # BUG-4: navigate through the real interior objects, then hand back
            # an independent copy of the element the read actually lands on.
            last = len(node.args) - 1
            for i, arg in enumerate(node.args):
                idx_v, idx_t = self.emit_expr(arg)
                idx_b = self.coerce_to_box(idx_v, idx_t)
                res = self.new_tmp()
                getter = "collection_get_copy" if i == last else "collection_get"
                self.emit(f"  {res} = call %Box* @{getter}(%Box* {col_b}, %Box* {idx_b})")
                col_b = res
            return col_b, "%Box*"

        if isinstance(node.name, str):
            node.name = node.name.replace(".", "_")

        # 1b. Class method calling another method of the SAME class (no 'self' keyword
        #     per spec — bare call like heal(40) inside use_potion resolves to
        #     _ClassName_heal(__self, 40) automatically).
        if self.cur_class:
            ir_method = self.method_ir_name(self.cur_class, node.name)
            if ir_method in self.functions:
                struct_t = self.class_ir_type(self.cur_class)
                self_ptr, _ = self.get_var_ptr("__self")
                # BUGFIX: get_var_ptr returns the ALLOCA slot (%class_X**), not
                # the actual instance pointer (%class_X*) — must load it first,
                # exactly like every other __self use in this file does (see
                # e.g. the field-assignment path a few lines above). Passing
                # the alloca pointer directly silently passed a wrong/garbage
                # self pointer to the callee, so field reads/writes through it
                # (like `health = health + amount` in a same-class helper
                # method) had no visible effect on the real instance.
                self_val = self.new_tmp()
                self.emit(f"  {self_val} = load {struct_t}*, {struct_t}** {self_ptr}")
                fn_def = self.functions[ir_method]
                # fn_def.params has the implicit __self prepended — the
                # user-visible call (e.g. heal(40)) never includes it.
                node.args = self._resolve_call_args(
                    node.name, fn_def.params[1:], getattr(fn_def, "defaults", {}), node.args)
                self._check_arg_count(node.name, fn_def.params[1:], node.args)
                param_types = [self.rubi_type_to_ir(pt) for _, pt in fn_def.params]
                ret_ir = self.rubi_type_to_ir(fn_def.ret_type) if fn_def.ret_type else "i64"
                args_ir = [f"{struct_t}* {self_val}"]
                for i, a in enumerate(node.args):
                    av, at = self.emit_expr(a)
                    # BUGFIX (RIG report): param_types[0] is __self's slot, so a
                    # real user arg at position i must be checked against
                    # param_types[i + 1], not param_types[i] — the old off-by-
                    # one coerced every argument against the PREVIOUS param's
                    # type (arg 0 against __self's struct type, arg 1 against
                    # param 0's type, etc.), and silently dropped type-checking
                    # for the last real argument entirely. This corrupted any
                    # sibling call mixing types (e.g. an f64 arg coerced against
                    # an i32 slot came out as garbage in the callee).
                    pt_idx = i + 1
                    if pt_idx < len(param_types):
                        av = self.coerce(av, at, param_types[pt_idx])
                        at = param_types[pt_idx]
                    args_ir.append(f"{at} {av}")
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call {ret_ir} @{ir_method}({', '.join(args_ir)})")
                self._emit_error_propagation_check()
                return tmp, ret_ir

        # 1. PRIORITY 1: Hardcoded System Built-ins
        # If it's one of these, it CANNOT be a collection or class method.
        if node.name == "print":
            # Direct to your print logic
            val, t = self.emit_expr(node.args[0])
            self.emit_print(node.args[0])
            return "0", "i64"

        if node.name == "input":
            return self.emit_expr(Input(node.args[0] if node.args else None))

        # syntax: PRINT & INPUT — clear() wipes the whole terminal (println()
        # already overwrites just the current line).
        if node.name == "clear":
            self.emit("  call void @rub_clear_screen()")
            return "0", "i64"

        # C library math functions.
        # BUG-8: a USER-DEFINED function of the same name must win. This branch
        # used to fire unconditionally, so `fn log(msg: str)` — which is the
        # syntax file's own FUNCTIONS example — compiled into a call to libm's
        # log(double), silently passing a char* as a double and never running
        # the user's body. Same for the spec's FFI-wrapper example, which
        # defines `fn sqrt(value: f64)`. The definition side already mangles
        # these to a safe LLVM symbol (see _safe_fn_symbol); only the call side
        # resolved them wrongly.
        c_math_fns = {"sin", "cos", "tan", "sqrt", "pow", "log", "log10", "exp", "fabs", "floor", "ceil", "round"}
        if node.name in c_math_fns and node.name not in self.functions:
            args_ir = []
            for a in node.args:
                v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
            tmp = self.new_tmp()
            ret_ir = "double" if node.name in {"sin", "cos", "tan", "sqrt", "pow", "log", "log10", "exp", "fabs", "floor", "ceil", "round"} else "i64"
            # Use @_rubidium_pow for pow to avoid conflict with global variable
            fn_name = "@_rubidium_pow" if node.name == "pow" else f"@{node.name}"
            self.emit(f"  {tmp} = call {ret_ir} {fn_name}({', '.join(args_ir)})")
            return tmp, ret_ir

        if node.name == "random" and len(node.args) >= 2:
            # random(min, max, type) — generate random number in [min, max]
            # type arg (3rd) is optional and guides float vs int output
            min_v, min_t = self.emit_expr(node.args[0])
            max_v, max_t = self.emit_expr(node.args[1])
            type_name = ""
            if len(node.args) >= 3 and isinstance(node.args[2], Var):
                type_name = node.args[2].name
            is_float = type_name.startswith("f") or min_t in ("float","double") or max_t in ("float","double")
            if is_float:
                # (rand() / RAND_MAX) * (max - min) + min
                r_raw = self.new_tmp(); r_f = self.new_tmp()
                rmax_f = self.new_tmp(); ratio = self.new_tmp()
                range_v = self.new_tmp(); result = self.new_tmp()
                self.emit(f"  {r_raw} = call i64 @random()")
                self.emit(f"  {r_f} = sitofp i64 {r_raw} to double")
                self.emit(f"  {rmax_f} = sitofp i64 2147483648 to double")  # RAND_MAX+1 for [min,max)
                self.emit(f"  {ratio} = fdiv double {r_f}, {rmax_f}")
                mn = self.coerce(min_v, min_t, "double")
                mx = self.coerce(max_v, max_t, "double")
                self.emit(f"  {range_v} = fsub double {mx}, {mn}")
                self.emit(f"  {result} = fmul double {ratio}, {range_v}")
                final = self.new_tmp()
                self.emit(f"  {final} = fadd double {result}, {mn}")
                return final, "double"
            else:
                # rand() % (max - min + 1) + min
                mn = self.coerce(min_v, min_t, "i64")
                mx = self.coerce(max_v, max_t, "i64")
                r_raw = self.new_tmp()
                range_v = self.new_tmp(); range1 = self.new_tmp()
                rem = self.new_tmp(); result = self.new_tmp()
                self.emit(f"  {r_raw} = call i64 @random()")
                r_i64 = r_raw  # random() already returns i64
                self.emit(f"  {range_v} = sub i64 {mx}, {mn}")
                self.emit(f"  {range1} = add i64 {range_v}, 1")
                self.emit(f"  {rem} = srem i64 {r_i64}, {range_v}")
                self.emit(f"  {result} = add i64 {rem}, {mn}")
                return result, "i64"


        if node.name == "thread" and len(node.args) == 2:
            func_call_node = node.args[0]
            tid_v, tid_t   = self.emit_expr(node.args[1])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            self._emit_slot_bounds_check(tid_v, 1024, "Thread")
            # BUG (found via syntax sweep): `syntax` documents "A thread ID
            # can only be reused after that thread has finished," but nothing
            # enforced it — thread(fn(), id) unconditionally overwrote
            # _thread_handles[id] with the new pthread handle, even while the
            # PREVIOUS thread using that id was still running. The old OS
            # thread keeps running (it isn't killed), but its handle is now
            # unreachable through the tracked array: a later thread.wait(id)
            # or thread.running(id) silently operates on the NEW thread
            # instead — wrong synchronization with no diagnostic. The runtime
            # already has the check needed to detect this (used for
            # thread.running()); use it here to turn "you violated the
            # documented ID-reuse rule" into a clean, catchable runtime error
            # instead of silently corrupting the tracking array.
            is_running = self.new_tmp()
            self.emit(f"  {is_running} = call i1 @_thread_is_running(i64 {tid_v})")
            ok_l = self.new_label("threadok")
            busy_l = self.new_label("threadbusy")
            self.emit(f"  br i1 {is_running}, label %{busy_l}, label %{ok_l}")
            self.emit(f"{busy_l}:")
            err_lbl, err_len = self.intern_str("Thread ID already in use — reuse only after that thread has finished")
            err_ptr = self.new_tmp()
            self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
            self._emit_raise_or_propagate(err_ptr)
            self.emit(f"{ok_l}:")
            # Store handle: _thread_handles[tid]
            h_ptr = self.new_tmp()
            self.emit(f"  {h_ptr} = getelementptr [1024 x i64], [1024 x i64]* @_thread_handles, i64 0, i64 {tid_v}")

            fn_name = func_call_node.name if isinstance(func_call_node, FnCall) else str(func_call_node)
            call_args = func_call_node.args if isinstance(func_call_node, FnCall) else []
            fn_def = self.functions.get(fn_name)
            param_types = [self.rubi_type_to_ir(pt) for _, pt in fn_def.params] if (fn_def and fn_def.params) else []
            ret_ir = self.rubi_type_to_ir(fn_def.ret_type) if (fn_def and fn_def.ret_type) else "i64"
            tramp = f"_tramp_{fn_name}"

            # Marshal arguments (if any) into a heap-allocated struct for the trampoline
            if call_args and param_types:
                struct_t = "{" + ", ".join(param_types) + "}"
                size_ptr, size_int, raw_ptr, struct_ptr = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {size_ptr} = getelementptr {struct_t}, {struct_t}* null, i64 1")
                self.emit(f"  {size_int} = ptrtoint {struct_t}* {size_ptr} to i64")
                self.emit(f"  {raw_ptr} = call i8* @malloc(i64 {size_int})")
                self.emit(f"  {struct_ptr} = bitcast i8* {raw_ptr} to {struct_t}*")
                for i, (arg, pt) in enumerate(zip(call_args, param_types)):
                    av, at = self.emit_expr(arg)
                    av = self.coerce(av, at, pt)
                    fptr = self.new_tmp()
                    self.emit(f"  {fptr} = getelementptr {struct_t}, {struct_t}* {struct_ptr}, i32 0, i32 {i}")
                    self.emit(f"  store {pt} {av}, {pt}* {fptr}")
                arg_ptr_for_create = raw_ptr
            else:
                arg_ptr_for_create = "null"

            if tramp not in self.functions:
                tramp_lines = [f"define i8* @{tramp}(i8* %_arg) {{", "entry:"]
                if call_args and param_types:
                    struct_t = "{" + ", ".join(param_types) + "}"
                    tramp_lines.append(f"  %s = bitcast i8* %_arg to {struct_t}*")
                    call_arg_strs = []
                    for i, pt in enumerate(param_types):
                        tramp_lines.append(f"  %fp{i} = getelementptr {struct_t}, {struct_t}* %s, i32 0, i32 {i}")
                        tramp_lines.append(f"  %v{i} = load {pt}, {pt}* %fp{i}")
                        call_arg_strs.append(f"{pt} %v{i}")
                    # BUGFIX (bugs.log #1): call the actual emitted symbol
                    # (fn_def.name), which differs from fn_name only when the
                    # task function's name collided with a reserved C symbol.
                    call_target = fn_def.name if fn_def else fn_name
                    tramp_lines.append(f"  call {ret_ir} @{call_target}({', '.join(call_arg_strs)})")
                    tramp_lines.append(f"  call void @free(i8* %_arg)")
                else:
                    call_target = fn_def.name if fn_def else fn_name
                    tramp_lines.append(f"  call {ret_ir} @{call_target}()")
                tramp_lines += ["  ret i8* null", "}", ""]
                self._pending_trampolines += tramp_lines
                self.functions[tramp] = FnDef(tramp, [], None, [])

            tramp_ptr = self.new_tmp()
            self.emit(f"  {tramp_ptr} = bitcast i8* (i8*)* @{tramp} to i8* (i8*)*")
            self.emit(f"  call i32 @pthread_create(i64* {h_ptr}, i64* null, i8* (i8*)* {tramp_ptr}, i8* {arg_ptr_for_create})")
            return "0", "i64"

        if node.name == "thread_wait":
            for arg in node.args:
                tid_v, tid_t = self.emit_expr(arg)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
            return "0", "i64"

        if node.name == "thread_result":
            tid_v, tid_t = self.emit_expr(node.args[0])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            self._emit_slot_bounds_check(tid_v, 1024, "Thread")
            r_ptr = self.new_tmp(); r_val = self.new_tmp()
            self.emit(f"  {r_ptr} = getelementptr [1024 x %Box*], [1024 x %Box*]* @_thread_results, i64 0, i64 {tid_v}")
            self.emit(f"  {r_val} = load %Box*, %Box** {r_ptr}")
            return r_val, "%Box*"

        # 3. PRIORITY 3: Actual Functions (main, user functions)
        # We check if it exists in self.functions FIRST. 
        # This prevents main() or user-defined functions from being caught by the collection logic.
        target_name = node.name
        if f"main_{node.name}" in self.functions:
            target_name = f"main_{node.name}"
        
        if target_name in self.functions:
            # FEATURE: inside a class method, only the class's own methods
            # (already handled above, before this point) are callable —
            # top-level functions are treated as if they don't exist, unless
            # passed in as a parameter (which resolves as a local, not here).
            if self.cur_class:
                raise RubidiumNameError(
                    f"Class '{self.cur_class}' cannot call function '{node.name}': "
                    f"functions outside the class are not accessible from inside it "
                    f"(pass it in as a parameter instead)"
                )
            fn_obj = self.functions[target_name]
            node.args = self._resolve_call_args(
                node.name, fn_obj.params or [], getattr(fn_obj, "defaults", {}), node.args)
            self._check_arg_count(node.name, fn_obj.params or [], node.args)
            # BUG (GLFW FFI report): FFI-bound functions (`fn lib symbol(...) as
            # name`) are DEFINED by emit_ffi_bind using _ffi_type_to_ir, which
            # maps `Any` to i64 (a raw pointer-sized integer — correct ABI for a
            # foreign function that knows nothing about Rubidium's Box struct).
            # But this call site used rubi_type_to_ir, which maps `Any` to
            # %Box* — a signature mismatch with the actual `define`. Worse,
            # coercing a `Null` argument to %Box* produced an actual heap
            # pointer (box_null()'s allocation), not a null pointer — so
            # `glfwCreateWindow(..., Null, Null)` passed GLFW two garbage
            # non-null "monitor"/"share" pointers instead of NULL, corrupting
            # the call. Use _ffi_type_to_ir for an FFI function's signature,
            # matching its actual `define`.
            is_ffi = target_name in self.ffi_functions
            type_map = self._ffi_type_to_ir if is_ffi else self.rubi_type_to_ir
            param_types = [type_map(pt) for _, pt in fn_obj.params] if fn_obj.params else []
            param_rubi_types = [pt for _, pt in fn_obj.params] if fn_obj.params else []
            args_ir = []
            for i, a in enumerate(node.args):
                v, t = self.emit_expr(a)
                if i < len(param_types):
                    target_t = param_types[i]
                    # syntax: FFI C TYPES — `FFI.type(list)` flattens a
                    # type-forced Rubidium list into a raw C-compatible
                    # buffer for this parameter, in place of you calling a
                    # manual conversion method yourself.
                    if is_ffi and self._c_type_name(param_rubi_types[i]) == "list":
                        if not isinstance(a, Var):
                            raise RubidiumTypeError(
                                f"FFI.type(list) parameter requires a plain list variable, got {type(a).__name__}"
                            )
                        elem_t = self.element_types.get(a.name)
                        kind = self._FFI_BUFFER_KINDS.get(elem_t)
                        if kind is None:
                            raise RubidiumTypeError(
                                f"'{a.name}' passed to an FFI.type(list) parameter must be a list with a "
                                f"forced element type of i32/i64/f32/f64 (e.g. `let {a.name}: list: f32 = [...]`), "
                                f"got {elem_t!r}"
                            )
                        buf_tmp = self.new_tmp()
                        self.emit(f"  {buf_tmp} = call i8* @list_to_flat_buffer(%Box* {v}, i32 {kind})")
                        # BUG: list_to_flat_buffer mallocs a fresh buffer with
                        # no Rubidium-level handle for the caller to .drop()
                        # themselves (unlike the old .as_ffi_buffer(), this
                        # conversion happens inline in argument evaluation) —
                        # track it in the scope's temp arena the same way
                        # every other freshly-malloc'd i8* result is (e.g.
                        # box_to_cstr's _track_temp call above), so it's
                        # freed automatically at the end of the block instead
                        # of leaking on every call.
                        v = self._track_temp(buf_tmp, "i8*")
                    # An explicit `Null` passed for an `Any` FFI parameter means
                    # the actual C NULL pointer (0), not Rubidium's %Box*
                    # Null-sentinel value — an FFI signature has no `%Box*` to
                    # even coerce into correctly.
                    elif is_ffi and param_rubi_types[i] == "Any" and isinstance(a, None_):
                        v = "0"
                    elif is_ffi and target_t == "i64" and t == "i8*":
                        # BUG: the generic coerce() path for i8*->i64 calls
                        # @atol() — correct for "parse this string's TEXT as
                        # a number" (e.g. .to(i64) on a str), but an i8* value
                        # crossing into an Any-typed FFI parameter is a raw
                        # pointer, not text to parse: an FFI-bound function
                        # expecting `Any` almost always wants the pointer's
                        # ADDRESS (a void* in C terms). atol() on a buffer of
                        # raw bytes reads them as if they were an ASCII
                        # decimal string — confirmed crashing (SIGSEGV)
                        # end-to-end. This path was never previously
                        # exercised: every existing FFI call site passes
                        # str-typed params directly (already i8*, no i64
                        # coercion needed) or an explicit Null literal
                        # (handled above) into Any slots — ptrtoint
                        # (reinterpret the address, don't parse it) is correct
                        # for every case that actually reaches here.
                        tmp_pi = self.new_tmp()
                        self.emit(f"  {tmp_pi} = ptrtoint i8* {v} to i64")
                        v = tmp_pi
                    else:
                        v = self.coerce(v, t, target_t)
                    t = target_t
                args_ir.append(f"{t} {v}")
            fn_ret = fn_obj.ret_type
            # bugs.log: an FFI binding with no `-> ret` means void (see
            # emit_ffi_bind) — matches this same default there. Non-FFI
            # internal functions keep the pre-existing i64 default
            # unchanged, since that's an established, separate behavior.
            ret_ir = type_map(fn_ret) if fn_ret else ("void" if is_ffi else "i64")
            # BUGFIX (bugs.log #1): call fn_obj.name, the actual emitted symbol,
            # which differs from target_name only for reserved-C-symbol collisions.
            if ret_ir == "void":
                # `%tmp = call void ...` is invalid IR — emit as a bare
                # statement and hand back a harmless placeholder value for
                # any expression-position caller (using a void FFI call's
                # "result" is a spec-level user error, not something codegen
                # needs to synthesize a real value for).
                self.emit(f"  call void @{fn_obj.name}({', '.join(args_ir)})")
                self._emit_error_propagation_check()
                return "0", "i64"
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call {ret_ir} @{fn_obj.name}({', '.join(args_ir)})")
            self._emit_error_propagation_check()
            return tmp, ret_ir

        # 4. PRIORITY 4: Dynamic Collection Access
        # Only reach here if it's NOT a function, NOT a built-in
        # Check if variable exists in any local scope or global scope
        var_exists = any(node.name in scope for scope in self.local_vars_stack) or node.name in self.global_vars
        if var_exists:
            col_v, col_t = self.emit_expr(Var(node.name))
            col_b = self.coerce_to_box(col_v, col_t)
            # Chain multiple get calls for nested access: col("key", 2) -> col["key"][2]
            # BUG-4: only the last hop copies (see emit_guarded_collection_get).
            last = len(node.args) - 1
            for i, arg in enumerate(node.args):
                idx_v, idx_t = self.emit_expr(arg)
                idx_b = self.coerce_to_box(idx_v, idx_t)
                col_b = self.emit_guarded_collection_get(col_b, idx_b, copy=(i == last))
            return col_b, "%Box*"

        # 4.5 Class field access via call syntax: e.g., inv() inside class → loads field "inv"
        if self.is_class_field(node.name):
            field_val, field_t = self.emit_field_access(Var("__self"), node.name)
            # If args provided, treat as collection access on the field
            if node.args:
                col_b = self.coerce_to_box(field_val, field_t)
                last = len(node.args) - 1
                for i, arg in enumerate(node.args):
                    idx_v, idx_t = self.emit_expr(arg)
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    # BUGFIX (bugs.log #16): was the unguarded collection_get,
                    # so a missing key (e.g. quantities(sku) for an unknown
                    # sku) crashed the whole program even inside try/error —
                    # unlike the near-identical branch just above (4.), which
                    # already used the guarded version.
                    col_b = self.emit_guarded_collection_get(col_b, idx_b, copy=(i == last))
                return col_b, "%Box*"
            return field_val, field_t

        # 5. PRIORITY 5: Class instantiation (ClassName())
        if node.name in self.class_defs:
            struct_t = self.class_ir_type(node.name)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call {struct_t}* @_{node.name}_new()")
            return tmp, f"{struct_t}*"

        raise RubidiumNameError(f"Undefined function or variable: {node.name}")

    def emit_method_call_expr(self, node):
        # 1. Resolve the object name
        obj_name = node.obj.name if hasattr(node.obj, 'name') else str(node.obj)

        # 0. File handle method calls — intercept before any other logic
        if obj_name in self._file_handle_vars:
            return self.emit_file_handle_method(obj_name, node.method, node.args)

        # 0a. Handle thread.wait and thread.running on the 'thread' module
        if obj_name == "thread" and node.method == "wait":
            for texpr in node.args:
                tid_v, tid_t = self.emit_expr(texpr)
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @_thread_smart_wait(i64 {tid_v})")
            return "0", "i64"
        if obj_name == "thread" and node.method == "running":
            tid_v, tid_t = self.emit_expr(node.args[0])
            tid_v = self.coerce(tid_v, tid_t, "i64")
            result = self.new_tmp()
            self.emit(f"  {result} = call i1 @_thread_is_running(i64 {tid_v})")
            return result, "i1"

        # 2. Handle .to() type conversion for any type (numeric, string, float)
        if node.method == "to" and isinstance(node.obj, Var) and node.args:
            val, val_t = self.emit_expr(node.obj)
            arg = node.args[0]
            if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                target_type = arg.name
            elif hasattr(arg, 'value'):
                target_type = arg.value
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            # Special handling for numeric->string
            if target_ir == "i8*":
                return self.coerce_to_string(val, val_t), "i8*"
            # If the object is a string (i8*) and we are converting to a non-string type, parse the string
            if val_t == "i8*" and target_ir != "i8*":
                if target_ir in self._INT_IR_SET:
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = call i64 @atol(i8* {val})")
                    # Now, if target_ir is not i64, we need to truncate or sign-extend
                    if target_ir == "i32":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = trunc i64 {tmp} to i32")
                        return tmp2, target_ir
                    elif target_ir == "i1":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = trunc i64 {tmp} to i1")
                        return tmp2, target_ir
                    elif target_ir == "i64":
                        return tmp, target_ir
                    else: # wider than i64
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = sext i64 {tmp} to {target_ir}")
                        return tmp2, target_ir
                elif target_ir in self._FLOAT_IR_SET:
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = call double @strtod(i8* {val}, i8* null)")
                    if target_ir == "float":
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = fptrunc double {tmp} to float")
                        return tmp2, target_ir
                    elif target_ir == "double":
                        return tmp, target_ir
                    else: # fp128
                        tmp2 = self.new_tmp()
                        self.emit(f"  {tmp2} = fpext double {tmp} to fp128")
                        return tmp2, target_ir
            return self.coerce(val, val_t, target_ir), target_ir
        
        # 2. Handle Collection 'set' method separately
        # This bypasses the class method logic entirely
        if node.method == "set" and isinstance(node.obj, FnCall):
            return self.emit_collection_set(node)
        
        # 2b. Handle Collection 'add' method on FnCall (e.g., chars().add(c))
        if node.method == "add" and isinstance(node.obj, FnCall):
            return self.emit_collection_add(node)
        
        # 2a. Handle set on MethodCall (e.g., p.scores(0).set(99))
        if node.method == "set" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) - inner is MethodCall(Var('p'), 'scores', [Number(0)])
                    # We need to get the field value, then do collection_set with the index
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    # Get the index from inner.args[0]
                    idx_v, idx_t = self.emit_expr(inner.args[0])
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    
                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_b = self.coerce_to_box(val_v, val_t)
                    
                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2a2. Handle add on MethodCall (e.g., p.scores().add(50))
        if node.method == "add" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores().add(50) - inner is MethodCall(Var('p'), 'scores', [])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    if len(node.args) == 1:
                        val_v, val_t = self.emit_expr(node.args[0])
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
                    elif len(node.args) == 2:
                        key_v, key_t = self.emit_expr(node.args[0])
                        val_v, val_t = self.emit_expr(node.args[1])
                        key_b = self.coerce_to_box(key_v, key_t)
                        val_b = self.coerce_to_box(val_v, val_t)
                        self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2a2b. Handle add on Var where Var is a class field (e.g., scores().add(0) inside a class method)
        if node.method == "add" and isinstance(node.obj, Var) and self.is_class_field(obj_name):
            field_val, field_t = self.emit_field_access(Var("__self"), obj_name)
            col_b = self.coerce_to_box(field_val, field_t)
            
            if len(node.args) == 1:
                val_v, val_t = self.emit_expr(node.args[0])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
            elif len(node.args) == 2:
                key_v, key_t = self.emit_expr(node.args[0])
                val_v, val_t = self.emit_expr(node.args[1])
                key_b = self.coerce_to_box(key_v, key_t)
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
            return "0", "i64"

        # 2a2b2. Handle add on a bare Var that's a plain local/global/parameter
        # collection (e.g. `val.add(7)` where val: Any/list — spec's Link Rule
        # example uses this exact form, with no () marker before .add).
        if node.method == "add" and isinstance(node.obj, Var) and not self.is_class_field(obj_name):
            var_exists = any(obj_name in scope for scope in self.local_vars_stack) or obj_name in self.global_vars
            if var_exists:
                col_v, col_t = self.emit_expr(node.obj)
                col_b = self.coerce_to_box(col_v, col_t)
                if len(node.args) == 1:
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_b = self.coerce_to_box(val_v, val_t)
                    self.emit(f"  call void @collection_add1(%Box* {col_b}, %Box* {val_b})")
                elif len(node.args) == 2:
                    key_v, key_t = self.emit_expr(node.args[0])
                    val_v, val_t = self.emit_expr(node.args[1])
                    key_b = self.coerce_to_box(key_v, key_t)
                    val_b = self.coerce_to_box(val_v, val_t)
                    self.emit(f"  call void @dict_set(%Box* {col_b}, %Box* {key_b}, %Box* {val_b})")
                return "0", "i64"

        # 2a2c. Handle set on Var where Var is a class field (e.g., scores(0).set(0) inside a class method)
        if node.method == "set" and isinstance(node.obj, Var) and self.is_class_field(obj_name):
            field_val, field_t = self.emit_field_access(Var("__self"), obj_name)
            col_b = self.coerce_to_box(field_val, field_t)
            
            # For scores(0).set(0), the args are [0, 0] - index and value
            if len(node.args) >= 2:
                idx_v, idx_t = self.emit_expr(node.args[0])
                idx_b = self.coerce_to_box(idx_v, idx_t)
                val_v, val_t = self.emit_expr(node.args[1])
                val_b = self.coerce_to_box(val_v, val_t)
                self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
            return "0", "i64"

        # 2a3. Handle set on MethodCall where inner is a field access (e.g., p.scores(0).set(99))
        if node.method == "set" and isinstance(node.obj, MethodCall):
            inner = node.obj
            inner_obj_name = inner.obj.name if hasattr(inner.obj, 'name') else str(inner.obj)
            if inner_obj_name in self.instances:
                class_name = self.instances[inner_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == inner.method for f in cls.fields):
                    # p.scores(0).set(99) - inner is MethodCall(Var('p'), 'scores', [Number(0)])
                    field_val, field_t = self.emit_field_access(inner.obj, inner.method)
                    col_b = self.coerce_to_box(field_val, field_t)
                    
                    # Get the index from inner.args[0]
                    idx_v, idx_t = self.emit_expr(inner.args[0])
                    idx_b = self.coerce_to_box(idx_v, idx_t)
                    
                    # Get the value from outer.args[0]
                    val_v, val_t = self.emit_expr(node.args[0])
                    val_b = self.coerce_to_box(val_v, val_t)
                    
                    self.emit(f"  call void @collection_set(%Box* {col_b}, %Box* {idx_b}, %Box* {val_b})")
                    return "0", "i64"

        # 2b. Handle FieldAccess.set(value) for class fields (e.g., class_one.obj_val.set(99))
        if node.method == "set" and isinstance(node.obj, FieldAccess):
            return self.emit_field_assign(FieldAssign(node.obj.obj, node.obj.field, node.args[0]))

        # 2b. Handle built-in module method calls: time.sleep, random.shuffle, random.choice
        if obj_name == "time":
            if node.method == "sleep":
                secs_v, secs_t = self.emit_expr(node.args[0])
                secs_v = self.coerce(secs_v, secs_t, "i64")
                sec_i32 = self.new_tmp()
                self.emit(f"  {sec_i32} = trunc i64 {secs_v} to i32")
                self.emit(f"  call i32 @sleep(i32 {sec_i32})")
                return "0", "i64"
            if node.method == "wait":
                # Syntax says time.wait(n) waits for n seconds - use sleep, not usleep
                secs_v, secs_t = self.emit_expr(node.args[0])
                secs_v = self.coerce(secs_v, secs_t, "i64")
                sec_i32 = self.new_tmp()
                self.emit(f"  {sec_i32} = trunc i64 {secs_v} to i32")
                self.emit(f"  call i32 @sleep(i32 {sec_i32})")
                return "0", "i64"
            if node.method == "timer_start":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                # type_hint is second arg but we just need it for type checking
                self.emit(f"  call void @time_timer_start(i64 {tid_v}, double 0.0)")
                return "0", "i64"
            if node.method == "timer_pause":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @time_timer_pause(i64 {tid_v})")
                return "0", "i64"
            if node.method == "timer_stop":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                self.emit(f"  call void @time_timer_stop(i64 {tid_v})")
                return "0", "i64"
            if node.method == "timer_read":
                tid_v, tid_t = self.emit_expr(node.args[0])
                tid_v = self.coerce(tid_v, tid_t, "i64")
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call double @time_timer_read(i64 {tid_v})")
                return tmp, "double"

        if obj_name == "random":
            if node.method in ("int", "range"):
                # random.int(min, max) / random.range(min, max) → i64 in [min, max]
                lo_v, lo_t = self.emit_expr(node.args[0])
                hi_v, hi_t = self.emit_expr(node.args[1])
                lo_v = self.coerce(lo_v, lo_t, "i64")
                hi_v = self.coerce(hi_v, hi_t, "i64")
                r_raw, span, r_mod, result = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {r_raw} = call i64 @random()")
                self.emit(f"  {span} = sub i64 {hi_v}, {lo_v}")
                span1 = self.new_tmp()
                self.emit(f"  {span1} = add i64 {span}, 1")
                self.emit(f"  {r_mod} = srem i64 {r_raw}, {span1}")
                self.emit(f"  {result} = add i64 {r_mod}, {lo_v}")
                return result, "i64"

            if node.method == "float":
                # random.float(min, max) → double in [min, max)
                lo_v, lo_t = self.emit_expr(node.args[0])
                hi_v, hi_t = self.emit_expr(node.args[1])
                lo_v = self.coerce(lo_v, lo_t, "double")
                hi_v = self.coerce(hi_v, hi_t, "double")
                r_raw = self.new_tmp(); r_d = self.new_tmp(); rand_max = self.new_tmp()
                scaled = self.new_tmp(); span = self.new_tmp(); mul = self.new_tmp(); result = self.new_tmp()
                self.emit(f"  {r_raw} = call i64 @random()")
                self.emit(f"  {r_d} = sitofp i64 {r_raw} to double")
                self.emit(f"  {rand_max} = sitofp i64 2147483648 to double")  # RAND_MAX+1 for [min,max)
                self.emit(f"  {scaled} = fdiv double {r_d}, {rand_max}")
                self.emit(f"  {span} = fsub double {hi_v}, {lo_v}")
                self.emit(f"  {mul} = fmul double {scaled}, {span}")
                self.emit(f"  {result} = fadd double {lo_v}, {mul}")
                return result, "double"

            if node.method == "shuffle":
                # random.shuffle(list) — Fisher-Yates shuffle on collection
                col_v, col_t = self.emit_expr(node.args[0])
                col_b = self.coerce_to_box(col_v, col_t)
                len_v = self.new_tmp()
                self.emit(f"  {len_v} = call i32 @collection_len(%Box* {col_b})")
                idx_ptr = self.new_tmp()
                self.emit(f"  {idx_ptr} = alloca i32")
                self.emit(f"  store i32 0, i32* {idx_ptr}")
                cond_l, body_l, end_l = self.new_label("sfl_cond"), self.new_label("sfl_body"), self.new_label("sfl_end")
                self.emit(f"  br label %{cond_l}\n{cond_l}:")
                cur_i, cond = self.new_tmp(), self.new_tmp()
                self.emit(f"  {cur_i} = load i32, i32* {idx_ptr}")
                self.emit(f"  {cond} = icmp slt i32 {cur_i}, {len_v}")
                self.emit(f"  br i1 {cond}, label %{body_l}, label %{end_l}\n{body_l}:")
                # pick random j in [i, len)
                range_v, j_v, j_add = self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {range_v} = sub i32 {len_v}, {cur_i}")
                r_raw_64 = self.new_tmp()
                self.emit(f"  {r_raw_64} = call i64 @random()")
                r_raw_32 = self.new_tmp()
                self.emit(f"  {r_raw_32} = trunc i64 {r_raw_64} to i32")
                self.emit(f"  {j_v} = srem i32 {r_raw_32}, {range_v}")
                self.emit(f"  {j_add} = add i32 {j_v}, {cur_i}")
                # Safe swap using list_swap (no ownership issues)
                self.emit(f"  call void @list_swap(%Box* {col_b}, i32 {cur_i}, i32 {j_add})")
                inc_i = self.new_tmp()
                self.emit(f"  {inc_i} = add i32 {cur_i}, 1")
                self.emit(f"  store i32 {inc_i}, i32* {idx_ptr}")
                self.emit(f"  br label %{cond_l}\n{end_l}:")
                return "0", "i64"

            if node.method == "seed":
                seed_v, seed_t = self.emit_expr(node.args[0])
                if seed_t == "i8*":
                    seed_hash = self.new_tmp()
                    self.emit(f"  {seed_hash} = call i32 @_rub_str_hash(i8* {seed_v})")
                    self.emit(f"  call void @srandom(i32 {seed_hash})")
                else:
                    seed_i = self.coerce(seed_v, seed_t, "i64")
                    seed_i32 = self.new_tmp()
                    self.emit(f"  {seed_i32} = trunc i64 {seed_i} to i32")
                    self.emit(f"  call void @srandom(i32 {seed_i32})")
                return "0", "i64"

            if node.method == "choice":
                # random.choice(collection) — pick random element/key
                col_v, col_t = self.emit_expr(node.args[0])
                col_b = self.coerce_to_box(col_v, col_t)
                len_v = self.new_tmp()
                self.emit(f"  {len_v} = call i32 @collection_len(%Box* {col_b})")
                # Guard: if len == 0 return box_i(0) to avoid division by zero
                safe_l, zero_l, merge_l = self.new_label("choice_safe"), self.new_label("choice_zero"), self.new_label("choice_merge")
                cmp_v = self.new_tmp()
                self.emit(f"  {cmp_v} = icmp sgt i32 {len_v}, 0")
                self.emit(f"  br i1 {cmp_v}, label %{safe_l}, label %{zero_l}\n{safe_l}:")
                r_raw, r_idx = self.new_tmp(), self.new_tmp()
                r_raw_64 = self.new_tmp()
                self.emit(f"  {r_raw_64} = call i64 @random()")
                r_raw_32 = self.new_tmp()
                self.emit(f"  {r_raw_32} = trunc i64 {r_raw_64} to i32")
                self.emit(f"  {r_idx} = srem i32 {r_raw_32}, {len_v}")
                result_safe = self.new_tmp()
                self.emit(f"  {result_safe} = call %Box* @collection_get_at(%Box* {col_b}, i32 {r_idx})")
                result_copy = self.new_tmp()
                self.emit(f"  {result_copy} = call %Box* @box_copy(%Box* {result_safe})")
                self.emit(f"  br label %{merge_l}\n{zero_l}:")
                zero_box = self.new_tmp()
                self.emit(f"  {zero_box} = call %Box* @box_i(i64 0)")
                self.emit(f"  br label %{merge_l}\n{merge_l}:")
                result = self.new_tmp()
                self.emit(f"  {result} = phi %Box* [ {result_copy}, %{safe_l} ], [ {zero_box}, %{zero_l} ]")
                return result, "%Box*"

        # 2c. Handle MethodCall on class instances where method is actually a field (e.g., p.scores(0))
        # This must come BEFORE the class method check
        if isinstance(node.obj, Var) and obj_name in self.instances:
            class_name = self.instances[obj_name]
            cls = self.class_defs.get(class_name)
            if cls and any(f.name == node.method for f in cls.fields):
                # This is a field access on a class instance - get the field value
                field_val, field_t = self.emit_field_access(node.obj, node.method)
                if node.args:
                    # Collection access on the field - e.g., p.scores(0) or p.scores().add(50)
                    # For p.scores(0), this returns the element at index 0
                    # For p.scores().add(50), this is handled by the outer MethodCall
                    # Here we just need to handle p.scores(0) as a collection get
                    if node.method in ("set", "add"):
                        # This is handled by the outer MethodCall, so we shouldn't reach here
                        pass
                    else:
                        # p.scores(0) or p.dict_field("key", 0) — chain get for each arg
                        col_b = field_val
                        last = len(node.args) - 1
                        for i, arg in enumerate(node.args):
                            idx_v, idx_t = self.emit_expr(arg)
                            idx_b = self.coerce_to_box(idx_v, idx_t)
                            col_b = self.emit_guarded_collection_get(col_b, idx_b, copy=(i == last))
                        return col_b, "%Box*"
                return field_val, field_t

        # 2d. Handle FieldAccess on class instances (e.g., p.scores(0).set(99))
        if isinstance(node.obj, FieldAccess):
            field_obj = node.obj.obj
            field_name = node.obj.field
            field_obj_name = field_obj.name if hasattr(field_obj, 'name') else str(field_obj)
            if field_obj_name in self.instances:
                class_name = self.instances[field_obj_name]
                cls = self.class_defs.get(class_name)
                if cls and any(f.name == field_name for f in cls.fields):
                    # This is a field access on a class instance - get the field value
                    field_val, field_t = self.emit_field_access(field_obj, field_name)
                    if node.method in ("set", "add"):
                        # Collection operation on a class field
                        if node.method == "set":
                            return self.emit_collection_set_on_field(node, field_val, field_t)
                        else:
                            return self.emit_collection_add_on_field(node, field_val, field_t)
                    # For other methods (e.g. `p.data.has("a")`), treat as a
                    # method call on the field value: reroute node.obj to a
                    # synthetic local holding it, then fall through to the
                    # normal Var-based method-call handling below.
                    #
                    # OPEN-D: this synthetic Var used to be backed by a plain
                    # self.new_tmp() SSA name (e.g. %t7), but every lookup of
                    # a Var goes through get_var_ptr(), which unconditionally
                    # expects a LOCAL variable's pointer to be named
                    # "%ptr_<name>" and the name to be registered in
                    # local_vars_stack. Neither was true here, so emit_expr
                    # on the reassigned node.obj a few lines down always
                    # raised "Undefined variable '_field_temp_<field>'" —
                    # this whole fallback path was unreachable for any method
                    # without special-cased handling above (e.g. .has() on a
                    # class field accessed via dotted syntax, `p.data(0)` was
                    # unaffected since indexed access is handled earlier).
                    # Naming the alloca "%ptr_<name>" and registering the name
                    # in local_vars_stack (guarded by _alloca_emitted the same
                    # way every other local is, so calling this twice in one
                    # function — e.g. `p.data.has("a")` then `p.data.has("b")`
                    # later in the same body — doesn't redefine the SSA value)
                    # makes it a real, lookup-able local like any other.
                    temp_name = f"_field_temp_{field_name}"
                    node.obj = Var(temp_name)
                    ptr_str = f"%ptr_{temp_name}"
                    if temp_name not in self._alloca_emitted:
                        self.emit(f"  {ptr_str} = alloca {field_t}")
                        self._alloca_emitted.add(temp_name)
                    self.emit(f"  store {field_t} {field_val}, {field_t}* {ptr_str}")
                    self.local_vars_stack[-1][temp_name] = field_t
                    # Continue to fallback handling below

        # 3. Handle Class Method Calls
        if obj_name in self.instances:
            class_name = self.instances[obj_name]
            mangled = self.method_ir_name(class_name, node.method)

            if mangled in self.functions:
                fn = self.functions[mangled]
                # fn.params has the implicit __self prepended — the
                # user-visible call (p.method(args)) never includes it.
                node.args = self._resolve_call_args(
                    f"{obj_name}.{node.method}", fn.params[1:], getattr(fn, "defaults", {}), node.args)
                self._check_arg_count(f"{obj_name}.{node.method}", fn.params[1:], node.args)
                struct_t = self.class_ir_type(class_name)
                ptr_str, _ = self.get_var_ptr(obj_name)

                inst_ptr = self.new_tmp()
                self.emit(f"  {inst_ptr} = load {struct_t}*, {struct_t}** {ptr_str}")

                args_ir = [f"{struct_t}* {inst_ptr}"]
                for i, arg_node in enumerate(node.args):
                    v, t = self.emit_expr(arg_node)
                    if i + 1 < len(fn.params):
                        expected_t = self.rubi_type_to_ir(fn.params[i + 1][1])
                        v = self.coerce(v, t, expected_t)
                        args_ir.append(f"{expected_t} {v}")
                    else:
                        args_ir.append(f"{t} {v}")

                ret_t = self.rubi_type_to_ir(fn.ret_type) if fn.ret_type else "i64"
                tmp = self.new_tmp()
                if ret_t == "void":
                    self.emit(f"  call void @{mangled}({', '.join(args_ir)})")
                    self._emit_error_propagation_check()
                    return "0", "i64"
                self.emit(f"  {tmp} = call {ret_t} @{mangled}({', '.join(args_ir)})")
                self._emit_error_propagation_check()
                return tmp, ret_t
            else:
                raise RubidiumNameError(f"Class '{class_name}' has no method '{node.method}'")

        # 4. Fallback: String method OR module function call (e.g. math.add -> math_add)
        # Check for module-namespaced function call before evaluating the object
        if isinstance(node.obj, Var):
            # Resolve import alias (e.g. 'mt' -> 'math_tools') before building target name
            resolved_name = self.import_aliases.get(obj_name, obj_name)
            target_name = f"{resolved_name}_{node.method}"
            # BUG-21: `helper.shared_list(0)` — indexed access into an imported
            # module's collection. Checked AFTER the function lookup below, so
            # a module function always wins the name.
            if target_name not in self.functions:
                merged = self._ns_global_name(node)
                if merged is not None:
                    return self.emit_call_expr(FnCall(merged, node.args))
            if target_name in self.functions:
                fn_obj = self.functions[target_name]
                node.args = self._resolve_call_args(
                    f"{obj_name}.{node.method}", fn_obj.params or [], getattr(fn_obj, "defaults", {}), node.args)
                self._check_arg_count(f"{obj_name}.{node.method}", fn_obj.params or [], node.args)
                # OPEN-12: coerce each arg to the callee's declared param type
                # (same class of bug as OPEN-11, here on the module-namespaced
                # function-call path — `tb.show(wrap_line)`). Without this, a
                # %Box*-typed value such as a `str` bound to a for-loop variable
                # was passed straight into an i8* parameter, arriving empty at
                # the callee (rendered as nothing).
                param_types = [self.rubi_type_to_ir(pt) for _, pt in fn_obj.params] if fn_obj.params else []
                args_ir = []
                for i, a in enumerate(node.args):
                    v, t = self.emit_expr(a)
                    if i < len(param_types):
                        v = self.coerce(v, t, param_types[i]); t = param_types[i]
                    args_ir.append(f"{t} {v}")
                tmp = self.new_tmp()
                fn_ret = fn_obj.ret_type
                ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
                # BUGFIX (bugs.log #1): call the real emitted symbol (fn_obj.name).
                self.emit(f"  {tmp} = call {ret_ir} @{fn_obj.name}({', '.join(args_ir)})")
                self._emit_error_propagation_check()
                return tmp, ret_ir

            # FFI bindings: c_lib.my_c_func -> method name alone is the bound symbol
            if node.method in self.functions:
                fn_def = self.functions[node.method]
                args_ir = []
                for i, a in enumerate(node.args):
                    v, t = self.emit_expr(a)
                    if i < len(fn_def.params):
                        target_t = self._ffi_type_to_ir(fn_def.params[i][1])
                        # GLFW FFI report: an explicit Null for an `Any` FFI
                        # param means the real NULL pointer (0), not a boxed
                        # Null-sentinel value (see the PRIORITY-3 call site's
                        # matching fix for the full explanation).
                        if fn_def.params[i][1] == "Any" and isinstance(a, None_):
                            v = "0"
                        else:
                            v = self.coerce(v, t, target_t)
                        t = target_t
                    args_ir.append(f"{t} {v}")
                tmp = self.new_tmp()
                fn_ret = fn_def.ret_type
                ret_ir = self._ffi_type_to_ir(fn_ret) if fn_ret else "i64"
                # BUGFIX (bugs.log #1): call fn_def.name (the safe emitted symbol),
                # not node.method (the raw, possibly-colliding native symbol name).
                self.emit(f"  {tmp} = call {ret_ir} @{fn_def.name}({', '.join(args_ir)})")
                return tmp, ret_ir

        # 4.5 Handle namespace.var.method() - e.g., wrap.glfw.glfwInit()
        # Resolves FieldAccess(Var(ns), attr).method() by trying ns_attr_method, ns_method, method
        if isinstance(node.obj, FieldAccess) and isinstance(node.obj.obj, Var):
            ns_name  = node.obj.obj.name
            attr_name = node.obj.field
            for target in (f"{ns_name}_{attr_name}_{node.method}", f"{ns_name}_{node.method}", node.method):
                if target in self.functions:
                    fn_obj = self.functions[target]
                    node.args = self._resolve_call_args(
                        f"{ns_name}.{attr_name}.{node.method}", fn_obj.params or [],
                        getattr(fn_obj, "defaults", {}), node.args)
                    self._check_arg_count(f"{ns_name}.{attr_name}.{node.method}", fn_obj.params or [], node.args)
                    # OPEN-12: coerce args to declared param types (see the
                    # module-function path above — same fix).
                    param_types = [self.rubi_type_to_ir(pt) for _, pt in fn_obj.params] if fn_obj.params else []
                    args_ir = []
                    for i, a in enumerate(node.args):
                        v, t = self.emit_expr(a)
                        if i < len(param_types):
                            v = self.coerce(v, t, param_types[i]); t = param_types[i]
                        args_ir.append(f"{t} {v}")
                    tmp = self.new_tmp()
                    fn_ret = fn_obj.ret_type
                    ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
                    self.emit(f"  {tmp} = call {ret_ir} @{fn_obj.name}({', '.join(args_ir)})")
                    self._emit_error_propagation_check()
                    return tmp, ret_ir

        # 4b. Object is a known import alias but the prefixed function wasn't registered.
        #     Emit a 'weak' stub definition instead of an extern declaration.
        #     Weak linkage means: if the real module is compiled in later, it wins;
        #     if not, the stub (returns a safe default) is used and the program still links.
        if isinstance(node.obj, Var) and obj_name in self.import_aliases:
            resolved = self.import_aliases.get(obj_name, obj_name)
            ext_name = f"{resolved}_{node.method}"
            args_ir, arg_types = [], []
            for a in node.args:
                v, t = self.emit_expr(a)
                args_ir.append(f"{t} {v}")
                arg_types.append(t)
            params_def = ", ".join(f"{t} %a{i}" for i, t in enumerate(arg_types))
            # BUG-1 class (RIG report, same root cause): anchor with the "("
            # that always follows an LLVM function name in a `define` line, so
            # an ext_name that's a prefix of another stub's name (e.g. "gl" vs
            # "glfw_init") can't false-positive as "already defined" via a bare
            # substring check and get silently skipped.
            stub_key = f"define weak i64 @{ext_name}("
            if not any(stub_key in d for d in self.global_decls):
                self.global_decls.append(
                    f"define weak i64 @{ext_name}({params_def}) {{\n"
                    f"entry:\n"
                    f"  ret i64 0\n"
                    f"}}\n"
                )
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @{ext_name}({', '.join(args_ir)})")
            return tmp, "i64"

        obj_val, obj_t = self.emit_expr(node.obj)
        
        # Handle .to() for numeric types (i64, i32, f64, f32, etc.)
        if node.method == "to" and obj_t in ("i32", "i64", "i1", "float", "double"):
            if len(node.args) == 1:
                # Check if arg is a Var with type-looking name (str, i32, etc.)
                arg = node.args[0]
                if hasattr(arg, 'name') and arg.name in ("i32", "i64", "i128", "i256", "i512", "i1024", "i2048", "f32", "f64", "f128", "f256", "f512", "f1024", "f2048", "str", "bool"):
                    target_type = arg.name
                elif hasattr(arg, 'value'):
                    target_type = arg.value
                else:
                    target_type = "i64"
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            # Special handling for numeric->string
            if target_ir == "i8*":
                return self.coerce_to_string(obj_val, obj_t), "i8*"
            return self.coerce(obj_val, obj_t, target_ir), target_ir
        
# Collection .len() check
        if obj_t == "%Box*" and node.method == "len" and not node.args:
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i32 @collection_len(%Box* {obj_val})")
            return tmp, "i32"

        # Collection .combine() — join all items as a string
        if obj_t == "%Box*" and node.method == "combine" and not node.args:
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i8* @list_combine(%Box* {obj_val})")
            return self._track_temp(tmp, "i8*"), "i8*"   # BUG-3

        # Collection/string .has() check — BUG-5: this must come BEFORE the
        # string dispatch below, not after. A `list`/`index`/`dict` global is
        # also a %Box*, so the string branch used to swallow every .has() call,
        # unbox the collection with box_to_cstr() and hand a non-string needle
        # (e.g. the integer 20) to strstr() as a pointer — an instant segfault.
        # @collection_has handles BOTH cases at runtime off the Box type tag:
        # substring search for a str Box, key/value scan for a collection.
        if obj_t == "%Box*" and node.method == "has" and node.args:
            needle_v, needle_t = self.emit_expr(node.args[0])
            needle_b = self.coerce_to_box(needle_v, needle_t)
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i1 @collection_has(%Box* {obj_val}, %Box* {needle_b})")
            return tmp, "i1"

        # String methods on Box*-typed variables (global scope): unbox to i8* first
        # Exception: numeric-returning methods on numeric Boxes must unbox via i64, not i8*
        known_str_methods_on_box = ("len", "to_int", "contains", "slice", "split",
                                    "concat", "combine", "to", "char",
                                    "set", "insert", "replace")
        if obj_t == "%Box*" and node.method in known_str_methods_on_box:
            if node.method in ("to_int", "to") and (not node.args or self._ffi_type_to_ir(
                    getattr(node.args[0], 'name', getattr(node.args[0], 'value', 'i64'))
                ) in self._INT_IR_SET):
                # Numeric unbox path: Box* → i64 directly
                i64_tmp = self.new_tmp()
                self.emit(f"  {i64_tmp} = call i64 @unbox_i(%Box* {obj_val})")
                return i64_tmp, "i64"
            unboxed = self.new_tmp()
            self.emit(f"  {unboxed} = call i8* @box_to_cstr(%Box* {obj_val})")
            unboxed = self._track_temp(unboxed, "i8*")   # BUG-3: fresh buffer
            return self.emit_string_method(unboxed, node.method, node.args)
        
        # Collection .combine() — join all items as a string
        if obj_t == "%Box*" and node.method == "combine" and not node.args:
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i8* @list_combine(%Box* {obj_val})")
            return self._track_temp(tmp, "i8*"), "i8*"   # BUG-3
        
        # bugs.log OPEN-9: dynamic (runtime) dispatch for a method call on a
        # value whose static class type is unknown — e.g. retrieved back out
        # of a generic list/dict/index. Only reached once every earlier,
        # statically-resolvable case above (string/collection methods, a
        # known instance variable, etc.) has already failed to match.
        if obj_t == "%Box*":
            candidates = [cn for cn, cls in self.class_defs.items()
                          if any(m.name == node.method for m in (cls.methods or []))]
            if candidates:
                self_ptr = self.new_tmp()
                self.emit(f"  {self_ptr} = call i8* @unbox_p(%Box* {obj_val})")
                cid_val = self.new_tmp()
                self.emit(f"  {cid_val} = call i64 @unbox_class_id(%Box* {obj_val})")
                # Evaluate call-site args ONCE, up front — every candidate
                # branch below reuses the same values, so an argument
                # expression's side effects don't happen once per candidate.
                arg_vals = [self.emit_expr(a) for a in node.args]

                def _emit_candidate_call(cn):
                    mfn = self.functions[self.method_ir_name(cn, node.method)]
                    struct_t = self.class_ir_type(cn)
                    casted = self.new_tmp()
                    self.emit(f"  {casted} = bitcast i8* {self_ptr} to {struct_t}*")
                    call_args = [f"{struct_t}* {casted}"]
                    for i, (v, t) in enumerate(arg_vals):
                        if i + 1 < len(mfn.params):
                            expected_t = self.rubi_type_to_ir(mfn.params[i + 1][1])
                            call_args.append(f"{expected_t} {self.coerce(v, t, expected_t)}")
                        else:
                            call_args.append(f"{t} {v}")
                    ret_ir = self.rubi_type_to_ir(mfn.ret_type) if mfn.ret_type else "i64"
                    res = self.new_tmp()
                    self.emit(f"  {res} = call {ret_ir} @{mfn.name}({', '.join(call_args)})")
                    self._emit_error_propagation_check()
                    # Every candidate's result is boxed uniformly to %Box*
                    # regardless of its own declared return type, so the
                    # merge point below (and callers like print()/coerce(),
                    # which already unbox %Box* generically) has one
                    # consistent type no matter which class actually ran.
                    return self.coerce_to_box(res, ret_ir)

                if len(candidates) == 1:
                    return _emit_candidate_call(candidates[0]), "%Box*"

                # Multiple classes define this method name: runtime switch on
                # class_id, merging each branch's boxed result through an
                # alloca (simpler and safer to generate correctly than a phi,
                # which needs exact predecessor-block bookkeeping).
                result_slot = self.new_tmp()
                self.emit(f"  {result_slot} = alloca %Box*")
                end_lbl = self.new_label("dyndispatch_end")
                default_lbl = self.new_label("dyndispatch_default")
                case_lbls = [self.new_label("dyndispatch_case") for _ in candidates]
                switch_cases = " ".join(
                    f"i64 {self.class_ids.get(cn, -1)}, label %{lbl}"
                    for cn, lbl in zip(candidates, case_lbls)
                )
                self.emit(f"  switch i64 {cid_val}, label %{default_lbl} [ {switch_cases} ]")
                for cn, lbl in zip(candidates, case_lbls):
                    self.emit(f"{lbl}:")
                    boxed_res = _emit_candidate_call(cn)
                    self.emit(f"  store %Box* {boxed_res}, %Box** {result_slot}")
                    self.emit(f"  br label %{end_lbl}")
                self.emit(f"{default_lbl}:")
                self.emit(f"  store %Box* null, %Box** {result_slot}")
                self.emit(f"  br label %{end_lbl}")
                self.emit(f"{end_lbl}:")
                final = self.new_tmp()
                self.emit(f"  {final} = load %Box*, %Box** {result_slot}")
                return final, "%Box*"

        if obj_t == "i8*":
            # Emit as string method; raises a clear RubidiumNameError on unknown methods
            # instead of silently falling through to a confusing "Undefined function" error.
            known_str_methods = ("len", "to_int", "contains", "slice", "split", "concat", "combine", "has", "to", "char", "set", "insert", "replace")
            if node.method not in known_str_methods:
                raise RubidiumNameError(
                    f"Unknown string method '.{node.method}()'. "
                    f"Available string methods: {', '.join(known_str_methods)}"
                )
            return self.emit_string_method(obj_val, node.method, node.args)

        # Not a string — treat as a module-namespaced function call (e.g. math.add -> math_add)
        target_name = f"{obj_name}_{node.method}"
        if target_name in self.functions:
            fn_obj = self.functions[target_name]
            node.args = self._resolve_call_args(
                f"{obj_name}.{node.method}", fn_obj.params or [], getattr(fn_obj, "defaults", {}), node.args)
            self._check_arg_count(f"{obj_name}.{node.method}", fn_obj.params or [], node.args)
            args_ir = []
            for a in node.args:
                v, t = self.emit_expr(a); args_ir.append(f"{t} {v}")
            tmp = self.new_tmp()
            fn_obj = self.functions[target_name]
            fn_ret = fn_obj.ret_type
            ret_ir = self.rubi_type_to_ir(fn_ret) if fn_ret else "i64"
            self.emit(f"  {tmp} = call {ret_ir} @{fn_obj.name}({', '.join(args_ir)})")
            self._emit_error_propagation_check()
            return tmp, ret_ir
        from rub_ast import FnCall as _FnCall
        return self.emit_call_expr(_FnCall(target_name, node.args))

    def emit_string_method(self, obj_val, method, args):
        # BUG-3: every i8*-returning string method below builds a FRESH buffer
        # (malloc/strndup/strdup/str_replace) that nothing ever freed — so a
        # loop doing `text.char(i)` leaked one allocation per call. Register
        # the result with the arena so it is released at block exit. The one
        # exception is `.to(str)`, which hands back the receiver unchanged;
        # tracking that would free a buffer this call does not own.
        #
        # BUGFIX (double free): this used to detect that passthrough case by
        # comparing `res != obj_val` (the ORIGINAL parameter). But
        # _emit_string_method_impl immediately reassigns its OWN local
        # `obj_val` to a fresh temp via _null_safe_str's `select` before any
        # method body runs — that reassignment is local to that function and
        # never visible here, so `.to(str)`'s "return obj_val unchanged" was
        # always returning a DIFFERENT temp name than the one this function
        # started with, making `res != obj_val` true even on a genuine
        # passthrough. That silently defeated the guard: the same heap
        # pointer already tracked once (by the box-unboxing call site, e.g.
        # `hand1(x).to(str)`) got tracked a SECOND time here, so the arena
        # freed it twice — confirmed crashing ("double free detected in
        # tcache") on any `tmp = tmp + " " + something(x).to(str)` pattern
        # inside a loop. `.to(str)` is the only method that can ever hand
        # back an unowned receiver, so key off the method name directly
        # instead of trying to detect it after the fact.
        res, res_t = self._emit_string_method_impl(obj_val, method, args)
        is_to_str_passthrough = (method == "to" and res_t == "i8*")
        if res_t == "i8*" and not is_to_str_passthrough:
            res = self._track_temp(res, "i8*")
        return res, res_t

    def _emit_string_method_impl(self, obj_val, method, args):
        # BUGFIX (see _null_safe_str above): the receiver could be a genuine
        # Null str at runtime — sanitize once here so every method below
        # (all of which eventually hand it to a raw libc string function)
        # is safe uniformly, instead of patching each one individually.
        obj_val = self._null_safe_str(obj_val)
        if method == "len":
            tmp = self.new_tmp()
            self.emit(f"  {tmp} = call i64 @strlen(i8* {obj_val})")
            return tmp, "i64"
        if method == "char" and len(args) == 1:
            # str.char(0) returns the character at 0-based index (per spec)
            idx_v, idx_t = self.emit_expr(args[0])
            idx_i = self.coerce(idx_v, idx_t, "i64")
            self._emit_str_index_bounds_check(obj_val, idx_i)
            char_ptr = self.new_tmp()
            self.emit(f"  {char_ptr} = getelementptr i8, i8* {obj_val}, i64 {idx_i}")
            char_val = self.new_tmp()
            self.emit(f"  {char_val} = load i8, i8* {char_ptr}")
            # Return as a single-char string by allocating and storing
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @malloc(i64 2)")
            char_store_ptr = self.new_tmp()
            self.emit(f"  {char_store_ptr} = getelementptr i8, i8* {result}, i64 0")
            self.emit(f"  store i8 {char_val}, i8* {char_store_ptr}")
            # Store null terminator
            next_ptr = self.new_tmp()
            self.emit(f"  {next_ptr} = getelementptr i8, i8* {result}, i64 1")
            self.emit(f"  store i8 0, i8* {next_ptr}")
            return result, "i8*"
        if method == "contains" and len(args) == 1:
            needle_v, needle_t = self.emit_expr(args[0])
            needle_v = self._null_safe_str(self.coerce(needle_v, needle_t, "i8*"))
            strstr_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {strstr_r} = call i8* @strstr(i8* {obj_val}, i8* {needle_v})")
            self.emit(f"  {tmp} = icmp ne i8* {strstr_r}, null")
            return tmp, "i1"
        if method == "has" and len(args) == 1:
            needle_v, needle_t = self.emit_expr(args[0])
            needle_v = self._null_safe_str(self.coerce(needle_v, needle_t, "i8*"))
            strstr_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {strstr_r} = call i8* @strstr(i8* {obj_val}, i8* {needle_v})")
            self.emit(f"  {tmp} = icmp ne i8* {strstr_r}, null")
            return tmp, "i1"
        if method == "to" or method == "to_int":
            if len(args) == 1:
                target_type = args[0].name if hasattr(args[0], 'name') else (args[0].value if hasattr(args[0], 'value') else "i64")
            else:
                target_type = "i64"
            target_ir = self.rubi_type_to_ir(target_type)
            tmp = self.new_tmp()
            if target_ir in ("i1", "i32", "i64"):
                self.emit(f"  {tmp} = call i64 @atol(i8* {obj_val})")
                tmp = self.coerce(tmp, "i64", target_ir)
            elif target_ir in ("float", "double", "fp128"):
                self.emit(f"  {tmp} = call double @strtod(i8* {obj_val}, i8* null)")
            elif target_ir == "i8*":
                return obj_val, "i8*"
            return tmp, target_ir
        if method == "slice" and len(args) == 2:
            start_v, start_t = self.emit_expr(args[0]); end_v, end_t = self.emit_expr(args[1])
            start_v = self.coerce(start_v, start_t, "i64"); end_v = self.coerce(end_v, end_t, "i64")
            # BUGFIX (same class as _emit_str_index_bounds_check above): an
            # end < start (or either out of [0, len]) turns `length` negative,
            # which strndup's size_t parameter reinterprets as an enormous
            # unsigned value — a massive out-of-bounds read/crash, not just a
            # Null-safety issue. end is allowed to equal len (an empty/
            # to-the-end slice), matching insert()'s "at end" allowance.
            self._emit_str_index_bounds_check(obj_val, start_v)
            self._emit_str_index_bounds_check(obj_val, end_v, allow_at_end=True)
            end_ok_l = self.new_label("slice_end_ok"); end_bad_l = self.new_label("slice_end_oob")
            end_before_start = self.new_tmp()
            self.emit(f"  {end_before_start} = icmp slt i64 {end_v}, {start_v}")
            self.emit(f"  br i1 {end_before_start}, label %{end_bad_l}, label %{end_ok_l}")
            self.emit(f"{end_bad_l}:")
            slice_err_lbl, slice_err_len = self.intern_str("Invalid index access")
            slice_err_ptr = self.new_tmp()
            self.emit(f"  {slice_err_ptr} = getelementptr [{slice_err_len} x i8], [{slice_err_len} x i8]* {slice_err_lbl}, i64 0, i64 0")
            self._emit_raise_or_propagate(slice_err_ptr)
            self.emit(f"{end_ok_l}:")
            length, src_ptr, result = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {length}  = sub i64 {end_v}, {start_v}")
            self.emit(f"  {src_ptr} = getelementptr i8, i8* {obj_val}, i64 {start_v}")
            self.emit(f"  {result}  = call i8* @strndup(i8* {src_ptr}, i64 {length})")
            return result, "i8*"
        if method == "concat" and len(args) == 1:
            other, _ = self.emit_expr(args[0])
            other = self._null_safe_str(other)
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {other})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {obj_val})")
            self.emit(f"  call i8* @strcat(i8* {buf}, i8* {other})")
            return buf, "i8*"
        if method == "split" and len(args) == 1:
            delim_v, delim_t = self.emit_expr(args[0])
            delim_v = self._null_safe_str(self.coerce(delim_v, delim_t, "i8*"))
            result = self.new_tmp()
            self.emit(f"  {result} = call %Box* @str_split(i8* {obj_val}, i8* {delim_v})")
            result = self._track_temp(result, "%Box*")   # BUG-3
            return result, "%Box*"
        if method == "combine" and len(args) == 1:
            other, _ = self.emit_expr(args[0])
            other = self._null_safe_str(other)
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {other})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {obj_val})")
            self.emit(f"  call i8* @strcat(i8* {buf}, i8* {other})")
            return buf, "i8*"
        if method == "set" and len(args) == 2:
            idx_v, idx_t = self.emit_expr(args[0])
            val_v, val_t = self.emit_expr(args[1])
            val_v = self._null_safe_str(val_v)
            idx_i = self.coerce(idx_v, idx_t, "i64")
            self._emit_str_index_bounds_check(obj_val, idx_i)
            char_val = self.new_tmp()
            self.emit(f"  {char_val} = load i8, i8* {val_v}")
            llen, total, buf = self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen} = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {total} = add i64 {llen}, 1")
            self.emit(f"  {buf} = call i8* @malloc(i64 {total})")
            self.emit(f"  call void @strcpy(i8* {buf}, i8* {obj_val})")
            char_ptr = self.new_tmp()
            self.emit(f"  {char_ptr} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  store i8 {char_val}, i8* {char_ptr}")
            return buf, "i8*"
        if method == "insert" and len(args) == 2:
            idx_v, idx_t = self.emit_expr(args[0])
            val_v, val_t = self.emit_expr(args[1])
            val_v = self._null_safe_str(val_v)
            idx_i = self.coerce(idx_v, idx_t, "i64")
            self._emit_str_index_bounds_check(obj_val, idx_i, allow_at_end=True)
            llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
            self.emit(f"  {llen}  = call i64 @strlen(i8* {obj_val})")
            self.emit(f"  {rlen}  = call i64 @strlen(i8* {val_v})")
            self.emit(f"  {total} = add i64 {llen}, {rlen}")
            self.emit(f"  {total2} = add i64 {total}, 1")
            self.emit(f"  {buf}   = call i8* @malloc(i64 {total2})")
            self.emit(f"  call void @strncpy(i8* {buf}, i8* {obj_val}, i64 {idx_i})")
            null_pos = self.new_tmp()
            self.emit(f"  {null_pos} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  store i8 0, i8* {null_pos}")
            val_ptr = self.new_tmp()
            self.emit(f"  {val_ptr} = getelementptr i8, i8* {buf}, i64 {idx_i}")
            self.emit(f"  call void @strcpy(i8* {val_ptr}, i8* {val_v})")
            src_ptr = self.new_tmp()
            self.emit(f"  {src_ptr} = getelementptr i8, i8* {obj_val}, i64 {idx_i}")
            self.emit(f"  call void @strcat(i8* {buf}, i8* {src_ptr})")
            return buf, "i8*"
        if method == "replace" and len(args) == 2:
            old_v, old_t = self.emit_expr(args[0])
            new_v, new_t = self.emit_expr(args[1])
            # BUGFIX: str_replace's own NULL guard (compiler.py) only covers
            # `str`/`old` — `new_str` goes straight into strlen() unguarded,
            # so `.replace("x", Null)` would crash the same way.
            new_v = self._null_safe_str(new_v)
            result = self.new_tmp()
            self.emit(f"  {result} = call i8* @str_replace(i8* {obj_val}, i8* {old_v}, i8* {new_v})")
            return result, "i8*"
        if method == "slice" and len(args) == 0:
            result = self.new_tmp()
            self.emit(f"  {result} = call %Box* @str_slice(i8* {obj_val})")
            # BUG-3: freshly built collection — block-scoped until bound.
            result = self._track_temp(result, "%Box*")
            return result, "%Box*"
        return "0", "i64"

    def emit_type_cast(self, node):
        # bugs.log OPEN-H / OPEN-J: `Null as str` on a runtime-tagged scalar
        # local/global used to leak the raw sentinel bit pattern instead of
        # "Null" — same root cause as OPEN-J's comparison bug (no runtime
        # signal to tell "genuinely Null" apart from "bits that merely
        # equal the sentinel"), fixed the same way: check the real flag
        # instead of formatting whatever bits the scalar holds. Only
        # intercepts a cast on a tagged Var; everything else (literals,
        # expressions, untagged vars — params/class fields, OPEN-J Phase 2)
        # goes through _emit_type_cast_body unchanged.
        to_t = self.rubi_type_to_ir(node.target_type)
        if to_t == "i8*" and isinstance(node.expr, Var):
            flag_ptr = self.get_null_flag_ptr(node.expr.name)
            if flag_ptr is not None:
                flag = self.new_tmp()
                self.emit(f"  {flag} = load i1, i1* {flag_ptr}")
                null_l, real_l, done_l = self.new_label("cnull"), self.new_label("cnum"), self.new_label("cdone")
                self.emit(f"  br i1 {flag}, label %{null_l}, label %{real_l}")
                self.emit(f"{null_l}:")
                nl_lbl, nl_len = self.intern_str("Null")
                nptr = self.new_tmp()
                self.emit(f"  {nptr} = getelementptr [{nl_len} x i8], [{nl_len} x i8]* {nl_lbl}, i64 0, i64 0")
                self.emit(f"  br label %{done_l}")
                self.emit(f"{real_l}:")
                real_val, _ = self._emit_type_cast_body(node, to_t)
                self.emit(f"  br label %{done_l}")
                self.emit(f"{done_l}:")
                merged = self.new_tmp()
                self.emit(f"  {merged} = phi i8* [ {nptr}, %{null_l} ], [ {real_val}, %{real_l} ]")
                return merged, "i8*"
        return self._emit_type_cast_body(node, to_t)

    def _emit_type_cast_body(self, node, to_t):
        val, from_t = self.emit_expr(node.expr)
        tmp = self.new_tmp()
        if from_t == to_t: return val, to_t
        # Box* casts: delegate to coerce() which handles unbox_f/unbox_i/unbox_s
        if from_t == "%Box*":
            return self.coerce(val, from_t, to_t), to_t
        # Integer ↔ Integer — delegate to coerce() which handles all widths
        if from_t in self._INT_IR_SET and to_t in self._INT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        # Integer ↔ Float
        if from_t in self._INT_IR_SET and to_t in self._FLOAT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        if from_t in self._FLOAT_IR_SET and to_t in self._INT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        # Float ↔ Float
        if from_t in self._FLOAT_IR_SET and to_t in self._FLOAT_IR_SET:
            return self.coerce(val, from_t, to_t), to_t
        if from_t in self._INT_IR_SET and to_t == "i8*":
            buf, fmt_ptr = self.new_tmp(), self.new_tmp()
            fmt_lbl, flen = self.intern_str("%lld")
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            cv = self.coerce(val, from_t, "i64")
            self.emit(f"  call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, i64 {cv})")
            return self._track_temp(buf, "i8*"), "i8*"   # BUG-3
        if from_t in ("float","double") and to_t == "i8*":
            buf, fmt_ptr = self.new_tmp(), self.new_tmp()
            fmt_lbl, flen = self.intern_str("%g")
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            dv = self.coerce(val, from_t, "double")
            self.emit(f"  call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, double {dv})")
            return self._track_temp(buf, "i8*"), "i8*"   # BUG-3
        if from_t == "i8*" and to_t in ("i1","i64"):
            t2 = self.new_tmp()
            self.emit(f"  {t2} = call i64 @atol(i8* {val})")
            return self.coerce(t2, "i64", to_t), to_t
        return val, to_t

    def emit_math_block(self, node):
        """FEATURE: `(expr): TYPE` — compute the whole bracketed expression at
        TYPE's precision. Sets the _math_block_type override so every arithmetic
        op emitted while the inner expression is generated computes at that type
        (see emit_binop), then coerces the final result to it (covers the case
        where the inner expr has no arithmetic, e.g. `(5): f2048`). Save/restore
        makes nested typed blocks work: the inner block's type applies to its own
        ops, the outer's resumes afterward."""
        block_ir = self.rubi_type_to_ir(node.vtype)
        prev = self._math_block_type
        self._math_block_type = block_ir
        val, vt = self.emit_expr(node.expr)
        self._math_block_type = prev
        val = self.coerce(val, vt, block_ir)
        return val, block_ir

    def _cur_block_label(self):
        """The label of the basic block currently being appended to — the
        most recently emitted 'name:' line. Needed for `phi` nodes, which
        must name the EXACT predecessor block a value flows in from — not
        just whatever label a branch was emitted to, since that block may
        have branched further internally by the time control reaches the
        phi (e.g. the right-hand side of `and`/`or` containing its own
        guarded access or nested and/or).

        BUGFIX (found investigating an and/or compiler crash inside a for
        loop): self.emit() appends its argument as ONE self.fn_lines entry
        regardless of how many actual IR lines are packed into it —
        emit_for/emit_while routinely emit a `br` and the label it jumps to
        as a single call joined by '\\n' (e.g. `br i1 {cond}, label
        %{body_l}, label %{end_l}\\n{body_l}:`). The old version treated
        each fn_lines entry as exactly one line, so `entry.strip().endswith(
        ":")` matched on the WHOLE combined string and returned everything
        up to the trailing colon — including the leading `br` text — as the
        "label". A phi built from that produced literal branch-instruction
        text inside its predecessor list (`phi i1 [ %t8, %br i1 %t6, label
        %fbody3, label %fend5`), invalid IR that crashed clang's parser
        outright. Now splits each entry on '\\n' and scans those individual
        lines too, so a combined entry's trailing label is found on its own,
        cleanly separated from whatever instruction preceded it."""
        for entry in reversed(self.fn_lines):
            for line in reversed(entry.split("\n")):
                s = line.strip()
                if s.endswith(":") and not s.startswith(";"):
                    return s[:-1]
        return "entry"

    def _emit_short_circuit(self, node):
        """BUG (found via syntax sweep): `and`/`or` did not short-circuit —
        emit_binop's very first line unconditionally evaluated BOTH operands
        before any operator-specific logic ran, so `False and boom()` still
        called and crashed inside boom(). Short-circuiting is a foundational,
        universally-expected semantic for these operators (every language
        that has and/or short-circuits) and is the standard way to write a
        guard in Rubidium itself (`x != Null and x.field > 0`,
        `items.len() > 0 and items(0) == y`) — without it, guards like that
        crash instead of protecting anything.

        Implemented with real branching: the right side is only evaluated
        when the left side didn't already decide the result (False for
        `and`, True for `or`)."""
        l, lt = self.emit_expr(node.left)
        l_bool = self.to_bool(l, lt)
        left_block = self._cur_block_label()
        rhs_l = self.new_label("sc_rhs")
        result_l = self.new_label("sc_result")
        if node.op == "and":
            self.emit(f"  br i1 {l_bool}, label %{rhs_l}, label %{result_l}")
        else:  # or
            self.emit(f"  br i1 {l_bool}, label %{result_l}, label %{rhs_l}")
        self.emit(f"{rhs_l}:")
        r, rt = self.emit_expr(node.right)
        r_bool = self.to_bool(r, rt)
        rhs_end_block = self._cur_block_label()
        self.emit(f"  br label %{result_l}")
        self.emit(f"{result_l}:")
        tmp = self.new_tmp()
        self.emit(f"  {tmp} = phi i1 [ {l_bool}, %{left_block} ], [ {r_bool}, %{rhs_end_block} ]")
        return tmp, "i1"

    def _emit_checked_arith(self, op, l, r, common):
        """Real overflow-checked +, -, * for integers (see the BUG note
        above the llvm.*.with.overflow declarations near the top of this
        file). Detects overflow via LLVM's with.overflow intrinsics, then
        clamps to the type's min/max and reports the spec's documented
        non-fatal runtime warning — the exact same clamp-and-warn contract
        coerce()'s narrowing path already implements, just now also applied
        directly at the arithmetic site instead of only when a result
        happens to get narrowed by a later assignment."""
        intrinsic = {"+": "sadd", "-": "ssub", "*": "smul"}[op]
        struct_t = f"{{ {common}, i1 }}"
        agg = self.new_tmp()
        self.emit(f"  {agg} = call {struct_t} @llvm.{intrinsic}.with.overflow.{common}({common} {l}, {common} {r})")
        raw = self.new_tmp(); ovf = self.new_tmp()
        self.emit(f"  {raw} = extractvalue {struct_t} {agg}, 0")
        self.emit(f"  {ovf} = extractvalue {struct_t} {agg}, 1")

        # Clamp direction (MAX vs MIN) must come from the OPERANDS' signs,
        # never from the raw wrapped result (which is meaningless once
        # overflow has actually happened). Standard signed-overflow rules:
        #   add: both operands >= 0 -> true sum is too big  -> clamp MAX
        #        both operands <  0 -> true sum is too small -> clamp MIN
        #        (mixed signs can never overflow addition)
        #   sub (l - r): l>=0, r<0 -> l - r behaves like l + |r| -> clamp MAX
        #                l<0, r>=0 -> clamp MIN
        #                (same-sign operands can never overflow subtraction)
        #   mul: operands same sign -> true product positive -> clamp MAX
        #        operands different sign -> true product negative -> clamp MIN
        min_v, max_v = self._int_bounds(common)
        l_neg = self.new_tmp(); self.emit(f"  {l_neg} = icmp slt {common} {l}, 0")
        r_neg = self.new_tmp(); self.emit(f"  {r_neg} = icmp slt {common} {r}, 0")
        l_nonneg = self.new_tmp(); self.emit(f"  {l_nonneg} = xor i1 {l_neg}, true")
        r_nonneg = self.new_tmp(); self.emit(f"  {r_nonneg} = xor i1 {r_neg}, true")

        toward_max = self.new_tmp()
        if op == "+":
            self.emit(f"  {toward_max} = and i1 {l_nonneg}, {r_nonneg}")
        elif op == "-":
            self.emit(f"  {toward_max} = and i1 {l_nonneg}, {r_neg}")
        else:  # "*"
            self.emit(f"  {toward_max} = icmp eq i1 {l_neg}, {r_neg}")

        clamped = self.new_tmp()
        self.emit(f"  {clamped} = select i1 {toward_max}, {common} {max_v}, {common} {min_v}")
        result = self.new_tmp()
        self.emit(f"  {result} = select i1 {ovf}, {common} {clamped}, {common} {raw}")

        ovf32 = self.new_tmp()
        self.emit(f"  {ovf32} = zext i1 {ovf} to i32")
        tn_lbl, tn_len = self.intern_str(common)
        tn_ptr = self.new_tmp()
        self.emit(f"  {tn_ptr} = getelementptr [{tn_len} x i8], [{tn_len} x i8]* {tn_lbl}, i64 0, i64 0")
        self.emit(f"  call void @rub_overflow_check(i32 {ovf32}, i8* {tn_ptr})")
        return result

    def emit_binop(self, node):
        if node.op in ("and", "or"):
            return self._emit_short_circuit(node)
        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)

        # If either side is Box*, unbox to a concrete type first.
        # For arithmetic ops we unbox to numeric; for + we check box type at runtime.
        # Simple approach: unbox Box* to i64 for arithmetic, to i8* for string context.
        # For +, if the other operand is i8* or the box might be a string, use box_to_cstr.
        if node.op == "+":
            # Unbox any Box* to string when mixed with i8*
            if lt == "%Box*" and rt == "i8*":
                l = self.coerce_to_string(l, lt); lt = "i8*"
            elif lt == "i8*" and rt == "%Box*":
                r = self.coerce_to_string(r, rt); rt = "i8*"
            elif lt == "%Box*" and rt == "%Box*":
                # Both boxed — could be list+list (merge, per spec:
                # "Hello".slice() + [1,2,3] -> ["H","e","l","l","o",1,2,3]),
                # or two boxed scalars (int+int -> string concat in string
                # context). box_add() decides at runtime based on the
                # actual boxed type tag.
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = call %Box* @box_add(%Box* {l}, %Box* {r})")
                return tmp, "%Box*"
            # Handle Box* + float / float + Box* — unbox to float for arithmetic
            elif lt == "%Box*" and rt in self._FLOAT_IR_SET:
                l = self.coerce(l, lt, rt); lt = rt
            elif rt == "%Box*" and lt in self._FLOAT_IR_SET:
                r = self.coerce(r, rt, lt); rt = lt
        # For non-string arithmetic, unbox Box* to i64/double based on the other operand's type
        if lt == "%Box*" and node.op not in ("+",):
            target_t = "double" if rt in self._FLOAT_IR_SET else "i64"
            l = self.coerce(l, lt, target_t); lt = target_t
        if rt == "%Box*" and node.op not in ("+",):
            target_t = "double" if lt in self._FLOAT_IR_SET else "i64"
            r = self.coerce(r, rt, target_t); rt = target_t
        # After unboxing, if either is still Box* (both were Box* and op is not +),
        # default to i64 unboxing
        if lt == "%Box*": l = self.coerce(l, lt, "i64"); lt = "i64"
        if rt == "%Box*": r = self.coerce(r, rt, "i64"); rt = "i64"

        if node.op == "+":
            # String + String -> String concatenation
            if lt == "i8*" and rt == "i8*":
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {r})")
                return self._track_temp(buf, "i8*"), "i8*"   # BUG-3
            # String + Other -> Convert other to string and concatenate  
            if lt == "i8*" and rt != "i8*":
                str_r = self.coerce_to_string(r, rt)
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {str_r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {str_r})")
                return self._track_temp(buf, "i8*"), "i8*"   # BUG-3
            # Other + String -> Convert other to string and concatenate
            if lt != "i8*" and rt == "i8*":
                str_l = self.coerce_to_string(l, lt)
                llen, rlen, total, total2, buf = self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp(), self.new_tmp()
                self.emit(f"  {llen} = call i64 @strlen(i8* {str_l})")
                self.emit(f"  {rlen} = call i64 @strlen(i8* {r})")
                self.emit(f"  {total} = add i64 {llen}, {rlen}")
                self.emit(f"  {total2} = add i64 {total}, 1")
                self.emit(f"  {buf} = call i8* @malloc(i64 {total2})")
                self.emit(f"  call i8* @strcpy(i8* {buf}, i8* {str_l})")
                self.emit(f"  call i8* @strcat(i8* {buf}, i8* {r})")
                return self._track_temp(buf, "i8*"), "i8*"   # BUG-3
        # BUGFIX (bugs.log #17): was hardcoded to "double" (if either side
        # looked like a float) or otherwise always "i64" — completely
        # ignoring wider types. Any i128+ arithmetic silently computed at
        # i64 width (wrapping/losing precision for anything not fitting in
        # 64 bits), and fp128 arithmetic silently computed at double
        # precision. promote_type() (previously built but never actually
        # wired in anywhere) picks the widest of the two operand types.
        common = self.promote_type(lt, rt)
        # FEATURE (typed math block `(expr): TYPE`): inside such a block every
        # arithmetic operation is computed AT the block's type/precision, not the
        # promoted operand type. So `(10 as i32 / 3 as i32): f2048` narrows each
        # operand to i32 (10, 3) but then runs the division at f2048 -> 3.333...,
        # rather than i32's integer division -> 3. Only reached for numeric
        # arithmetic (string-concat `+` paths above already returned).
        if self._math_block_type is not None:
            common = self._math_block_type
        is_int = common in self._INT_IR_SET
        l = self.coerce(l, lt, common); r = self.coerce(r, rt, common)
        tmp = self.new_tmp()
        if is_int:
            # `and`/`or` are handled earlier in emit_binop, via
            # _emit_short_circuit — never reached here.
            # BUG (found via syntax sweep): +, -, * used to be plain,
            # unchecked LLVM add/sub/mul — real overflow-checked-and-clamped
            # arithmetic now runs for these (see the llvm.*.with.overflow
            # declarations and _emit_checked_arith above emit_binop). i1
            # (bool) has no overflow concept, matching coerce()'s narrowing
            # path, which also excludes i1.
            if node.op in ("+", "-", "*") and common != "i1":
                return self._emit_checked_arith(node.op, l, r, common), common
            instr = {"+":"add","-":"sub","*":"mul","/":"sdiv","%":"srem"}.get(node.op)
            if instr:
                if node.op in ("/", "%"):
                    # OPEN-7: division/modulo-by-zero is now ALWAYS guarded, not
                    # just inside a lexical try — a bare sdiv/srem by zero traps
                    # in hardware (SIGFPE) regardless of try, so this must run
                    # unconditionally and fall through to catch-or-propagate.
                    is_zero = self.new_tmp()
                    safe_l  = self.new_label("divok")
                    zero_l  = self.new_label("divzero")
                    err_lbl, err_len = self.intern_str("Division by zero")
                    err_ptr = self.new_tmp()
                    self.emit(f"  {is_zero} = icmp eq {common} {r}, 0")
                    self.emit(f"  br i1 {is_zero}, label %{zero_l}, label %{safe_l}")
                    self.emit(f"{zero_l}:")
                    self.emit(f"  {err_ptr} = getelementptr [{err_len} x i8], [{err_len} x i8]* {err_lbl}, i64 0, i64 0")
                    self._emit_raise_or_propagate(err_ptr)
                    self.emit(f"{safe_l}:")
                self.emit(f"  {tmp} = {instr} {common} {l}, {r}")
                return tmp, common
            # Handle power operator (**)
            if node.op == "**":
                # Call pow() from math library
                # NOTE: pow/root still compute at double precision even for
                # i128+ operands — a genuinely wide-precision pow is a much
                # bigger undertaking (needs its own arbitrary-precision
                # implementation) than fixing +,-,*,/,% arithmetic, which is
                # what was concretely broken/reported.
                l_d = self.coerce(l, common, "double")
                r_d = self.coerce(r, common, "double")
                pow_d = self.new_tmp()
                self.emit(f"  {pow_d} = call double @_rubidium_pow(double {l_d}, double {r_d})")
                # BUG (found via syntax sweep): this used to unconditionally
                # return "double" even for two INTEGER operands, unlike
                # every other operator in this branch (+,-,*,/,%), which all
                # return `common` (an int type). That made an int**int result
                # get formatted with the generic FLOAT print path — invisible
                # for small results (test.rub's own `2 ** 3` example prints
                # "8" either way), but for a result large enough that %g
                # switches to scientific notation, `print(2 ** 30)` showed
                # "1.07374e+09" instead of the plain integer
                # "1073741824" the debugger (and every other operator)
                # already gave. Convert back to the integer common type,
                # matching every sibling operator's return type.
                self.emit(f"  {tmp} = fptosi double {pow_d} to {common}")
                return tmp, common
            # Handle binary root operator (n */ value) -> value ** (1/n)
            if node.op == "*/":
                l_d = self.coerce(l, common, "double")
                r_d = self.coerce(r, common, "double")
                inv = self.new_tmp()
                self.emit(f"  {inv} = fdiv double 1.0, {l_d}")
                self.emit(f"  {tmp} = call double @_rubidium_pow(double {r_d}, double {inv})")
                return tmp, "double"
        else:
            instr = {"+":"fadd","-":"fsub","*":"fmul","/":"fdiv","%":"frem"}.get(node.op)
            if instr:
                self.emit(f"  {tmp} = {instr} {common} {l}, {r}")
                return tmp, common
            # Handle power operator (**) for float types
            if node.op == "**":
                l_d = self.coerce(l, common, "double")
                r_d = self.coerce(r, common, "double")
                self.emit(f"  {tmp} = call double @_rubidium_pow(double {l_d}, double {r_d})")
                return tmp, "double"
            # Handle binary root operator (n */ value) for float types
            if node.op == "*/":
                l_d = self.coerce(l, common, "double")
                inv = self.new_tmp()
                self.emit(f"  {inv} = fdiv double 1.0, {l_d}")
                r_d = self.coerce(r, common, "double")
                self.emit(f"  {tmp} = call double @_rubidium_pow(double {r_d}, double {inv})")
                return tmp, "double"
        return "0", "i64"

    def emit_compare(self, node):
        # Null comparisons: Null behaves as negative infinity per spec.
        left_is_null  = isinstance(node.left,  None_)
        right_is_null = isinstance(node.right, None_)
        if left_is_null or right_is_null:
            # Null op Null — always constant fold
            if left_is_null and right_is_null:
                result = {"==": True, "!=": False, "<": False, ">": False, "<=": True, ">=": True}[node.op]
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = add i1 0, {'1' if result else '0'}")
                return tmp, "i1"
            # Null op Literal (Number/Str/Bool/UnaryOp negation) — can constant fold safely
            non_null = node.right if left_is_null else node.left
            if isinstance(non_null, (Number, Str, Bool, UnaryOp)):
                if left_is_null:
                    result = {"==": False, "!=": True, "<": True, ">": False, "<=": True, ">=": False}[node.op]
                else:
                    result = {"==": False, "!=": True, "<": False, ">": True, "<=": False, ">=": True}[node.op]
                tmp = self.new_tmp()
                self.emit(f"  {tmp} = add i1 0, {'1' if result else '0'}")
                return tmp, "i1"
            # Null op Variable — if it's a runtime-tagged scalar local/global
            # (bugs.log OPEN-J), resolve with a REAL runtime check instead of
            # falling through to a raw sentinel-bit comparison below, which
            # can't tell "genuinely Null" apart from "merely computed/
            # clamped to the same bit pattern" (that exact collision is
            # OPEN-J), and is blind to Null-ness assigned on only one branch
            # of a runtime if/else (the old compile-time-only null_valued
            # set loses track of that; a real flag load does not).
            if isinstance(non_null, Var):
                flag_ptr = self.get_null_flag_ptr(non_null.name)
                if flag_ptr is not None:
                    flag = self.new_tmp()
                    self.emit(f"  {flag} = load i1, i1* {flag_ptr}")
                    null_result = {"==": True, "!=": False, "<": False, ">": False, "<=": True, ">=": True}[node.op]
                    if left_is_null:
                        nonnull_result = {"==": False, "!=": True, "<": True, ">": False, "<=": True, ">=": False}[node.op]
                    else:
                        nonnull_result = {"==": False, "!=": True, "<": False, ">": True, "<=": False, ">=": True}[node.op]
                    tmp = self.new_tmp()
                    self.emit(f"  {tmp} = select i1 {flag}, i1 {'1' if null_result else '0'}, i1 {'1' if nonnull_result else '0'}")
                    return tmp, "i1"
            # Untagged variable (param/class field/loop var — OPEN-J Phase 2)
            # — fall through to runtime comparison, unchanged from before.
            # Variable assigned Null stores 0, so 0 == 0 → True correctly for == and !=.
            # Note: ordinal comparisons here still can't distinguish genuine
            # Null from a same-bit-pattern clamped value (bugs.log OPEN-J).

        l, lt = self.emit_expr(node.left); r, rt = self.emit_expr(node.right)
        # BUG-7: the Null literal carries the pseudo-type "null" (see OPEN-4 in
        # emit_expr), which is NOT a real LLVM type. `let n: i32 = Null` then
        # `n == Null` reaches here as i32 vs "null", falls through to the
        # generic path at the bottom and emits `icmp eq null %n, -2147483648`
        # — invalid IR, so the whole program failed to compile. Resolve the
        # literal against the other operand's concrete type first; coerce()
        # already knows the right Null representation per type (the INT32_MIN
        # sentinel for ints, the float sentinel for floats, a null pointer for
        # strings), and coerce_to_box() produces a real Null-tagged Box.
        if lt == "null" and rt != "null":
            l = self.coerce_to_box(l, "null") if rt == "%Box*" else self.coerce(l, "null", rt)
            lt = rt
        elif rt == "null" and lt != "null":
            r = self.coerce_to_box(r, "null") if lt == "%Box*" else self.coerce(r, "null", lt)
            rt = lt
        # Collection equality (list/index/dict) — compare contents recursively
        if lt == "%Box*" and rt == "%Box*" and node.op in ("==", "!="):
            eq_i32, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {eq_i32} = call i32 @box_equal(%Box* {l}, %Box* {r})")
            pred = "ne" if node.op == "==" else "eq"
            self.emit(f"  {tmp} = icmp {pred} i32 {eq_i32}, 0")
            return tmp, "i1"
        # BUGFIX (bugs.log OPEN-10 follow-up): ordering comparison between two
        # boxed scalars (e.g. two polymorphic-typed globals promoted to
        # %Box*) — unbox both per their own runtime type tag rather than
        # falling into the generic path below, which would otherwise pick
        # %Box* as the "common" type and icmp the raw pointer ADDRESSES.
        if lt == "%Box*" and rt == "%Box*" and node.op in ("<", ">", "<=", ">="):
            cmp_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cmp_r} = call i32 @box_compare_num(%Box* {l}, %Box* {r})")
            pred = {"<":"slt", ">":"sgt", "<=":"sle", ">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i32 {cmp_r}, 0")
            return tmp, "i1"
        # BUGFIX (bugs.log OPEN-10 follow-up): exactly one side is a boxed
        # scalar (e.g. a variable redeclared elsewhere with a conflicting
        # type, promoted to %Box* everywhere) — unbox it to the other side's
        # concrete type before the generic comparison path below. Without
        # this, promote_type() picks %Box* as the "common" type and emits a
        # raw pointer icmp, comparing box ADDRESSES instead of their values.
        if lt == "%Box*" and rt != "%Box*":
            l = self.coerce(l, lt, rt); lt = rt
        elif rt == "%Box*" and lt != "%Box*":
            r = self.coerce(r, rt, lt); rt = lt
        if lt == "i8*" and rt == "i8*":
            # BUGFIX (found reviewing user code): a `str` value CAN
            # genuinely be a real null i8* pointer at runtime — Rubidium's
            # Null for `str` (see coerce()'s "null" handling) IS a null
            # pointer, not a sentinel byte pattern like the int/float
            # types use — reachable from an explicit `Null` literal (which
            # the coercion above this point already retypes to match the
            # OTHER side's "i8*", so it lands here, not in any dedicated
            # null-literal branch) or from a `str` variable a prior branch
            # left/reassigned to Null. Plain strcmp() on a NULL argument is
            # undefined behavior — confirmed segfaulting glibc's
            # implementation. rub_strcmp_null_safe (compiler.py) treats a
            # NULL side as "smaller than any real string," matching Null's
            # documented -infinity ordering for every other type, and
            # never dereferences a null pointer.
            cmp_r, tmp = self.new_tmp(), self.new_tmp()
            self.emit(f"  {cmp_r} = call i32 @rub_strcmp_null_safe(i8* {l}, i8* {r})")
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} i32 {cmp_r}, 0")
            return tmp, "i1"
        common = self.promote_type(lt, rt)
        l = self.coerce(l, lt, common); r = self.coerce(r, rt, common)
        tmp = self.new_tmp()
        if common in self._FLOAT_IR_SET:
            pred = {"==":"oeq","!=":"one","<":"olt",">":"ogt","<=":"ole",">=":"oge"}[node.op]
            self.emit(f"  {tmp} = fcmp {pred} {common} {l}, {r}")
        else:
            pred = {"==":"eq","!=":"ne","<":"slt",">":"sgt","<=":"sle",">=":"sge"}[node.op]
            self.emit(f"  {tmp} = icmp {pred} {common} {l}, {r}")
        return tmp, "i1"

    def coerce_to_box(self, val, t):
        if t == "%Box*": return val
        tmp = self.new_tmp()
        if t == "null":
            # OPEN-4: box the Null literal as its own real Box type (6),
            # never as a type==0 int holding the old sentinel value.
            self.emit(f"  {tmp} = call %Box* @box_null()")
            return tmp
        if t == "i1":
            v = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = call %Box* @box_b(i64 {v})")
        elif t in self._INT_IR_SET:
            # BUGFIX (bugs.log #17): previously only i32/i64 were routed
            # here — any wider type (i128+) fell through to the generic
            # `else` branch below, which did `bitcast {t} {val} to i8*`.
            # bitcast requires equal bit widths, so bitcasting e.g. an i128
            # value to a 64-bit pointer is invalid IR. The Box struct's
            # integer field is a 64-bit `long long`, so boxing (used for
            # collections/Any-type storage/print) is necessarily limited to
            # 64-bit precision regardless — narrow via the same clamping
            # coerce() path used elsewhere, rather than emitting invalid IR.
            v = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = call %Box* @box_i(i64 {v})")
        elif t in self._FLOAT_IR_SET:
            # Same reasoning as above — the Box struct's float field is a
            # 64-bit double, so fp128/etc narrow to double when boxed.
            v = self.coerce(val, t, "double")
            self.emit(f"  {tmp} = call %Box* @box_f(double {v})")
        elif t == "i8*":
            # bugs.log: a str-typed Null is a genuine null i8* pointer at
            # runtime (see _null_safe_str) — box_s() strdup()s it unguarded,
            # so boxing a Null str (e.g. list.add(s) where s: str = Null)
            # crashed. Can't just substitute an empty string like
            # _null_safe_str does for string methods: box_equal/print_boxed
            # distinguish a real Null (box type 6) from an empty string (box
            # type 2, s="") — substituting would silently turn Null into ""
            # once read back out of the collection. Branch instead, so a
            # genuine null pointer boxes as an actual box_null().
            is_null = self.new_tmp()
            self.emit(f"  {is_null} = icmp eq i8* {val}, null")
            null_l = self.new_label("box_s_null")
            str_l = self.new_label("box_s_str")
            join_l = self.new_label("box_s_join")
            self.emit(f"  br i1 {is_null}, label %{null_l}, label %{str_l}")
            self.emit(f"{null_l}:")
            null_box = self.new_tmp()
            self.emit(f"  {null_box} = call %Box* @box_null()")
            self.emit(f"  br label %{join_l}")
            self.emit(f"{str_l}:")
            str_box = self.new_tmp()
            self.emit(f"  {str_box} = call %Box* @box_s(i8* {val})")
            self.emit(f"  br label %{join_l}")
            self.emit(f"{join_l}:")
            self.emit(f"  {tmp} = phi %Box* [ {null_box}, %{null_l} ], [ {str_box}, %{str_l} ]")
        elif t.startswith("%class_") and t.endswith("*"):
            # bugs.log OPEN-9: tag the box with this class's id so a value
            # retrieved back out of a collection can still be dispatched to
            # the right class's method at runtime — a generic box_p would
            # lose the class identity entirely.
            cls_name = t[len("%class_"):-1]
            cid = self.class_ids.get(cls_name, -1)
            v = self.new_tmp()
            self.emit(f"  {v} = bitcast {t} {val} to i8*")
            self.emit(f"  {tmp} = call %Box* @box_class(i8* {v}, i64 {cid})")
        else:
            v = self.new_tmp()
            self.emit(f"  {v} = bitcast {t} {val} to i8*")
            self.emit(f"  {tmp} = call %Box* @box_p(i8* {v})")
        # BUG-3: every branch above (an already-%Box* value returns early and
        # is NOT touched) allocates a brand-new Box — most of them short-lived
        # keys and boxed arguments that nothing ever freed. Hand them to the
        # arena; a container that takes ownership calls rub_temp_untrack in the
        # runtime, so only genuinely temporary boxes are released.
        return self._track_temp(tmp, "%Box*")

    def coerce_to_string(self, val, t):
        """Convert a value of any type to a string (i8*)"""
        if t == "i8*":
            return val
        tmp = self.new_tmp()
        buf = self.new_tmp()
        if t == "%Box*":
            # box_to_cstr always returns a fresh heap buffer (BUG-3).
            self.emit(f"  {tmp} = call i8* @box_to_cstr(%Box* {val})")
            return self._track_temp(tmp, "i8*")
        if t == "i1":
            true_lbl, true_len = self.intern_str("True")
            false_lbl, false_len = self.intern_str("False")
            true_ptr = self.new_tmp(); false_ptr = self.new_tmp(); sel_ptr = self.new_tmp()
            self.emit(f"  {true_ptr} = getelementptr [{true_len} x i8], [{true_len} x i8]* {true_lbl}, i64 0, i64 0")
            self.emit(f"  {false_ptr} = getelementptr [{false_len} x i8], [{false_len} x i8]* {false_lbl}, i64 0, i64 0")
            self.emit(f"  {sel_ptr} = select i1 {val}, i8* {true_ptr}, i8* {false_ptr}")
            return sel_ptr
        if t in ("i32", "i64"):
            fmt_lbl, flen = self.intern_str("%lld")
            fmt_ptr = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            cv = self.coerce(val, t, "i64")
            self.emit(f"  {tmp} = call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, i64 {cv})")
            return self._track_temp(buf, "i8*")   # BUG-3: fresh buffer
        if t == "i128":
            # BUGFIX (bugs.log #17): previously fell through to the generic
            # `else: return val` below, which returned the raw i128 bit
            # pattern completely unconverted — later code (e.g. strlen/
            # strcpy on the "string") would treat that as a garbage pointer.
            self.emit(f"  {tmp} = call i8* @i128_to_str(i128 {val})")
            return self._track_temp(tmp, "i8*")   # BUG-3
        if t in ("i256", "i512", "i1024", "i2048"):
            # OPEN-6: true bignum string conversion — previously routed
            # through the i32/i64 branch above via sprintf("%lld", ...),
            # which silently narrowed to i64 first.
            ptr64, n = self._emit_bignum_ptr(val, t)
            self.emit(f"  {tmp} = call i8* @bignum_to_str(i64* {ptr64}, i32 {n})")
            return self._track_temp(tmp, "i8*")   # BUG-3
        elif t == "fp128":
            # Float-precision fix: same reasoning as emit_print's fp128 case —
            # exact decimal conversion straight from the raw bits, no double
            # intermediate (and the exact-string length isn't bounded, unlike
            # double's %g, so this calls its own malloc rather than using the
            # fixed 32-byte `buf` above).
            lo, hi = self._emit_fp128_halves(val)
            self.emit(f"  {tmp} = call i8* @fp128_to_exact_decimal_str(i64 {lo}, i64 {hi})")
            return self._track_temp(tmp, "i8*")   # BUG-3
        elif t in ("float", "double"):
            fmt_lbl, flen = self.intern_str("%g")
            fmt_ptr = self.new_tmp()
            self.emit(f"  {buf} = call i8* @malloc(i64 32)")
            self.emit(f"  {fmt_ptr} = getelementptr [{flen} x i8], [{flen} x i8]* {fmt_lbl}, i64 0, i64 0")
            dv = self.coerce(val, t, "double")
            self.emit(f"  {tmp} = call i32 (i8*, i8*, ...) @sprintf(i8* {buf}, i8* {fmt_ptr}, double {dv})")
            return self._track_temp(buf, "i8*")   # BUG-3
        else:
            return val

    def coerce(self, val, from_t, to_t):
        if from_t == to_t: return val
        tmp = self.new_tmp()

        # ---- OPEN-4: Null literal (see emit_expr's None_ handling) ----
        if from_t == "null":
            if to_t == "%Box*": return self.coerce_to_box(val, from_t)
            if to_t == "i8*": return "null"
            if to_t in self._FLOAT_IR_SET: return self._NULL_SENTINEL_FLOAT
            # Any raw int width: the sentinel value itself is already the
            # correct unboxed scalar representation (unchanged from before
            # this pseudo-type existed).
            return val

        # ---- Unbox Box* to concrete type ----
        if from_t == "%Box*":
            if to_t in self._INT_IR_SET:
                # Unbox to i64 then widen/narrow
                i64_tmp = self.new_tmp()
                self.emit(f"  {i64_tmp} = call i64 @unbox_i(%Box* {val})")
                if to_t == "i64": return i64_tmp
                if to_t == "i1":
                    self.emit(f"  {tmp} = trunc i64 {i64_tmp} to i1"); return tmp
                if to_t == "i32":
                    self.emit(f"  {tmp} = trunc i64 {i64_tmp} to i32"); return tmp
                # Wider than i64 — sext
                self.emit(f"  {tmp} = sext i64 {i64_tmp} to {to_t}"); return tmp
            if to_t in self._FLOAT_IR_SET:
                dbl_tmp = self.new_tmp()
                self.emit(f"  {dbl_tmp} = call double @unbox_f(%Box* {val})")
                if to_t == "double": return dbl_tmp
                if to_t == "float":
                    self.emit(f"  {tmp} = fptrunc double {dbl_tmp} to float"); return tmp
                if to_t == "fp128":
                    self.emit(f"  {tmp} = fpext double {dbl_tmp} to fp128"); return tmp
                return dbl_tmp
            if to_t == "i8*":
                # BUG-4: unbox_s() hands back the Box's OWN ->s buffer, so a
                # `str` read out of a collection aliased the collection's
                # characters — dropping either one invalidated the other.
                # unbox_s_dup returns an owned copy; the arena (BUG-3) frees it
                # at block exit unless it is bound to something longer-lived.
                dup = self.new_tmp()
                self.emit(f"  {dup} = call i8* @unbox_s_dup(%Box* {val})")
                self.emit(f"  {tmp} = call i8* @rub_temp_track_str(i8* {dup})")
                return tmp
            # Pointer cast
            p_tmp = self.new_tmp()
            self.emit(f"  {p_tmp} = call i8* @unbox_p(%Box* {val})")
            self.emit(f"  {tmp} = bitcast i8* {p_tmp} to {to_t}"); return tmp

        if to_t == "%Box*": return self.coerce_to_box(val, from_t)

        # ---- i8* (string) special case: 0 or Null sentinel converts to null pointer ----
        if to_t == "i8*" and from_t in self._INT_IR_SET:
            if val == "0" or val == self._NULL_SENTINEL:
                return "null"
            self.emit(f"  {tmp} = inttoptr {from_t} {val} to i8*")
            return tmp

        # ---- Integer ↔ Integer ----
        if from_t in self._INT_IR_SET and to_t in self._INT_IR_SET:
            fr = self._TYPE_RANK.get(from_t, 2)
            tr = self._TYPE_RANK.get(to_t, 2)
            if fr == tr: return val
            if fr > tr:  # narrowing → clamp + trunc
                # BUGFIX (bugs.log #4): a raw `trunc` here silently wraps
                # out-of-range values — and for i32 specifically, a value one
                # over INT32_MAX truncates to exactly INT32_MIN, which is
                # also this compiler's Null sentinel, so genuine overflow was
                # being displayed/read back as Null. The syntax file's
                # "Integer Overflow" section requires clamping to the target
                # type's min/max and reporting a (non-fatal) runtime warning,
                # with execution continuing using the clamped value. i1
                # (bool) has no overflow concept, so it's left as a plain trunc.
                if to_t == "i1":
                    self.emit(f"  {tmp} = trunc {from_t} {val} to {to_t}"); return tmp
                min_v, max_v = self._int_bounds(to_t)
                gt_max = self.new_tmp(); lt_min = self.new_tmp(); ovf = self.new_tmp()
                clamped1 = self.new_tmp(); clamped2 = self.new_tmp(); ovf32 = self.new_tmp()
                tn_lbl, tn_len = self.intern_str(to_t)
                tn_ptr = self.new_tmp()
                self.emit(f"  {gt_max} = icmp sgt {from_t} {val}, {max_v}")
                self.emit(f"  {lt_min} = icmp slt {from_t} {val}, {min_v}")
                self.emit(f"  {ovf} = or i1 {gt_max}, {lt_min}")
                self.emit(f"  {clamped1} = select i1 {gt_max}, {from_t} {max_v}, {from_t} {val}")
                self.emit(f"  {clamped2} = select i1 {lt_min}, {from_t} {min_v}, {from_t} {clamped1}")
                self.emit(f"  {tn_ptr} = getelementptr [{tn_len} x i8], [{tn_len} x i8]* {tn_lbl}, i64 0, i64 0")
                self.emit(f"  {ovf32} = zext i1 {ovf} to i32")
                self.emit(f"  call void @rub_overflow_check(i32 {ovf32}, i8* {tn_ptr})")
                self.emit(f"  {tmp} = trunc {from_t} {clamped2} to {to_t}"); return tmp
            else:        # widening
                if from_t == "i1":
                    # bool is unsigned 0/1 — zero-extend (sext would turn
                    # True into -1)
                    self.emit(f"  {tmp} = zext {from_t} {val} to {to_t}"); return tmp
                # signed integer types — sign-extend, or e.g. -1/Null
                # sentinels would turn into huge positive values
                self.emit(f"  {tmp} = sext {from_t} {val} to {to_t}"); return tmp

        # ---- Float ↔ Float ----
        if from_t in self._FLOAT_IR_SET and to_t in self._FLOAT_IR_SET:
            fr = self._TYPE_RANK.get(from_t, 11)
            tr = self._TYPE_RANK.get(to_t, 11)
            if fr == tr: return val
            if fr > tr:
                self.emit(f"  {tmp} = fptrunc {from_t} {val} to {to_t}"); return tmp
            else:
                self.emit(f"  {tmp} = fpext {from_t} {val} to {to_t}"); return tmp

        # ---- Integer → Float ----
        if from_t in self._INT_IR_SET and to_t in self._FLOAT_IR_SET:
            if val == self._NULL_SENTINEL and to_t in ("float", "double"):
                return self._NULL_SENTINEL_FLOAT
            self.emit(f"  {tmp} = sitofp {from_t} {val} to {to_t}"); return tmp

        # ---- Float → Integer ----
        if from_t in self._FLOAT_IR_SET and to_t in self._INT_IR_SET:
            self.emit(f"  {tmp} = fptosi {from_t} {val} to {to_t}"); return tmp

        # ---- i8* (string) to Integer ----
        if from_t == "i8*" and to_t in self._INT_IR_SET:
            if val == "null":
                return "0"
            self.emit(f"  {tmp} = call i64 @atol(i8* {val})")
            if to_t == "i64":
                return tmp
            if to_t == "i32":
                t2 = self.new_tmp()
                self.emit(f"  {t2} = trunc i64 {tmp} to i32")
                return t2
            if to_t == "i1":
                t2 = self.new_tmp()
                self.emit(f"  {t2} = trunc i64 {tmp} to i1")
                return t2
            return tmp

        return val