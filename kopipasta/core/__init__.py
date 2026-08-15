"""Core: the logic behind the verbs, with no interactive prompts and no TUI.

Everything here must be callable from a subprocess with no terminal attached.
Anything that would block on a human belongs behind `kopipasta.interaction`.
"""
