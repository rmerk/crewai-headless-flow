"""
Helper to create a minimal sample target repo for live M5 demos.

Usage:
    python examples/create_sample_target.py /tmp/my-demo-repo
"""

import shutil
import sys
from pathlib import Path


def create_sample_target(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)

    dest.mkdir(parents=True)

    # A tiny Python module with a deliberate small issue
    (dest / "src").mkdir()
    (dest / "tests").mkdir()

    (dest / "src" / "math_utils.py").write_text("""
def add(a: int, b: int) -> int:
    return a + b

def multiply(a: int, b: int) -> int:
    return a * b   # TODO: should this handle floats?
""")

    (dest / "tests" / "test_math_utils.py").write_text("""
from src.math_utils import add, multiply

def test_add():
    assert add(2, 3) == 5

def test_multiply():
    assert multiply(4, 5) == 20
""")

    (dest / "README.md").write_text(
        "# Sample Target Repo\n\nUsed for crewai-headless-flow demos.\n"
    )

    # Initialize git so the workers see a real repo
    import subprocess

    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=dest, check=True, capture_output=True
    )

    print(f"Created sample target repo at: {dest}")
    print("You can now point the Flow at it with a request like:")
    print('  "Add a subtract function and corresponding test following TDD"')


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/crewai-demo-target")
    create_sample_target(target)
