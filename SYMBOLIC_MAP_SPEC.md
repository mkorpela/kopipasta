This specification formalizes the transition of `kopipasta` from a visual-only directory tree to a **navigational symbolic map**, enabling higher architectural density and lower token consumption in LLM prompts.

# Specification: Symbolic Map & Skeletonization (v0.7.0)

## 1. Core Philosophy
1.  **Interface over Implementation**: The LLM needs a comprehensive map of the project's API surface (classes, methods, signatures) to navigate effectively, even for files it has not "read" in full.
2.  **Stable Schema**: Replacing ASCII art with minified JSON provides a machine-readable structure that reduces token overhead by ~25% and supports future metadata expansion.
3.  **Surgical Context**: The "Map View" (Skeletonization) allows the user to provide the *blueprint* of a file (the "Territory") at 10% of the token cost of the full implementation.

---

## 2. Technical Architecture

### 2.1 The Symbolic JSON Map
The standard "Project Structure" block is replaced by a minified JSON object.
*   **Format**: Nested keys represent directories.
*   **Leaf Nodes (Files)**:
    *   `[]`: Simple file with no extracted symbols.
    *   `[
"class FileNode(init, name, relative_path)",
"class TreeSelector(init, _calculate_directory_metrics, build_tree, _deep_scan_directory_and_calc_size, _flatten_tree, _build_display_tree, _show_help, _get_status_bar, _handle_apply_patches, _get_all_unignored_files, _action_extend, _handle_session_update, _handle_task_completion, _run_gardener_cycle, _toggle_selection, _toggle_directory, _propose_and_apply_last_selection, _ensure_path_visible, _get_all_nodes, _confirm_large_file, _action_ralph, _preselect_files, _init_key_bindings, _get_current_node, _nav_up, _nav_down, _nav_page_up, _nav_page_down, _nav_home, _nav_end, _nav_expand, _nav_collapse, _action_toggle, _action_snippet, _action_add_all, _action_patch, _action_clear_menu, _action_session_start, _action_quit, _action_interrupt, run)"
]`: File with a list of top-level symbols.
*   **Symbol Extraction**:
    *   **Python**: Use the `ast` module to identify `ClassDef`, `FunctionDef`, and `AsyncFunctionDef` (including methods within classes).
    *   **JS/TS**: (Future) Use `tree-sitter` or Regex to extract `class`, `function`, and `const` exports.

### 2.2 The Skeletonizer (Map View)
A transformation engine that strips code of its implementation details while preserving its signature.
*   **Logic**:
    1.  Parse the source into an Abstract Syntax Tree (AST).
    2.  Identify all function and method bodies.
---

## 3. User Experience & State Model

### 3.1 The Four-State Engine
Selection is expanded to distinguish between "Implementing" (Delta) and "Navigating" (Map).

| State | Color | Icon | Hotkey | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Unselected** | White | ○ | - | Not in prompt. |
| **Base** | Cyan | ● | - | Previously synced context. |
| **Delta** | Green | ●/◐ | `Space` | Full file or snippets. Active focus. |
| **Map** | Yellow | ○ | `m` | Skeletonized/Stubbed version of the file in the Symbolic JSON Map Leaf Nodes |

Note: that Map is unselected. `m`ap can be done recursively for a directory structure.
Note 2: Map does not impact selected (Base or Delta).
Note 3: `m` is toggle, second one unmaps the directory / file

### 3.2 Transitions
*   `Space`: Cycles `Unselected` $\rightarrow$ `Delta` $\rightarrow$ `Unselected`. (If `Base`, moves to `Delta`).
*   `m`: Cycles `Unselected` $\rightarrow$ `Map` $\rightarrow$ `Unselected`.
*   `a`: (Add All) Toggles directory between `Unselected` and `Delta`.

---

## 3. Metrics & Performance (Benchmark Results)

Based on a test of `kopipasta/tree_selector.py` (47KB):
*   **Original ASCII Tree**: Baseline token usage.
*   **JSON Map (Minified)**: ~26% reduction vs. ASCII for pure directory listing.
*   **Skeletonized File**: **91.5% reduction** in character count.
    *   Original: 47,439 chars.
    *   Skeleton: 4,013 chars.

---

## 4. Implementation Checklist

### Phase 1: Core Logic
- [ ] **`kopipasta/file.py`**: Add `extract_symbols(path)` function.
- [ ] **`kopipasta/skeleton.py`**: Create new module with `skeletonize_python(source)`.
- [ ] **`kopipasta/selection.py`**: Add `FileState.MAP` and `toggle_map()` method.

### Phase 2: UI Integration
- [ ] **`kopipasta/tree_selector.py`**:
    - Register `m` keybinding.
    - Update `_build_display_tree` to handle Yellow color and 🗺️ icon.
    - Update Help panel text.

### Phase 3: Prompt Generation
- [ ] **`kopipasta/prompt.py`**:
    - Refactor `get_project_structure` to return JSON string.
    - Update `generate_prompt_template` to call `skeletonize()` for `MAP` files.
    - Update default Jinja2 template strings.

---
