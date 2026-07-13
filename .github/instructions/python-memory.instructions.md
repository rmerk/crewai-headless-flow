---
description: Guidelines and best practices for Python coding and dynamic class instantiation within the project.
applyTo: "**/*.py"
---

# Python Memory

Best practices and patterns for writing clean, robust, and type-safe Python code.

## Robust Parameter Compatibility Verification

When instantiating classes dynamically with keyword arguments (such as worker adapters), do not rely on broad `try/except TypeError` blocks to handle unsupported arguments. A `TypeError` can be raised inside the constructor itself due to internal code bugs, which would then be incorrectly caught and cause silent parameter-dropping.

Instead, use Python's standard `inspect` module to verify signature compatibility:

```python
import inspect

sig = inspect.signature(cls.__init__)
# Verify parameter name or if the constructor accepts dynamic keywords (**kwargs)
if "parameter_name" in sig.parameters or any(
    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
):
    kwargs["parameter_name"] = value
```
