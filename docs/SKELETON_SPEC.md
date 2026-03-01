# Specification: Semantic Maps (AST-Driven Project Context)

## 1. Problem Statement: The Need for High-Density Context
Previously, the Map (`m`) state in `kopipasta` provided project-wide visibility but was semantically shallow. It extracted only the names of classes and functions (e.g., `class User(login)`). This forced the LLM to guess data contracts, arguments, and return types, often requiring a full file load (`Space`) just to see a function signature.

While we have Snippet (`s`) mode to indiscriminately grab the top 50 lines of a file, Snippets are semantically blind. A 50-line cutoff frequently truncates files in the middle of a `for` loop, cuts off the return types of functions, or captures 40 lines of import statements while missing the actual class definition. We need a way to provide deep semantic context without the token cost of a full file or the randomness of a raw snippet.

## 2. The Solution: "Semantic Maps"
Instead of replacing the Snippet functionality, we enhance the Map (`m`) functionality to generate a **Semantic Skeleton**. 
When a file is mapped, `kopipasta` parses it into an Abstract Syntax Tree (AST), extracts the critical contracts (signatures, types, docstrings), and unparses them into a highly compressed, readable representation.

This creates a robust 4-tier semantic "Zoom Level" in the `kopipasta` UI:
1. **Unselected (Hidden)**
2. **Map (`m`)**: Project-wide scale. (Semantic Skeletons: Full signatures and docstrings).
3. **Snippet (`s`)**: Top-of-file scale. (Raw 50-line fallback, unchanged).
4. **Focus (`Space`)**: Implementation scale. (Full source code).

---

## 3. Language-Specific Semantic Rules

### 3.1 Python (via standard `ast` module) - **IMPLEMENTED**
* **Keep:** Class definitions (with base classes), method/function signatures (with type hints and default args), and the first line of docstrings.
* **Strip:** The internal logic/body of all functions and methods.
* **Format:** `def name(args) -> type  # Docstring` or `class Name(Base) [methods]  # Docstring`.

**Before (Legacy Map - Low Context):**
```python
class PaymentProcessor(process_payment)
```
**After (Semantic Map - High Context):**
```python
class PaymentProcessor(BaseService) [process_payment]  # Handles Stripe transactions.
def process_payment(self, user_id: int, amount: float) -> bool  # Charges the user and updates the ledger.
```

### 3.2 JSON (via Schema Inference) - *FUTURE*
Large JSON arrays of identical objects destroy token limits.
* **Keep:** The shape of the data.
* **Strip:** The duplicate items.
* **Replace With:** A generated TypeScript-style interface or a truncated representation with a meta-comment.

### 3.3 React / JSX / TSX (via `tree-sitter`) - **IMPLEMENTED**
React components are bloated by visual styling and internal state (hooks).
* **Keep:** Component signatures (including arrow functions, default exports, and HOC-wrapped components like `memo`), Props interfaces, type aliases, and JS/TS classes.
* **Strip:** Implementation details, internal hooks (`useState`, `useEffect`), and massive JSX return blocks.
* **Format:** `function Button({ label, onClick })` or `interface CardProps`

**Before (Legacy Map - Low Context):**
Empty (or raw snippet fallback).

**After (Semantic Map - High Context):**
```tsx
interface CardProps
const Card: React.FC<CardProps> = ({ title, children }) =>
function useAuth()  // Custom hook to manage authentication state.
```

---

## 4. Implementation Details (`kopipasta`)

### 4.1 UI & State Updates
* **Keybinding:** The `m` key toggles Semantic Map mode. `s` remains the raw snippet mode.
* **Extraction:** Handled natively in Python via `extract_symbols()` in `kopipasta/file.py`.

### 4.2 Python AST Parsing Pattern
To natively support complex formatting (like type hints, async, and default arguments), we avoid brittle string parsing or regex. Instead:
1. Parse the code into an AST and walk the nodes.
2. Create a shallow copy of the node (e.g., `ast.FunctionDef`, `ast.ClassDef`).
3. Replace its `.body` with `[ast.Pass()]`.
4. Clear decorators if necessary (to save space and reduce noise).
5. Use `ast.unparse()` to reliably generate the correct Python signature string natively.
6. Use `ast.get_docstring()` to extract and append the first line of the docstring.
