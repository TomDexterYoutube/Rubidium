import sys
import os
import subprocess
import tempfile

from lexer import tokenize
from parser import Parser
from codegen import CodeGen

def compile_file(source_file, output=None):
    code = open(source_file).read()

    tokens = tokenize(code)
    ast    = Parser(tokens).parse()

    gen     = CodeGen()
    ir_code = gen.gen(ast)

    base        = os.path.splitext(os.path.basename(source_file))[0]
    output_bin  = output or base

    # Write IR to a temp file
    with tempfile.NamedTemporaryFile(suffix=".ll", delete=False, mode="w") as f:
        f.write(ir_code)
        ir_path = f.name

    try:
        result = subprocess.run(
            ["clang", ir_path, "-o", output_bin, "-O2"],
            capture_output=True, text=True
        )
    finally:
        os.unlink(ir_path)

    if result.returncode != 0:
        print("✖ Compilation failed:")
        print(result.stderr)
        sys.exit(1)

    print(f"✔ Compiled → ./{output_bin}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python compiler.py <file.rub> [output]")
        sys.exit(1)
    output = sys.argv[2] if len(sys.argv) > 2 else None
    compile_file(sys.argv[1], output)
