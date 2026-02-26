# Specification: Semantic Skeletons (Replacing Blind Snippets)

## 1. Problem Statement: The Flaw in "Blind Snippets"
Currently, pressing `s` in `kopipasta` activates "Snippet Mode", which indiscriminately grabs the first 50 lines (or 4KB) of a file. 

**The Mechanical Failure:** 
A 50-line cutoff is semantically blind. It frequently truncates files in the middle of a `for` loop, cuts off the return types of functions, or captures 40 lines of import statements while missing the actual class definition. 

**The Semantic Impact:** 
The LLM receives fragmented, broken syntax that confuses its context window. It spends attention trying to guess the missing braces rather than understanding the architecture.

## 2. The Solution: "Semantic Skeletons" (AST-Driven Compression)
We will repurpose the `s` hotkey to generate a **Semantic Skeleton**. 
Instead of truncating text, `kopipasta` will parse the file into an Abstract Syntax Tree (AST), strip out the low-value implementation details (the "noise"), and unparse it back into valid syntax (the "signal").

This creates a 4-tier semantic "Zoom Level" in the `kopipasta` UI:
1. **Unselected (Hidden)**
2. **Map (`m`)**: Project-wide scale. (Names only: `class User(login)`).
3. **Skeleton (`s`)**: File-wide scale. (Contracts, Docstrings, and Topologies).
4. **Focus (`Space`)**: Implementation scale. (Full source code).

---

## 3. Language-Specific Skeleton Rules

### 3.1 Python (via standard `ast` module)
* **Keep:** Imports, top-level variables, class definitions, method/function signatures (with type hints), and docstrings.
* **Strip:** The internal logic/body of all functions and methods.
* **Replace With:** `...` (Python's native Ellipsis) or `pass`.

**Before (Raw - 400 tokens):**
```python
def process_payment(user_id: int, amount: float) -> bool:
    """Charges the user and updates the ledger."""
    user = db.get_user(user_id)
    if not user.is_active:
        raise ValueError("Inactive user")
    # ... 40 lines of Stripe API calls ...
    db.update_ledger(user_id, amount)
    return True
```
**After (Skeleton - ~30 tokens):**
```python
def process_payment(user_id: int, amount: float) -> bool:
    """Charges the user and updates the ledger."""
    ...
```

### 3.2 JSON (via Schema Inference)
Large JSON arrays of identical objects destroy token limits.
* **Keep:** The shape of the data.
* **Strip:** The duplicate items.
* **Replace With:** A generated TypeScript-style interface or a truncated representation with a meta-comment.

**Before (Raw - 10,000 lines):**
```json
[
  { "id": 1, "name": "Alice", "role": "admin", "metadata": {"last_login": "2024-01-01"} },
  { "id": 2, "name": "Bob", "role": "user", "metadata": {"last_login": "2024-01-02"} },
  // ... 998 more objects ...
]
```
**After (Skeleton):**
```typescript
/* INFERRED SCHEMA (Array of 1000 items) */
type JsonData = Array<{
  id: number;
  name: string;
  role: string;
  metadata: {
    last_login: string;
  }
}>;
```

### 3.3 React / JSX / TSX (via `tree-sitter`)
React components are bloated by visual styling. 
* **Keep:** Component signatures, Props interfaces, custom child components (`<StatusBadge />`), and dynamic data bindings (`{user.name}`).
* **Strip:** Tailwind `className` attributes, inline `<svg>` blocks, and standard HTML attributes (`style`, `aria-*`).

**Before (Raw - High Token Cost):**
```tsx
export function UserCard({ user }: { user: User }) {
  return (
    <div className="flex items-center p-4 bg-white shadow-md rounded-lg hover:bg-gray-50">
      <svg viewBox="0 0 24 24" className="w-6 h-6 text-gray-400">...</svg>
      <h2 className="text-xl font-bold text-gray-800">{user.name}</h2>
      <StatusBadge status={user.status} size="small" />
    </div>
  );
}
```
**After (Skeleton - Pure Component Topology):**
```tsx
export function UserCard({ user }: { user: User }) {
  return (
    <div>
      <h2>{user.name}</h2>
      <StatusBadge status={user.status} size="small" />
    </div>
  );
}
```

---

## 4. Implementation Plan (`kopipasta`)

### 4.1 UI & State Updates
* **Keybinding:** The `s` key remains the toggle.
* **Labeling:** In the TUI, replace the `(snippet)` label with `(skeleton)`.
* **SelectionManager:** The internal tuple `(state, is_snippet, chunks)` conceptually treats `is_snippet` as `is_skeleton`. 

### 4.2 Module Creation: `skeletonizer.py`
Create a new module dedicated to semantic compression.

```python
def generate_skeleton(file_path: str) -> str:
    """Returns a semantically compressed version of the file."""
    ext = get_extension(file_path)
    
    try:
        if ext == ".py":
            return _skeletonize_python(file_path)
        elif ext == ".json":
            return _skeletonize_json(file_path)
        elif ext in [".js", ".ts", ".jsx", ".tsx"]:
            return _skeletonize_treesitter(file_path)
        else:
            # Fallback for unsupported languages
            return _fallback_snippet(file_path)
    except Exception as e:
        # Graceful degradation on syntax errors
        return _fallback_snippet(file_path)
```

### 4.3 Phased Rollout
* **Phase 1 (Zero Dependency):** Implement Python AST unparsing (using the built-in `ast` module) and JSON schema inference. Update the UI to reflect the new "Skeleton" terminology.
* **Phase 2 (Tree-sitter Integration):** Introduce the `tree-sitter` dependency to support JS/TS/JSX stripping. This unlocks the massive token savings for frontend codebases.

---

## 5. Architectural Impact
1. **Context Window Multiplication:** By shrinking a 1000-line file into a 50-line skeleton, you can load the architecture of 20 distinct files into the prompt for the token cost of 1.
2. **Elimination of Noise:** The LLM is forced to focus on the API boundaries and data contracts, preventing it from hallucinating or obsessing over irrelevant implementation details.
3. **Graceful Degradation:** If a file contains a syntax error preventing AST parsing, `kopipasta` seamlessly falls back to the legacy 50-line string truncation, ensuring the tool never breaks your workflow.