import sys
import os
import subprocess
import tempfile
import glob

from lexer import tokenize
from parser import Parser
from ast import Import, Use, VarDecl, FnDef, ClassDef
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
            Box** items = *(Box***)((char*)b->p + sizeof(int));
            int count = *(int*)((char*)b->p + sizeof(int) + sizeof(Box**));
            for(int i=0; i<count; i++) box_drop(items[i]);
            free(items); free(b->p);
        } else if (magic && *magic == 2) {
            Box** keys = *(Box***)((char*)b->p + sizeof(int));
            Box** vals = *(Box***)((char*)b->p + sizeof(int) + sizeof(Box**));
            int count = *(int*)((char*)b->p + sizeof(int) + 2*sizeof(Box**));
            for(int i=0; i<count; i++) { box_drop(keys[i]); box_drop(vals[i]); }
            free(keys); free(vals); free(b->p);
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
Box* box_copy(Box* src) {
    if(!src) return box_i(0);
    if(src->type==0) return box_i(src->i);
    if(src->type==1) return box_f(src->f);
    if(src->type==2) return box_s(src->s);
    return src; /* collections: return same pointer, caller must not drop */
}

long long unbox_i(Box* b) { return b ? b->i : 0; }
double unbox_f(Box* b) { return b ? b->f : 0.0; }
char* unbox_s(Box* b) { return (b && b->type==2) ? b->s : ""; }
void* unbox_p(Box* b) { return (b && b->type==3) ? b->p : NULL; }

typedef struct { int magic; Box** items; int count; int cap; } RList;
Box* make_list() { RList* l=malloc(sizeof(RList)); l->magic=1; l->count=0; l->cap=8; l->items=malloc(8*sizeof(Box*)); return box_p(l); }
void list_append(Box* lst, Box* b) { RList* l=lst->p; if(l->count==l->cap){l->cap*=2; l->items=realloc(l->items,l->cap*sizeof(Box*));} l->items[l->count++]=b; }
void list_swap(Box* lst, int i, int j) {
    RList* l=lst->p;
    if(i>=0 && i<l->count && j>=0 && j<l->count) {
        Box* tmp=l->items[i]; l->items[i]=l->items[j]; l->items[j]=tmp;
    }
}
Box* list_get(void* col, Box* idx) {
    RList* l=col; int i=idx->i; /* 0-based indexing */
    if(i>=0 && i<l->count) return l->items[i];
    return box_i(0);
}

typedef struct { int magic; Box** keys; Box** vals; int count; int cap; } RDict;
Box* make_dict() { RDict* d=malloc(sizeof(RDict)); d->magic=2; d->count=0; d->cap=8; d->keys=malloc(8*sizeof(Box*)); d->vals=malloc(8*sizeof(Box*)); return box_p(d); }
int box_eq(Box* a, Box* b) {
    if(!a || !b || a->type!=b->type) return 0;
    if(a->type==0) return a->i==b->i;
    if(a->type==1) return a->f==b->f;
    if(a->type==2) return strcmp(a->s,b->s)==0;
    return a->p==b->p;
}
void dict_set(Box* dct, Box* k, Box* v) {
    RDict* d=dct->p;
    for(int i=0;i<d->count;i++) if(box_eq(d->keys[i],k)) { 
        box_drop(d->vals[i]);
        d->vals[i]=v; 
        return; 
    }
    if(d->count==d->cap){d->cap*=2; d->keys=realloc(d->keys,d->cap*sizeof(Box*)); d->vals=realloc(d->vals,d->cap*sizeof(Box*));}
    d->keys[d->count]=k; d->vals[d->count]=v; d->count++;
}
Box* dict_get(void* col, Box* k) {
    RDict* d=col;
    for(int i=0;i<d->count;i++) if(box_eq(d->keys[i],k)) return d->vals[i];
    return box_i(0);
}

Box* collection_get(Box* col_box, Box* key) {
    if (!col_box || col_box->type != 3) return box_i(0);
    void* col = col_box->p;
    int* magic = (int*)col;
    if (*magic == 1) return list_get(col, key);
    if (*magic == 2) return dict_get(col, key);
    return box_i(0);
}

void collection_set(Box* col_box, Box* key, Box* val) {
    if (!col_box || col_box->type != 3) return;
    void* col = col_box->p;
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

int collection_len(Box* col_box) {
    if (!col_box || col_box->type != 3) return 0;
    void* col = col_box->p;
    int* magic = (int*)col;
    if (*magic == 1) return ((RList*)col)->count;
    if (*magic == 2) return ((RDict*)col)->count;
    return 0;
}

Box* collection_get_at(Box* col_box, int idx) {
    if (!col_box || col_box->type != 3) return box_i(0);
    void* col = col_box->p;
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
        _timer_starts[tid] = clock() / (double)CLOCKS_PER_SEC;
        _timer_running[tid] = 1;
    }
}

void time_timer_pause(int tid) {
    if(tid >= 0 && tid < 1024 && _timer_running[tid]) {
        double now = clock() / (double)CLOCKS_PER_SEC;
        _timer_accum[tid] += now - _timer_starts[tid];
        _timer_running[tid] = 0;
    }
}

void time_timer_stop(int tid) {
    if(tid >= 0 && tid < 1024) {
        if(_timer_running[tid]) {
            double now = clock() / (double)CLOCKS_PER_SEC;
            _timer_accum[tid] += now - _timer_starts[tid];
        }
        _timer_running[tid] = 0;
    }
}

double time_timer_read(int tid) {
    double result = 0.0;
    if(tid >= 0 && tid < 1024) {
        if(_timer_running[tid]) {
            double now = clock() / (double)CLOCKS_PER_SEC;
            result = _timer_accum[tid] + (now - _timer_starts[tid]);
        } else {
            result = _timer_accum[tid];
        }
    }
    return result;
}

void print_boxed(Box* b) {
    if(!b) { printf("null\n"); fflush(stdout); return; }
    if(b->type==0) printf("%lld\n", b->i);
    else if(b->type==1) printf("%g\n", b->f);
    else if(b->type==2) printf("%s\n", b->s);
    else {
        int* magic = (int*)(b->p);
        if(magic && *magic==1) {
            RList* l = (RList*)b->p;
            printf("[");
            for(int i=0; i<l->count; i++) {
                if(i>0) printf(", ");
                Box* item = l->items[i];
                if(!item) printf("null");
                else if(item->type==0) printf("%lld", item->i);
                else if(item->type==1) printf("%g", item->f);
                else if(item->type==2) printf("%s", item->s);
                else printf("...");
            }
            printf("]\n");
        } else if(magic && *magic==2) {
            RDict* d = (RDict*)b->p;
            printf("{");
            for(int i=0; i<d->count; i++) {
                if(i>0) printf(", ");
                Box* k = d->keys[i];
                Box* v = d->vals[i];
                if(!k) printf("null");
                else if(k->type==0) printf("%lld", k->i);
                else if(k->type==1) printf("%g", k->f);
                else if(k->type==2) printf("\"%s\"", k->s);
                printf(": ");
                if(!v) printf("null");
                else if(v->type==0) printf("%lld", v->i);
                else if(v->type==1) printf("%g", v->f);
                else if(v->type==2) printf("%s", v->s);
                else printf("...");
            }
            printf("}\n");
        } else printf("<object>\n");
    }
    fflush(stdout);
}

// Convert a Box* to a heap-allocated C string for string concatenation.
// Caller must free() the result.
char* box_to_cstr(Box* b) {
    if(!b) { char* r = malloc(5); strcpy(r,"null"); return r; }
    char* buf = malloc(64);
    if(b->type==0)      { snprintf(buf, 64, "%lld", b->i); }
    else if(b->type==1) { snprintf(buf, 64, "%g",   b->f); }
    else if(b->type==2) { free(buf); return strdup(b->s ? b->s : ""); }
    else                { free(buf); buf = malloc(7); strcpy(buf,"<obj>"); }
    return buf;
}

// -------------------------------------------------------
// OS MODULE — hidden shell sessions per ID
// -------------------------------------------------------
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <errno.h>

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

    // Write command + newline
    write(t->stdin_fd, cmd, strlen(cmd));
    write(t->stdin_fd, "\n", 1);
    // If there's interactive input to send, write it after a short delay
    if(input && strlen(input)>0) {
        usleep(200000);
        write(t->stdin_fd, input, strlen(input));
        if(input[strlen(input)-1]!='\n') write(t->stdin_fd, "\n", 1);
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

// Open a file handle slot — creates file if it doesn't exist, opens r+
long long file_open(long long slot, const char* path) {
    if(slot < 0 || slot >= 1024) return -1;
    if(_file_handles[slot]) { fclose(_file_handles[slot]); _file_handles[slot] = NULL; }
    if(_file_paths[slot]) { free(_file_paths[slot]); }
    _file_paths[slot] = strdup(path);
    // Ensure file exists
    FILE* touch = fopen(path, "a"); if(touch) fclose(touch);
    _file_handles[slot] = fopen(path, "r+");
    return slot;
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
    if(_file_handles[slot]) { fclose(_file_handles[slot]); }
    _file_handles[slot] = fopen(_file_paths[slot], "w");
    if(_file_handles[slot]) {
        fputs(data, _file_handles[slot]);
        fclose(_file_handles[slot]);
        _file_handles[slot] = fopen(_file_paths[slot], "r+");
    }
}

// file.append(data) — add to end of file
void file_append_all(long long slot, const char* data) {
    if(slot < 0 || slot >= 1024 || !_file_handles[slot]) return;
    fseek(_file_handles[slot], 0, SEEK_END);
    fputs(data, _file_handles[slot]);
    fflush(_file_handles[slot]);
}

// file.read() — read entire file as string
char* file_read_all(long long slot) {
    if(slot < 0 || slot >= 1024 || !_file_handles[slot]) return strdup("");
    FILE* f = _file_handles[slot];
    fseek(f, 0, SEEK_SET);
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    char* buf = malloc(sz + 1);
    size_t rd = fread(buf, 1, sz, f);
    buf[rd] = '\0';
    return buf;
}

// file.readln(n) — read specific line (1-based)
char* file_readln(long long slot, long long line_num) {
    if(slot < 0 || slot >= 1024 || !_file_handles[slot]) return strdup("");
    FILE* f = _file_handles[slot];
    rewind(f);
    char buf[4096]; long long cur = 1;
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

// file.writeln(line_num, data) — replace or write specific line
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
    // Extend if needed
    while(count < line_num) {
        if(count == cap) { cap *= 2; lines = realloc(lines, cap * sizeof(char*)); }
        lines[count++] = strdup("\n");
    }
    free(lines[line_num - 1]);
    size_t dlen = strlen(data);
    char* newline = malloc(dlen + 2);
    memcpy(newline, data, dlen); newline[dlen] = '\n'; newline[dlen+1] = '\0';
    lines[line_num - 1] = newline;
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
"""

def parse_file(filepath, parsed_files, combined_ast, is_main=False):
    abs_path = os.path.abspath(filepath)
    if abs_path in parsed_files:
        return
    parsed_files.add(abs_path)
    
    # Generate module name from file (e.g., 'math_tools' from 'math_tools.rub')
    mod_name = os.path.splitext(os.path.basename(filepath))[0]
    
    try:
        with open(filepath, "r") as f:
            code = f.read()
    except FileNotFoundError:
        print(f"✖ Error: Could not find imported file '{filepath}'")
        sys.exit(1)
        
    tokens = tokenize(code)
    ast = Parser(tokens).parse()
    
    for node in ast:
        # Prepend module name to avoid naming collisions (only for imported files, not main)
        if not is_main and isinstance(node, (VarDecl, FnDef, ClassDef)):
            node.name = f"{mod_name}_{node.name}"
            
        if isinstance(node, Import):
            mod_file = node.module_name.replace(".", os.sep) + ".rub"
            base_dir = os.path.dirname(filepath)
            mod_path = os.path.join(base_dir, mod_file) if base_dir else mod_file
            parse_file(mod_path, parsed_files, combined_ast)
        elif isinstance(node, Use):
            continue
            
    combined_ast.extend(ast)

def compile_files(source_files, output=None):
    try:
        parsed_files = set()
        combined_ast = []
        
        for i, source_file in enumerate(source_files):
            parse_file(source_file, parsed_files, combined_ast, is_main=(i == 0))

        gen = CodeGen()
        ir_code = gen.gen(combined_ast)

        if output: output_bin = output
        else: output_bin = os.path.splitext(os.path.basename(source_files[0]))[0]

        with tempfile.NamedTemporaryFile(suffix=".ll", delete=False, mode="w") as f_ll:
            f_ll.write(ir_code)
            ir_path = f_ll.name
            
        with tempfile.NamedTemporaryFile(suffix=".c", delete=False, mode="w") as f_c:
            f_c.write(RUNTIME_C)
            c_path = f_c.name

        try:
            result = subprocess.run(
                ["clang", ir_path, c_path, "-o", output_bin, "-O2", "-pthread", "-ldl"],
                capture_output=True, text=True
            )
        finally:
            os.unlink(ir_path)
            os.unlink(c_path)

        if result.returncode != 0:
            print("✖ Compilation failed:")
            print(result.stderr)
            sys.exit(1)

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
        print("Usage: python compiler.py <file1.rub> [file2.rub ...] [output]")
        sys.exit(1)
    
    if not sys.argv[-1].endswith('.rub'):
        output = sys.argv[-1]
        source_files = sys.argv[1:-1]
    else:
        output = None
        source_files = sys.argv[1:]
    
    expanded_files = []
    for pattern in source_files:
        if '*' in pattern or '?' in pattern or '[' in pattern:
            expanded_files.extend(glob.glob(pattern))
        else:
            expanded_files.append(pattern)
    
    compile_files(expanded_files, output)