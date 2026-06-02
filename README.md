# Rubidium Programming Language

**Simple enough for beginners. Powerful enough for systems programming.**

Rubidium is a statically typed, compiled programming language designed around simplicity, predictability, and direct control of memory. It removes complex ownership systems and hidden behavior while still providing features required for real-world software development.

Programs are written in `.rub` files and compile into native executables.

---

## Features

* Beginner-friendly syntax
* Compile-time type checking
* Explicit memory management
* Deep-copy assignment semantics
* Global shared memory model
* Built-in threading support
* File system access
* Foreign Function Interface (FFI)
* Cross-platform OS interaction
* High-precision integer and floating-point types
* Automatic program entry through `main()`

---

# Hello World

```rub
print("This runs before main()")

fn main() {
    print("Hello, Rubidium!")
}
```

Output:

```text
This runs before main()
Hello, Rubidium!
```

---

# Execution Model

Rubidium executes in two phases:

### Phase 1 — Top Level

All code outside functions runs immediately from top to bottom.

* Global variables are created
* Top-level function calls execute
* Functions and classes are defined

### Phase 2 — Main

After top-level execution completes, Rubidium automatically runs:

```rub
fn main() {

}
```

---

# Memory Philosophy

Rubidium uses a shared global memory pool.

```rub
let score = 100
```

Once a variable exists, it can be accessed anywhere unless created inside an isolated scope.

### Manual Memory Management

Memory is never freed automatically.

```rub
let data = [1,2,3]

data.drop()
```

Exceptions:

* Loop variables
* File handles created by `open()`

These are automatically released at the end of their block.

---

# Variables

Immutable:

```rub
let name = "Rubidium"
```

Mutable:

```rub
let mut counter = 0

counter = 10
```

Attempting to modify an immutable variable causes a compile-time error.

---

# Data Types

## Integers

```text
i32
i64
i128
i256
i512
i1024
i2048
```

## Floating Point

```text
f32
f64
f128
f256
f512
f1024
f2048
```

## Other Types

```text
str
bool
Null
```

Example:

```rub
let age: i32 = 25
let pi: f64 = 3.14
let active: bool = True
let username: str = "Alice"
```

---

# Null Values

Every type accepts `Null`.

```rub
let age: i32 = Null
let name: str = Null
```

This means the variable exists but currently contains no value.

---

# Type Casting

```rub
let a: i32 = 5

let b: i64 = a as i64
let c: f64 = a as f64
```

---

# Deep Copy Semantics

Every assignment creates a completely independent copy.

```rub
let a = [1,2,3]
let b = a

b(0).set(999)

print(a)
print(b)
```

Output:

```text
[1,2,3]
[999,2,3]
```

There is no borrowing, referencing, or ownership system.

---

# Strings

```rub
let first = "Hello"
let second = "World"

let combined = first + second
```

Interpolation:

```rub
let name = "Rubidium"

print(i"Hello {name}")
```

Useful methods:

```rub
text.len()
text.has("abc")
```

Conversions:

```rub
let num = "123".to(i32)
let text = 42.to(str)
```

---

# Collections

Rubidium provides three built-in collection types.

## List

Ordered collection.

```rub
let mut items: list = [1,2,3]

items().add(4)

items(0).set(100)
```

---

## Index

Single-value key/value map.

```rub
let mut users: index = [
    "name": "Alice",
    1: "Admin"
]

users().add("email", "alice@example.com")
```

---

## Dict

Multi-value key collection.

```rub
let mut scores: dict = {
    "math" = [90,95,100]
}

scores("math").add(85)
```

---

# Conditionals

```rub
if score > 90 {

    print("Excellent")

} else if score > 50 {

    print("Pass")

} else {

    print("Fail")

}
```

---

# Loops

## While Loop

```rub
while count < 10 {

    count = count + 1

}
```

## Range Loop

```rub
for i in range(0, 10) {

    print(i)

}
```

## Collection Loop

```rub
for item in items {

    print(item)

}
```

---

# Functions

Basic function:

```rub
fn greet() {

    print("Hello")

}
```

Function with return value:

```rub
fn add(a: i32, b: i32) -> i32 {

    return a + b

}
```

Usage:

```rub
let result = add(5, 3)
```

---

# Error Handling

```rub
try {

    let result = 10 / 0

} error {

    print(error)

}
```

The `error` variable contains the runtime error message.

---

# Classes

```rub
class player() {

    let mut health: i32 = 100

    fn damage(amount: i32) {

        health = health - amount

    }
}
```

Create an instance:

```rub
let mut p = player()

p.health = 50

p.damage(10)
```

---

# File Handling

```rub
open("data.txt") as file {

    file.write("Hello")

    let content = file.read()

}
```

Iterating over a file:

```rub
open("data.txt") as file {

    for line in file {

        print(line)

    }

}
```

---

# Threading

```rub
use thread

thread(task_one(), 1)

thread.wait(1)
```

Check if a thread is still running:

```rub
thread.running(1)
```

---

# Random

```rub
use random

let value = random(0, 100, i32)

random.shuffle(my_list)

random.choice(my_list)
```

---

# Time

```rub
use time

time.wait(5)
```

Named timers:

```rub
time.timer_start(1)

let elapsed = time.timer_read(1)
```

---

# Operating System Access

```rub
use os

os.start(1)

let output = os.run(1, "echo hello")

os(1).drop()
```

---

# Foreign Function Interface (FFI)

Load a shared library:

```rub
use FFI

let lib = FFI("libs/mylib.so")
```

Bind a native function:

```rub
fn lib add_numbers(a: i32, b: i32) -> i32
```

Call it normally:

```rub
let result = add_numbers(5, 10)
```

Compatible with:

* C
* C++
* Rust
* C#

---

# Modules

Built-in modules:

```rub
use thread
use random
use time
use os
use FFI
```

External modules:

```rub
import math_tools
```

Imports load `.rub` files from the current project.

---

# Project Structure

```text
project/
│
├── main.rub
├── math_tools.rub
│
└── libs/
    └── mylib.so
```

---

# Design Goals

Rubidium was created with several core principles:

1. Easy to learn
2. Predictable behavior
3. Explicit memory control
4. Strong compile-time safety
5. No hidden ownership systems
6. Simple syntax that scales to large applications
7. Native performance

---

# Quick Syntax Reference

```rub
# Comment

let value = 10

let mut count = 0

if value > 5 {

}

while True {

}

for i in range(0,10) {

}

fn add(a: i32, b: i32) -> i32 {

    return a + b

}

class player() {

}

print("Hello")

let name = input("Name: ")

value.drop()
```

---

# License

Choose a license that matches your project's goals:

* MIT
* Apache 2.0
* GPLv3
* Custom License

---

**Rubidium — a language that starts simple and stays powerful.**
