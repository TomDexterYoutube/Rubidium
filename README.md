# Rubidium

> A beginner-friendly programming language built for real systems programming.
> Python-style readability. Rust-inspired safety. Direct native binaries.
> Because apparently humans looked at C++, cried a little, and decided to invent another language. This one is actually pretty interesting though.

---

## What is Rubidium?

Rubidium is a compiled programming language designed to feel simple for beginners while still scaling into low-level and high-performance software development.

It combines:

* Clean, readable syntax
* Static typing
* Compile-time type checking
* Manual memory control
* POSIX threading
* Native binary compilation
* Structured collections
* Beginner-friendly design choices

Rubidium aims to remove a lot of the unnecessary pain found in traditional systems languages without turning everything into a slow abstraction soup.

---

# Features

* **Compiled directly to native binaries**
* **Static type system**
* **Compile-time error checking**
* **Mutable and immutable variables**
* **Manual memory dropping with `.drop()`**
* **POSIX hardware threading**
* **Dynamic collections**
* **Python-like readability**
* **Rust-inspired safety concepts**
* **No semicolons required**
* **Built-in random, threading, and time modules**
* **Simple class system**
* **Built-in file I/O**
* **Structured error handling**

---

# Hello World

```rub
fn main() {

    print("Hello, Rubidium!")
}
```

---

# Variables

Rubidium supports optional static typing with type inference.

```rub
let x = 10
let mut y: i32 = 5

let name: str = "Rubidium"
let pi: f64 = 3.14159
let active: bool = True
```

Mutable variables use `mut`.

```rub
y = 15
```

---

# Data Types

## Integer Types

```text
i8
i16
i32
i64
i128
i256
```

## Floating Point Types

```text
f4
f8
f16
f32
f64
f128
f256
```

## Other Types

```text
str
bool
list
index
dict
```

---

# Type Safety

Rubidium performs compile-time type checking.

```rub
let x: i32 = 10
x = "hello"   # Compiler error
```

Because discovering type errors before runtime is generally preferable to discovering them at 3AM while staring at logs and reconsidering your career path.

---

# Math & Logic

```rub
let result = (1 + 2) * 3

let valid = result > 5

let state = True and not False
```

Supported operators:

```text
+  -  *  /
== != > < >= <=
and or not
```

---

# Strings

```rub
let greeting = "Hello, "
let target = "World!"

let full = greeting.combine(target)

print(full)
```

## String Methods

```rub
full.len()
full.has("World")
```

## String Conversion

```rub
let number = "404"

let value: i32 = number.to(i32)
```

## String Interpolation

```rub
print(i"Hello {name}")
```

Tiny feature. Massive quality-of-life improvement. Humanity spent decades manually concatenating strings like cave people.

---

# Collections

## Lists

Lists use **1-based indexing**.

```rub
let items: list = [10, 20, 30]

print(items(1))
```

## Indexes

Mixed key/value structures.

```rub
let values: index = [
    "name": "Rubidium",
    1: "hello"
]
```

## Dicts

Structured nested collections.

```rub
let table: dict = {
    "numbers" = [1, 2, 3]
}
```

## Mutation

```rub
items(1).set(99)
```

---

# Input & Output

## Printing

```rub
print("Hello")
```

## Updating Console Lines

```rub
println("Loading...")
println("Done")
```

`println()` replaces the current console line instead of creating a new one.

Useful for:

* Loading bars
* Progress displays
* Status updates

---

# File I/O

## Write Files

```rub
file_write("save.txt", "Hello")
```

## Read Files

```rub
let contents = file_read("save.txt")
```

Because every language eventually becomes "move text around and panic about paths."

---

# Memory Management

Rubidium supports explicit memory dropping.

```rub
my_list.drop()
```

Dropping recursively frees nested allocations immediately.

This gives developers more direct memory control without requiring full manual allocation management everywhere.

---

# Conditionals

```rub
if x > 5 {

    print("Large")

} else {

    print("Small")
}
```

---

# Loops

## While Loops

```rub
while count < 10 {

    count = count + 1
}
```

## Range Loops

```rub
for i in range(1, 10) {

    print(i)
}
```

## Collection Iteration

```rub
for item in items {

    print(item)
}
```

---

# Functions

```rub
fn add(a: i32, b: i32) -> i32 {

    return a + b
}
```

## Calling Functions

```rub
let result = add(5, 10)
```

---

# Error Handling

```rub
try {

    let x = 10 / 0

} error {

    print("Error: " + error)
}
```

Errors inside the `error` block are exposed as strings.

Simple. Predictable. No seventeen-layer exception hierarchy named after Greek tragedies.

---

# Classes

```rub
class player() {

    let mut health: i32 = 100

    fn damage() {

        health = health - 10
    }
}
```

## Creating Instances

```rub
let mut p = player()

p.health.set(50)
```

### Class Rules

* No `self`
* Class scope directly accesses variables
* Mutable class instances can only mutate `mut` fields

---

# Threading

Rubidium supports real POSIX threading.

```rub
use thread

fn task() {

    print("Running")
}

thread(task(), 1)

thread.wait(1)
```

---

# Random Module

```rub
use random

let value = random(0, 100, i32)
```

## Utilities

```rub
random.shuffle(my_list)

random.choice(my_list)
```

---

# Time Module

```rub
use time

time.sleep(5)
```

---

# Imports

## Built-in Modules

```rub
use thread
use random
use time
```

## External Files

```rub
import math_tools
```

---

# Program Entry Point

Rubidium programs start at `main()`.

```rub
fn main() {

    print("Program started")
}
```

---

# Syntax Philosophy

Rubidium tries to follow a few core ideas:

* Readable without being weak
* Explicit without being noisy
* Powerful without looking like encrypted tax forms
* Beginner-friendly without hiding how computers work

It is designed to grow with the programmer instead of forcing them to abandon the language once projects become serious.

---

# Example Program

```rub
use random
use thread

fn worker() {

    let value = random(1, 100, i32)

    print(i"Generated: {value}")
}

fn main() {

    thread(worker(), 1)

    thread.wait(1)

    print("Done")
}
```

---

# Current Goals

* Native compiler backend
* Better module system
* Improved memory ownership rules
* Standard library expansion
* Better tooling and diagnostics
* Cross-platform compilation
* Package manager

---

# Why Rubidium?

Because modern programming somehow became a war between:

* unreadable low-level languages,
* painfully slow abstraction-heavy languages,
* and JavaScript pretending to be an operating system.

Rubidium tries to sit in the middle:

* fast,
* readable,
* safe,
* and actually enjoyable to write.

A dangerous idea in software engineering, apparently.

---

# License

MIT License

Use it, modify it, break it, rebuild it into something cursed. Humanity seems committed to doing that with every technology anyway.
