# Rubidium Programming Language

**Rubidium** is a high-performance, statically-typed compiled language designed for efficiency and safety. It combines the readable, clean syntax of Python-like languages with the low-level control and memory safety patterns typically found in Rust.

---

## Key Features

* **Static Typing:** Rubidium performs rigorous compile-time type checking to prevent type mismatches before your code ever runs.


* **Manual Memory Management:** Utilize the `.drop()` method to explicitly and safely free memory, providing granular control over your application's resource lifecycle.


* **Hardware Concurrency:** Leverage real POSIX hardware threads for parallel execution, with `thread.wait()` synchronization to manage task completion.


* **Dynamic Collections:** Built-in `list`, `index`, and `dict` types allow you to manage complex, mixed-type data structures while the compiler manages the underlying memory boxing automatically.


* **High-Performance Backend:** Rubidium compiles directly into highly optimized machine code via LLVM, ensuring execution speeds comparable to C or Rust.


* **Modular Architecture:** Supports a clear separation between internal language directives (`use`) and external file imports (`import`).



---

## Quick Syntax Overview

### Variables and Types

```ruby
let mut count i32 = 0
let name str = "Rubidium"
let active bool = True

# Reassignment
count = count + 1

# Explicit memory reclamation
name.drop()

```

### Collections

```ruby
let my_list list = [1, "two", 3.14]
my_list(1).set(99) # Mutation
print(my_list(1))

```

### Functions

```ruby
fn add(a: i32, b: i32) -> i32 {
    return a + b
}

```

### Threading

```ruby
fn worker() {
    print("Working...")
}

fn main() {
    thread(worker(), 1)
    thread.wait(1)
}

```

---

## Getting Started

1. **Install Xeon:** Use the provided install scripts (`install.sh` or `install.bat`) to set up the Xeon build manager.


2. **Initialize:** Run `xeon init` in your project folder to generate the `src/` directory and a `main.rub` entry point.


3. **Build/Run:** Use `xeon build` to compile your project or `xeon run` to compile and execute it immediately.



---

## License & Resources

* **Official Repository:** [https://github.com/TomDexterYoutube/Rubidium](https://github.com/TomDexterYoutube/Rubidium)
* **Language Design:** Inspired by Python readability and Rust memory safety principles.
