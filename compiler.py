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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { int type; long long i; double f; char* s; void* p; } Box;

Box* _thread_results[1024]; 

void _set_thread_result(int tid, Box* val) {
    if(tid >= 0 && tid < 1024) {
        _thread_results[tid] = val;
    }
}

// Global stdin pointer
void* _stdin_ptr;

__attribute__((constructor)) void init_runtime() {
    _stdin_ptr = (void*)stdin;
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
    RList* l=col; int i=idx->i - 1; /* 1-based indexing */
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
        int i = key->i - 1; /* 1-based indexing */
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

void print_boxed(Box* b) {
    if(!b) { printf("null\n"); fflush(stdout); return; }
    if(b->type==0) printf("%lld\n", b->i);
    else if(b->type==1) printf("%g\n", b->f);
    else if(b->type==2) printf("%s\n", b->s);
    else {
        int* magic = (int*)(b->p);
        if(magic && *magic==1) printf("<list>\n");
        else if(magic && *magic==2) printf("<dict>\n");
        else printf("<object>\n");
    }
    fflush(stdout);
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
                ["clang", ir_path, c_path, "-o", output_bin, "-O2", "-pthread"],
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