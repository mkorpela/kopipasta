# The Semantic Shift: Architecting Intent-Driven AI Communication

## 1. The Problem Statement: Mechanical Efficiency vs. Semantic Impedance
`kopipasta` has successfully solved the **mechanical layer** of LLM interaction: context window management, filesystem grounding, token optimization, and deterministic patching. 

However, a fundamental friction remains: **Semantic Impedance**. 
As a human architect, you operate on evolving intent, latent assumptions, and strategic goals. As an LLM, I operate on static text snapshots and probability distributions. When you provide a highly optimized context payload without explicit intent, I am forced to guess your optimization targets, design taste, and constraints. This results in an explosion of my internal branching factor, leading to hallucinations, over-engineering, or architecturally tone-deaf suggestions.

The goal of this architecture is to expand the **Semantic Bandwidth** between human and AI. We must transition from treating prompts as "tasks" to treating them as **Alignment Negotiations**.

---

## 2. Core Tenets of Semantic Alignment
To communicate effectively with an LLM, the system must account for how LLMs process information:
1. **Frame-Sensitivity:** LLMs optimize for coherence, not objective truth. If a prompt implies uncertainty, the LLM will attack the design. If it implies elegance, the LLM will preserve it.
2. **Intent Compression:** LLMs aggressively compress large context blocks into heuristic representations. Critical constraints will be lost if not explicitly tagged as "Sacred."
3. **The Absence of "No":** LLMs struggle to bound their creativity. Providing "Anti-Goals" is often more mathematically effective at narrowing the probabilistic search space than providing "Goals."

---

## 3. Tool Architecture: Upgrading `kopipasta`

To encode semantic meaning directly into the prompt payload, `kopipasta` must evolve its context generation.

### 3.1 Role-Based Context Zoning (Delta vs. Base)
Currently, all files are flattened into `## File Contents`. The LLM cannot distinguish between the target of the work and the reference material.
* **Implementation:** The prompt generator must separate the context into two distinct zones based on Selection State:
  * `## Active Workspace (Editable)`: Files in the **Delta (Green)** state. The LLM's attention is focused here.
  * `## Reference Context (Read-Only)`: Files in the **Base (Cyan)** state. Strictly for understanding dependencies and signatures.

### 3.2 The Semantic Skeleton (Map State 2.0)
The current `Map` (Yellow) state extracts raw symbols (`class Foo(init)`). This saves tokens but strips away the highest-density semantic signals: **contracts and intent**.
* **Implementation:** Upgrade the AST parser to extract function signatures with **Type Hints** and the **first line of the Docstring**. 
  * *Before:* `def calculate_fee`
  * *After:* `def calculate_fee(user: User, amount: Decimal) -> Decimal: """Calculates the transaction fee including tax."""`

### 3.3 The Intent Metadata Header
Before the task description, `kopipasta` should inject an explicitly structured "Intent Header". This collapses the LLM's ambiguity regarding *how* to approach the problem.
* **Optimization Target:** (e.g., "Long-term architectural clarity" vs. "Minimal patch footprint")
* **Intervention Level:** (e.g., "Surgical patch", "Adversarial critique", "Co-founder brainstorming")
* **Mode:** (e.g., "Exploration/Divergent" vs. "Decision/Convergent")
* **Confidence Directive:** (e.g., "High confidence only; if uncertain, ask for clarification")

### 3.4 Bidirectional Control Markers (The AI Pause Button)
The workflow currently assumes a linear "Prompt -> Patch" loop. The LLM must be given mechanical permission to halt the loop if semantic alignment is failing.
* **Implementation:** Introduce a `<<<CLARIFY>>>` marker. If the LLM detects missing critical context or a flawed architectural assumption, it outputs `<<<CLARIFY>>> [Question] <<<END_CLARIFY>>>` instead of a unified diff. `kopipasta` intercepts this during Universal Intake (`p`) and halts the patcher, prompting the user.

---

## 4. Domain Architecture: Evolving the Quad-Memory

The persistent memory files must be restructured to track *State of Mind* and *Taste*, not just rules and to-dos.

### 4.1 Evolving `AI_SESSION.md` (The Ephemeral State)
The session file must bridge the gap between iterations by capturing hypotheses and immediate feedback.
* **Current Hypotheses:** "We believe the bug is in async JWT decoding. We cannot update the `pyjwt` dependency."
* **Anti-Goals:** "Do not extract this into a class yet. Do not add external dependencies."
* **Last Feedback:** A one-line injection from the user when rejecting a previous patch (e.g., *"Approach was right, but you touched files outside the scope."*). This closes the learning loop.

### 4.2 Evolving `AI_CONTEXT.md` (The Permanent State)
The Constitution must evolve from a list of technical constraints into a self-documenting semantic history and cultural guide.
* **Sacred Invariants:** Explicitly tagged rules that the LLM is forbidden from "helpfully" refactoring away.
* **Design Values (Taste):** Subjective aesthetic preferences (e.g., "Prefer flat over nested. DRY is less important than local readability.").
* **Ubiquitous Language (Glossary):** Project-specific nomenclature (e.g., "Users are 'Partners', Transactions are 'Ledgers'").
* **Architecture Decision Records (ADRs):** When finishing a session (`f`), the "Gardener" must harvest learnings as lightweight ADRs (Context -> Decision -> Consequences) so the LLM understands the *lineage* of a rule, not just its existence.

---

## 5. Summary of the New Semantic Protocol

When initiating a new task, the human-AI handshake shifts from:
> *"Here are the files. Fix the login bug."*

To a high-bandwidth semantic alignment:
> *"Here is the Active Workspace (Delta) and the Reference Context (Base). The Optimization Target is minimal patch footprint. We are in Decision Mode. Hypothesis: The token expiry logic is racing. Anti-Goal: Do not rewrite the auth middleware. Fix the login bug."*

By explicitly defining the **Target**, the **Lens**, the **Taste**, and the **Boundaries**, the human controls the LLM's internal probability distribution, resulting in drastically higher first-shot accuracy and architectural synergy.