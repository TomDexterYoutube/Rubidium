import sys
import os
import subprocess
import tempfile
import glob

from lexer import tokenize
from parser import Parser
from rub_ast import Import, Use, VarDecl, FnDef, ClassDef, Assign, Drop, FFIBind, If, While, For, Try, FileOpen, ClassDef
from codegen import CodeGen, RubidiumTypeError, RubidiumNameError

RUNTIME_C = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <pthread.h>

// class_id (bugs.log OPEN-9): only meaningful when type==5 (a boxed class
// instance) — lets a value retrieved from a generic collection remember
// which class it was, since the compiler's normal method-call resolution
// is static/name-based and a %Box* alone has no such information. (Tag 4
// is already used for bool — see box_b below.)
typedef struct { int type; long long i; double f; char* s; void* p; long long class_id; } Box;
typedef struct { int magic; Box** items; int count; int cap; } RList;
typedef struct { int magic; Box** keys; Box** vals; int count; int cap; } RDict;
// FEATURE: dict+ reuses the exact same RDict layout as dict — the only
// runtime difference is the magic number (4 instead of 2), used solely to
// distinguish what `.add(newkey)` (empty path, 1 arg) should create as the
// new key's default value: dict creates Null, dict+ creates an empty
// nested dict+ (magic 4 again, recursively). Every other operation
// (get/set/drop/len/iterate/print) treats magic 2 and 4 identically.
#define IS_DICT_MAGIC(m) ((m) == 2 || (m) == 4)

Box* _thread_results[1024]; 

void _set_thread_result(int tid, Box* val) {
    if(tid >= 0 && tid < 1024) {
        _thread_results[tid] = val;
    }
}

// Global stdin pointer
void* _stdin_ptr;

// Main thread id — set at startup so we can detect if thread.wait is called from main
pthread_t _main_thread_id;

// _thread_handles is declared in the LLVM IR globals; declare extern here for C access
extern long long _thread_handles[1024];

// Timer state: array of timers storing start time and accumulated time
double _timer_starts[1024];
double _timer_accum[1024];
int _timer_running[1024];

// Wall-clock time in seconds (clock() measures CPU time, which does NOT advance
// during sleep() — timers must use wall-clock time per spec: "Read elapsed time
// in seconds" should reflect real elapsed time including time.wait()/sleep()).
static double _wall_time() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

__attribute__((constructor)) void init_runtime() {
    _stdin_ptr = (void*)stdin;
    _main_thread_id = pthread_self();
    for(int i = 0; i < 1024; i++) {
        _timer_starts[i] = 0.0;
        _timer_accum[i] = 0.0;
        _timer_running[i] = 0;
    }
}

// Waiter: background thread that joins a target thread and then exits.
// Used so a non-main thread can "wait" on a child without blocking main.
typedef struct { long long tid; } _WaiterArg;

static void* _waiter_thread_fn(void* arg) {
    _WaiterArg* wa = (_WaiterArg*)arg;
    long long tid = wa->tid;
    free(wa);
    if(tid >= 0 && tid < 1024) {
        pthread_join((pthread_t)_thread_handles[tid], NULL);
    }
    return NULL;
}

// _thread_smart_wait: if called from the main thread, block (normal join).
// If called from any other thread, spawn a detached background waiter and
// return immediately so the caller (and main) keep running.
void _thread_smart_wait(long long tid) {
    if(tid < 0 || tid >= 1024) return;
    pthread_t handle = (pthread_t)_thread_handles[tid];
    if(!handle) return;
    if(pthread_equal(pthread_self(), _main_thread_id)) {
        // Called from main — block until target thread is done
        pthread_join(handle, NULL);
        _thread_handles[tid] = 0;  // Mark as no longer running
    } else {
        // Called from a child thread — hand the join off to a detached waiter
        // so main is never blocked by this wait
        _WaiterArg* wa = malloc(sizeof(_WaiterArg));
        wa->tid = tid;
        pthread_t waiter;
        pthread_create(&waiter, NULL, _waiter_thread_fn, wa);
        pthread_detach(waiter);
        // Return immediately — caller thread continues
    }
}

// _thread_is_running: returns 1 (true) if the thread is still running, 0 if done.
// Uses pthread_tryjoin_np: returns EBUSY if thread is still running, 0 if it has exited.
int _thread_is_running(long long tid) {
    if(tid < 0 || tid >= 1024) return 0;
    pthread_t handle = (pthread_t)_thread_handles[tid];
    if(!handle) return 0;
    int rc = pthread_tryjoin_np(handle, NULL);
    // EBUSY (16) means the thread is still alive
    if(rc == 16) return 1;
    // 0 means the thread finished (join succeeded); we zero out the handle to avoid re-joining
    if(rc == 0) _thread_handles[tid] = 0;
    return 0;
}

// Called as the FIRST instruction of every thread trampoline (see codegen's
// _emit_raw_thread_trampoline and the generic thread() trampoline) so
// thread.kill() below can use PTHREAD_CANCEL_ASYNCHRONOUS instead of the
// pthread default (PTHREAD_CANCEL_DEFERRED, which only cancels at I/O calls
// — useless against a pure compute loop with no syscalls in it, which is
// exactly what `syntax`'s THREADING section says thread.kill() must stop:
// "meant for loops but can be used anywhere"). Trade-off: async cancellation
// can in principle land mid-malloc/free inside a thread and corrupt the
// heap, where deferred cancellation never would — accepted here because the
// documented behavior ("immediately... anywhere") isn't achievable with
// deferred cancellation at all.
void _thread_enable_async_cancel(void) {
    int old_type;
    pthread_setcanceltype(PTHREAD_CANCEL_ASYNCHRONOUS, &old_type);
}

// thread.kill(id) — forcibly stops that thread right away.
void _thread_kill(long long tid) {
    if(tid < 0 || tid >= 1024) return;
    pthread_t handle = (pthread_t)_thread_handles[tid];
    if(!handle) return;
    pthread_cancel(handle);
    pthread_join(handle, NULL);
    _thread_handles[tid] = 0;
}

void box_drop(Box* b) {
    if (!b) return;
    if (b->type == 2 && b->s) free(b->s);
    if (b->type == 3 && b->p) {
        int* magic = (int*)b->p;
        if (magic && *magic == 1) {
            RList* l = (RList*)b->p;
            for(int i=0; i<l->count; i++) box_drop(l->items[i]);
            free(l->items); free(l);
        } else if (magic && IS_DICT_MAGIC(*magic)) {
            RDict* d = (RDict*)b->p;
            for(int i=0; i<d->count; i++) { box_drop(d->keys[i]); box_drop(d->vals[i]); }
            free(d->keys); free(d->vals); free(d);
        } else {
            free(b->p);
        }
    }
    free(b);
}

Box* box_i(long long i) { Box* b=malloc(sizeof(Box)); b->type=0; b->i=i; return b; }
Box* box_f(double f) { Box* b=malloc(sizeof(Box)); b->type=1; b->f=f; return b; }
Box* box_s(char* s) { Box* b=malloc(sizeof(Box)); b->type=2; b->s=strdup(s); return b; }
Box* box_p(void* p) { Box* b=malloc(sizeof(Box)); b->type=3; b->p=p; return b; }
Box* box_b(long long i) { Box* b=malloc(sizeof(Box)); b->type=4; b->i=i; return b; }
// OPEN-4: Null gets its own real Box type tag (6) instead of being encoded
// as a type==0 int holding the INT32_MIN sentinel value — a genuine boxed
// int that happens to equal INT32_MIN is no longer misidentified as Null.
// `i` is still set to the old sentinel as a defensive fallback for any
// code path that unboxes via unbox_i()/->i without checking type==6 first.
Box* box_null(void) { Box* b=malloc(sizeof(Box)); b->type=6; b->i=-2147483648LL; b->f=0.0; b->s=NULL; b->p=NULL; b->class_id=-1; return b; }
// bugs.log OPEN-9: boxed class instance — p is the raw struct pointer,
// class_id identifies which class it is (see codegen's self.class_ids).
Box* box_class(void* p, long long class_id) { Box* b=malloc(sizeof(Box)); b->type=5; b->p=p; b->class_id=class_id; return b; }
Box* box_deep_copy(Box* src);  // OPEN-9: forward decl (defined later)
// BUG-3: forward decls for the temporary arena (defined further down). Every
// function below that stores a Box BY POINTER calls rub_temp_untrack on it,
// so the container becomes the owner and the arena stops tracking it. Without
// this a nested literal such as {"A" = ["x","y"]} kept its inner list in the
// arena, which then freed it at block exit while the dict still pointed at it.
Box* rub_temp_untrack(Box* b);
Box* box_copy(Box* src) {
    if(!src) return box_i(0);
    if(src->type==0) return box_i(src->i);
    if(src->type==1) return box_f(src->f);
    if(src->type==2) return box_s(src->s);
    if(src->type==4) return box_b(src->i);
    if(src->type==6) return box_null();
    // OPEN-9 (SIGSEGV / heap-use-after-free): a collection (type 3) used to be
    // returned by SHARED POINTER here — but every caller of box_copy takes
    // ownership of the result and later box_drop()s it (the for-loop's per-
    // iteration loop variable, indexed element reads `coll(i)`, and .random()).
    // box_drop on a collection recursively frees its storage, so dropping the
    // "copy" freed the ORIGINAL collection's memory out from under it. Confirmed
    // via AddressSanitizer: iterating a `list` of `index`/`dict` values a second
    // time read freed memory. Deep-copy the collection so the caller owns a
    // fully independent structure that is safe to drop (also the spec's
    // deep-copy-on-read/assign semantics, which these call sites already
    // *intended* per their comments — box_copy just never delivered it).
    if(src->type==3) return box_deep_copy(src);
    // Class instance (type 5): keep reference semantics to the underlying
    // instance data (->p is shared, not cloned — a generic deep copy isn't
    // possible without per-class layout knowledge), but hand back a FRESH Box
    // wrapper so a caller's box_drop frees only this wrapper, never the
    // original element's wrapper (same double-free class of bug as above).
    if(src->type==5) { Box* b=malloc(sizeof(Box)); *b=*src; return b; }
    return src;
}

long long unbox_i(Box* b) { return b ? b->i : 0; }
double unbox_f(Box* b) { return b ? b->f : 0.0; }
char* unbox_s(Box* b) { return (b && b->type==2) ? b->s : ""; }
// bugs.log OPEN-9: accept type==5 (boxed class instance) too, not just the
// generic type==3 pointer/collection tag — every EXISTING static-type
// pointer coercion (e.g. `let x: Item = d("k1")`) already goes through
// unbox_p, so a boxed class instance must unbox the same way a plain boxed
// pointer does; only the NEW dynamic-dispatch path additionally needs the
// class_id alongside it (see unbox_class_id below).
void* unbox_p(Box* b) { return (b && (b->type==3 || b->type==5)) ? b->p : NULL; }
long long unbox_class_id(Box* b) { return (b && b->type==5) ? b->class_id : -1; }

// ── Rubidium runtime error mechanism ──────────────────────────────────────
// try/error uses explicit IR branches (no setjmp/longjmp). Collection errors
// call rub_throw() which writes the message and then crashes so the caller's
// IR-level branch can never fire... UNLESS we're inside a try block, in which
// case the IR branch was already emitted *before* the collection call and the
// throw path is taken. For safety, rub_throw also prints to stderr and exits.
// The _rub_error_msg global is written before branching to the error block.
char* _rub_error_buf[256];
extern char* _rub_error_msg;  // defined in IR preamble

void rub_throw(const char* msg) {
    _rub_error_msg = (char*)msg;
    fprintf(stderr, "Runtime Error: %s\n", msg);
    exit(1);
}

// BUGFIX/FEATURE (bugs.log #9): backing store for runtime SY reflection.
// `let (x): dict = {}` inside a loop, where x's value is generated fresh
// each iteration (e.g. via concatenation with the loop variable), needs a
// genuine runtime-keyed store — LLVM globals/allocas must have a name fixed
// at compile time, so this is implemented as a simple string-keyed map from
// the actual runtime string to a Box* value. Linear scan is fine for the
// scale this language's collections are typically used at; can become a
// real hash table later if this becomes a bottleneck.
//
// BUGFIX (bugs.log #11): originally had a fixed RUB_DYNVAR_CAP of 8192 —
// once full, rub_dynvar_set() silently dropped new entries (no error at
// insert time), and a LATER lookup for one of those silently-dropped
// entries would crash the whole program with "undefined dynamic variable".
// Grows via realloc instead of having any hard cap.
typedef struct { char* key; Box* value; } RubDynVarEntry;
static RubDynVarEntry* _rub_dynvars = NULL;
static int _rub_dynvar_count = 0;
static int _rub_dynvar_cap = 0;

Box* rub_dynvar_get(const char* key) {
    for (int i = 0; i < _rub_dynvar_count; i++) {
        if (strcmp(_rub_dynvars[i].key, key) == 0) return _rub_dynvars[i].value;
    }
    rub_throw("undefined dynamic variable");
    return NULL;
}

void rub_dynvar_set(const char* key, Box* value) {
    rub_temp_untrack(value);    /* BUG-3: the table owns it from here on */
    for (int i = 0; i < _rub_dynvar_count; i++) {
        if (strcmp(_rub_dynvars[i].key, key) == 0) { _rub_dynvars[i].value = value; return; }
    }
    if (_rub_dynvar_count >= _rub_dynvar_cap) {
        _rub_dynvar_cap = _rub_dynvar_cap ? _rub_dynvar_cap * 2 : 256;
        _rub_dynvars = (RubDynVarEntry*)realloc(_rub_dynvars, _rub_dynvar_cap * sizeof(RubDynVarEntry));
    }
    _rub_dynvars[_rub_dynvar_count].key = strdup(key);
    _rub_dynvars[_rub_dynvar_count].value = value;
    _rub_dynvar_count++;
}

// BUGFIX (bugs.log #4): Integer Overflow section of the syntax file requires
// overflow to clamp to the type's min/max AND report a runtime error, but
// WITHOUT stopping execution (unlike rub_throw above, which exits). Clamping
// itself happens inline in the generated IR (select instructions in
// CodeGen.coerce); this just reports the non-fatal warning when told an
// overflow occurred, then returns normally so the clamped value keeps flowing.
void rub_overflow_check(int overflowed, const char* type_name) {
    if (overflowed) {
        fprintf(stderr, "Runtime Warning: integer overflow — value clamped to %s range\n", type_name);
        fflush(stderr);
    }
}

void print_int_or_null(long long v) {
    if (v == -2147483648LL) { printf("Null\n"); } else { printf("%lld\n", v); }
    fflush(stdout);
}

// OPEN-4 (scalar Null): plain integer print with NO Null-sentinel check —
// codegen now decides "Null" vs a real number at COMPILE TIME (only an
// explicit Null literal / statically-Null variable prints "Null"), so the
// runtime must print the true value even when it equals INT32_MIN. This is
// what makes "a value at the bottom limit is just the bottom limit, not Null"
// actually hold for computed/clamped minimums. print_int_or_null is kept for
// any legacy path but the print() codegen path uses this now.
void print_int_plain(long long v) { printf("%lld\n", v); fflush(stdout); }

// syntax: PRINT & INPUT — clear() wipes the whole terminal, not just the
// current line (that's what println()'s \r-overwrite already does). "\x1b[2J"
// clears the visible screen, "\x1b[H" moves the cursor back to the top-left
// — the same portable ANSI sequence `clear`/`cls`-equivalent tools use;
// works on Linux/Mac terminals and modern (Windows 10+) Windows consoles.
void rub_clear_screen(void) { printf("\x1b[2J\x1b[H"); fflush(stdout); }

// BUG (found reviewing user code — a str variable that can genuinely be
// Null at runtime, either an explicit `Null` literal or a `str` variable
// left/reassigned to Null by earlier control flow, compared with ==/!=/</>
// etc. against another string): every i8*-vs-i8* comparison codegen path
// called plain strcmp() directly — passing a real NULL pointer to strcmp()
// is undefined behavior, confirmed segfaulting glibc's implementation
// (which dereferences it to read bytes). A `str`'s Null is represented as
// an actual null i8* pointer (see coerce()'s "null" handling), so this was
// reachable from ordinary, spec-legal code, not just a contrived case.
// Null-safe replacement: two NULLs are equal, one NULL sorts before any
// real string ("Null behaves as -infinity" — see syntax's NULL BEHAVIOR
// section, already the rule for every other type), otherwise a normal
// strcmp. Used everywhere codegen previously called strcmp() directly on
// two `str` values.
int rub_strcmp_null_safe(const char* a, const char* b) {
    if (a == b) return 0;
    if (!a) return -1;
    if (!b) return 1;
    return strcmp(a, b);
}

// BUGFIX/FEATURE (bugs.log #17): print() used to always narrow to i64
// before printing, so even correctly-computed i128 arithmetic (see the
// codegen-side promote_type fix) couldn't actually be verified/displayed
// beyond i64's range. Clang supports __int128 natively; printf's %lld
// doesn't handle it directly, so convert to decimal manually. Same Null-
// sentinel convention as print_int_or_null (see bugs.log #4's known
// collision caveat — a real i128 value that happens to equal the sentinel
// still prints as "Null", same pre-existing ambiguity, just at this width
// too). i256+ genuinely printing beyond i64 range would need true bignum
// string conversion on top of that — out of scope here, still clamps.
void print_i128_or_null(__int128 v) {
    if (v == -2147483648LL) { printf("Null\n"); fflush(stdout); return; }
    char buf[48];
    int i = 46;
    buf[47] = '\0';
    int neg = v < 0;
    unsigned __int128 uv = neg ? (unsigned __int128)(-v) : (unsigned __int128)v;
    if (uv == 0) buf[i--] = '0';
    while (uv > 0) { buf[i--] = '0' + (int)(uv % 10); uv /= 10; }
    if (neg) buf[i--] = '-';
    printf("%s\n", &buf[i + 1]);
    fflush(stdout);
}
// OPEN-4: plain i128 print, no Null-sentinel check (see print_int_plain).
void print_i128_plain(__int128 v) {
    char buf[48];
    int i = 46;
    buf[47] = '\0';
    int neg = v < 0;
    unsigned __int128 uv = neg ? (unsigned __int128)(-v) : (unsigned __int128)v;
    if (uv == 0) buf[i--] = '0';
    while (uv > 0) { buf[i--] = '0' + (int)(uv % 10); uv /= 10; }
    if (neg) buf[i--] = '-';
    printf("%s\n", &buf[i + 1]);
    fflush(stdout);
}

// BUGFIX (bugs.log #17): string-conversion counterpart to print_i128_or_null,
// for string interpolation (coerce_to_string) rather than direct printing.
// Returns a malloc'd, null-terminated decimal string.
char* i128_to_str(__int128 v) {
    char* out = (char*)malloc(48);
    int i = 46;
    out[47] = '\0';
    int neg = v < 0;
    unsigned __int128 uv = neg ? (unsigned __int128)(-v) : (unsigned __int128)v;
    if (uv == 0) out[i--] = '0';
    while (uv > 0) { out[i--] = '0' + (int)(uv % 10); uv /= 10; }
    if (neg) out[i--] = '-';
    memmove(out, &out[i + 1], 47 - i);
    return out;
}

// OPEN-6: true bignum decimal conversion for i256/i512/i1024/i2048, which
// have no native C integer type to build a print path on (unlike i128,
// via Clang's __int128 above) — this operates directly on the raw LLVM
// bit pattern instead, passed as a little-endian array of 64-bit limbs
// (limbs[0] is the LEAST significant 64 bits — matches how x86 stores a
// wide integer in memory, so codegen just allocas the value and bitcasts
// the pointer to i64* to call this).
//
// Algorithm: repeated divide-by-10 across the whole limb array, using
// __int128 for each step's (remainder<<64 | limb) intermediate so a
// single 64-bit division instruction-equivalent handles it correctly —
// this is the standard bignum/small-divisor technique, just applied one
// decimal digit at a time (simplicity over speed; only used for
// occasional print/string-interpolation calls, never a hot loop).
static unsigned long long _bignum_divmod10(unsigned long long* limbs, int n) {
    unsigned __int128 rem = 0;
    for (int i = n - 1; i >= 0; i--) {
        unsigned __int128 cur = (rem << 64) | limbs[i];
        limbs[i] = (unsigned long long)(cur / 10);
        rem = cur % 10;
    }
    return (unsigned long long)rem;
}
static int _bignum_is_zero(unsigned long long* limbs, int n) {
    for (int i = 0; i < n; i++) if (limbs[i] != 0) return 0;
    return 1;
}
// Shared core: negates (two's complement, in place) if the top bit is set
// and the caller wants signed interpretation, then converts the
// (now-unsigned) magnitude to decimal digits into `digits` (written
// most-significant-first), returning the digit count. `*out_negative` is
// set to whether the original value was negative.
static int _bignum_magnitude_digits(unsigned long long* limbs, int n, char* digits, int* out_negative) {
    int negative = (limbs[n - 1] >> 63) ? 1 : 0;
    *out_negative = negative;
    if (negative) {
        unsigned __int128 carry = 1;
        for (int i = 0; i < n; i++) {
            unsigned __int128 v = (unsigned __int128)(~limbs[i]) + carry;
            limbs[i] = (unsigned long long)v;
            carry = v >> 64;
        }
    }
    if (_bignum_is_zero(limbs, n)) { digits[0] = '0'; return 1; }
    int pos = 0;
    while (!_bignum_is_zero(limbs, n)) {
        digits[pos++] = '0' + (int)_bignum_divmod10(limbs, n);
    }
    // digits were collected least-significant-first — reverse in place
    for (int a = 0, b = pos - 1; a < b; a++, b--) {
        char t = digits[a]; digits[a] = digits[b]; digits[b] = t;
    }
    return pos;
}
// Rubidium's Null sentinel (see bugs.log OPEN-4) sign-extends -2147483648
// to every width, so a bignum Null's negated magnitude is EXACTLY
// 2147483648 with every other limb zero — checked before the digit buffer
// (which the digit-extraction above would otherwise have destroyed) is
// examined, same convention print_int_or_null/print_i128_or_null use.
static int _bignum_is_null_sentinel(unsigned long long* limbs, int n) {
    if (!(limbs[n - 1] >> 63)) return 0;  // must be negative
    unsigned long long copy[32];
    for (int i = 0; i < n; i++) copy[i] = limbs[i];
    unsigned __int128 carry = 1;
    for (int i = 0; i < n; i++) {
        unsigned __int128 v = (unsigned __int128)(~copy[i]) + carry;
        copy[i] = (unsigned long long)v;
        carry = v >> 64;
    }
    if (copy[0] != 2147483648ULL) return 0;
    for (int i = 1; i < n; i++) if (copy[i] != 0) return 0;
    return 1;
}
void print_bignum_or_null(unsigned long long* limbs, int num_limbs) {
    if (_bignum_is_null_sentinel(limbs, num_limbs)) { printf("Null\n"); fflush(stdout); return; }
    char digits[700];  // 2048 bits ~= 617 decimal digits, +sign, +margin
    int negative, len;
    // digits are written starting at index 1, reserving index 0 for '-' —
    // null terminator always goes at index (len+1) regardless of sign.
    len = _bignum_magnitude_digits(limbs, num_limbs, digits + 1, &negative);
    digits[len + 1] = '\0';
    if (negative) { digits[0] = '-'; printf("%s\n", digits); }
    else { printf("%s\n", digits + 1); }
    fflush(stdout);
}
// OPEN-4: plain bignum print, no Null-sentinel check (see print_int_plain).
void print_bignum_plain(unsigned long long* limbs, int num_limbs) {
    char digits[700];
    int negative, len;
    len = _bignum_magnitude_digits(limbs, num_limbs, digits + 1, &negative);
    digits[len + 1] = '\0';
    if (negative) { digits[0] = '-'; printf("%s\n", digits); }
    else { printf("%s\n", digits + 1); }
    fflush(stdout);
}
char* bignum_to_str(unsigned long long* limbs, int num_limbs) {
    char digits[700];
    int negative, len;
    len = _bignum_magnitude_digits(limbs, num_limbs, digits + 1, &negative);
    char* out = (char*)malloc(len + 2);
    if (negative) { out[0] = '-'; memcpy(out + 1, digits + 1, len); out[len + 1] = '\0'; }
    else { memcpy(out, digits + 1, len); out[len] = '\0'; }
    return out;
}

// ---- fp128 EXACT decimal conversion (float precision-display fix) ----
// print()/string-interpolation of an f128 value used to narrow it to
// `double` and format with printf's default "%g" (6 significant digits),
// silently discarding all the extra precision a typed math block
// `(expr): f128` (or any f128 arithmetic) actually computed. This is the
// same class of gap the i256+ bignum printing fix (OPEN-6) closed for wide
// INTEGERS, done here for wide FLOATS: convert the raw IEEE-754 binary128
// bit pattern to its EXACT decimal representation directly — no double
// intermediate, so nothing is lost.
//
// How: any binary float M * 2^E (M an integer significand) has an EXACT,
// finite decimal representation (unlike decimal->binary, binary->decimal
// always terminates) because 2^E for E<0 equals 5^(-E) / 10^(-E) — so
// multiplying the integer significand by 5, |E| times, and placing the
// decimal point |E| digits from the right gives the exact value with no
// rounding at all.

// Multiply a decimal-digit string (ASCII '0'-'9', most-significant first) by
// 5 in place; the buffer must have room for one extra leading digit.
static int _fp128_digits_mul5(char* digits, int len, int cap) {
    int carry = 0;
    for (int i = len - 1; i >= 0; i--) {
        int v = (digits[i] - '0') * 5 + carry;
        digits[i] = '0' + (v % 10);
        carry = v / 10;
    }
    while (carry > 0 && len < cap) {
        memmove(digits + 1, digits, len);
        digits[0] = '0' + (carry % 10);
        carry /= 10;
        len++;
    }
    return len;
}

// Left-shift an unsigned integer (given as 2 64-bit limbs, lo/hi) by `shift`
// bits, growing into as many 64-bit limbs as needed. Returns a malloc'd
// little-endian limb array and sets *out_n to its length; caller frees.
static unsigned long long* _u128_shl_grow(unsigned long long lo, unsigned long long hi, int shift, int* out_n) {
    int n = 2 + shift / 64 + 1;
    unsigned long long* limbs = calloc(n, sizeof(unsigned long long));
    limbs[0] = lo; limbs[1] = hi;
    int word_shift = shift / 64, bit_shift = shift % 64;
    if (word_shift > 0) {
        for (int i = n - 1; i >= word_shift; i--) limbs[i] = limbs[i - word_shift];
        for (int i = 0; i < word_shift && i < n; i++) limbs[i] = 0;
    }
    if (bit_shift > 0) {
        for (int i = n - 1; i > 0; i--) limbs[i] = (limbs[i] << bit_shift) | (limbs[i-1] >> (64 - bit_shift));
        limbs[0] <<= bit_shift;
    }
    *out_n = n;
    return limbs;
}

// limbs (little-endian, n words) -> decimal digit string, most-significant
// first. Destroys `limbs`. Unsigned only (sign handled by the caller).
static int _unsigned_limbs_to_digits(unsigned long long* limbs, int n, char* digits, int cap) {
    if (_bignum_is_zero(limbs, n)) { digits[0] = '0'; return 1; }
    int pos = 0;
    while (!_bignum_is_zero(limbs, n) && pos < cap) {
        digits[pos++] = '0' + (int)_bignum_divmod10(limbs, n);
    }
    for (int a = 0, b = pos - 1; a < b; a++, b--) { char t = digits[a]; digits[a] = digits[b]; digits[b] = t; }
    return pos;
}

// Takes the raw 128-bit IEEE-754 binary128 bit pattern as (lo, hi) — the same
// little-endian limb convention as the OPEN-6 bignum functions, matching how
// codegen allocas the fp128 value and bitcasts the pointer to i64* — and
// returns a malloc'd, exact decimal string.
char* fp128_to_exact_decimal_str(unsigned long long lo, unsigned long long hi) {
    int sign = (int)(hi >> 63) & 1;
    int biased_exp = (int)((hi >> 48) & 0x7FFF);
    unsigned long long mant_hi = hi & 0xFFFFFFFFFFFFULL; // top 48 bits of the 112-bit explicit mantissa
    unsigned long long mant_lo = lo;                      // low 64 bits

    if (biased_exp == 0x7FFF) { // Inf/NaN
        if (mant_hi == 0 && mant_lo == 0) return strdup(sign ? "-Infinity" : "Infinity");
        return strdup("NaN");
    }
    if (biased_exp == 0 && mant_hi == 0 && mant_lo == 0) return strdup(sign ? "-0" : "0");

    int implicit, unbiased_exp;
    if (biased_exp == 0) { implicit = 0; unbiased_exp = 1 - 16383; } // subnormal
    else { implicit = 1; unbiased_exp = biased_exp - 16383; }

    unsigned long long M_lo = mant_lo;
    unsigned long long M_hi = mant_hi | ((unsigned long long)implicit << 48);
    int shift = unbiased_exp - 112; // value = M * 2^shift

    char* result;
    if (shift >= 0) {
        // Exact integer: M << shift, then straight decimal conversion.
        int n;
        unsigned long long* limbs = _u128_shl_grow(M_lo, M_hi, shift, &n);
        int cap = n * 20 + 8;
        char* digits = malloc(cap);
        int len = _unsigned_limbs_to_digits(limbs, n, digits, cap);
        free(limbs);
        result = malloc(len + 2);
        int p = 0;
        if (sign) result[p++] = '-';
        memcpy(result + p, digits, len); p += len;
        result[p] = '\0';
        free(digits);
    } else {
        // Fractional: value = M / 2^k = (M * 5^k) with the decimal point k
        // digits from the right, k = -shift.
        int k = -shift;
        unsigned long long limbs2[2] = { M_lo, M_hi };
        int cap = k + 40; // room to grow by ~k*log10(5) digits plus margin
        char* digits = malloc(cap);
        int len = _unsigned_limbs_to_digits(limbs2, 2, digits, cap);
        for (int i = 0; i < k; i++) len = _fp128_digits_mul5(digits, len, cap);
        // Place the decimal point k digits from the right; pad with leading
        // zeros if the digit string is shorter than k (value < 1).
        int int_len = len - k;
        char* frac_buf = malloc(cap + 4);
        int fp = 0;
        if (int_len <= 0) {
            frac_buf[fp++] = '0'; frac_buf[fp++] = '.';
            for (int z = 0; z < -int_len; z++) frac_buf[fp++] = '0';
            memcpy(frac_buf + fp, digits, len); fp += len;
        } else {
            memcpy(frac_buf, digits, int_len); fp = int_len;
            frac_buf[fp++] = '.';
            memcpy(frac_buf + fp, digits + int_len, k); fp += k;
        }
        frac_buf[fp] = '\0';
        free(digits);
        // Trim trailing zeros after the decimal point (exact value is
        // unchanged — trailing zeros past the point are not significant),
        // and drop a bare trailing '.' if the fraction was all zeros.
        int end = fp;
        while (end > 0 && frac_buf[end-1] == '0') end--;
        if (end > 0 && frac_buf[end-1] == '.') end--;
        frac_buf[end] = '\0';
        result = malloc(end + 2);
        int p = 0;
        if (sign) result[p++] = '-';
        memcpy(result + p, frac_buf, end); p += end;
        result[p] = '\0';
        free(frac_buf);
    }
    return result;
}
void print_fp128_exact(unsigned long long lo, unsigned long long hi) {
    char* s = fp128_to_exact_decimal_str(lo, hi);
    printf("%s\n", s);
    free(s);
    fflush(stdout);
}

double _rubidium_pow(double base, double exp) { return pow(base, exp); }

Box* make_list() { RList* l=malloc(sizeof(RList)); l->magic=1; l->count=0; l->cap=8; l->items=malloc(8*sizeof(Box*)); return box_p(l); }
// OPEN-4 follow-up: unconditional append, no .add()-specific special casing.
// Used for LITERAL list construction ([Null, 1, 2] must keep every element
// verbatim) — list_append below (used by the actual .add() method) is NOT
// safe for this, since its singleton-Null-replace rule would otherwise fire
// mid-construction on any literal that starts with a Null element.
void list_append_raw(Box* lst, Box* b) {
    rub_temp_untrack(b);        /* the list owns b from here on */
    if(!lst || lst->type != 3) return;
    RList* l=lst->p;
    if(!l || l->magic != 1) return; /* not a list */
    if(l->count==l->cap){l->cap*=2; l->items=realloc(l->items,l->cap*sizeof(Box*));}
    l->items[l->count++]=b;
}
void list_append(Box* lst, Box* b) {
    rub_temp_untrack(b);        /* the list owns b from here on */
    if(!lst || lst->type != 3) return;
    RList* l=lst->p;
    if(!l || l->magic != 1) return; /* not a list */
    // Spec: [Null].add(x) -> [x]  (single null is replaced, not appended to)
    // [1, Null].add(x) -> [1, Null, x]  (null in non-singleton list is kept)
    if (l->count == 1 && l->items[0] && l->items[0]->type == 6) {
        box_drop(l->items[0]);
        l->items[0] = b;
        return;
    }
    if(l->count==l->cap){l->cap*=2; l->items=realloc(l->items,l->cap*sizeof(Box*));}
    l->items[l->count++]=b;
}
// Split a string by a delimiter — returns a Box* list of string Box* parts.
Box* str_split(char* src, char* delim) {
    Box* result = make_list();
    if (!src || !delim || !delim[0]) {
        list_append(result, box_s(src ? src : ""));
        return result;
    }
    int dlen = (int)strlen(delim);
    char* cur = src;
    char* found;
    while ((found = strstr(cur, delim)) != NULL) {
        int partlen = (int)(found - cur);
        char* part = strndup(cur, partlen);
        list_append(result, box_s(part));
        free(part);
        cur = found + dlen;
    }
    list_append(result, box_s(cur));
    return result;
}
void list_swap(Box* lst, int i, int j) {
    if(!lst) return;
    RList* l=lst->p;
    if(i>=0 && i<l->count && j>=0 && j<l->count) {
        Box* tmp=l->items[i]; l->items[i]=l->items[j]; l->items[j]=tmp;
    }
}
Box* list_get(void* col, Box* idx) {
    if(!col) return box_i(0);
    RList* l=col; int i=idx->i; /* 0-based indexing */
    if(i>=0 && i<l->count) return l->items[i];
    char msg[64]; snprintf(msg, sizeof(msg), "list index %d out of bounds (length %d)", i, l->count);
    _rub_error_msg = strdup(msg);
    rub_throw(msg); return box_i(0);
}

Box* make_dict() { RDict* d=malloc(sizeof(RDict)); d->magic=2; d->count=0; d->cap=8; d->keys=malloc(8*sizeof(Box*)); d->vals=malloc(8*sizeof(Box*)); return box_p(d); }
// FEATURE: dict+ — identical to make_dict() except for the magic number
// (see IS_DICT_MAGIC). Used for dict+ literals and for the empty nested
// dict+ that collection_add1 creates as a new key's default value.
Box* make_dictplus() { RDict* d=malloc(sizeof(RDict)); d->magic=4; d->count=0; d->cap=8; d->keys=malloc(8*sizeof(Box*)); d->vals=malloc(8*sizeof(Box*)); return box_p(d); }

// Recursive content equality for any Box value, including nested list/index/dict
// collections. Per spec: "Collection equality compares contents... checked
// recursively... identical contents are equal" regardless of insertion order
// for index/dict (key->value maps), and element-by-element for lists.
int box_equal(Box* a, Box* b) {
    if (a == b) return 1;
    if (!a || !b) return 0;
    if (a->type != b->type) return 0;
    switch (a->type) {
        case 0: return a->i == b->i;
        case 1: return a->f == b->f;
        // OPEN-4: Null == Null is True (per spec), and both are type==6
        // here already (a->type != b->type would have returned 0 above).
        case 6: return 1;
        // BUGFIX (found while implementing bugs.log OPEN-9): bool (type==4)
        // had no case here at all, so two equal-valued-but-distinct bool
        // boxes fell through every case and hit the final `return 0` below
        // — comparing two boxed `True` values would incorrectly say unequal.
        case 4: return a->i == b->i;
        // bugs.log OPEN-9: class instances compare equal only if they're
        // the same underlying instance (and, redundantly but harmlessly,
        // the same class) — field-by-field structural equality isn't
        // possible generically without per-class layout knowledge.
        case 5: return a->p == b->p && a->class_id == b->class_id;
        case 2:
            if (!a->s || !b->s) return a->s == b->s;
            return strcmp(a->s, b->s) == 0;
        case 3: {
            if (!a->p || !b->p) return a->p == b->p;
            int magic_a = *(int*)a->p;
            int magic_b = *(int*)b->p;
            if (magic_a != magic_b) return 0;
            if (magic_a == 1) {
                RList* la = (RList*)a->p; RList* lb = (RList*)b->p;
                if (la->count != lb->count) return 0;
                for (int i = 0; i < la->count; i++) {
                    if (!box_equal(la->items[i], lb->items[i])) return 0;
                }
                return 1;
            } else if (magic_a == 2) {
                RDict* da = (RDict*)a->p; RDict* db = (RDict*)b->p;
                if (da->count != db->count) return 0;
                for (int i = 0; i < da->count; i++) {
                    int found = 0;
                    for (int j = 0; j < db->count; j++) {
                        if (box_equal(da->keys[i], db->keys[j]) && box_equal(da->vals[i], db->vals[j])) {
                            found = 1; break;
                        }
                    }
                    if (!found) return 0;
                }
                return 1;
            }
            return a->p == b->p;
        }
    }
    return 0;
}

// Numeric ordering compare for two boxed scalars (bugs.log OPEN-10 follow-up):
// used when a variable was promoted to %Box* by the polymorphic-global
// mechanism and is compared with <, >, <=, >= against another boxed scalar.
// Reads each box's own type tag so an int-tagged box and a float-tagged box
// compare correctly against each other. Returns -1 / 0 / 1.
int box_compare_num(Box* a, Box* b) {
    if (!a || !b) return 0;
    // OPEN-4: Null (type==6) is genuinely -infinity here, rather than
    // relying on it coincidentally holding the smallest representable int.
    double av = (a->type == 6) ? -INFINITY : (a->type == 1) ? a->f : (double)a->i;
    double bv = (b->type == 6) ? -INFINITY : (b->type == 1) ? b->f : (double)b->i;
    if (av < bv) return -1;
    if (av > bv) return 1;
    return 0;
}

// Recursive deep copy — creates a fully independent clone of any Box value.
// Called on every variable assignment to satisfy the spec's deep-copy semantics.
Box* box_deep_copy(Box* src) {
    if(!src) return box_i(0);
    if(src->type==0) return box_i(src->i);
    if(src->type==1) return box_f(src->f);
    if(src->type==2) return box_s(src->s ? src->s : "");
    // BUGFIX (found while implementing bugs.log OPEN-9): bool (type==4) was
    // falling through to the `type!=3` branch below, which resets it to a
    // plain int 0 — silently destroying a boxed bool's value/type on every
    // deep-copy (e.g. a bool stored in a collection or a polymorphic global).
    if(src->type==4) return box_b(src->i);
    // OPEN-4: Null (type==6) must copy as Null, not silently fall through to
    // the generic "reset to int 0" fallback below (which would turn a Null
    // in a collection into a real 0 on the very next deep-copy/assignment).
    if(src->type==6) return box_null();
    if(src->type!=3 || !src->p) {
        // bugs.log OPEN-9: class instances (type==5) use reference
        // semantics here, same as collections just below — deep-copying
        // an arbitrary class struct generically isn't possible in the C
        // runtime without per-class field-layout knowledge, which only
        // codegen has.
        if(src->type==5) return src;
        Box* b=malloc(sizeof(Box)); b->type=0; b->i=0; return b;
    }
    int* magic = (int*)src->p;
    if(*magic==1) {
        RList* sl = (RList*)src->p;
        RList* dl = malloc(sizeof(RList));
        dl->magic=1; dl->count=sl->count; dl->cap=sl->count>0?sl->count:1;
        dl->items = malloc(dl->cap * sizeof(Box*));
        for(int i=0;i<sl->count;i++) dl->items[i]=box_deep_copy(sl->items[i]);
        return box_p(dl);
    }
    if(IS_DICT_MAGIC(*magic)) {
        RDict* sd = (RDict*)src->p;
        RDict* dd = malloc(sizeof(RDict));
        // Preserve the ORIGINAL magic: 2 = dict, 4 = dict+ (see IS_DICT_MAGIC).
        // Hardcoding 2 silently demoted every deep-copied dict+ to a plain
        // dict, so nested-key access on the copy stopped working.
        dd->magic=sd->magic; dd->count=sd->count; dd->cap=sd->count>0?sd->count:1;
        dd->keys=malloc(dd->cap*sizeof(Box*)); dd->vals=malloc(dd->cap*sizeof(Box*));
        for(int i=0;i<sd->count;i++) {
            dd->keys[i]=box_deep_copy(sd->keys[i]);
            dd->vals[i]=box_deep_copy(sd->vals[i]);
        }
        return box_p(dd);
    }
    return src;
}
int box_eq(Box* a, Box* b) {
    if(!a || !b || a->type!=b->type) return 0;
    if(a->type==0) return a->i==b->i;
    if(a->type==1) return a->f==b->f;
    if(a->type==2) return strcmp(a->s,b->s)==0;
    if(a->type==6) return 1;  // OPEN-4: Null == Null
    return a->p==b->p;
}
void dict_set(Box* dct, Box* k, Box* v) {
    rub_temp_untrack(k); rub_temp_untrack(v);   /* the dict owns them now */
    if(!dct || dct->type != 3) return;
    RDict* d=dct->p;
    if(!d || !IS_DICT_MAGIC(d->magic)) return; /* not a dict/dict+ */
    for(int i=0;i<d->count;i++) if(box_eq(d->keys[i],k)) { 
        box_drop(d->vals[i]);
        d->vals[i]=v; 
        return; 
    }
    if(d->count==d->cap){d->cap*=2; d->keys=realloc(d->keys,d->cap*sizeof(Box*)); d->vals=realloc(d->vals,d->cap*sizeof(Box*));}
    d->keys[d->count]=k; d->vals[d->count]=v; d->count++;
}
// Single-arg .add(): dispatches based on the collection's runtime type.
// list.add(val)    -> append val to the list.
// dict.add(new_key) -> create a new top-level key with value Null (per spec).
// null sentinel.add(val) -> auto-promote null to a list, append val in-place.
void collection_add1(Box* col_box, Box* arg) {
    // OPEN-10 (SIGSEGV / `<obj>` corruption): the collection must store its OWN
    // independent DEEP COPY of the added value, not the caller's box by
    // reference. Per the spec's deep-copy-on-insert rule, and — concretely —
    // because the caller frequently owns `arg` and frees it right after adding
    // (the classic case: a `for`-loop variable, which the loop body adds to a
    // list and the loop then box_drop()s at the end of each iteration; also a
    // local `str`/`index` reassigned or dropped after the add). Storing `arg`
    // directly left the collection holding a dangling pointer: reading it back
    // later saw freed/garbage memory — a wrong type tag ("<obj>"), lost string
    // content (""), or, in the real app, a garbage `->s` that box_deep_copy
    // then strdup()'d into a hard SIGSEGV. box_deep_copy makes the collection's
    // copy fully self-owned so the caller's later free never touches it.
    Box* owned = box_deep_copy(arg);
    // Null sentinel: auto-promote to a list and append in-place.
    // The box is heap-allocated and parent collections hold a pointer to it,
    // so mutating type+p is visible through all references.
    if (col_box && col_box->type == 6) {
        RList* l = malloc(sizeof(RList));
        l->magic = 1; l->count = 0; l->cap = 8;
        l->items = malloc(8 * sizeof(Box*));
        col_box->type = 3;
        col_box->p = l;
        list_append(col_box, owned);
        return;
    }
    if (!col_box || col_box->type != 3 || !col_box->p) return;
    int magic = *(int*)col_box->p;
    if (magic == 1) {
        list_append(col_box, owned);
    } else if (magic == 2) {
        Box* null_val = box_null();  // OPEN-4: was box_i(-2147483648LL)
        dict_set(col_box, owned, null_val);
    } else if (magic == 4) {
        // dict+ : new key defaults to an empty nested dict+, not Null.
        dict_set(col_box, owned, make_dictplus());
    }
}
Box* dict_get(void* col, Box* k) {
    if(!col) return box_i(0);
    RDict* d=col;
    for(int i=0;i<d->count;i++) if(box_eq(d->keys[i],k)) return d->vals[i];
    _rub_error_msg = "key not found in collection";
    rub_throw("key not found in collection"); return box_i(0);
}

// Returns NULL on out-of-bounds/missing-key instead of exiting.
// Used inside try blocks so the IR can null-check and branch to the error label.
Box* try_collection_get(Box* col_box, Box* key) {
    if (!col_box || col_box->type != 3 || !col_box->p) return NULL;
    int magic = *(int*)col_box->p;
    if (magic == 1) {
        RList* l = (RList*)col_box->p; int i = key->i;
        return (i >= 0 && i < l->count) ? l->items[i] : NULL;
    }
    RDict* d = (RDict*)col_box->p;
    for (int j = 0; j < d->count; j++) if (box_eq(d->keys[j], key)) return d->vals[j];
    return NULL;
}

Box* collection_get(Box* col_box, Box* key) {
    if (!col_box || col_box->type != 3) return box_i(0);
    void* col = col_box->p;
    if(!col) return box_i(0);
    int* magic = (int*)col;
    if (*magic == 1) return list_get(col, key);
    if (IS_DICT_MAGIC(*magic)) return dict_get(col, key);
    return box_i(0);
}

void collection_set(Box* col_box, Box* key, Box* val) {
    rub_temp_untrack(val);      /* stored by pointer — the collection owns it */
    // Null sentinel: auto-promote to a dict and set the key in-place.
    if (col_box && col_box->type == 6) {
        RDict* d = malloc(sizeof(RDict));
        d->magic = 2; d->count = 0; d->cap = 8;
        d->keys = malloc(8 * sizeof(Box*)); d->vals = malloc(8 * sizeof(Box*));
        col_box->type = 3;
        col_box->p = d;
        dict_set(col_box, key, val);
        return;
    }
    if (!col_box || col_box->type != 3) return;
    void* col = col_box->p;
    if(!col) return;
    int* magic = (int*)col;
    if (*magic == 1) {
        RList* l = col;
        int i = key->i; /* 0-based indexing */
        if(i >= 0 && i < l->count) {
            box_drop(l->items[i]);
            l->items[i] = val;
        } else if (i == l->count) {
            list_append(col_box, val);
        }
    } else if (IS_DICT_MAGIC(*magic)) {
        dict_set(col_box, key, val);
    }
}

// items(1).drop() — remove element/key and shift (spec: NOT replaced with Null)
void collection_drop(Box* col_box, Box* key) {
    if (!col_box || col_box->type != 3 || !col_box->p) return;
    int* magic = (int*)col_box->p;
    if (*magic == 1) {
        RList* l = (RList*)col_box->p;
        long long idx = key->i;
        if (idx < 0 || idx >= l->count) return;
        box_drop(l->items[idx]);
        for (int j = (int)idx; j < l->count - 1; j++) l->items[j] = l->items[j+1];
        l->count--;
    } else if (IS_DICT_MAGIC(*magic)) {
        RDict* d = (RDict*)col_box->p;
        for (int j = 0; j < d->count; j++) {
            if (box_eq(d->keys[j], key)) {
                box_drop(d->keys[j]); box_drop(d->vals[j]);
                for (int k = j; k < d->count - 1; k++) { d->keys[k] = d->keys[k+1]; d->vals[k] = d->vals[k+1]; }
                d->count--;
                return;
            }
        }
    }
}

int collection_len(Box* col_box) {
    if (!col_box) return 0;
    if (col_box->type == 2) return col_box->s ? (int)strlen(col_box->s) : 0;
    if (col_box->type != 3) return 0;
    void* col = col_box->p;
    if(!col) return 0;
    int* magic = (int*)col;
    if (*magic == 1) return ((RList*)col)->count;
    if (IS_DICT_MAGIC(*magic)) return ((RDict*)col)->count;
    return 0;
}

unsigned int _rub_str_hash(const char* s) {
    unsigned long hash = 5381;
    int c;
    while ((c = *s++)) hash = ((hash << 5) + hash) + c;
    return (unsigned int)hash;
}

// BUG-5: single runtime entry point for `.has()` on ANY %Box*. The caller
// cannot always know statically whether the receiver is a str or a
// collection (both are %Box* in global scope), so the dispatch happens here
// off the type tag instead of in codegen.
//   str        -> substring search
//   list       -> value scan
//   index/dict -> keys AND values (spec: ".has() checks for keys or values")
// Anything else (or a needle of the wrong shape) is simply "not found"
// rather than a crash.
int collection_has(Box* col, Box* needle) {
    if (!col || !needle) return 0;
    // str receiver: substring / character containment.
    if (col->type == 2) {
        if (!col->s || needle->type != 2 || !needle->s) return 0;
        return strstr(col->s, needle->s) != NULL;
    }
    if (col->type != 3) return 0;
    void* ptr = col->p;
    if(!ptr) return 0;
    int* magic = (int*)ptr;
    if (*magic == 1) {
        RList* l = ptr;
        for(int i = 0; i < l->count; i++)
            if(box_equal(l->items[i], needle)) return 1;
    } else if (IS_DICT_MAGIC(*magic)) {
        RDict* d = ptr;
        for(int i = 0; i < d->count; i++)
            if(box_equal(d->keys[i], needle)) return 1;
        for(int i = 0; i < d->count; i++)
            if(box_equal(d->vals[i], needle)) return 1;
    }
    return 0;
}

/* =======================================================================
   BUG-3 — SCOPE-OWNED TEMPORARY ARENA
   -----------------------------------------------------------------------
   The spec says "Temporary scoped values are dropped automatically" and
   that locals/loop variables are released at the end of their block, but
   nothing ever freed the intermediate Boxes and strings an expression
   allocates. A loop that merely READ a collection therefore grew without
   bound (~230 B per read; 400 MB over 300k iterations).

   Every heap temporary an expression produces is registered here. codegen
   takes a mark at the start of each block and releases back to it after
   every statement, so a temporary lives exactly as long as the statement
   that made it. A value that outlives its statement — because it gets
   bound to a variable or returned — is handed to rub_temp_untrack* first,
   which transfers ownership out of the arena to that variable.

   Thread-local: per spec, temporaries/locals belong only to the thread
   executing them and are never shared.
   ======================================================================= */
typedef struct { void* p; int is_box; } RTemp;
static __thread RTemp*    _rub_tmp     = NULL;
static __thread long long _rub_tmp_n   = 0;
static __thread long long _rub_tmp_cap = 0;

// BUG (found stress-testing FFI CALLBACKS under AddressSanitizer, but not
// specific to callbacks at all — confirmed with a plain `thread(...)`/
// `thread.wait()` program too): _rub_tmp/_rub_tmp_n/_rub_tmp_cap are
// `__thread` (compiler-level TLS) with no destructor hook, so the arena's
// OWN backing array is never freed when a thread actually terminates —
// only when the whole PROCESS exits. Confirmed leaking 2048 bytes (the
// initial backing-array size) per thread that finishes, for ANY thread
// that ever tracks a temporary: Rubidium's own `thread()`-spawned workers,
// and an FFI callback fired from a C-spawned pthread Rubidium never
// created. A pthread_key_create() destructor solves this, but calling
// pthread_getspecific()/pthread_setspecific() on every single push/forget/
// mark/release would add real per-call overhead to code this arena's own
// original BUG-3 fix was written specifically to keep fast (a tight loop
// went from 400MB/300k iterations to 1.8MB) — so the arena itself stays
// exactly as fast __thread storage as before; a pthread_key_t is used
// ONLY to register a once-per-thread destructor, guarded by a __thread
// flag so every push after the first pays nothing beyond that one bool
// check. The destructor runs ON the terminating thread itself, so it can
// read that thread's own (still __thread) arena variables directly — no
// value needs to round-trip through pthread_set/getspecific at all.
static pthread_key_t _rub_tmp_dtor_key;
static pthread_once_t _rub_tmp_dtor_once = PTHREAD_ONCE_INIT;
static __thread int _rub_tmp_dtor_registered = 0;

static void _rub_tmp_thread_dtor(void* unused) {
    (void)unused;
    // Defensive: emit_body's own mark/release already empties this before
    // any thread function returns in the normal case, but free whatever
    // is still here rather than leaking it if something didn't.
    for (long long i = 0; i < _rub_tmp_n; i++) {
        if (!_rub_tmp[i].p) continue;
        if (_rub_tmp[i].is_box) box_drop((Box*)_rub_tmp[i].p);
        else free(_rub_tmp[i].p);
    }
    free(_rub_tmp);
    _rub_tmp = NULL; _rub_tmp_n = 0; _rub_tmp_cap = 0;
}

static void _rub_tmp_dtor_key_init(void) {
    pthread_key_create(&_rub_tmp_dtor_key, _rub_tmp_thread_dtor);
}

static void _rub_tmp_push(void* p, int is_box) {
    if (!_rub_tmp_dtor_registered) {
        pthread_once(&_rub_tmp_dtor_once, _rub_tmp_dtor_key_init);
        // The stored value just needs to be non-NULL for the destructor to
        // fire on thread exit — pthread ignores what it actually points to.
        pthread_setspecific(_rub_tmp_dtor_key, (void*)1);
        _rub_tmp_dtor_registered = 1;
    }
    if (!p) return;
    if (_rub_tmp_n == _rub_tmp_cap) {
        _rub_tmp_cap = _rub_tmp_cap ? _rub_tmp_cap * 2 : 128;
        _rub_tmp = realloc(_rub_tmp, (size_t)_rub_tmp_cap * sizeof(RTemp));
    }
    _rub_tmp[_rub_tmp_n].p = p;
    _rub_tmp[_rub_tmp_n].is_box = is_box;
    _rub_tmp_n++;
}

Box*  rub_temp_track(Box* b)       { _rub_tmp_push(b, 1); return b; }
char* rub_temp_track_str(char* s)  { _rub_tmp_push(s, 0); return s; }

// Ownership escapes the current statement (bound to a variable, returned,
// or explicitly .drop()ed) — forget it so the arena never double-frees it.
// Scans from the newest entry: an escaping temporary is nearly always the
// one just created, so this is O(1) in practice.
static void _rub_tmp_forget(void* p) {
    if (!p) return;
    for (long long i = _rub_tmp_n - 1; i >= 0; i--)
        if (_rub_tmp[i].p == p) { _rub_tmp[i].p = NULL; return; }
}
Box*  rub_temp_untrack(Box* b)      { _rub_tmp_forget((void*)b); return b; }
char* rub_temp_untrack_str(char* s) { _rub_tmp_forget((void*)s); return s; }

long long rub_temp_mark(void) { return _rub_tmp_n; }

void rub_temp_release_to(long long mark) {
    if (mark < 0) mark = 0;
    while (_rub_tmp_n > mark) {
        RTemp t = _rub_tmp[--_rub_tmp_n];
        if (!t.p) continue;
        if (t.is_box) box_drop((Box*)t.p);
        else          free(t.p);
    }
}

/* BUG-4 — reads return an INDEPENDENT DEEP COPY.
   list_get/dict_get hand back the collection's own interior Box, so
   `let first = chars(0)` aliased the element: dropping `first` and `chars`
   double-freed, and dropping a value read out of a global destroyed the
   global's data. The spec is explicit ("Assignment always creates an
   independent value", "Rubidium does not use references"), so element
   reads copy. The copy is a temporary owned by the arena above, which is
   what keeps the extra allocation from turning into a leak.
   Navigation for MUTATION (`d("scores").add(40)`) still uses the plain
   collection_get — that intentionally needs the real interior object. */
static Box* _rub_own_copy(Box* elem) {
    Box* c = box_deep_copy(elem);
    // box_deep_copy keeps reference semantics for class instances (type 5)
    // and returns the very same Box — tracking that would free the
    // collection's own element.
    if (c == elem) return c;
    return rub_temp_track(c);
}

Box* collection_get_copy(Box* col, Box* key) {
    return _rub_own_copy(collection_get(col, key));
}

// NULL on missing key/out-of-bounds (used inside try blocks), otherwise a copy.
Box* try_collection_get_copy(Box* col, Box* key) {
    Box* e = try_collection_get(col, key);
    if (!e) return NULL;
    return _rub_own_copy(e);
}

// Box* -> char* for a `str`-typed binding. unbox_s() returns the Box's OWN
// ->s buffer, so a string read out of a collection aliased the collection's
// characters (BUG-4) and freeing either one invalidated the other. This
// hands back an owned copy instead; the caller (codegen) tracks or binds it.
char* unbox_s_dup(Box* b) {
    return strdup((b && b->type == 2 && b->s) ? b->s : "");
}

// BUG (found via syntax sweep): `let t = s` for plain `str` variables used to
// just copy the raw pointer — s and t ended up aliased, and dropping either
// side left the other dangling (a real heap-use-after-free, confirmed under
// AddressSanitizer). codegen now strdup()s on variable-to-variable string
// assignment to give the target its own independent buffer (THE DEEP COPY
// RULE is a GENERAL rule, not scoped to collections). Plain strdup(NULL) is
// undefined behavior in C, and NULL here means the source has already been
// .drop()'d — propagate NULL through rather than crashing or masking it as
// an empty string.
char* rub_strdup_safe(char* s) {
    return s ? strdup(s) : NULL;
}

Box* collection_get_at(Box* col_box, int idx) {
    if (!col_box) return box_i(0);
    if (col_box->type == 2) {
        if (!col_box->s || idx < 0 || idx >= (int)strlen(col_box->s)) return box_s("");
        char ch[2] = {col_box->s[idx], '\0'};
        return box_s(ch);
    }
    if (col_box->type != 3) return box_i(0);
    void* col = col_box->p;
    if(!col) return box_i(0);
    int* magic = (int*)col;
    if (*magic == 1) {
        RList* l = col;
        if(idx>=0 && idx<l->count) return l->items[idx];
    }
    if (IS_DICT_MAGIC(*magic)) {
        RDict* d = col;
        if(idx>=0 && idx<d->count) return d->keys[idx];
    }
    return box_i(0);
}

void collection_set_at(Box* col_box, int idx, Box* val) {
    if (!col_box || col_box->type != 3) return;
    void* col = col_box->p;
    if(!col) return;
    int* magic = (int*)col;
    if (*magic == 1) {
        RList* l = col;
        if(idx>=0 && idx<l->count) {
            Box* copy = box_copy(val);
            if (l->items[idx]) box_drop(l->items[idx]);
            l->items[idx] = copy;
        }
    }
    if (IS_DICT_MAGIC(*magic)) {
        RDict* d = col;
        if(idx>=0 && idx<d->count) {
            Box* copy = box_copy(val);
            if (d->vals[idx]) box_drop(d->vals[idx]);
            d->vals[idx] = copy;
        }
    }
}

// Timer functions
void time_timer_start(int tid, double type_hint) {
    if(tid >= 0 && tid < 1024) {
        _timer_starts[tid] = _wall_time();
        _timer_accum[tid] = 0.0;
        _timer_running[tid] = 1;
    }
}

void time_timer_pause(int tid) {
    if(tid >= 0 && tid < 1024 && _timer_running[tid]) {
        double now = _wall_time();
        _timer_accum[tid] += now - _timer_starts[tid];
        _timer_running[tid] = 0;
    }
}

// Stop a timer — resets it (per spec), unlike pause which keeps elapsed time.
void time_timer_stop(int tid) {
    if(tid >= 0 && tid < 1024) {
        _timer_accum[tid] = 0.0;
        _timer_starts[tid] = _wall_time();
        _timer_running[tid] = 0;
    }
}

double time_timer_read(int tid) {
    double result = 0.0;
    if(tid >= 0 && tid < 1024) {
        if(_timer_running[tid]) {
            double now = _wall_time();
            result = _timer_accum[tid] + (now - _timer_starts[tid]);
        } else {
            result = _timer_accum[tid];
        }
    }
    return result;
}

char* box_to_cstr(Box* b);  // forward declaration
// elem_cstr(): like box_to_cstr but wraps plain strings in quotes — used when
// formatting an element NESTED inside a list/dict/index, per spec: "when a
// list is printed it shows text in "" and everything else as is".
char* elem_cstr(Box* b) {
    if(b && b->type==2) {
        const char* s = b->s ? b->s : "";
        char* out = malloc(strlen(s)+3);
        snprintf(out, strlen(s)+3, "\"%s\"", s);
        return out;
    }
    return box_to_cstr(b);
}
void print_boxed(Box* b) {
    // Delegate entirely to box_to_cstr so nested collections print recursively.
    if(!b) { printf("null\n"); fflush(stdout); return; }
    char* s = box_to_cstr(b);
    printf("%s\n", s);
    free(s);
    fflush(stdout);
}

// Convert a Box* to a heap-allocated C string for string concatenation.
// Caller must free() the result.
char* box_to_cstr(Box* b) {
    if(!b) { char* r = malloc(5); strcpy(r,"null"); return r; }
    char* buf = malloc(64);
    if(b->type==0) { snprintf(buf, 64, "%lld", b->i); }
    // OPEN-4: Null is now its own real Box type (6), so a genuine boxed int
    // that happens to equal the old sentinel value (INT32_MIN) is no longer
    // misprinted as "Null" — only an actual box_null() prints "Null" here.
    else if(b->type==6) { snprintf(buf, 64, "Null"); }
    else if(b->type==4) { snprintf(buf, 64, "%s", b->i ? "True" : "False"); }
    // bugs.log OPEN-R: was bare "%g" (6-sig-fig default), silently
    // truncating real digits. b->f is always stored as a C double here
    // (float values are widened to double before boxing), so "%.17g"
    // (DBL_DECIMAL_DIG) is the minimum precision that round-trips exactly.
    else if(b->type==1) { snprintf(buf, 64, "%.17g", b->f); }
    else if(b->type==2) { free(buf); return strdup(b->s ? b->s : ""); }
    else if(b->type==3 && b->p) {
        int* magic = (int*)b->p;
        if(magic && *magic==1) {
            // List → "[item, item, ...]"
            RList* l = (RList*)b->p;
            size_t cap = 256; char* out = malloc(cap); size_t pos = 0;
            out[pos++] = '[';
            for(int i=0; i<l->count; i++) {
                if(i>0) { out[pos++]= ','; out[pos++]=' '; }
                char* s = elem_cstr(l->items[i]);
                size_t slen = strlen(s);
                while(pos+slen+4 >= cap) { cap*=2; out=realloc(out,cap); }
                memcpy(out+pos, s, slen); pos+=slen; free(s);
            }
            out[pos++] = ']'; out[pos] = '\0';
            free(buf); return out;
        } else if(magic && IS_DICT_MAGIC(*magic)) {
            // Dict → "{key: val, ...}"
            RDict* d = (RDict*)b->p;
            size_t cap = 256; char* out = malloc(cap); size_t pos = 0;
            out[pos++] = '{';
            for(int i=0; i<d->count; i++) {
                if(i>0) { out[pos++]=','; out[pos++]=' '; }
                char* ks = elem_cstr(d->keys[i]);
                char* vs = elem_cstr(d->vals[i]);
                size_t kl=strlen(ks), vl=strlen(vs);
                while(pos+kl+vl+4 >= cap) { cap*=2; out=realloc(out,cap); }
                memcpy(out+pos,ks,kl); pos+=kl;
                out[pos++]=':'; out[pos++]=' ';
                memcpy(out+pos,vs,vl); pos+=vl;
                free(ks); free(vs);
            }
            out[pos++] = '}'; out[pos] = '\0';
            free(buf); return out;
        }
        free(buf); buf = malloc(7); strcpy(buf,"<obj>");
    }
    else { free(buf); buf = malloc(7); strcpy(buf,"<obj>"); }
    return buf;
}

// merges two lists into a new list (deep-copying each element)
Box* list_concat(Box* a, Box* b) {
    Box* result = make_list();
    if (a && a->type==3 && a->p) {
        int* am = (int*)a->p;
        if (am && *am==1) {
            RList* la = (RList*)a->p;
            for(int i=0; i<la->count; i++) list_append(result, box_copy(la->items[i]));
        }
    }
    if (b && b->type==3 && b->p) {
        int* bm = (int*)b->p;
        if (bm && *bm==1) {
            RList* lb = (RList*)b->p;
            for(int i=0; i<lb->count; i++) list_append(result, box_copy(lb->items[i]));
        }
    }
    return result;
}

// `+` between two boxed values: merges lists (per spec's list-concat
// example), otherwise falls back to stringify-and-concatenate (covers
// boxed scalars, e.g. two Any-typed numbers used in a string context)
Box* box_add(Box* a, Box* b) {
    if (a && a->type==3 && a->p && b && b->type==3 && b->p) {
        int* am = (int*)a->p; int* bm = (int*)b->p;
        if (am && bm && *am==1 && *bm==1) return list_concat(a, b);
    }
    char* sa = box_to_cstr(a);
    char* sb = box_to_cstr(b);
    char* buf = malloc(strlen(sa)+strlen(sb)+1);
    strcpy(buf, sa); strcat(buf, sb);
    Box* result = box_s(buf);
    free(sa); free(sb); free(buf);
    return result;
}

// list.combine() — joins all list items as strings with no separator
char* list_combine(Box* col_box) {
    if(!col_box || col_box->type != 3) return strdup("");
    int* magic = (int*)col_box->p;
    if(!magic || *magic != 1) return strdup("");
    RList* l = (RList*)col_box->p;
    size_t cap = 256;
    char* out = malloc(cap);
    size_t pos = 0;
    for(int i = 0; i < l->count; i++) {
        char* s = box_to_cstr(l->items[i]);
        size_t slen = strlen(s);
        while(pos + slen + 1 >= cap) { cap *= 2; out = realloc(out, cap); }
        memcpy(out + pos, s, slen);
        pos += slen;
        free(s);
    }
    out[pos] = '\0';
    return out;
}

// -------------------------------------------------------
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <errno.h>
#include <dirent.h>
#include <sys/stat.h>
#include <poll.h>   // OPEN-13: wait on child stdout instead of fixed-usleep polling

typedef struct {
    pid_t pid;
    int stdin_fd;   // write end — send commands to shell
    int stdout_fd;  // read end  — read output from shell
    int active;
} OsTerminal;

static OsTerminal _os_terminals[1024];

__attribute__((constructor)) void init_os_terminals() {
    for(int i=0;i<1024;i++) { _os_terminals[i].active=0; _os_terminals[i].pid=0; }
}

void os_start(long long id) {
    if(id<0||id>=1024) return;
    if(_os_terminals[id].active) return; // already started

    int to_child[2], from_child[2];
    if(pipe(to_child)<0 || pipe(from_child)<0) return;

    pid_t pid = fork();
    if(pid == 0) {
        // Child: replace stdin/stdout with pipes
        dup2(to_child[0], STDIN_FILENO);
        dup2(from_child[1], STDOUT_FILENO);
        dup2(from_child[1], STDERR_FILENO);
        close(to_child[0]); close(to_child[1]);
        close(from_child[0]); close(from_child[1]);
        // Find available shell
        char* shells[] = {"/bin/bash", "/bin/sh", NULL};
        for(int i=0; shells[i]; i++) {
            if(access(shells[i], X_OK)==0) { execl(shells[i], shells[i], NULL); }
        }
        _exit(127);
    }
    // Parent
    close(to_child[0]);
    close(from_child[1]);
    _os_terminals[id].pid = pid;
    _os_terminals[id].stdin_fd = to_child[1];
    _os_terminals[id].stdout_fd = from_child[0];
    _os_terminals[id].active = 1;
    // Set stdout_fd to non-blocking for reads
    fcntl(from_child[0], F_SETFL, O_NONBLOCK);
    // Brief settle
    usleep(50000);
}

// Run a command in the terminal, optionally sending `input` to stdin.
// Returns all output as a heap-allocated string. Caller should free.
// If command fails (non-zero exit), returns NULL and sets _rub_error_msg for try/error handler.
char* os_run(long long id, const char* cmd, const char* input, long long timeout_ms) {
    if(id<0||id>=1024||!_os_terminals[id].active) return strdup("");

    OsTerminal* t = &_os_terminals[id];

    // Write command + newline.
    if(input && strlen(input)>0) {
        size_t ilen = strlen(input);
        char* escaped = malloc(ilen*4+4);
        size_t ei = 0;
        for(size_t k=0; k<ilen; k++) {
            if(input[k]=='\'') { escaped[ei++]='\''; escaped[ei++]='\\'; escaped[ei++]='\''; escaped[ei++]='\''; }
            else escaped[ei++] = input[k];
        }
        escaped[ei] = '\0';
        size_t clen = strlen(cmd);
        char* full = malloc(clen + ei + 32);
        sprintf(full, "printf '%%s' '%s' | %s\n", escaped, cmd);
        write(t->stdin_fd, full, strlen(full));
        free(escaped); free(full);
    } else {
        write(t->stdin_fd, cmd, strlen(cmd));
        write(t->stdin_fd, "\n", 1);
    }

    // Send exit code capture command
    write(t->stdin_fd, "echo \"RUBIDIUM_EXIT_CODE:$?\"\n", 29);

    // OPEN-13: collect output by WAITING FOR ACTUAL COMPLETION, not a fixed
    // quiet-period heuristic. The old loop gave up after ~250ms of silence
    // between the command's writes (and a hard 1.5s ceiling), silently
    // truncating any slow/bursty command — e.g. an LLM stream with >250ms gaps
    // between tokens came back cut off with no error. Instead: block on poll()
    // for the child's stdout and keep reading until the appended
    // RUBIDIUM_EXIT_CODE marker appears (true completion) or an absolute safety
    // timeout elapses. An idle gap of any length no longer ends collection
    // early. timeout_ms is the absolute ceiling (a positive value overrides;
    // <=0 falls back to a generous default) so a genuinely hung command can't
    // block forever.
    char buf[4096];
    char* out = malloc(1);
    out[0]='\0';
    size_t out_len=0;
    int exit_code = 0;
    int exit_code_found = 0;
    long long deadline_ms = timeout_ms > 0 ? timeout_ms : 300000; // default 5 min
    struct timespec start_ts; clock_gettime(CLOCK_MONOTONIC, &start_ts);
    while(!exit_code_found) {
        struct timespec now_ts; clock_gettime(CLOCK_MONOTONIC, &now_ts);
        long long elapsed = (long long)(now_ts.tv_sec - start_ts.tv_sec) * 1000
                          + (now_ts.tv_nsec - start_ts.tv_nsec) / 1000000;
        if(elapsed >= deadline_ms) break;               // absolute safety ceiling
        struct pollfd pfd; pfd.fd = t->stdout_fd; pfd.events = POLLIN; pfd.revents = 0;
        int pr = poll(&pfd, 1, (int)(deadline_ms - elapsed));
        if(pr == 0) break;                              // no data within remaining budget
        if(pr < 0) { if(errno==EINTR) continue; break; }
        ssize_t n = read(t->stdout_fd, buf, sizeof(buf)-1);
        if(n>0) {
            buf[n]='\0';
            out = realloc(out, out_len+n+1);
            memcpy(out+out_len, buf, n);
            out_len+=n; out[out_len]='\0';

            // Check for exit code marker (true completion)
            char* marker = strstr(out, "RUBIDIUM_EXIT_CODE:");
            if(marker) {
                exit_code = atoi(marker + 19);
                exit_code_found = 1;
                // Remove the exit code line from output
                char* newline = strchr(marker, '\n');
                if(newline) {
                    size_t before = marker - out;
                    size_t after = out_len - (newline + 1 - out);
                    memmove(marker, newline + 1, after + 1);
                    out_len = before + after;
                    out[out_len] = '\0';
                }
            }
        } else if(n==0) {
            break; // EOF — child closed its stdout
        } else if(n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK) {
            break; // genuine read error (EAGAIN just means "poll lied"/retry)
        }
    }

    // If command failed, return NULL and set error message for try/error handler
    if(exit_code_found && exit_code != 0) {
        char* err_msg = malloc(out_len + 128);
        sprintf(err_msg, "Command failed (exit %d): %s", exit_code, out);
        free(out);
        _rub_error_msg = err_msg;
        return NULL;
    }

    return out; // caller must free
}

void os_terminal_drop(long long id) {
    if(id<0||id>=1024||!_os_terminals[id].active) return;
    OsTerminal* t = &_os_terminals[id];
    write(t->stdin_fd, "exit\n", 5);
    usleep(100000);
    close(t->stdin_fd);
    close(t->stdout_fd);
    waitpid(t->pid, NULL, WNOHANG);
    t->active=0;
}

// thread(os.run(cmd[, input], id), id) — runs a command in the background.
// os_start() is idempotent (returns immediately if `id` is already active),
// so the caller doesn't need its own os.start(id) first. Owns cmd/input
// (frees them — see codegen.py's _emit_os_thread_call, which always hands
// this an independent strdup'd copy) and discards the captured output:
// there's no caller left waiting for a return value once this is running
// on a background thread.
void rub_os_run_thread(long long id, char* cmd, char* input, long long timeout_ms) {
    os_start(id);
    char* out = os_run(id, cmd, input, timeout_ms);
    if (out) free(out);
    free(cmd);
    free(input);
}

// -------------------------------------------------------
// NET MODULE — LAN discovery + peer messaging (see `syntax`'s NET section)
//
// One UDP socket per process, bound to a fixed port. Discovery/handshake
// packets are broadcast/unicast text frames of the form
// "<TYPE>\x01<sender_id>\x01<field>..." (TYPE is a single char: P=ping,
// F=find request, R=find reply, C=connect request, A=connect ack,
// D=data, X=close). net.process()'s background loop owns the socket's
// only recvfrom() call and demuxes every packet type from there — nothing
// else ever reads the socket, so net.listen() (which the spec says must
// be running to receive) is a thin peer-liveness check that otherwise
// just idles: the actual receiving already happens centrally.
// -------------------------------------------------------
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdint.h>

#define RUB_NET_PORT 47990
#define RUB_NET_MAX_PEERS 128
#define RUB_NET_NAME_LEN 128
#define RUB_NET_BUF_LEN 4096

typedef struct {
    int in_use;
    int id;
    char name[RUB_NET_NAME_LEN];
    struct sockaddr_in addr;
    double last_seen;
    double rtt;            // seconds; sentinel 1e9 means "never measured via find()"
    int connected;
    int pending_in;        // they sent CONNECT, awaiting our accept()
    int pending_out;       // we sent CONNECT, awaiting their ack
    char* mailbox;         // last unread DATA payload (consuming read), NULL if empty
} RubNetPeer;

typedef struct { int id; char name[RUB_NET_NAME_LEN]; double rtt; } RubNetFindHit;

static RubNetPeer g_net_peers[RUB_NET_MAX_PEERS];
static pthread_mutex_t g_net_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_net_sock = -1;
static int g_net_self_id = 0;
static char g_net_self_name[RUB_NET_NAME_LEN];
static volatile int g_net_started = 0;
static struct sockaddr_in g_net_bcast_addr;

static RubNetFindHit g_find_hits[RUB_NET_MAX_PEERS];
static int g_find_count = 0;
static volatile int g_finding = 0;
static double g_find_sent_at = 0;
static pthread_mutex_t g_find_lock = PTHREAD_MUTEX_INITIALIZER;

static double rub_net_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

// Resolve a name-or-id Box* to a known peer. CALLER MUST HOLD g_net_lock.
// Duplicate names resolve to the lowest-rtt (closest/fastest, i.e. first
// in find()/list() order) match — see the NET section's DISCOVERY notes.
static RubNetPeer* _net_resolve_locked(Box* target) {
    if (!target) return NULL;
    if (target->type == 2 && target->s) {
        RubNetPeer* best = NULL;
        for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
            RubNetPeer* p = &g_net_peers[i];
            if (p->in_use && p->name[0] && strcmp(p->name, target->s) == 0) {
                if (!best || p->rtt < best->rtt) best = p;
            }
        }
        return best;
    }
    long long id = target->i;
    for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
        if (g_net_peers[i].in_use && g_net_peers[i].id == id) return &g_net_peers[i];
    }
    return NULL;
}

// CALLER MUST HOLD g_net_lock.
static RubNetPeer* _net_find_or_create_locked(int id, const char* name, struct sockaddr_in* addr, double now) {
    for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
        if (g_net_peers[i].in_use && g_net_peers[i].id == id) {
            RubNetPeer* p = &g_net_peers[i];
            if (name && name[0]) { strncpy(p->name, name, RUB_NET_NAME_LEN - 1); p->name[RUB_NET_NAME_LEN-1] = 0; }
            if (addr) p->addr = *addr;
            p->last_seen = now;
            return p;
        }
    }
    for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
        if (!g_net_peers[i].in_use) {
            RubNetPeer* p = &g_net_peers[i];
            memset(p, 0, sizeof(RubNetPeer));
            p->in_use = 1;
            p->id = id;
            if (name) { strncpy(p->name, name, RUB_NET_NAME_LEN - 1); }
            if (addr) p->addr = *addr;
            p->last_seen = now;
            p->rtt = 1e9;
            return p;
        }
    }
    return NULL; // peer table full — drop silently
}

static void _rub_net_send_raw(struct sockaddr_in* dst, const char* msg) {
    if (g_net_sock < 0) return;
    sendto(g_net_sock, msg, strlen(msg), 0, (struct sockaddr*)dst, sizeof(*dst));
}

static void _rub_net_send_ping(void) {
    char msg[RUB_NET_NAME_LEN + 32];
    snprintf(msg, sizeof(msg), "P\x01%d\x01%s", g_net_self_id, g_net_self_name);
    _rub_net_send_raw(&g_net_bcast_addr, msg);
}

static void _rub_net_send_find(void) {
    char msg[RUB_NET_NAME_LEN + 32];
    snprintf(msg, sizeof(msg), "F\x01%d\x01%s", g_net_self_id, g_net_self_name);
    _rub_net_send_raw(&g_net_bcast_addr, msg);
}

static void _rub_net_handle_packet(char* buf, int n, struct sockaddr_in* src) {
    char* fields[8];
    int nf = 0;
    fields[nf++] = buf;
    for (int i = 0; i < n && nf < 8; i++) {
        if (buf[i] == '\x01') { buf[i] = 0; fields[nf++] = buf + i + 1; }
    }
    if (nf < 2) return;
    char type = fields[0][0];
    int sender_id = atoi(fields[1]);
    if (sender_id == 0 || sender_id == g_net_self_id) return;
    const char* name = (nf > 2) ? fields[2] : "";
    double now = rub_net_now();

    if (type == 'P') {
        pthread_mutex_lock(&g_net_lock);
        _net_find_or_create_locked(sender_id, name, src, now);
        pthread_mutex_unlock(&g_net_lock);
    } else if (type == 'F') {
        pthread_mutex_lock(&g_net_lock);
        _net_find_or_create_locked(sender_id, name, src, now);
        pthread_mutex_unlock(&g_net_lock);
        char msg[RUB_NET_NAME_LEN + 32];
        snprintf(msg, sizeof(msg), "R\x01%d\x01%s", g_net_self_id, g_net_self_name);
        _rub_net_send_raw(src, msg);
    } else if (type == 'R') {
        int finding = g_finding;
        double rtt = finding ? (now - g_find_sent_at) : 0.0;
        pthread_mutex_lock(&g_net_lock);
        RubNetPeer* p = _net_find_or_create_locked(sender_id, name, src, now);
        if (p && finding) p->rtt = rtt;
        pthread_mutex_unlock(&g_net_lock);
        if (finding) {
            pthread_mutex_lock(&g_find_lock);
            int found = 0;
            for (int i = 0; i < g_find_count; i++) {
                if (g_find_hits[i].id == sender_id) { g_find_hits[i].rtt = rtt; found = 1; break; }
            }
            if (!found && g_find_count < RUB_NET_MAX_PEERS) {
                g_find_hits[g_find_count].id = sender_id;
                strncpy(g_find_hits[g_find_count].name, name, RUB_NET_NAME_LEN - 1);
                g_find_hits[g_find_count].name[RUB_NET_NAME_LEN - 1] = 0;
                g_find_hits[g_find_count].rtt = rtt;
                g_find_count++;
            }
            pthread_mutex_unlock(&g_find_lock);
        }
    } else if (type == 'C') {
        pthread_mutex_lock(&g_net_lock);
        RubNetPeer* p = _net_find_or_create_locked(sender_id, name, src, now);
        if (p) p->pending_in = 1;
        pthread_mutex_unlock(&g_net_lock);
    } else if (type == 'A') {
        pthread_mutex_lock(&g_net_lock);
        for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
            if (g_net_peers[i].in_use && g_net_peers[i].id == sender_id) {
                if (g_net_peers[i].pending_out) { g_net_peers[i].connected = 1; g_net_peers[i].pending_out = 0; }
                if (name[0]) { strncpy(g_net_peers[i].name, name, RUB_NET_NAME_LEN - 1); g_net_peers[i].name[RUB_NET_NAME_LEN-1]=0; }
                break;
            }
        }
        pthread_mutex_unlock(&g_net_lock);
    } else if (type == 'D') {
        const char* payload = (nf > 2) ? fields[2] : "";
        pthread_mutex_lock(&g_net_lock);
        for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
            if (g_net_peers[i].in_use && g_net_peers[i].id == sender_id && g_net_peers[i].connected) {
                if (g_net_peers[i].mailbox) free(g_net_peers[i].mailbox);
                g_net_peers[i].mailbox = strdup(payload);
                break;
            }
        }
        pthread_mutex_unlock(&g_net_lock);
    } else if (type == 'X') {
        pthread_mutex_lock(&g_net_lock);
        for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
            if (g_net_peers[i].in_use && g_net_peers[i].id == sender_id) {
                g_net_peers[i].connected = 0;
                g_net_peers[i].pending_in = 0;
                g_net_peers[i].pending_out = 0;
                break;
            }
        }
        pthread_mutex_unlock(&g_net_lock);
    }
}

// Thread body for `thread(net.process(rate, name), id)` — never returns.
void rub_net_process(double rate, char* name) {
    if (g_net_started) return; // already running (idempotent re-entry guard)
    if (rate <= 0) rate = 2.0;

    // NOTE: deliberately no SO_REUSEPORT — two net.process() instances on
    // the SAME machine would silently split incoming discovery/connect/data
    // packets between them (the kernel hashes each packet to exactly one of
    // the reusing sockets), which looks like random, hard-to-debug packet
    // loss rather than a clean error. One net.process() per machine is the
    // whole model (see the NET section), so a second instance on the same
    // host should fail loudly at bind() below, not silently steal traffic.
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    int one = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    setsockopt(sock, SOL_SOCKET, SO_BROADCAST, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(RUB_NET_PORT);
    if (bind(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        rub_throw("net.process: failed to bind the LAN discovery port — another program on this machine may already be using it");
        return;
    }

    memset(&g_net_bcast_addr, 0, sizeof(g_net_bcast_addr));
    g_net_bcast_addr.sin_family = AF_INET;
    g_net_bcast_addr.sin_port = htons(RUB_NET_PORT);
    g_net_bcast_addr.sin_addr.s_addr = htonl(INADDR_BROADCAST);

    unsigned int seed = (unsigned int)(time(NULL) ^ getpid() ^ (uintptr_t)&addr);
    g_net_self_id = (int)((rand_r(&seed) & 0x7FFFFFFF) | 1);
    g_net_self_name[0] = 0;
    if (name && name[0]) { strncpy(g_net_self_name, name, RUB_NET_NAME_LEN - 1); }
    free(name);

    g_net_sock = sock;
    g_net_started = 1;

    double period = 1.0 / rate;
    double next_ping = rub_net_now();
    struct pollfd pfd; pfd.fd = sock; pfd.events = POLLIN;
    char buf[RUB_NET_BUF_LEN];

    for (;;) {
        double now = rub_net_now();
        double wait_s = next_ping - now;
        int timeout_ms = (wait_s > 0) ? (int)(wait_s * 1000) : 0;
        if (timeout_ms > 200) timeout_ms = 200;
        int pr = poll(&pfd, 1, timeout_ms);
        if (pr > 0 && (pfd.revents & POLLIN)) {
            struct sockaddr_in src; socklen_t slen = sizeof(src);
            ssize_t n = recvfrom(sock, buf, sizeof(buf) - 1, 0, (struct sockaddr*)&src, &slen);
            if (n > 0) { buf[n] = 0; _rub_net_handle_packet(buf, (int)n, &src); }
        }
        now = rub_net_now();
        if (now >= next_ping) {
            _rub_net_send_ping();
            next_ping = now + period;
        }
    }
}

// Thread body for `thread(net.listen(id_or_name), id)`. The actual receive
// loop lives in net.process() (single reader on the shared socket) — this
// just validates the peer and idles, matching the documented "must be
// running to receive" contract without a second thread fighting for the
// same recvfrom().
void rub_net_listen(Box* target) {
    if (!g_net_started) { rub_throw("net.listen: net.process must be running first"); return; }
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    pthread_mutex_unlock(&g_net_lock);
    if (!p) { rub_throw("net.listen: unknown peer — connect()/accept() it first"); return; }
    for (;;) { usleep(50000); }
}

Box* rub_net_find(void) {
    if (!g_net_started) { rub_throw("net.find: net.process must be running first"); return make_list(); }

    pthread_mutex_lock(&g_find_lock);
    g_find_count = 0;
    g_finding = 1;
    g_find_sent_at = rub_net_now();
    pthread_mutex_unlock(&g_find_lock);

    _rub_net_send_find();
    usleep(400000); // discovery window

    pthread_mutex_lock(&g_find_lock);
    g_finding = 0;
    int count = g_find_count;
    RubNetFindHit local[RUB_NET_MAX_PEERS];
    memcpy(local, g_find_hits, sizeof(RubNetFindHit) * count);
    pthread_mutex_unlock(&g_find_lock);

    for (int i = 1; i < count; i++) {
        RubNetFindHit key = local[i];
        int j = i - 1;
        while (j >= 0 && local[j].rtt > key.rtt) { local[j+1] = local[j]; j--; }
        local[j+1] = key;
    }

    Box* result = make_list();
    for (int i = 0; i < count; i++) {
        if (local[i].name[0]) list_append(result, box_s(local[i].name));
        else list_append(result, box_i(local[i].id));
    }
    return result;
}

Box* rub_net_list(void) {
    Box* result = make_list();
    if (!g_net_started) return result;
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* items[RUB_NET_MAX_PEERS]; int n = 0;
    for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
        if (g_net_peers[i].in_use && g_net_peers[i].connected) items[n++] = &g_net_peers[i];
    }
    for (int i = 1; i < n; i++) {
        RubNetPeer* key = items[i];
        int j = i - 1;
        while (j >= 0 && items[j]->rtt > key->rtt) { items[j+1] = items[j]; j--; }
        items[j+1] = key;
    }
    for (int i = 0; i < n; i++) {
        if (items[i]->name[0]) list_append(result, box_s(items[i]->name));
        else list_append(result, box_i(items[i]->id));
    }
    pthread_mutex_unlock(&g_net_lock);
    return result;
}

Box* rub_net_requests(void) {
    Box* result = make_list();
    if (!g_net_started) return result;
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* items[RUB_NET_MAX_PEERS]; int n = 0;
    for (int i = 0; i < RUB_NET_MAX_PEERS; i++) {
        if (g_net_peers[i].in_use && g_net_peers[i].pending_in) items[n++] = &g_net_peers[i];
    }
    for (int i = 1; i < n; i++) {
        RubNetPeer* key = items[i];
        int j = i - 1;
        while (j >= 0 && items[j]->rtt > key->rtt) { items[j+1] = items[j]; j--; }
        items[j+1] = key;
    }
    for (int i = 0; i < n; i++) {
        if (items[i]->name[0]) list_append(result, box_s(items[i]->name));
        else list_append(result, box_i(items[i]->id));
    }
    pthread_mutex_unlock(&g_net_lock);
    return result;
}

void rub_net_connect(Box* target) {
    if (!g_net_started) { rub_throw("net.connect: net.process must be running first"); return; }
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    if (!p) { pthread_mutex_unlock(&g_net_lock); rub_throw("net.connect: peer not found — call net.find() first"); return; }
    p->pending_out = 1;
    struct sockaddr_in addr = p->addr;
    pthread_mutex_unlock(&g_net_lock);
    char msg[RUB_NET_NAME_LEN + 32];
    snprintf(msg, sizeof(msg), "C\x01%d\x01%s", g_net_self_id, g_net_self_name);
    _rub_net_send_raw(&addr, msg);
}

void rub_net_accept(Box* target) {
    if (!g_net_started) { rub_throw("net.accept: net.process must be running first"); return; }
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    if (!p || !p->pending_in) { pthread_mutex_unlock(&g_net_lock); rub_throw("net.accept: no pending connection request from that peer"); return; }
    p->connected = 1;
    p->pending_in = 0;
    struct sockaddr_in addr = p->addr;
    pthread_mutex_unlock(&g_net_lock);
    char msg[RUB_NET_NAME_LEN + 32];
    snprintf(msg, sizeof(msg), "A\x01%d\x01%s", g_net_self_id, g_net_self_name);
    _rub_net_send_raw(&addr, msg);
}

void rub_net_close(Box* target) {
    if (!g_net_started) return;
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    if (!p) { pthread_mutex_unlock(&g_net_lock); return; }
    struct sockaddr_in addr = p->addr;
    p->connected = 0;
    p->pending_in = 0;
    p->pending_out = 0;
    if (p->mailbox) { free(p->mailbox); p->mailbox = NULL; }
    pthread_mutex_unlock(&g_net_lock);
    char msg[32];
    snprintf(msg, sizeof(msg), "X\x01%d", g_net_self_id);
    _rub_net_send_raw(&addr, msg);
}

// Recursive: 'l' elements are themselves TAG:value frames (see the NET
// section's wire-format table). Only flat lists of scalars are valid input.
static char* rub_net_serialize(Box* v) {
    char buf[64];
    if (!v || v->type == 6) return strdup("n:");
    if (v->type == 0) { snprintf(buf, sizeof(buf), "i:%lld", (long long)v->i); return strdup(buf); }
    if (v->type == 1) { snprintf(buf, sizeof(buf), "f:%.15g", v->f); return strdup(buf); }
    if (v->type == 4) return strdup(v->i ? "b:True" : "b:False");
    if (v->type == 2) {
        const char* s = v->s ? v->s : "";
        char* out = malloc(strlen(s) + 3);
        sprintf(out, "s:%s", s);
        return out;
    }
    if (v->type == 3) {
        int* magic = (int*)v->p;
        if (!magic || *magic != 1) { rub_throw("net.send: only flat lists of scalars are supported (v1)"); return strdup("n:"); }
        RList* l = (RList*)v->p;
        char** parts = malloc(sizeof(char*) * (l->count > 0 ? l->count : 1));
        size_t total = 3;
        for (int i = 0; i < l->count; i++) { parts[i] = rub_net_serialize(l->items[i]); total += strlen(parts[i]) + 1; }
        char* out = malloc(total);
        strcpy(out, "l:");
        for (int i = 0; i < l->count; i++) { if (i) strcat(out, ","); strcat(out, parts[i]); free(parts[i]); }
        free(parts);
        return out;
    }
    rub_throw("net.send: unsupported value type");
    return strdup("n:");
}

static Box* rub_net_deserialize(const char* s) {
    if (!s || !s[0]) return box_null();
    char tag = s[0];
    const char* val = (s[1] == ':') ? s + 2 : s + 1;
    if (tag == 'i') return box_i(atoll(val));
    if (tag == 'f') return box_f(atof(val));
    if (tag == 's') return box_s((char*)val);
    if (tag == 'b') return box_b(strcmp(val, "True") == 0 ? 1 : 0);
    if (tag == 'l') {
        Box* lst = make_list();
        char* dup = strdup(val);
        char* saveptr = NULL;
        char* tok = strtok_r(dup, ",", &saveptr);
        while (tok) { list_append(lst, rub_net_deserialize(tok)); tok = strtok_r(NULL, ",", &saveptr); }
        free(dup);
        return lst;
    }
    return box_null();
}

void rub_net_send(Box* target, Box* value) {
    if (!g_net_started) { rub_throw("net.send: net.process must be running first"); return; }
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    if (!p || !p->connected) { pthread_mutex_unlock(&g_net_lock); rub_throw("net.send: not connected to that peer"); return; }
    struct sockaddr_in addr = p->addr;
    pthread_mutex_unlock(&g_net_lock);
    char* payload = rub_net_serialize(value);
    char msg[RUB_NET_BUF_LEN];
    snprintf(msg, sizeof(msg), "D\x01%d\x01%s", g_net_self_id, payload);
    free(payload);
    _rub_net_send_raw(&addr, msg);
}

Box* rub_net_data(Box* target) {
    if (!g_net_started) { rub_throw("net.data: net.process must be running first"); return box_null(); }
    pthread_mutex_lock(&g_net_lock);
    RubNetPeer* p = _net_resolve_locked(target);
    if (!p) { pthread_mutex_unlock(&g_net_lock); rub_throw("net.data: unknown peer"); return box_null(); }
    char* mb = p->mailbox;
    p->mailbox = NULL;
    pthread_mutex_unlock(&g_net_lock);
    if (!mb) return box_null();
    Box* result = rub_net_deserialize(mb);
    free(mb);
    return result;
}

// -------------------------------------------------------
// KEYBOARD MODULE — real-time single-key reads (see `syntax`'s KEYBOARD
// section). Uses termios raw mode (ICANON+ECHO off) so a key is seen the
// instant it's pressed, no Enter needed. Letters always come back
// lowercase; Shift+letter and Ctrl+letter are reported as "shift+<letter>"
// / "ctrl+<letter>" (the only modifier info a terminal actually exposes —
// a bare Shift/Ctrl/Alt/CapsLock press with nothing else can't be detected
// this way; no terminal program can see that, only its effect on a
// following key).
// -------------------------------------------------------
#include <termios.h>

static struct termios g_kb_orig_termios;
static int g_kb_raw_active = 0;

static void _kb_restore(void) {
    if (g_kb_raw_active) {
        tcsetattr(STDIN_FILENO, TCSANOW, &g_kb_orig_termios);
        g_kb_raw_active = 0;
    }
}

static void _kb_enable_raw(void) {
    if (g_kb_raw_active) return;
    struct termios raw;
    tcgetattr(STDIN_FILENO, &g_kb_orig_termios);
    raw = g_kb_orig_termios;
    raw.c_lflag &= ~(ICANON | ECHO);
    raw.c_cc[VMIN] = 1;
    raw.c_cc[VTIME] = 0;
    tcsetattr(STDIN_FILENO, TCSANOW, &raw);
    g_kb_raw_active = 1;
    atexit(_kb_restore); // otherwise the user's shell is left echo-less/unbuffered after exit
}

// Decode one raw input byte into a key name string (caller must free()).
// ESC gets a short lookahead so arrow-key escape sequences (ESC [ A/B/C/D)
// resolve to "up"/"down"/"right"/"left" instead of three separate reads.
static char* _kb_decode(unsigned char c) {
    if (c == 8 || c == 127) return strdup("backspace");
    if (c == 9) return strdup("tab");
    if (c == 10 || c == 13) return strdup("enter");
    if (c == 32) return strdup("space");
    if (c == 27) {
        struct pollfd pfd; pfd.fd = STDIN_FILENO; pfd.events = POLLIN;
        if (poll(&pfd, 1, 50) > 0) {
            unsigned char c2 = 0;
            if (read(STDIN_FILENO, &c2, 1) == 1 && c2 == '[') {
                unsigned char c3 = 0;
                if (poll(&pfd, 1, 50) > 0) read(STDIN_FILENO, &c3, 1);
                if (c3 == 'A') return strdup("up");
                if (c3 == 'B') return strdup("down");
                if (c3 == 'C') return strdup("right");
                if (c3 == 'D') return strdup("left");
            }
        }
        return strdup("esc");
    }
    if (c >= 1 && c <= 26) {
        char buf[8];
        snprintf(buf, sizeof(buf), "ctrl+%c", (char)('a' + (c - 1)));
        return strdup(buf);
    }
    if (c >= 'A' && c <= 'Z') {
        char buf[8];
        snprintf(buf, sizeof(buf), "shift+%c", (char)(c - 'A' + 'a'));
        return strdup(buf);
    }
    if (c >= 33 && c <= 126) {
        char buf[2]; buf[0] = (char)c; buf[1] = 0;
        return strdup(buf);
    }
    char buf[8];
    snprintf(buf, sizeof(buf), "0x%02x", c);
    return strdup(buf);
}

// keyboard.wait() — blocks until a key is pressed, returns it.
char* rub_keyboard_wait(void) {
    _kb_enable_raw();
    unsigned char c;
    ssize_t n = read(STDIN_FILENO, &c, 1);
    if (n <= 0) return strdup("");
    return _kb_decode(c);
}

static char* g_kb_last_key = NULL;
static pthread_mutex_t g_kb_lock = PTHREAD_MUTEX_INITIALIZER;

// Thread body for `thread(keyboard.thread(rate), id)` — never returns.
void rub_keyboard_thread(double rate) {
    if (rate <= 0) rate = 100.0;
    _kb_enable_raw();
    int timeout_ms = (int)(1000.0 / rate);
    if (timeout_ms < 1) timeout_ms = 1;
    struct pollfd pfd; pfd.fd = STDIN_FILENO; pfd.events = POLLIN;
    for (;;) {
        if (poll(&pfd, 1, timeout_ms) > 0) {
            unsigned char c;
            if (read(STDIN_FILENO, &c, 1) == 1) {
                char* key = _kb_decode(c);
                pthread_mutex_lock(&g_kb_lock);
                if (g_kb_last_key) free(g_kb_last_key);
                g_kb_last_key = key;
                pthread_mutex_unlock(&g_kb_lock);
            }
        }
    }
}

// keyboard.last() — non-blocking, consuming read of keyboard.thread()'s
// latest captured key; "" if nothing new since the last call (mirrors
// net.data()'s single-slot consuming-read convention).
char* rub_keyboard_last(void) {
    pthread_mutex_lock(&g_kb_lock);
    char* key = g_kb_last_key;
    g_kb_last_key = NULL;
    pthread_mutex_unlock(&g_kb_lock);
    if (!key) return strdup("");
    return key;
}

// -------------------------------------------------------
// FFI MODULE — dynamic library loading
// -------------------------------------------------------
#include <dlfcn.h>
#include <stdint.h>

static void* _ffi_handles[1024];
static int _ffi_handle_count = 0;

// GLFW/FFI bundling report (Bug 1): `xeon build` copies a project's .so files
// into build/lib/ next to the compiled binary, but a bare dlopen("libx.so")
// only searches the SYSTEM's standard paths (LD_LIBRARY_PATH, rpath baked in
// at link time, ld.so.cache, /lib, /usr/lib, ...) — none of which include the
// bundled build/lib/ directory, so the binary can't find its own bundled
// library unless the user manually sets LD_LIBRARY_PATH first. Fix: if the
// plain dlopen() fails for a BARE filename (no '/' in it — an explicit
// relative/absolute path is left exactly as given, since the caller already
// knows where the library is), retry next to the RUNNING EXECUTABLE's own
// directory, in a `lib/` subdirectory — exactly where xeon bundles it.
static void _exe_dir(char* out, size_t out_sz) {
    ssize_t n = readlink("/proc/self/exe", out, out_sz - 1);
    if(n <= 0) { out[0] = '\0'; return; }
    out[n] = '\0';
    char* slash = strrchr(out, '/');
    if(slash) *slash = '\0'; else out[0] = '\0';
}
// Load a shared library, return a slot index (used as the "handle" in Rubidium)
// BUG-25: resolve a bare, UNVERSIONED library name to the versioned soname
// actually installed on the system. `FFI("libglfw.so")` is the natural thing
// to write, but the plain "libglfw.so" name only exists if the distro's -dev
// package is installed — a normal runtime install ships only "libglfw.so.3".
// dlopen() does NOT do this fallback itself, so the load failed and every
// call through that handle silently returned 0 (a window that never opened,
// with no error at the call site). Scan the standard library directories for
// "<name>.<version>" and use the highest version found — the same thing the
// -dev symlink would have pointed at.
static int _ffi_version_rank(const char* suffix) {
    // "3" -> 3, "3.3" -> 3, anything non-numeric -> -1 (not a version)
    if(!*suffix) return -1;
    int major = 0, seen = 0;
    for(const char* p = suffix; *p; p++) {
        if(*p >= '0' && *p <= '9') { major = major * 10 + (*p - '0'); seen = 1; }
        else if(*p == '.') break;
        else return -1;
    }
    return seen ? major : -1;
}

static void* _ffi_try_versioned(const char* name) {
    static const char* dirs[] = {
        "/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu",
        "/lib64", "/usr/lib64", "/lib", "/usr/lib", "/usr/local/lib", NULL
    };
    size_t nlen = strlen(name);
    char best[4096]; best[0] = '\0';
    int best_rank = -1;
    for(int d = 0; dirs[d]; d++) {
        DIR* dp = opendir(dirs[d]);
        if(!dp) continue;
        struct dirent* ent;
        while((ent = readdir(dp))) {
            if(strncmp(ent->d_name, name, nlen) != 0) continue;
            if(ent->d_name[nlen] != '.') continue;          // need "<name>.<something>"
            int rank = _ffi_version_rank(ent->d_name + nlen + 1);
            if(rank < 0 || rank <= best_rank) continue;
            best_rank = rank;
            snprintf(best, sizeof(best), "%s/%s", dirs[d], ent->d_name);
        }
        closedir(dp);
    }
    if(best[0]) return dlopen(best, RTLD_LAZY | RTLD_LOCAL);
    return NULL;
}

long long ffi_load(const char* path) {
    void* h = dlopen(path, RTLD_LAZY | RTLD_LOCAL);
    // BUG (found running a real xeon-built project from its PROJECT ROOT,
    // not build/): the exe-dir-relative retry below used to only fire for
    // a BARE name (no '/' at all) — a NESTED relative path like
    // FFI("lib/libraylib.so") (exactly what the smart FFI bundler
    // preserves under build/) was left "exactly as given" on the theory
    // that an explicit path meant the caller already knew where the
    // library was — but the caller only knows where it is RELATIVE TO THE
    // COMPILED OUTPUT, not relative to whatever directory the process
    // happens to be launched from (build/ when run directly, the project
    // ROOT when launched via `xeon run`/most other conventions). ANY
    // relative path (doesn't start with '/') gets this retry now, not
    // just a bare name — an absolute path is left alone (already fully
    // specified, no ambiguity to resolve).
    if(!h && path[0] != '/') {
        char exe_dir[4096];
        _exe_dir(exe_dir, sizeof(exe_dir));
        if(exe_dir[0]) {
            char candidate[4096];
            snprintf(candidate, sizeof(candidate), "%s/%s", exe_dir, path);
            h = dlopen(candidate, RTLD_LAZY | RTLD_LOCAL);
        }
    }
    // BUG-25: last resort for a bare name — find the installed versioned soname.
    if(!h && !strchr(path, '/')) {
        h = _ffi_try_versioned(path);
    }
    if(!h) {
        fprintf(stderr, "[FFI] dlopen failed: %s\n", dlerror());
        return -1;
    }
    if(_ffi_handle_count >= 1024) { dlclose(h); return -1; }
    int idx = _ffi_handle_count++;
    _ffi_handles[idx] = h;
    return idx;
}

// Resolve a symbol from a loaded FFI handle (returns raw function pointer as i64)
long long ffi_sym(long long handle_idx, const char* symbol) {
    if(handle_idx<0||handle_idx>=1024||!_ffi_handles[handle_idx]) return 0;
    dlerror(); // clear errors
    void* sym = dlsym(_ffi_handles[handle_idx], symbol);
    if(!sym) {
        fprintf(stderr, "[FFI] dlsym('%s') failed: %s\n", symbol, dlerror());
        return 0;
    }
    return (long long)(uintptr_t)sym;
}

// -------------------------------------------------------
// FILE HANDLE MODULE — file I/O with automatic close
// -------------------------------------------------------
FILE* _file_handles[1024];
char* _file_paths[1024];
static int _file_handle_count = 0;

// Open a file handle slot — mode: 0 = r+ (read/write, no truncate), 1 = w (create/truncate)
long long file_open(long long slot, const char* path, int mode) {
    if(slot < 0 || slot >= 1024) return -1;
    if(_file_handles[slot]) { fclose(_file_handles[slot]); _file_handles[slot] = NULL; }
    if(_file_paths[slot]) { free(_file_paths[slot]); }
    _file_paths[slot] = strdup(path);
    if(mode == 1) {
        _file_handles[slot] = fopen(path, "w");
        return _file_handles[slot] ? slot : -1;
    }
    // open() as — per syntax file: "If the file does not exist then open
    // will give a error make the file then continue". So a missing file is
    // reported (caller decides catchable-vs-silent based on try/error
    // context) but the file is still created and the block still runs.
    _file_handles[slot] = fopen(path, "r+");
    if (_file_handles[slot]) return slot;          // already existed
    _file_handles[slot] = fopen(path, "w+");       // create + open r/w
    if (_file_handles[slot]) return -2;            // missing, now created
    return -1;                                     // truly unrecoverable (e.g. bad path)
}

// Close a file handle
void file_close(long long slot) {
    if(slot >= 0 && slot < 1024 && _file_handles[slot]) {
        fclose(_file_handles[slot]);
        _file_handles[slot] = NULL;
    }
}

// file.write(data) — overwrite entire file
void file_write_all(long long slot, const char* data) {
    if(slot < 0 || slot >= 1024 || !_file_paths[slot]) return;
    if(_file_handles[slot]) { fclose(_file_handles[slot]); _file_handles[slot] = NULL; }
    FILE* f = fopen(_file_paths[slot], "w");
    if(f) { fputs(data, f); fclose(f); }
    FILE* touch = fopen(_file_paths[slot], "a"); if(touch) fclose(touch);
    _file_handles[slot] = fopen(_file_paths[slot], "r+");
}

// file.read() — read entire file contents
char* file_read_all(long long slot) {
    if(slot < 0 || slot >= 1024 || !_file_paths[slot]) return strdup("");
    if(_file_handles[slot]) { fclose(_file_handles[slot]); _file_handles[slot] = NULL; }
    FILE* f = fopen(_file_paths[slot], "r");
    if(!f) return strdup("");
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    char* buf = malloc(sz + 1);
    size_t rd = fread(buf, 1, sz, f);
    buf[rd] = '\0';
    fclose(f);
    FILE* touch = fopen(_file_paths[slot], "a"); if(touch) fclose(touch);
    _file_handles[slot] = fopen(_file_paths[slot], "r+");
    return buf;
}

// file.append(data) — add to end of file
void file_append_all(long long slot, const char* data) {
    if(slot < 0 || slot >= 1024 || !_file_handles[slot]) return;
    fseek(_file_handles[slot], 0, SEEK_END);
    fputs(data, _file_handles[slot]);
    fflush(_file_handles[slot]);
}

// file.readln(n) — read specific line (0-based, matching the rest of the
// language's indexing convention — see bugs.log follow-up)
char* file_readln(long long slot, long long line_num) {
    if(slot < 0 || slot >= 1024 || !_file_handles[slot]) return strdup("");
    FILE* f = _file_handles[slot];
    rewind(f);
    char buf[4096]; long long cur = 0;
    while(fgets(buf, sizeof(buf), f)) {
        if(cur == line_num) {
            size_t len = strlen(buf);
            if(len > 0 && buf[len-1] == '\n') buf[len-1] = '\0';
            return strdup(buf);
        }
        cur++;
    }
    return strdup("");
}

// file.writeln(line_num, data) — replace or write specific line (0-based)
void file_writeln(long long slot, long long line_num, const char* data) {
    if(slot < 0 || slot >= 1024 || !_file_paths[slot]) return;
    // Read all lines, replace target, write back
    if(_file_handles[slot]) { fclose(_file_handles[slot]); _file_handles[slot] = NULL; }
    FILE* f = fopen(_file_paths[slot], "r");
    char** lines = NULL; int count = 0, cap = 8;
    lines = malloc(cap * sizeof(char*));
    char buf[4096];
    while(fgets(buf, sizeof(buf), f)) {
        if(count == cap) { cap *= 2; lines = realloc(lines, cap * sizeof(char*)); }
        lines[count++] = strdup(buf);
    }
    fclose(f);
    // Extend if needed (need indices 0..line_num, i.e. at least line_num+1 lines)
    while(count <= line_num) {
        if(count == cap) { cap *= 2; lines = realloc(lines, cap * sizeof(char*)); }
        lines[count++] = strdup("\n");
    }
    free(lines[line_num]);
    size_t dlen = strlen(data);
    char* newline = malloc(dlen + 2);
    memcpy(newline, data, dlen); newline[dlen] = '\n'; newline[dlen+1] = '\0';
    lines[line_num] = newline;
    f = fopen(_file_paths[slot], "w");
    for(int i = 0; i < count; i++) { fputs(lines[i], f); free(lines[i]); }
    free(lines);
    fclose(f);
    FILE* touch = fopen(_file_paths[slot], "a"); if(touch) fclose(touch);
    _file_handles[slot] = fopen(_file_paths[slot], "r+");
}

// Legacy write/append for old-style file ops
void file_open_write(long long slot, const char* path) {
    if(slot >= 0 && slot < 1024) {
        if(_file_handles[slot]) fclose(_file_handles[slot]);
        _file_handles[slot] = fopen(path, "w");
    }
}
void file_open_append(long long slot, const char* path) {
    if(slot >= 0 && slot < 1024) {
        if(_file_handles[slot]) fclose(_file_handles[slot]);
        _file_handles[slot] = fopen(path, "a");
    }
}

// file.exists / file.delete / file.rename / file.copy
int file_exists(const char* path) {
    FILE* f = fopen(path, "r");
    if(f) { fclose(f); return 1; }
    return 0;
}
int file_delete(const char* path) {
    return remove(path);
}
int file_rename_file(const char* old_path, const char* new_path) {
    return rename(old_path, new_path);
}
int file_copy_file(const char* src, const char* dst) {
    FILE* in  = fopen(src, "rb");
    if(!in) return -1;
    FILE* out = fopen(dst, "wb");
    if(!out) { fclose(in); return -1; }
    char buf[4096]; size_t n;
    while((n = fread(buf, 1, sizeof(buf), in)) > 0) fwrite(buf, 1, n, out);
    fclose(in); fclose(out);
    return 0;
}

// file.list(path) — list directory entries, like `ls`. Directories get a
// trailing "/" appended to their name; regular files/other entries are
// listed as-is. "." and ".." are skipped.
Box* file_list_dir(const char* path) {
    Box* lst = make_list();
    DIR* d = opendir(path);
    if(!d) return lst;
    struct dirent* ent;
    while((ent = readdir(d)) != NULL) {
        const char* name = ent->d_name;
        if(strcmp(name, ".") == 0 || strcmp(name, "..") == 0) continue;
        char full[4096];
        snprintf(full, sizeof(full), "%s/%s", path, name);
        struct stat st;
        int is_dir = 0;
        if(stat(full, &st) == 0) is_dir = S_ISDIR(st.st_mode);
        if(is_dir) {
            char with_slash[4100];
            snprintf(with_slash, sizeof(with_slash), "%s/", name);
            list_append(lst, box_s(with_slash));
        } else {
            list_append(lst, box_s(name));
        }
    }
    closedir(d);
    return lst;
}

// str.replace(old, new) - replace all occurrences of old with new
char* str_replace(const char* str, const char* old, const char* new_str) {
    if(!str || !old) return strdup(str ? str : "");
    size_t old_len = strlen(old);
    // BUG (found via syntax sweep): an EMPTY `old` string is a genuine
    // infinite loop, confirmed hanging: strstr(p, "") matches at the
    // CURRENT position on every call (an empty needle matches everywhere),
    // and `p += old_len` with old_len==0 never advances p, so the
    // occurrence-counting loop below never terminates — `.replace("", "X")`
    // on any string hangs the program forever. There's no single obvious
    // "correct" semantics for replacing an empty string (languages differ:
    // e.g. Python inserts between every character), and the spec doesn't
    // address it, so this treats it as a safe no-op — the string comes back
    // unchanged — rather than hanging or guessing at a new behavior nobody
    // asked for.
    if(old_len == 0) return strdup(str);
    size_t new_len = strlen(new_str);
    size_t count = 0;
    const char* p = str;
    while((p = strstr(p, old)) != NULL) {
        count++;
        p += old_len;
    }
    size_t str_len = strlen(str);
    size_t result_len = str_len + count * (new_len - old_len);
    char* result = malloc(result_len + 1);
    if(!result) return strdup("");
    char* out = result;
    p = str;
    while(*p) {
        if(strncmp(p, old, old_len) == 0) {
            memcpy(out, new_str, new_len);
            out += new_len;
            p += old_len;
        } else {
            *out++ = *p++;
        }
    }
    *out = '\0';
    return result;
}

// str.slice() - return list of characters
Box* str_slice(const char* str) {
    Box* lst = make_list();
    if(!str) return lst;
    for(size_t i = 0; str[i]; i++) {
        char buf[2] = {str[i], '\0'};
        list_append(lst, box_s(buf));
    }
    return lst;
}
"""

def _prefix_fn_calls(node, prefix, local_fns=None):
    """Recursively prefix FnCall names with module prefix, but only for functions defined in this module."""
    from rub_ast import FnCall, MethodCall, Return, Print, Println, If, While, For, VarDecl, Assign, FieldAssign, Try, FnDef, ClassDef, FileOpen, FileExists, FileDelete, FileRename, FileCopy, FileNew, OsStart, OsRun, OsDrop, FFIBind, Use, Import, Break, Continue, Drop, ThreadCall, ThreadWait, ThreadRunning, BinOp, UnaryOp, Compare, TypeCast, Input, Number, Bool, None_, Str, InterpolatedStr, Var, ListExpr, DictExpr, FieldAccess, ClassInstantiate, FFILoad, FileHandleStmt
    
    if local_fns is None:
        local_fns = set()
    
    if isinstance(node, FnCall):
        if isinstance(node.name, str) and "." not in node.name:
            # Only prefix if it's a function defined in this module
            if node.name in local_fns:
                node.name = f"{prefix}_{node.name}"
    elif isinstance(node, MethodCall):
        _prefix_fn_calls(node.obj, prefix, local_fns)
        for arg in node.args:
            _prefix_fn_calls(arg, prefix, local_fns)
    elif isinstance(node, BinOp):
        _prefix_fn_calls(node.left, prefix, local_fns)
        _prefix_fn_calls(node.right, prefix, local_fns)
    elif isinstance(node, UnaryOp):
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, Compare):
        _prefix_fn_calls(node.left, prefix, local_fns)
        _prefix_fn_calls(node.right, prefix, local_fns)
    elif isinstance(node, TypeCast):
        _prefix_fn_calls(node.expr, prefix, local_fns)
    elif isinstance(node, Return):
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, Print):
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, Println):
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, If):
        _prefix_fn_calls(node.cond, prefix, local_fns)
        for s in node.then_body:
            _prefix_fn_calls(s, prefix, local_fns)
        for s in (node.else_body or []):
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, While):
        _prefix_fn_calls(node.cond, prefix, local_fns)
        for s in node.body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, For):
        for s in node.body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, VarDecl):
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, Assign):
        if isinstance(node.name, str) and "." not in node.name:
            if node.name in local_fns:
                node.name = f"{prefix}_{node.name}"
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, FieldAssign):
        _prefix_fn_calls(node.obj, prefix, local_fns)
        _prefix_fn_calls(node.value, prefix, local_fns)
    elif isinstance(node, Try):
        for s in node.try_body:
            _prefix_fn_calls(s, prefix, local_fns)
        for s in node.error_body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, FnDef):
        for s in node.body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, ClassDef):
        for f in node.fields:
            _prefix_fn_calls(f.value, prefix, local_fns)
        for m in node.methods:
            for s in m.body:
                _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, FileOpen):
        _prefix_fn_calls(node.path_expr, prefix, local_fns)
        for s in node.body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, FileExists):
        _prefix_fn_calls(node.path_expr, prefix, local_fns)
    elif isinstance(node, FileDelete):
        _prefix_fn_calls(node.path_expr, prefix, local_fns)
    elif isinstance(node, FileRename):
        _prefix_fn_calls(node.old_path, prefix, local_fns)
        _prefix_fn_calls(node.new_path, prefix, local_fns)
    elif isinstance(node, FileCopy):
        _prefix_fn_calls(node.src_path, prefix, local_fns)
        _prefix_fn_calls(node.dst_path, prefix, local_fns)
    elif isinstance(node, FileNew):
        _prefix_fn_calls(node.path_expr, prefix, local_fns)
        for s in node.body:
            _prefix_fn_calls(s, prefix, local_fns)
    elif isinstance(node, OsStart):
        _prefix_fn_calls(node.id_expr, prefix, local_fns)
    elif isinstance(node, OsRun):
        _prefix_fn_calls(node.id_expr, prefix, local_fns)
        _prefix_fn_calls(node.cmd_expr, prefix, local_fns)
        if node.input_expr:
            _prefix_fn_calls(node.input_expr, prefix, local_fns)
        if node.struct_args:
            for k, v in node.struct_args.items():
                _prefix_fn_calls(v, prefix, local_fns)
    elif isinstance(node, OsDrop):
        _prefix_fn_calls(node.id_expr, prefix, local_fns)
    elif isinstance(node, FFIBind):
        pass  # FFI bindings don't need prefixing
    elif isinstance(node, ThreadCall):
        _prefix_fn_calls(node.func_call, prefix, local_fns)
        _prefix_fn_calls(node.thread_id, prefix, local_fns)
    elif isinstance(node, ThreadWait):
        for tid in node.thread_ids:
            _prefix_fn_calls(tid, prefix, local_fns)
    elif isinstance(node, ThreadRunning):
        _prefix_fn_calls(node.thread_id, prefix, local_fns)
    elif isinstance(node, FFILoad):
        _prefix_fn_calls(node.path_expr, prefix, local_fns)
    elif isinstance(node, FileHandleStmt):
        for arg in node.args:
            _prefix_fn_calls(arg, prefix, local_fns)
    elif isinstance(node, Input):
        if node.prompt:
            _prefix_fn_calls(node.prompt, prefix, local_fns)
    elif isinstance(node, ListExpr):
        for e in node.elements:
            _prefix_fn_calls(e, prefix, local_fns)
    elif isinstance(node, DictExpr):
        for k, v in node.pairs:
            _prefix_fn_calls(k, prefix, local_fns)
            _prefix_fn_calls(v, prefix, local_fns)
    elif isinstance(node, InterpolatedStr):
        for part in node.parts:
            _prefix_fn_calls(part, prefix, local_fns)

def _collect_module_globals(ast):
    """BUG-20: every top-level variable an imported module declares, plus the
    non-local `let`s inside its functions (per spec those enter the global
    pool too). These are the names that must be rewritten to their prefixed
    module-global form wherever the module refers to them."""
    from rub_ast import Var
    names = set()

    def scan(stmts):
        for s in stmts:
            if isinstance(s, VarDecl):
                if not getattr(s, 'is_local', False):
                    names.add(s.name)
                scan_expr_bodies(s)
            elif isinstance(s, FnDef):
                scan(s.body)
            elif isinstance(s, ClassDef):
                for m in (s.methods or []):
                    scan(m.body)
            elif isinstance(s, If):
                scan(s.then_body or []); scan(s.else_body or [])
            elif isinstance(s, (While, For)):
                scan(s.body or [])
            elif isinstance(s, Try):
                scan(s.try_body or []); scan(s.error_body or [])
            elif isinstance(s, FileOpen):
                scan(s.body or [])

    def scan_expr_bodies(_s):
        return

    scan(ast)
    return names


def _qualify_module_globals(node, prefix, mod_vars, shadowed):
    """BUG-20: rewrite a module's references to its OWN module-level variables
    into their prefixed form.

    parse_file renames an imported module's top-level `let x` to `mod_x`, but
    nothing rewrote the references — so a module function reading its own
    global failed with "Undefined variable", and assigning to one failed with
    "Immutable". That made any imported module WITH STATE unusable; only pure
    functions and externally-read variables happened to work.

    `shadowed` carries the names bound more locally (parameters, `let local`,
    loop variables, file handles) which must NOT be rewritten — per spec a
    local shadows a global and takes priority.
    """
    from rub_ast import (FnCall, MethodCall, Return, Print, Println, FieldAssign,
                         BinOp, UnaryOp, Compare, TypeCast, Input, InterpolatedStr,
                         Var, ListExpr, DictExpr, FieldAccess, MathBlock, LinkArg,
                         ThreadCall, ThreadWait, ThreadRunning, ElementDrop, Raise,
                         FileHandleStmt, OsStart, OsRun, OsDrop, FileNew, FileExists,
                         FileDelete, FileRename, FileCopy, FileList, DynVarDecl)

    def q(name):
        """Prefixed form of `name`, or None when it must be left alone."""
        if not isinstance(name, str) or "." in name:
            return None
        if name in shadowed or name not in mod_vars:
            return None
        if name.startswith(f"{prefix}_"):
            return None
        return f"{prefix}_{name}"

    def walk(n, sh):
        if n is None or isinstance(n, (str, int, float, bool)):
            return
        if isinstance(n, list):
            for c in n:
                walk(c, sh)
            return

        if isinstance(n, Var):
            new = _qualify_module_globals._q(n.name, sh, mod_vars, prefix)
            if new:
                n.name = new
            return

        if isinstance(n, FnCall):
            # `items(0)` — collection access parses as a call on the variable.
            new = _qualify_module_globals._q(n.name, sh, mod_vars, prefix)
            if new:
                n.name = new
            else:
                walk(n.name, sh)
            walk(n.args, sh)
            return

        if isinstance(n, Assign):
            new = _qualify_module_globals._q(n.name, sh, mod_vars, prefix)
            if new:
                n.name = new
            walk(n.value, sh)
            return

        if isinstance(n, Drop):
            new = _qualify_module_globals._q(n.name, sh, mod_vars, prefix)
            if new:
                n.name = new
            return

        if isinstance(n, VarDecl):
            walk(n.value, sh)
            # A `let local` binds a NEW name for the rest of this scope, so it
            # shadows any module global of the same name from here on.
            if getattr(n, 'is_local', False):
                sh.add(n.name)
            else:
                new = _qualify_module_globals._q(n.name, sh, mod_vars, prefix)
                if new:
                    n.name = new
            return

        if isinstance(n, FnDef):
            inner = set(sh)
            inner.update(p[0] for p in (n.params or []))
            walk(n.body, inner)
            return

        if isinstance(n, ClassDef):
            for f in (n.fields or []):
                walk(getattr(f, 'value', None), set(sh))
            for m in (n.methods or []):
                inner = set(sh)
                inner.update(p[0] for p in (m.params or []))
                # Class fields are reached bare inside methods (no `self`), so
                # they shadow module globals of the same name.
                inner.update(f.name for f in (n.fields or []))
                walk(m.body, inner)
            return

        if isinstance(n, For):
            inner = set(sh)
            if isinstance(n.var, str):
                inner.add(n.var)
            walk(getattr(n, 'iterable', None), sh)
            walk(getattr(n, 'start', None), sh)
            walk(getattr(n, 'end', None), sh)
            walk(n.body, inner)
            return

        if isinstance(n, FileOpen):
            walk(n.path_expr, sh)
            inner = set(sh)
            inner.add(n.var_name)
            walk(n.body or [], inner)
            return

        # Everything else: walk each child attribute generically. A block body
        # gets its own shadow set so declarations inside don't leak out.
        for attr in ('value', 'expr', 'left', 'right', 'cond', 'obj',
                     'path_expr', 'message', 'prompt', 'thread_id',
                     'func_call', 'id_expr', 'cmd_expr', 'input_expr',
                     'old_path', 'new_path', 'src_path', 'dst_path',
                     'access_node'):
            if hasattr(n, attr):
                walk(getattr(n, attr), sh)
        for attr in ('args', 'elements', 'parts', 'thread_ids'):
            if hasattr(n, attr):
                walk(getattr(n, attr), sh)
        if hasattr(n, 'pairs'):
            for k, v in (n.pairs or []):
                walk(k, sh); walk(v, sh)
        if hasattr(n, 'struct_args') and n.struct_args:
            for v in n.struct_args.values():
                walk(v, sh)
        for attr in ('body', 'then_body', 'else_body', 'try_body', 'error_body'):
            if hasattr(n, attr):
                walk(getattr(n, attr) or [], set(sh))

    walk(node, set(shadowed))


def _qualify_module_globals_q(name, shadowed, mod_vars, prefix):
    if not isinstance(name, str) or "." in name:
        return None
    if name in shadowed or name not in mod_vars:
        return None
    if name.startswith(f"{prefix}_"):
        return None
    return f"{prefix}_{name}"


_qualify_module_globals._q = _qualify_module_globals_q


def _rebind_namespace(node, old_ns, new_ns):
    """FEATURE (`import local`): point one file's references at a PRIVATE copy
    of a module. Rewrites `old_ns.foo` -> `new_ns.foo` everywhere in this
    file's AST — dotted call names (`file_one.bump()`), and the Var that names
    the namespace in a field access / method call (`file_one.counter`).
    Only this file's references move; every other importer keeps pointing at
    the shared instance."""
    from rub_ast import FnCall, Var

    def walk(n):
        if n is None or isinstance(n, (str, int, float, bool)):
            return
        if isinstance(n, list):
            for c in n:
                walk(c)
            return
        if isinstance(n, FnCall) and isinstance(n.name, str) and n.name.startswith(old_ns + "."):
            n.name = new_ns + n.name[len(old_ns):]
        elif isinstance(n, Var) and n.name == old_ns:
            n.name = new_ns
        elif isinstance(n, FnCall):
            walk(n.name)
        for attr in ('value', 'expr', 'left', 'right', 'cond', 'obj',
                     'path_expr', 'message', 'prompt', 'thread_id',
                     'func_call', 'id_expr', 'cmd_expr', 'input_expr',
                     'old_path', 'new_path', 'src_path', 'dst_path',
                     'access_node', 'iterable', 'start', 'end'):
            if hasattr(n, attr):
                walk(getattr(n, attr))
        for attr in ('args', 'elements', 'parts', 'thread_ids', 'fields'):
            if hasattr(n, attr):
                walk(getattr(n, attr))
        if hasattr(n, 'pairs'):
            for k, v in (n.pairs or []):
                walk(k); walk(v)
        if hasattr(n, 'struct_args') and n.struct_args:
            for v in n.struct_args.values():
                walk(v)
        for attr in ('body', 'then_body', 'else_body', 'try_body', 'error_body'):
            if hasattr(n, attr):
                walk(getattr(n, attr) or [])
        if hasattr(n, 'methods'):
            for m in (n.methods or []):
                walk(m.body or [])

    walk(node)


def parse_file(filepath, parsed_files, combined_ast, is_main=False, mod_name_override=None):
    abs_path = os.path.abspath(filepath)
    # The dedupe key includes the module name a file is being parsed UNDER, not
    # just its path. A plain `import` always resolves to the same name, so it
    # is still parsed exactly once and every importer shares that one instance.
    # An `import local` asks for a private copy under a different name, so it
    # must be allowed to parse the same file again (FEATURE: import local).
    parse_key = (abs_path, mod_name_override or "")
    if parse_key in parsed_files:
        return
    parsed_files.add(parse_key)
    
    # Generate module name from file (e.g., 'math_tools' from 'math_tools.rub').
    # Package imports (`xeon <name>`) override this: every package's main file
    # is literally named pkg.rub, so deriving the prefix from the filename
    # would collide across packages — use the package name instead.
    # Sanitize: replace hyphens with underscores for valid LLVM identifiers
    mod_name = (mod_name_override or os.path.splitext(os.path.basename(filepath))[0]).replace("-", "_")
    
    try:
        with open(filepath, "r") as f:
            code = f.read()
    except FileNotFoundError:
        print(f"✖ Error: Could not find imported file '{filepath}'")
        sys.exit(1)
        
    tokens = tokenize(code)
    ast = Parser(tokens).parse()
    
    # Collect local function names BEFORE prefixing (for _prefix_fn_calls)
    local_fns_original = set()
    for node in ast:
        if isinstance(node, FnDef):
            local_fns_original.add(node.name)  # Original name before prefixing
    
    # BUG-20: the module's own global variable names, captured BEFORE any
    # renaming, so references to them can be rewritten to the prefixed form.
    mod_vars_original = set() if is_main else _collect_module_globals(ast)

    # First pass: prefix all names (only for imported files, not main)
    if not is_main:
        for node in ast:
            if isinstance(node, (VarDecl, FnDef, ClassDef)):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, Assign) and not node.name.startswith(mod_name + "_"):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, Drop) and not node.name.startswith(mod_name + "_"):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, FFIBind) and not node.handle_name.startswith(mod_name + "_"):
                node.handle_name = f"{mod_name}_{node.handle_name}"
    
    # Collect local function names AFTER prefixing (for CodeGen)
    local_fns_prefixed = set()
    for node in ast:
        if isinstance(node, FnDef):
            local_fns_prefixed.add(node.name)  # This is the PREFIXED name
    
    # Second pass: prefix function calls inside bodies (only for imported files)
    if not is_main:
        for node in ast:
            if isinstance(node, FnDef):
                for stmt in node.body:
                    _prefix_fn_calls(stmt, mod_name, local_fns_original)

    # Third pass (BUG-20): rewrite the module's references to its OWN globals
    # into the prefixed names the first pass gave those declarations. Without
    # this a module function could not read or assign its own module-level
    # state at all. Top-level statements are qualified too, since a module's
    # top level runs as part of program init and may read its own globals.
    if not is_main and mod_vars_original:
        for node in ast:
            _qualify_module_globals(node, mod_name, mod_vars_original, set())
    
    def _find_imports(stmts):
        """Recursively find Import nodes in a list of statements (including nested in functions)."""
        imports = []
        for stmt in stmts:
            if isinstance(stmt, Import):
                imports.append(stmt)
            elif isinstance(stmt, FnDef):
                imports.extend(_find_imports(stmt.body))
            elif isinstance(stmt, If):
                imports.extend(_find_imports(stmt.then_body))
                imports.extend(_find_imports(stmt.else_body or []))
            elif isinstance(stmt, While):
                imports.extend(_find_imports(stmt.body))
            elif isinstance(stmt, For):
                imports.extend(_find_imports(stmt.body))
            elif isinstance(stmt, Try):
                imports.extend(_find_imports(stmt.try_body))
                imports.extend(_find_imports(stmt.error_body))
            elif isinstance(stmt, FileOpen):
                imports.extend(_find_imports(stmt.body))
            elif isinstance(stmt, ClassDef):
                for m in stmt.methods:
                    imports.extend(_find_imports(m.body))
        return imports

    # Find all Import nodes (including those nested in function bodies)
    all_imports = _find_imports(ast)
    
    for node in all_imports:
        if isinstance(node, Import):
            if node.is_xeon_pkg:
                pkg_dir = os.path.join(os.path.expanduser("~"), ".xeon", "packages", node.module_name)
                mod_path = os.path.join(pkg_dir, "pkg.rub")
                if os.path.exists(mod_path):
                    parse_file(mod_path, parsed_files, combined_ast, mod_name_override=node.module_name)
                else:
                    alias_note = f" (alias: {node.alias})" if getattr(node, 'alias', None) else ""
                    print(f"\033[1;33mWARNING\033[0m: Package '{node.module_name}'{alias_note} "
                          f"is not installed — run 'xeon pkg pull {node.module_name}' first. "
                          f"Calls to this module will be no-ops.")
                continue
            mod_file = node.module_name.replace(".", os.sep) + ".rub"
            base_dir = os.path.dirname(filepath)
            mod_path = os.path.join(base_dir, mod_file) if base_dir else mod_file
            if os.path.exists(mod_path):
                if getattr(node, 'is_local', False):
                    # FEATURE (`import local`): a private instance for THIS
                    # file only. Parse the module again under a name unique to
                    # this importer, then point this file's references at it.
                    # Other importers are untouched and keep sharing the one
                    # global instance.
                    base_ns = os.path.splitext(os.path.basename(mod_file))[0].replace("-", "_")
                    private_ns = f"{mod_name}__{base_ns}"
                    parse_file(mod_path, parsed_files, combined_ast,
                               mod_name_override=private_ns)
                    for stmt in ast:
                        _rebind_namespace(stmt, base_ns, private_ns)
                    if getattr(node, 'alias', None):
                        for stmt in ast:
                            _rebind_namespace(stmt, node.alias, private_ns)
                else:
                    parse_file(mod_path, parsed_files, combined_ast)
            else:
                alias_note = f" (alias: {node.alias})" if getattr(node, 'alias', None) else ""
                print(f"\033[1;33mWARNING\033[0m: Import '{node.module_name}'{alias_note} "
                      f"not found at '{mod_path}' — calls to this module will be no-ops.")
        elif isinstance(node, Use):
            continue
    
    combined_ast.extend(ast)

NATIVE_CHOICES = ("current", "amd64", "amd32", "arm32", "arm64")


def compile_files(source_files, output=None, shared_lib=False, native=None):
    def _find_imports_in_ast(ast_nodes):
        """Recursively find Import nodes in AST (including nested in functions)."""
        imports = []
        for node in ast_nodes:
            if isinstance(node, Import):
                imports.append(node)
            elif isinstance(node, FnDef):
                imports.extend(_find_imports_in_ast(node.body))
            elif isinstance(node, ClassDef):
                for m in node.methods:
                    imports.extend(_find_imports_in_ast(m.body))
        return imports

    try:
        parsed_files = set()
        combined_ast = []
        
        for i, source_file in enumerate(source_files):
            parse_file(source_file, parsed_files, combined_ast, is_main=(i == 0))

        # Find all Import nodes in the combined AST (including nested in functions)
        all_imports = _find_imports_in_ast(combined_ast)
        
        gen = CodeGen(import_aliases={
            node.alias: os.path.splitext(os.path.basename(node.module_name.replace(".", os.sep)))[0].replace("-", "_")
            for node in all_imports
            if isinstance(node, Import) and getattr(node, 'alias', None)
        }, shared_lib=shared_lib)
        ir_code = gen.gen(combined_ast)
        with open('/tmp/dump.ll', 'w') as _f:
            _f.write(ir_code)

        if output: output_bin = output
        else: output_bin = os.path.splitext(os.path.basename(source_files[0]))[0]

        if shared_lib and not output_bin.endswith((".so", ".dylib", ".dll")):
            output_bin += ".so"

        with tempfile.NamedTemporaryFile(suffix=".ll", delete=False, mode="w") as f_ll:
            f_ll.write(ir_code)
            ir_path = f_ll.name
            
        with tempfile.NamedTemporaryFile(suffix=".c", delete=False, mode="w") as f_c:
            f_c.write(RUNTIME_C)
            c_path = f_c.name

        def _clang_cmd(flags):
            base = ["clang", "-shared", "-fPIC", ir_path, c_path, "-o", output_bin] if shared_lib \
                else ["clang", ir_path, c_path, "-o", output_bin]
            return base + flags + ["-pthread", "-ldl", "-lm"]

        # `native` selects what the binary is tuned/targeted for:
        #   "current" — tune for THIS machine's exact CPU (-march=native).
        #               Fastest, but not portable to other machines.
        #   "amd64" / "amd32" / "arm64" / "arm32" — cross-target a generic
        #               baseline for that whole architecture family (no
        #               per-CPU tuning), so the binary just works on any
        #               machine of that family instead of only this one.
        #               "amd64"/"amd32" cover both Intel and AMD chips —
        #               x86-64 and 32-bit x86 respectively are the same ISA
        #               on either vendor, there's no separate Intel name.
        # None (the default) — plain host-default codegen, same as before
        # this option existed.
        CROSS_TARGETS = {
            "amd64": ["--target=x86_64-linux-gnu"],
            "amd32": ["--target=i386-linux-gnu"],
            "arm64": ["--target=aarch64-linux-gnu"],
            "arm32": ["--target=arm-linux-gnueabihf"],
        }
        # Cross-target flags define WHICH architecture we're building for —
        # unlike LTO/native-tuning below, these can never be dropped as a
        # "safety" fallback: silently losing --target on a retry wouldn't
        # make the build safer, it would silently build for the wrong
        # architecture while still reporting success.
        target_flags = CROSS_TARGETS.get(native, [])

        # Extra flags that make the binary faster but carry their own crash
        # risk, layered on top of the base -O level:
        #   -flto         cross-TU inlining between the generated IR and
        #                 RUNTIME_C (currently compiled/linked as separate
        #                 translation units, so hot runtime helpers like
        #                 box_s/list_append can't be inlined without it).
        #                 Always on — it's link-time, not a new opt level.
        #   -march=native tunes codegen for this exact CPU. Only added for
        #                 native == "current" (wired up from `xeon build
        #                 --native current`) — meaningless (and invalid)
        #                 together with a cross-target above.
        tuning_flags = ["-flto"] + (["-march=native"] if native == "current" else [])

        # An internal compiler crash inside clang/LLVM's own optimizer (as
        # opposed to a normal "your IR/code is wrong" diagnostic) shows up as
        # a distinct signature in stderr — a stack dump and a request to file
        # an LLVM bug report — rather than a plain error message. This is a
        # known class of register-allocator/vectorizer crash unrelated to
        # whether the Rubidium source is valid (the static analyzer already
        # passed it); we can't patch LLVM itself from here, but we can back
        # off through a ladder of progressively safer (and slower) flag sets
        # until one of them doesn't trip whatever pass is crashing, instead
        # of jumping straight from full -O2 to unoptimized -O0. tuning_flags
        # ride along with every rung except the last (dropped there in case
        # LTO/native-tuning is itself what's crashing); target_flags ride
        # along with EVERY rung including the last, since they're not an
        # optimization to give up on.
        def _is_llvm_ice(stderr):
            markers = ("PLEASE submit a bug report", "Stack dump:",
                       "clang frontend command failed due to signal")
            return any(m in stderr for m in markers)

        OPT_LADDER = [
            ("-O2", ["-O2"] + tuning_flags + target_flags),
            ("-O2 (no vectorizer)", ["-O2", "-fno-vectorize", "-fno-slp-vectorize"] + tuning_flags + target_flags),
            ("-O1", ["-O1"] + tuning_flags + target_flags),
            ("-O0 (safe fallback)", ["-O0"] + target_flags),
        ]

        try:
            result = None
            for idx, (label, flags) in enumerate(OPT_LADDER):
                result = subprocess.run(_clang_cmd(flags), capture_output=True, text=True)
                if result.returncode == 0:
                    if idx > 0:
                        print(f"⚠ clang/LLVM crashed internally at earlier optimization levels "
                              f"(not a Rubidium error) — built successfully at {label}.")
                    break
                if _is_llvm_ice(result.stderr):
                    print(f"⚠ {label} crashed inside clang/LLVM itself — falling back...")
                    continue
                # A real (non-crash) compile error — no point trying weaker
                # optimization levels, they'll fail the same way.
                break
        finally:
            pass
            # os.unlink(ir_path)
            # os.unlink(c_path)

        if result.returncode != 0:
            print("✖ Compilation failed:")
            print(result.stderr)
            sys.exit(1)

        if shared_lib:
            print(f"✔ Compiled shared library → ./{output_bin} (no entry point — fn's are exported for FFI)")
        else:
            print(f"✔ Compiled → ./{output_bin}")
    
    except SyntaxError as e:
        print(f"✖ Syntax Error: {e}")
        sys.exit(1)
    except RubidiumTypeError as e:
        print(f"✖ Type Error: {e}")
        sys.exit(1)
    except RubidiumNameError as e:
        print(f"✖ Name Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✖ Compilation Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    USAGE = (f"Usage: python compiler.py [-s] [--native {{{'|'.join(NATIVE_CHOICES)}}}] "
             f"<file1.rub> [file2.rub ...] [output]")
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    argv = sys.argv[1:]
    shared_lib = "-s" in argv

    native = None
    if "--native" in argv:
        idx = argv.index("--native")
        if idx + 1 >= len(argv) or argv[idx + 1] not in NATIVE_CHOICES:
            print(USAGE)
            print(f"  --native requires one of: {', '.join(NATIVE_CHOICES)}")
            sys.exit(1)
        native = argv[idx + 1]
        del argv[idx:idx + 2]

    argv = [a for a in argv if a != "-s"]

    if not argv:
        print(USAGE)
        sys.exit(1)

    if not argv[-1].endswith('.rub'):
        output = argv[-1]
        source_files = argv[:-1]
    else:
        output = None
        source_files = argv[:]

    expanded_files = []
    for pattern in source_files:
        if '*' in pattern or '?' in pattern or '[' in pattern:
            expanded_files.extend(glob.glob(pattern))
        else:
            expanded_files.append(pattern)

    compile_files(expanded_files, output, shared_lib=shared_lib, native=native)