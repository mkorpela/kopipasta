# Specification: Kopipasta Smart Context (v0.6.0)

**Status:** Final
**Core Philosophy:**

1. **State-Awareness:** Distinguish between **Base** (Background/Synced) and **Delta** (Focus/Unsynced) context.
2. **Unified Input:** The `p` hotkey handles all text ingestion (Patches, Imports, Resets).
3. **Fluid Navigation:** Shortcuts to move files between states (`Space`, `c`, `e`).

## 1. Architecture: The State Model

### 1.1 State Definitions

* **Unselected:** File is ignored.
* **Base (Blue):** Files previously sent to the LLM. "Background Context."
* **Delta (Green):** Files newly selected, imported, or modified. "Active Focus."

### 1.2 Transitions

* **Selection (`Space`):** Unselected  **Delta**  **Base**  Unselected.
* *Logic:* Tapping a Blue file promotes it to Green (Focus). Tapping it again removes it.


* **Process (`p`):** Patched/Imported files  **Delta**.
* **Extend (`e`):** **Delta**  **Base** (after copy).
* **Clear (`c`):** **Base**  Unselected. (Keeps Delta).

---

## 2. Feature Specifications

### Feature 1: Unified Process (`p`)

**Goal:** Handle *any* LLM text response intelligently.

**Logic:**

1. **Reset Scan:** Check for `<<<RESET>>>`. If found, ignore all text preceding it.
2. **Patch Scan:** Check for code blocks with headers (`# FILE: ...`) or diffs.
* **If Patches Found:** Apply them.
* **Action:** Mark success files as **Delta**.
* **Safety:** If block is `<<<DELETE>>>`, prompt for deletion.




3. **Path Scan:** If *no* patches are found, regex-scan for file paths.
* **If Paths Found:**
* **Prompt:** `Found X paths. [A]ppend to current or [R]eplace selection?`
* **Action (Append):** Add files to **Delta**.
* **Action (Replace):** Clear *all* selection, then add files to **Delta**.





### Feature 2: Extend Context (`e`)

**Goal:** Create a "Follow-up" prompt with minimal tokens.

**Logic:**

1. **Filter:** Get all **Delta (Green)** files.
2. **Fallback:** If no Delta files, ask: `No new changes. Extend with ALL (Base) files? [y/N]`
3. **Generate:** Create prompt using *only* the filtered files.
4. **Commit:** Change state of these files from **Delta**  **Base** (Blue).

### Feature 3: The Fix Workflow (`x`)

**Goal:** Auto-diagnose errors with full context.

**Logic:**

1. **Run:** execute `KOPIPASTA_FIX_CMD` (defaults to `pre-commit` or `git diff --check`).
2. **Capture:** `stdout`, `stderr`.
3. **Context Assembly:**
* **Traceback:** Parse `stderr` for paths. Add them to **Delta**.
* **Diff:** Capture `git diff HEAD`.


4. **Generate:** Prompt = Error + Diff + Content of Delta files.
* *Note:* Do *not* auto-commit Delta  Base here, as the user might want to `e` (Extend) later with the same context if they manually add more files.



### Feature 4: Clear Selection (`c`)

**Goal:** Manual cleanup.

**Logic:**

1. **Action:** Deselect all **Base (Blue)** files.
2. **Constraint:** Leave **Delta (Green)** files active. (If user wants to clear *everything*, they press `c` then `c` again, or `Space` on the Greens).

---

## 3. UI/UX Standards

### Tree View Colors

* **Dim/Gray:** Ignored.
* **White:** Unselected.
* **Cyan (Blue):** **Base** (Synced/History).
* **Green:** **Delta** (Modified/New/Imported).

### Keyboard Shortcuts

| Key | Action | Description |
| --- | --- | --- |
| `Space` | **Toggle** | Unselected  Delta  Base  Unselected. |
| `p` | **Process** | Universal paste. Handles Patches, Imports (Append/Replace), Resets. |
| `e` | **Extend** | Copies Delta files. Transitions Delta  Base. |
| `x` | **Fix** | Runs command. Adds error files to Delta. Copies prompt. |
| `c` | **Clear Base** | Unselects all Blue files. Keeps Green files. |
| `q` | **Quit** | Copies full context (Base + Delta). |

---

## 4. Implementation Checklist

### Phase 1: State Engine

* [ ] Refactor `TreeSelector.selected_files` to store `Enum` state.
* [ ] Update `Space` key logic (3-state cycle).
* [ ] Update `_build_display_tree` to render Cyan/Green.

### Phase 2: The Processor (`p`)

* [ ] Implement `<<<RESET>>>` and `<<<DELETE>>>` in Parser.
* [ ] Implement Regex Path Scanner.
* [ ] Implement `[A]ppend / [R]eplace` logic in `p` handler.
* [ ] Ensure patched files promote to Delta.

### Phase 3: Workflow Actions (`e`, `x`, `c`)

* [ ] Implement `e`: Filter Delta, Generate, Commit (Delta  Base).
* [ ] Implement `c`: Clear Base only.
* [ ] Implement `x`: Run cmd, parse traceback  Delta, include `git diff`.

### Phase 4: Prompt Templates

* [ ] Update System Prompt to explain `<<<DELETE>>>` and `<<<RESET>>>`.
* [ ] Create `fix_template` (Error + Diff + Files).
* [ ] Create `extension_template` (Files only).