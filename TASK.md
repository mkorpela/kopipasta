Based on your request found within `kopipasta/prompt.py`, I have reviewed the codebase and the proposed **"Quad-Memory" Architecture**.

Here is my analysis of how this design addresses "Context Drift" and "DRY" (Don't Repeat Yourself), along with an architectural review of how to integrate it into `kopipasta`.

### Executive Summary

The **Quad-Memory Architecture** is a highly effective solution for the two main problems `kopipasta` users face:
1.  **DRY (Don't Repeat Yourself):** You are currently forced to repeat tool preferences (VS Code, Poetry, etc.) and project constraints (XML libraries, hard rules) in every new chat.
2.  **Context Drift:** Long LLM sessions lose coherence. The "Working Memory" file (`AI_SESSION.md`) allows you to treat the LLM chat window as ephemeral (disposable) while keeping the *state* of the task persistent on disk.

The file-based nature of this architecture aligns perfectly with `kopipasta`'s philosophy of "Explicit Context Control."

---

### Component Analysis & Implementation Strategy

#### 1. The Global Kernel (`~/.ai_profile.md`)
*   **Goal:** Solve DRY for user preferences (Identity, Toolchain).
*   **Codebase Fit:**
    *   Currently, `kopipasta/prompt.py` looks for `~/.config/kopipasta/` for the Jinja2 template.
    *   **Review:** This should not be part of the project structure. It should be injected directly into the prompt template, likely before the project files.
    *   **Implementation Recommendation:**
        *   Modify `kopipasta/prompt.py` to check for `~/.config/kopipasta/ai_profile.md` (or `~/.ai_profile.md`).
        *   Read this string and pass it to the Jinja template as a variable `{{ user_profile }}`.
        *   Update `DEFAULT_TEMPLATE` to render this at the very top.

#### 2. The Project Constitution (`./AI_CONTEXT.md`)
*   **Goal:** Solve DRY for project constraints and reduce hallucinations.
*   **Codebase Fit:**
    *   Currently, the user must manually select this file in the tree. If they forget, the "Laws of Physics" are lost.
    *   **Review:** This file is too important to be treated as "just another file." It should be **auto-detected** and **pinned**.
    *   **Implementation Recommendation:**
        *   In `kopipasta/main.py`, scan `project_root` for `AI_CONTEXT.md`.
        *   If found, automatically read it.
        *   Do *not* bundle it with the standard `files_to_include` list (which can get buried). Pass it to the template as `{{ project_context }}` to ensure it gets a dedicated header in the prompt (e.g., `## Project Constitution`).

#### 3. The Working Memory (`./AI_SESSION.md`)
*   **Goal:** Solve Context Drift.
*   **Codebase Fit:**
    *   This is the most critical component for the "Context Drift" problem. It acts as a "Save Game" state.
    *   **Review:** This file needs to be mutable by the LLM (via patches) but ignored by git.
    *   **Implementation Recommendation:**
        *   **Git Ignore:** Modify `kopipasta/ops.py` -> `read_gitignore`. Hardcode `AI_SESSION.md` into `default_ignore_patterns` so `kopipasta` doesn't accidentally commit it, but *remove* it from the internal ignore list used by the Tree Selector so the user *can* select it.
        *   **Auto-Inclusion:** Like the Constitution, if this file exists, it should be auto-detected.
        *   **Patching:** The existing `patcher.py` handles markdown well, so the LLM can easily update its own checklist in `AI_SESSION.md` using the standard patching workflow.

#### 4. The Gardener (The Update Loop)
*   **Goal:** Update the memory components (Session -> Context).
*   **Review:** You correctly noted: *"In both cases [Success or Failure] The Gardener is actually needed."*
    *   **Scenario A: Task Failed / Context Drifting.**
        *   Action: You ask the LLM to "Dump state to `AI_SESSION.md`."
        *   Result: You apply the patch to `AI_SESSION.md`, close the LLM chat window, open a new one, and run `kopipasta` again. The new prompt includes the updated `AI_SESSION.md`. *Drift solved.*
    *   **Scenario B: Task Completed.**
        *   Action: You need to consolidate `AI_SESSION.md` (scratchpad) into `AI_CONTEXT.md` (permanent knowledge).
        *   **Implementation Recommendation:** This requires a specific **Prompt Mode**.
        *   Add a flag: `kopipasta --garden`.
        *   This generates a specialized prompt (bypassing the file tree):
            > "Review `AI_SESSION.md` and `AI_CONTEXT.md`. Extract generic learnings, architectural decisions, and new constraints from the session. Create a patch to update `AI_CONTEXT.md`. Then, create a patch to clear/reset `AI_SESSION.md`."

---

### Architectural Gaps in Current Code

To support this, `kopipasta` needs specific modification in `prompt.py` and the Jinja template.

**Current Template (`kopipasta/prompt.py`):**
```python
DEFAULT_TEMPLATE = """# Project Overview
## Project Structure
{{ structure }}
## File Contents
{% for file in files -%} ...
```

**Proposed Template Update:**
The prompt structure should be reorganized to prioritize instructions in this order:
1.  **Identity (Global Kernel)** ("Act as Senior Dev...")
2.  **Context (Constitution)** ("Here are the project constraints...")
3.  **State (Session)** ("Here is the current status of the task...")
4.  **Files** ("Here is the code...")

### Addressing "DRY" Specifically

The current codebase is "Wet" (Repetitive) because `kopipasta` is stateless regarding the *nature* of the project.

By implementing **Auto-Detection** in `main.py`:
```python
# Pseudo-code idea for main.py
profile_content = read_global_profile() # ~/.ai_profile.md
context_content = read_project_context() # ./AI_CONTEXT.md
session_content = read_session_state()   # ./AI_SESSION.md

# Only launch tree selector for code files
files_to_include = tree_selector.run(...)

generate_prompt_template(
    files_to_include, 
    profile=profile_content,
    context=context_content, 
    session=session_content
)
```
You eliminate the need to ever type standard instructions or select standard context files again.

### Conclusion

The "Quad-Memory" architecture is valid and necessary. It moves `kopipasta` from being a "Text Dumper" to a "State Manager."

**Immediate Action Plan (Pre-implementation):**
1.  **Manual Prototype:** Before writing code, manually create these 3 markdown files in your repo.
2.  **Template Test:** Edit your local `prompt_template.j2` (using `kopipasta --edit-template`) to hardcode imports for these files to see if the LLM respects the hierarchy.
3.  **Validation:** Verify that `kopipasta/patcher.py` can successfully patch `AI_SESSION.md` when the LLM outputs a markdown block updating a checkbox list. (It should work, but verifying regex behavior on markdown lists is prudent).