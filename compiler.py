import sys
import os
import subprocess
import tempfile
import glob

from lexer import tokenize
from parser import Parser
from rub_ast import Import, Use, VarDecl, FnDef, ClassDef, Assign, Drop, FFIBind
from codegen import CodeGen, RubidiumTypeError, RubidiumNameError

RUNTIME_C = r"""
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <pthread.h>

typedef struct { int type; long long i; double f; char* s; void* p; } Box;
typedef struct { int magic; Box** items; int count; int cap; } RList;
typedef struct { int magic; Box** keys; Box** vals; int count; int cap; } RDict;

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

void box_drop(Box* b) {
    if (!b) return;
    if (b->type == 2 && b->s) free(b->s);
    if (b->type == 3 && b->p) {
        int* magic = (int*)b->p;
        if (magic && *magic == 1) {
            RList* l = (RList*)b->p;
            for(int i=0; i<l->count; i++) box_drop(l->items[i]);
            free(l->items); free(l);
        } else if (magic && *magic == 2) {
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
Box* box_copy(Box* src) {
    if(!src) return box_i(0);
    if(src->type==0) return box_i(src->i);
    if(src->type==1) return box_f(src->f);
    if(src->type==2) return box_s(src->s);
    if(src->type==4) return box_b(src->i);
    return src; /* collections: return same pointer, caller must not drop */
}

long long unbox_i(Box* b) { return b ? b->i : 0; }
double unbox_f(Box* b) { return b ? b->f : 0.0; }
char* unbox_s(Box* b) { return (b && b->type==2) ? b->s : ""; }
void* unbox_p(Box* b) { return (b && b->type==3) ? b->p : NULL; }

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
double _rubidium_pow(double base, double exp) { return pow(base, exp); }

Box* make_list() { RList* l=malloc(sizeof(RList)); l->magic=1; l->count=0; l->cap=8; l->items=malloc(8*sizeof(Box*)); return box_p(l); }
void list_append(Box* lst, Box* b) { 
    if(!lst || lst->type != 3) return;
    RList* l=lst->p; 
    if(!l || l->magic != 1) return; /* not a list */
    // Spec: [Null].add(x) -> [x]  (single null is replaced, not appended to)
    // [1, Null].add(x) -> [1, Null, x]  (null in non-singleton list is kept)
    if (l->count == 1 && l->items[0] && l->items[0]->type == 0 && l->items[0]->i == -2147483648LL) {
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

// Recursive deep copy — creates a fully independent clone of any Box value.
// Called on every variable assignment to satisfy the spec's deep-copy semantics.
Box* box_deep_copy(Box* src) {
    if(!src) return box_i(0);
    if(src->type==0) return box_i(src->i);
    if(src->type==1) return box_f(src->f);
    if(src->type==2) return box_s(src->s ? src->s : "");
    if(src->type!=3 || !src->p) { Box* b=malloc(sizeof(Box)); b->type=0; b->i=0; return b; }
    int* magic = (int*)src->p;
    if(*magic==1) {
        RList* sl = (RList*)src->p;
        RList* dl = malloc(sizeof(RList));
        dl->magic=1; dl->count=sl->count; dl->cap=sl->count>0?sl->count:1;
        dl->items = malloc(dl->cap * sizeof(Box*));
        for(int i=0;i<sl->count;i++) dl->items[i]=box_deep_copy(sl->items[i]);
        return box_p(dl);
    }
    if(*magic==2) {
        RDict* sd = (RDict*)src->p;
        RDict* dd = malloc(sizeof(RDict));
        dd->magic=2; dd->count=sd->count; dd->cap=sd->count>0?sd->count:1;
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
    return a->p==b->p;
}
void dict_set(Box* dct, Box* k, Box* v) {
    if(!dct || dct->type != 3) return;
    RDict* d=dct->p;
    if(!d || d->magic != 2) return; /* not a dict */
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
    // Null sentinel: auto-promote to a list and append in-place.
    // The box is heap-allocated and parent collections hold a pointer to it,
    // so mutating type+p is visible through all references.
    if (col_box && col_box->type == 0 && col_box->i == -2147483648LL) {
        RList* l = malloc(sizeof(RList));
        l->magic = 1; l->count = 0; l->cap = 8;
        l->items = malloc(8 * sizeof(Box*));
        col_box->type = 3;
        col_box->p = l;
        list_append(col_box, arg);
        return;
    }
    if (!col_box || col_box->type != 3 || !col_box->p) return;
    int magic = *(int*)col_box->p;
    if (magic == 1) {
        list_append(col_box, arg);
    } else if (magic == 2) {
        Box* null_val = box_i(-2147483648LL);  // Rubidium Null sentinel
        dict_set(col_box, arg, null_val);
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
    if (*magic == 2) return dict_get(col, key);
    return box_i(0);
}

void collection_set(Box* col_box, Box* key, Box* val) {
    // Null sentinel: auto-promote to a dict and set the key in-place.
    if (col_box && col_box->type == 0 && col_box->i == -2147483648LL) {
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
    } else if (*magic == 2) {
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
    } else if (*magic == 2) {
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
    if (*magic == 2) return ((RDict*)col)->count;
    return 0;
}

unsigned int _rub_str_hash(const char* s) {
    unsigned long hash = 5381;
    int c;
    while ((c = *s++)) hash = ((hash << 5) + hash) + c;
    return (unsigned int)hash;
}

int collection_has(Box* col, Box* needle) {
    if (!col || col->type != 3) return 0;
    void* ptr = col->p;
    if(!ptr) return 0;
    int* magic = (int*)ptr;
    if (*magic == 1) {
        RList* l = ptr;
        for(int i = 0; i < l->count; i++)
            if(box_eq(l->items[i], needle)) return 1;
    } else if (*magic == 2) {
        RDict* d = ptr;
        for(int i = 0; i < d->count; i++)
            if(box_eq(d->keys[i], needle)) return 1;
    }
    return 0;
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
    if (*magic == 2) {
        RDict* d = col;
        if(idx>=0 && idx<d->count) return d->keys[idx];
    }
    return box_i(0);
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
    if(b->type==0) {
        if(b->i == -2147483648LL) { snprintf(buf, 64, "Null"); }  // sentinel
        else { snprintf(buf, 64, "%lld", b->i); }
    }
    else if(b->type==4) { snprintf(buf, 64, "%s", b->i ? "True" : "False"); }
    else if(b->type==1) { snprintf(buf, 64, "%g",   b->f); }
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
        } else if(magic && *magic==2) {
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
char* os_run(long long id, const char* cmd, const char* input) {
    if(id<0||id>=1024||!_os_terminals[id].active) return strdup("");

    OsTerminal* t = &_os_terminals[id];

    // Write command + newline.
    // When input is provided, use printf 'INPUT' | CMD so the child process
    // gets a self-contained closed stdin — prevents it lingering and consuming
    // subsequent os.run commands as its own stdin.
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

    // Collect output with timeout
    char buf[4096];
    char* out = malloc(1);
    out[0]='\0';
    size_t out_len=0;
    int retries=30; // up to 1.5s total
    while(retries-->0) {
        usleep(50000);
        ssize_t n = read(t->stdout_fd, buf, sizeof(buf)-1);
        if(n>0) {
            buf[n]='\0';
            out = realloc(out, out_len+n+1);
            memcpy(out+out_len, buf, n);
            out_len+=n; out[out_len]='\0';
            retries=5; // got data, keep reading a bit more
        } else if(n<0 && errno==EAGAIN) {
            if(out_len>0 && retries<10) break; // got some output, we're done
        }
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

// -------------------------------------------------------
// FFI MODULE — dynamic library loading
// -------------------------------------------------------
#include <dlfcn.h>
#include <stdint.h>

static void* _ffi_handles[1024];
static int _ffi_handle_count = 0;

// Load a shared library, return a slot index (used as the "handle" in Rubidium)
long long ffi_load(const char* path) {
    void* h = dlopen(path, RTLD_LAZY | RTLD_LOCAL);
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

def parse_file(filepath, parsed_files, combined_ast, is_main=False, mod_name_override=None):
    abs_path = os.path.abspath(filepath)
    if abs_path in parsed_files:
        return
    parsed_files.add(abs_path)
    
    # Generate module name from file (e.g., 'math_tools' from 'math_tools.rub').
    # Package imports (`xeon <name>`) override this: every package's main file
    # is literally named pkg.rub, so deriving the prefix from the filename
    # would collide across packages — use the package name instead.
    mod_name = mod_name_override or os.path.splitext(os.path.basename(filepath))[0]
    
    try:
        with open(filepath, "r") as f:
            code = f.read()
    except FileNotFoundError:
        print(f"✖ Error: Could not find imported file '{filepath}'")
        sys.exit(1)
        
    tokens = tokenize(code)
    ast = Parser(tokens).parse()
    
    # First pass: prefix all names (only for imported files, not main)
    for node in ast:
        if not is_main:
            if isinstance(node, (VarDecl, FnDef, ClassDef)):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, Assign) and not node.name.startswith(mod_name + "_"):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, Drop) and not node.name.startswith(mod_name + "_"):
                node.name = f"{mod_name}_{node.name}"
            elif isinstance(node, FFIBind) and not node.handle_name.startswith(mod_name + "_"):
                node.handle_name = f"{mod_name}_{node.handle_name}"
    
    # Collect local function names BEFORE prefixing (for _prefix_fn_calls)
    local_fns_original = set()
    for node in ast:
        if isinstance(node, FnDef):
            local_fns_original.add(node.name)  # This is the ORIGINAL name before prefixing
    
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
    
    for node in ast:
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
                parse_file(mod_path, parsed_files, combined_ast)
            else:
                alias_note = f" (alias: {node.alias})" if getattr(node, 'alias', None) else ""
                print(f"\033[1;33mWARNING\033[0m: Import '{node.module_name}'{alias_note} "
                      f"not found at '{mod_path}' — calls to this module will be no-ops.")
        elif isinstance(node, Use):
            continue
    
    combined_ast.extend(ast)
    
def compile_files(source_files, output=None, shared_lib=False):
    try:
        parsed_files = set()
        combined_ast = []
        
        for i, source_file in enumerate(source_files):
            parse_file(source_file, parsed_files, combined_ast, is_main=(i == 0))

        gen = CodeGen(import_aliases={
            node.alias: os.path.splitext(os.path.basename(node.module_name.replace(".", os.sep)))[0]
            for node in combined_ast
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

        try:
            if shared_lib:
                clang_cmd = ["clang", "-shared", "-fPIC", ir_path, c_path,
                             "-o", output_bin, "-O2", "-pthread", "-ldl", "-lm"]
            else:
                clang_cmd = ["clang", ir_path, c_path, "-o", output_bin,
                             "-O2", "-pthread", "-ldl", "-lm"]
            result = subprocess.run(clang_cmd, capture_output=True, text=True)
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
    if len(sys.argv) < 2:
        print("Usage: python compiler.py [-s] <file1.rub> [file2.rub ...] [output]")
        sys.exit(1)

    argv = sys.argv[1:]
    shared_lib = "-s" in argv
    argv = [a for a in argv if a != "-s"]

    if not argv:
        print("Usage: python compiler.py [-s] <file1.rub> [file2.rub ...] [output]")
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
    
    compile_files(expanded_files, output, shared_lib=shared_lib)