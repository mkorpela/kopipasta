# Specification: Structural Patching (AST "Tree Surgery")

## 1. Overview
Currently, `kopipasta` relies on **Text-Based Patching** (Unified Diffs, Search/Replace blocks, and Fuzzy Matching) to apply LLM-generated code. While effective for simple scripts, text-based patching treats highly nested, tree-like structures (JSX, TSX, JSON, HTML) as flat 1D strings.

This leads to known failure modes (documented in `test_patcher_repro_failures.py`):
1. **Duplicate Context Ambiguity:** Generic closing tags (e.g., `</div>\n</Table>`) confuse the diff parser because they appear multiple times in the same file.
2. **Indentation Brittleness:** LLMs often hallucinate whitespace (e.g., 2 spaces instead of 4), causing fuzzy matchers to inject syntax errors into Python or YAML files.
3. **Token Waste:** To ensure safe diff application, LLMs often output the *entire file* or massive context blocks, wasting tokens and time.

**The Solution:** Introduce **Structural Patching** using Abstract Syntax Trees (AST). Instead of targeting line numbers or text blocks, the LLM targets semantic nodes (e.g., "Replace the `TableBody` component" or "Update the `verify_token` function").

---

## 2. The AI-to-Tool Protocol (Syntax)

To trigger AST patching, the LLM will output a standard markdown code block containing a new metadata directive: `TARGET_NODE` or `TARGET_PATH`.

### 2.1 Node Targeting (Python, JS, TS, JSX)
The LLM specifies the type and name of the structural node it wants to replace.

```tsx
// FILE: src/components/DataTable.tsx
// TARGET_NODE: function TableBody
export function TableBody({ data }) {
  return (
    <tbody>
      {data.map(row => (
        <TableRow key={row.id} row={row} />
        <NewElement /> {/* Safely injected without context ambiguity */}
      ))}
    </tbody>
  );
}
```
*Supported Node Types (Heuristic mapping): `function`, `class`, `method`, `interface`.*

### 2.2 Path Targeting (JSON)
For pure data trees, the LLM provides a dot-notation path.

```json
// FILE: package.json
// TARGET_PATH: dependencies
{
  "react": "^18.2.0",
  "framer-motion": "^10.0.0",
  "kopipasta-core": "workspace:*"
}
```

---

## 3. Implementation Architecture (`kopipasta`)

### 3.1 Dependencies
To support cross-language AST parsing without requiring the user to install local compilers, `kopipasta` will depend on `tree-sitter`.
* **Package:** `tree-sitter` and standard language bindings (e.g., `tree-sitter-python`, `tree-sitter-javascript`, `tree-sitter-typescript`).
* **Installation:** Seamlessly managed via `uv` in `pyproject.toml`.

### 3.2 The Patching Pipeline (`patcher.py`)
When `p` (Universal Intake) is triggered, the `PatchParser` state machine will be updated:

1. **Directive Detection:** If the parser detects `TARGET_NODE: <type> <name>` inside a code block, it flags the patch as `type: ast_node`.
2. **AST Compilation:** `kopipasta` loads the target file from disk and parses it using the appropriate `tree-sitter` language grammar based on the file extension.
3. **Node Resolution (Querying):** `kopipasta` executes a Tree-sitter query to locate the node.
   * *Example TSX Query:* `(function_declaration name: (identifier) @name (#eq? @name "TableBody"))`
   * *Example Python Query:* `(function_definition name: (identifier) @name (#eq? @name "verify_token"))`
4. **Byte-Level Splice:** Tree-sitter returns the `start_byte` and `end_byte` of the matched node in the original file. `kopipasta` slices the original file's byte array, inserts the LLM's new code block, decodes back to UTF-8, and writes to disk.

### 3.3 JSON Implementation (No Tree-Sitter Required)
For `TARGET_PATH` in JSON files, `kopipasta` will simply:
1. `json.load()` the original file.
2. `json.loads()` the LLM output.
3. Traverse the dictionary using the target path (e.g., `dict["dependencies"] = new_data`).
4. `json.dump()` with standard indentation.

---

## 4. Safety & Fallback Mechanisms

A core philosophy of `kopipasta` is **Explicit Control over Silent Corruption**. 

* **Deterministic Failure:** If the specified `TARGET_NODE` is not found in the AST (e.g., the LLM hallucinated the component name, or the query returns 0 matches), the patch **fails explicitly**. It does not fall back to fuzzy text matching. It logs: `❌ Node 'TableBody' not found in AST of DataTable.tsx`.
* **Indentation Auto-Correction:** When splicing an AST node, `kopipasta` will read the leading whitespace of the original node's `start_byte` line, and auto-indent the incoming LLM block to match the surrounding tree. This completely eliminates the "2-space vs 4-space" Python bug.

---

## 5. Benefits & Impact on LLM Performance

By guaranteeing Structural Patching, the LLM's behavior will fundamentally optimize:

1. **Drastic Token Reduction:** The LLM no longer needs to output surrounding context lines or full-file rewrites for deeply nested JSX. It outputs exactly and only the semantic node being altered.
2. **Zero Context Hallucination:** The LLM is freed from the cognitive load of matching generic closing tags (`</div>`).
3. **High Confidence Execution:** The LLM can execute complex refactors in large files (>1000 lines) confidently, knowing `kopipasta` will surgically swap the exact node boundary without text-diff collision.

---

## 6. Phased Rollout Plan

* **Phase 1 (JSON & Python):** Implement `TARGET_PATH` using native `json`, and `TARGET_NODE` using native `ast.parse` for Python files. This proves the protocol with zero new dependencies.
* **Phase 2 (Tree-sitter Integration):** Introduce `tree-sitter` for `js`, `ts`, and `tsx` support, targeting the highest-pain points for tree-like syntax patching.
* **Phase 3 (Prompt Updates):** Update `prompt_template.j2` to instruct the LLM: *"When modifying functions or React components, use `// TARGET_NODE: ComponentName` instead of unified diffs."*