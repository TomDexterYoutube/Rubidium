# Rubidium

Rubidium is a compiled, statically-typed programming language (file extension `.rub`) designed to be approachable for beginners while still scaling up to real systems-level work. This repository holds the compiler toolchain (lexer, parser, AST, codegen, debugger) along with the full syntax specification.

## Design Philosophy

Rubidium favors explicit, visible behavior over hidden machinery. Rather than relying on garbage collection, ownership, borrow checking, or reference counting, memory rules are simple and predictable: everything lives in a shared global pool unless you say otherwise, and you free it yourself. The language leans toward clarity and consistency even when that costs some raw performance.

## Execution Model

A Rubidium program runs in two phases. Top-level code executes first, from top to bottom, populating global variables and running any top-level calls immediately (function and class definitions are registered but not run). Once the top level finishes, the compiler automatically invokes `fn main()` as the real entry point.

## Memory Model

Variables declared normally join a shared global pool visible everywhere; `local` variables are scoped instead and clean themselves up automatically. There's no automatic memory management for global data — you release it explicitly with `.drop()`. The main automatic exceptions are loop variables and `open()` file handles, which are cleaned up when their block ends. Assignment always performs a deep copy; Rubidium has no implicit references or shared ownership. When you deliberately want two names to track the same underlying value, you opt in explicitly with `link`.

## Data Types

- **Integers:** `i32` through `i2048` (signed, multiple widths)
- **Floats:** `f32` through `f2048`
- **Text:** `str`, and `str+` for triple-quoted multi-line strings
- **Other scalars:** `bool`, `Null` (a valid, "empty" value for any type), `Any`, and `SY` (a string that can name a variable or function in code)
- **Collections:** `list` (ordered), `index` (key to single scalar), `dict` (key to collection of values), `dict+` (key to arbitrarily nested dict+ structure)

Type checking happens at compile time, and mutability is opt-in via `mut` — variables, collections, function parameters, and class fields are all immutable unless explicitly marked mutable.

## Core Language Features

- **Control flow:** standard `if` / `else if` / `else`, `while` loops, and `for ... in range(start, end)` or `for ... in <collection>` loops, with `break` and `continue`.
- **Functions:** share the global pool, support typed parameters and optional return types, and use the `link` rule when a function needs to mutate a caller's local variable without deep-copying it.
- **Classes:** self-contained templates with no `self` keyword — fields and methods are referenced directly. Instances deep-copy on assignment, and only `mut`-declared fields can be changed from outside.
- **Error handling:** `try` / `error` blocks catch runtime errors (including file errors), and `raise` triggers a catchable error with a custom message.
- **Threading:** real POSIX threads via `use thread`, with global variables shared across threads and locals/parameters kept private per thread.
- **Built-in modules:** `random`, `time`, and `os` (for scripting shell commands) are enabled with `use`.
- **FFI:** call into compiled C libraries, rename foreign symbols to cleaner Rubidium names, move data across the boundary with `.as_ffi_buffer()` / `.from_ffi_buffer()`, and register Rubidium functions as C-callable callbacks with `fn callback`.
- **Modules:** `import` loads another `.rub` file as a shared, namespaced instance; `import local` gives the importing file its own private copy instead.

## Package Management

Rubidium projects are managed with **Xeon**, which can install and use published packages (`xeon <package>`) alongside local imports, and can also compile Rubidium code as a library with no top level or `main()`.

## Repository Contents

| File | Purpose |
|---|---|
| `syntax` | Full language syntax and semantics reference |
| `lexer.py` | Tokenizes Rubidium source |
| `parser.py` | Builds the AST from tokens |
| `rub_ast.py` | AST node definitions |
| `codegen.py` | Code generation |
| `compiler.py` | Compiler entry point / orchestration |
| `debug.py` | Debugging support |

## Status

The syntax spec is actively evolving — expect breaking changes as the language design is refined.
