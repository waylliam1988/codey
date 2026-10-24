"""Bash grammar frontend; unsupported Sh/Zsh forms stay opaque, never guessed."""
from __future__ import annotations

from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import (
    ShellAnalysis,
    ShellCommand,
    ShellRedirect,
    ShellWord,
    dynamic_name,
    posix_literal,
)

_EXPANSIONS = {"simple_expansion", "expansion", "command_substitution", "process_substitution",
               "arithmetic_expansion", "brace_expression", "extglob_pattern"}
_KNOWN = {"program", "command", "command_name", "word", "string", "string_content", "raw_string",
          "concatenation", "pipeline", "list", "redirected_statement", "file_redirect", "file_descriptor",
          "number", "comment", "variable_name", "special_variable_name", "subscript", "variable_assignment",
          "heredoc_redirect", "heredoc_start", "heredoc_body", "heredoc_end", *_EXPANSIONS}


def analyze_posix(command: str, shell: ShellSpec, digest: str) -> ShellAnalysis:
    try:
        import tree_sitter_bash
        from tree_sitter import Language, Parser
    except ImportError:
        return ShellAnalysis(digest, "opaque", "tree-sitter-bash", reason="required Bash parser is unavailable")
    source = command.encode("utf-8")
    root = Parser(Language(tree_sitter_bash.language())).parse(source).root_node
    parser = "tree-sitter-bash/0.25.1"
    if root.has_error:
        return ShellAnalysis(digest, "invalid" if shell.kind == "bash" else "opaque", parser,
                             reason="syntax outside the supported shell grammar")
    from tree_sitter import Node
    nodes: list[Node] = []
    pending = [root]
    while pending and len(nodes) < 10000:
        node = pending.pop()
        nodes.append(node)
        pending.extend(reversed(node.children))
    if pending:
        return ShellAnalysis(digest, "opaque", parser, reason="shell syntax node budget exceeded")

    def text(node: object) -> str:
        from tree_sitter import Node
        assert isinstance(node, Node)
        return source[node.start_byte:node.end_byte].decode("utf-8")

    def word(node: object) -> ShellWord:
        from tree_sitter import Node
        assert isinstance(node, Node)
        raw = text(node)
        expanded = tuple(text(item) for item in nodes if item.type in _EXPANSIONS
                         and item.start_byte >= node.start_byte and item.end_byte <= node.end_byte)
        expanded += tuple(text(item) for item in nodes if item.type == "word"
                          and item.start_byte >= node.start_byte and item.end_byte <= node.end_byte
                          and any(char in text(item) for char in "*?[]~{}"))
        return ShellWord(raw, None if expanded else posix_literal(raw), expanded)

    commands = tuple(ShellCommand(tuple(word(child) for child in node.named_children
                                       if child.type != "variable_assignment"))
                     for node in nodes if node.type == "command")
    # command_name wraps a single word and is decoded as one whole argument.
    redirects = []
    for node in nodes:
        if node.type in {"file_redirect", "heredoc_redirect"}:
            target = node.child_by_field_name("destination")
            operator = "".join(text(child) for child in node.children if not child.is_named)
            redirects.append(ShellRedirect(operator or "<<", word(target) if target else None))
    expanded = tuple(text(node) for node in nodes if node.type in _EXPANSIONS | {"variable_assignment"})
    expanded += tuple(item for row in commands for value in row.words for item in value.expansions)
    expanded += tuple(item for redirect in redirects if redirect.target for item in redirect.target.expansions)
    unknown = sorted({node.type for node in nodes if node.is_named and node.type not in _KNOWN})
    if shell.kind == "sh" and any(node.type in {"process_substitution", "extglob_pattern"} for node in nodes):
        unknown.append("non-POSIX syntax")
    operators = tuple(text(node) for node in nodes if not node.is_named and node.type in {"|", "|&", "&&", "||", ";", "&"})
    dynamic = any(node.type in {"command_substitution", "process_substitution"} for node in nodes)
    dynamic = dynamic or any(dynamic_name(row.words) for row in commands)
    reason = "unsupported syntax: " + ", ".join(unknown) if unknown else ""
    return ShellAnalysis(digest, "opaque" if unknown else "parsed", parser, commands, operators,
                         tuple(redirects), tuple(dict.fromkeys(expanded)), dynamic, reason[:240])
