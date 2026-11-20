"""Opt-in compatibility for the generated pi 1.5.2 system-prompt scaffold.

Only recognized generated sections are normalized. Additional rules, appended
instructions, conversation messages and wire tool schemas are not rewritten.
"""
import re

COMPAT_SYSTEM = (
    "Help the user with programming tasks. Use the client-provided tools when needed. "
    "Follow their schemas and report results accurately."
)
_HEADER = (
    "You are a coding assistant running inside pi, a coding agent harness. "
    "Help users inspect files, run commands, and modify code."
)
# Preserve operational rules (tool selection, safe editing, response format).
# Only this known harness-introspection hint is unrelated to normal client work.
_FRAMEWORK_RULES = frozenset((
    "- You can inspect PI_* environment variables for current model and session details.",
))
_DOC_LINES = (
    "Pi documentation (read only when the user asks about the harness itself, its SDK, extensions, themes, skills, or TUI):",
    "- When reading pi docs or examples, resolve docs/... under Additional docs and examples/... under Examples, not the current working directory",
    "- When asked about: extensions (docs/extensions.md, examples/extensions/), themes (docs/themes.md), skills (docs/skills.md), prompt templates (docs/prompt-templates.md), TUI components (docs/tui.md), keybindings (docs/keybindings.md), SDK integrations (docs/sdk.md), custom providers (docs/custom-provider.md), adding models (docs/models.md), packages (docs/packages.md), environment variables (docs/environment-variables.md), MCP servers (docs/mcp.md), codemode scripts and non-LLM models such as classifiers and image models (docs/codemode.md)",
    "- Before implementing harness changes, read the relevant docs and examples in full, following related cross-references.",
)
_DOC_PATHS = (
    re.compile(r"- Main documentation: [^\r\n]+/README\.md"),
    re.compile(r"- Additional docs: [^\r\n]+/docs"),
    re.compile(r"- Examples: [^\r\n]+/examples \(extensions, custom tools, SDK\)"),
)
_SCAFFOLD = re.compile(
    re.escape(_HEADER) + r"\n\n<tools>(?P<tools>.*?)</tools>"
    r"\n\n<rules>(?P<rules>.*?)</rules>\n\n<docs>(?P<docs>.*?)</docs>", re.S
)


def _is_inventory(text: str) -> bool:
    return all(not line.strip() or line == "Additional tools may be available."
               or re.fullmatch(r"- [\w.-]+: [^\r\n]+", line)
               for line in text.splitlines())


def _is_default_docs(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != len(_DOC_LINES) + len(_DOC_PATHS):
        return False
    return (all(lines.count(line) == 1 for line in _DOC_LINES)
            and all(sum(bool(pattern.fullmatch(line)) for line in lines) == 1 for pattern in _DOC_PATHS))


def rewrite_pi_prompt(system: str) -> str:
    """Normalize a known pi scaffold; return unfamiliar system prompts unchanged."""
    match = _SCAFFOLD.match(system)
    if not match or not _is_inventory(match["tools"]) or not _is_default_docs(match["docs"]):
        return system
    # The inventory duplicates tool schemas sent separately by the client.
    # Keep operational and additional/modified rules verbatim, including line endings.
    rules = "".join(line for line in match["rules"].splitlines(keepends=True)
                    if line.rstrip("\r\n") not in _FRAMEWORK_RULES)
    preserved = "\n\n<rules>" + rules + "</rules>" if rules.strip() else ""
    return COMPAT_SYSTEM + preserved + system[match.end():]
