# AGENTS.md — Local Workspace Rules

## Coding Standards & Invariants

### 1. Code Portability & Path Configurations
* **Rule:** Do not hardcode developer-specific workspace directories (e.g., `/Users/<username>/...`).
* **Implementation:** Always resolve absolute paths using environment variables with local path fallbacks, or parameterize them via configuration.

### 2. FastAPI Lifespan Hooks
* **Rule:** Avoid using deprecated `@app.on_event("startup")` or `@app.on_event("shutdown")` decorators.
* **Implementation:** Always use FastAPI's modern lifespan context manager style (`@asynccontextmanager def lifespan(app: FastAPI)`).

### 3. CrewAI Flow Method Typing in mypy
* **Rule:** When calling class methods decorated with `@listen` or `@router` directly from other methods in the flow class, bypass type checking errors.
* **Implementation:** Cast the method call to `Any` using `typing.cast` (e.g., `cast(Any, self.my_method)(arg)`) because the decorators wrap/transform the method descriptor, which triggers mypy mismatch errors.

### 4. Subprocess Reference Pruning
* **Rule:** When tracking active subprocesses inside a global dict or list registry, ensure that finished process references are popped and cleaned up.
* **Implementation:** Regularly prune the registry on listing/cancel execution paths to prevent resource reference leaks.

### 5. Path Traversal Guardrails on API Routes
* **Rule:** Validate any API route parameter mapping directly to disk paths.
* **Implementation:** Use a strict alphanumeric regex check (e.g., `r"^[a-zA-Z0-9_\-]+$"` for IDs) or check resolves via `Path.resolve()` to prevent arbitrary directory traversal.
