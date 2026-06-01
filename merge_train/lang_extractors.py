"""Language-specific symbol extractors for TypeScript, JavaScript, Go, Rust, Java, C/C++, C#.

Uses tree-sitter (optional) for AST-based extraction with regex fallback when tree-sitter
is not installed. All extractors return a list of Symbol objects matching the interface
defined in symbols.py.
"""

from __future__ import annotations

import re
from typing import Optional

from merge_train.symbols import Symbol, UnsupportedLanguageError

# --------------------------------------------------------------------------- #
# tree-sitter availability
# --------------------------------------------------------------------------- #

_TS_AVAILABLE = False
try:
    import tree_sitter_languages
    _TS_AVAILABLE = True
except ImportError:
    pass


_TS_LANG_MAP = {
    "typescript": "typescript",
    "tsx": "tsx",
    "javascript": "javascript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
    "csharp": "c_sharp",  # tree-sitter-languages uses underscore
}


def _parse_ts(language: str, source: bytes) -> Optional[object]:
    """Parse source with tree-sitter and return the tree, or None on failure."""
    if not _TS_AVAILABLE:
        return None
    ts_lang = _TS_LANG_MAP.get(language, language)
    try:
        parser = tree_sitter_languages.get_parser(ts_lang)
        return parser.parse(source)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# TypeScript / JavaScript
# --------------------------------------------------------------------------- #

def extract_typescript_symbols(source: str, *, language: str = "typescript") -> list[Symbol]:
    """Extract symbols from TypeScript/JavaScript using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_ts_tree_sitter(source, language=language)
        except Exception:
            pass
    return _extract_ts_regex(source)


def _extract_ts_tree_sitter(source: str, *, language: str = "typescript") -> list[Symbol]:
    """Extract TS/JS symbols via tree-sitter TypeScript/TSX grammar."""
    tree = _parse_ts(language, source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("typescript")

    out: list[Symbol] = []
    # We capture: function_declaration, class_declaration, method_definition, interface_declaration, type_alias
    _capture_ts_nodes(tree.root_node, out)
    return out


def _capture_ts_nodes(node: object, out: list[Symbol]) -> None:
    """Recursively walk tree-sitter node tree capturing symbol nodes."""
    # node type is tree_sitter.Tree.node / we access via getattr
    node_type = node.type  # type: ignore[attr-defined]

    if node_type in (
        "function_declaration",
        "class_declaration",
        "method_definition",
        "interface_declaration",
        "type_alias_declaration",
        "arrow_function",
    ):
        name_node = None
        if node_type == "function_declaration":
            name_node = _get_child(node, "identifier")
        elif node_type == "class_declaration":
            name_node = _get_child(node, "identifier")
        elif node_type == "method_definition":
            name_node = _get_child(node, "property_identifier")
        elif node_type == "interface_declaration":
            name_node = _get_child(node, "type_identifier")
        elif node_type == "type_alias_declaration":
            name_node = _get_child(node, "type_identifier")
        elif node_type == "arrow_function":
            # Arrow functions don't have names normally; skip anonymous ones
            name_node = _get_child(node, "identifier")

        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            out.append(Symbol(name=name_node.text.decode("utf-8"), start=start, end=end))  # type: ignore[attr-defined]

    for child in node.children:  # type: ignore[attr-defined]
        _capture_ts_nodes(child, out)


def _get_child(node: object, child_type: str) -> Optional[object]:
    """Get first child of node with the given type."""
    for child in node.children:  # type: ignore[attr-defined]
        if child.type == child_type:  # type: ignore[attr-defined]
            return child
    return None


_TS_FUNC_RE = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(", re.MULTILINE)
_TS_CLASS_RE = re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE)
_TS_ARROW_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)
_TS_INTERFACE_RE = re.compile(r"^interface\s+(\w+)", re.MULTILINE)
_TS_TYPE_RE = re.compile(r"^type\s+(\w+)\s*=", re.MULTILINE)


def _extract_ts_regex(source: str) -> list[Symbol]:
    """Regex-based TypeScript/JavaScript symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()

    for i, line in enumerate(lines, start=1):
        m = _TS_CLASS_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _TS_INTERFACE_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _TS_TYPE_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _TS_FUNC_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _TS_ARROW_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# Go
# --------------------------------------------------------------------------- #

def extract_go_symbols(source: str) -> list[Symbol]:
    """Extract symbols from Go using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_go_tree_sitter(source)
        except Exception:
            pass
    return _extract_go_regex(source)


def _extract_go_tree_sitter(source: str) -> list[Symbol]:
    """Extract Go symbols via tree-sitter Go grammar."""
    tree = _parse_ts("go", source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("go")

    out: list[Symbol] = []
    _capture_go_nodes(tree.root_node, out)
    return out


def _extract_go_receiver_type_from_str(receiver_str: str) -> str:
    """Helper to extract receiver type name from receiver parameter string."""
    receiver_str = receiver_str.strip()
    parts = receiver_str.split()
    if not parts:
        return ""
    type_part = parts[-1]
    type_part = type_part.lstrip("*")
    if "." in type_part:
        type_part = type_part.split(".")[-1]
    return type_part


def _get_go_receiver_type(node: object) -> Optional[str]:
    """Find receiver parameter list and extract the receiver type name recursively."""
    receiver_list = _get_child(node, "parameter_list")
    if receiver_list is None:
        return None

    def find_type_name(n: object) -> Optional[str]:
        ntype = getattr(n, "type", "")
        if ntype == "type_identifier":
            return getattr(n, "text", b"").decode("utf-8")
        for child in getattr(n, "children", []):
            res = find_type_name(child)
            if res:
                return res
        return None

    return find_type_name(receiver_list)


def _capture_go_nodes(node: object, out: list[Symbol]) -> None:
    """Recursively walk Go tree-sitter nodes."""
    node_type = node.type  # type: ignore[attr-defined]

    if node_type in ("function_declaration",):
        name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            out.append(Symbol(name=name_node.text.decode("utf-8"), start=start, end=end))  # type: ignore[attr-defined]
    elif node_type in ("type_declaration",):
        # Type name is in type_spec > type_identifier
        type_spec = _get_child(node, "type_spec")
        if type_spec is not None:
            name_node = _get_child(type_spec, "type_identifier") or _get_child(type_spec, "identifier")
            if name_node is not None:
                start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
                end = node.end_point[0] + 1  # type: ignore[attr-defined]
                out.append(Symbol(name=name_node.text.decode("utf-8"), start=start, end=end))  # type: ignore[attr-defined]
    elif node_type in ("method_declaration",):
        # Method name is in field_identifier (not identifier) on the receiver
        name_node = _get_child(node, "field_identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            method_name = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            receiver_type = _get_go_receiver_type(node)
            if receiver_type:
                symbol_name = f"{receiver_type}.{method_name}"
            else:
                symbol_name = method_name
            out.append(Symbol(name=symbol_name, start=start, end=end))  # type: ignore[attr-defined]

    for child in node.children:  # type: ignore[attr-defined]
        _capture_go_nodes(child, out)


_GO_FUNC_RE = re.compile(r"^func\s+(\w+)\s*\(", re.MULTILINE)
_GO_METHOD_RE = re.compile(r"^func\s+\(([^)]+)\)\s+(\w+)\s*\(", re.MULTILINE)
_GO_TYPE_RE = re.compile(r"^type\s+(\w+)\s+(struct|interface)", re.MULTILINE)


def _extract_go_regex(source: str) -> list[Symbol]:
    """Regex-based Go symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()

    for i, line in enumerate(lines, start=1):
        m = _GO_METHOD_RE.match(line)
        if m:
            receiver_str = m.group(1)
            method_name = m.group(2)
            receiver_type = _extract_go_receiver_type_from_str(receiver_str)
            if receiver_type:
                name = f"{receiver_type}.{method_name}"
            else:
                name = method_name
            out.append(Symbol(name=name, start=i, end=i))
            continue

        m = _GO_FUNC_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _GO_TYPE_RE.match(line)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# Rust
# --------------------------------------------------------------------------- #

def extract_rust_symbols(source: str) -> list[Symbol]:
    """Extract symbols from Rust using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_rust_tree_sitter(source)
        except Exception:
            pass
    return _extract_rust_regex(source)


def _extract_rust_tree_sitter(source: str) -> list[Symbol]:
    """Extract Rust symbols via tree-sitter Rust grammar."""
    tree = _parse_ts("rust", source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("rust")

    out: list[Symbol] = []
    _capture_rust_nodes(tree.root_node, out, None)
    return out


def _capture_rust_nodes(node: object, out: list[Symbol], impl_trait: Optional[str]) -> None:
    """Recursively walk Rust tree-sitter nodes, tracking impl trait for method names."""
    node_type = node.type  # type: ignore[attr-defined]

    if node_type == "function_item":
        name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            name = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            if impl_trait:
                name = f"{impl_trait}.{name}"
            out.append(Symbol(name=name, start=start, end=end))
    elif node_type == "struct_item":
        name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            out.append(Symbol(name=name_node.text.decode("utf-8"), start=start, end=end))  # type: ignore[attr-defined]
    elif node_type == "impl_item":
        # Get the trait/type being implemented — type is in the "type" field
        # which contains a type_identifier, not a direct identifier child
        type_field = _get_child(node, "type")
        trait_name = None
        if type_field is not None:
            name_node = _get_child(type_field, "type_identifier") or _get_child(type_field, "identifier")
            if name_node is not None:
                trait_name = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
        for child in node.children:  # type: ignore[attr-defined]
            _capture_rust_nodes(child, out, trait_name)
        return

    for child in node.children:  # type: ignore[attr-defined]
        _capture_rust_nodes(child, out, impl_trait)


_RUST_FN_RE = re.compile(r"^(?:pub\s+)?fn\s+(\w+)", re.MULTILINE)
_RUST_STRUCT_RE = re.compile(r"^struct\s+(\w+)", re.MULTILINE)
_RUST_IMPL_RE = re.compile(r"^impl\s+(?:<[^>]+>\s+)?(\w+)", re.MULTILINE)


def _extract_rust_regex(source: str) -> list[Symbol]:
    """Regex-based Rust symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()
    impl_stack: list[Optional[str]] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()

        m = _RUST_STRUCT_RE.match(stripped)
        if m:
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _RUST_IMPL_RE.match(stripped)
        if m:
            impl_stack.append(m.group(1))
            continue

        # Only pop at a bare } at the start of a line (impl block end)
        if stripped == "}" and impl_stack:
            impl_stack.pop()
            continue

        m = _RUST_FN_RE.match(stripped)
        if m:
            fn_name = m.group(1)
            if impl_stack:
                fn_name = f"{impl_stack[-1]}.{fn_name}"
            out.append(Symbol(name=fn_name, start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# Java
# --------------------------------------------------------------------------- #

def extract_java_symbols(source: str) -> list[Symbol]:
    """Extract symbols from Java using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_java_tree_sitter(source)
        except Exception:
            pass
    return _extract_java_regex(source)


def _extract_java_tree_sitter(source: str) -> list[Symbol]:
    """Extract Java symbols via tree-sitter Java grammar."""
    tree = _parse_ts("java", source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("java")

    out: list[Symbol] = []
    _capture_java_nodes(tree.root_node, out, None)
    return out


def _capture_java_nodes(node: object, out: list[Symbol], class_name: Optional[str]) -> None:
    """Recursively walk Java tree-sitter nodes, tracking class context for methods."""
    node_type = node.type  # type: ignore[attr-defined]

    if node_type == "class_declaration":
        name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            cn = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            out.append(Symbol(name=cn, start=start, end=end))
            for child in node.children:  # type: ignore[attr-defined]
                _capture_java_nodes(child, out, cn)
        return
    elif node_type in ("method_declaration", "constructor_declaration"):
        name_node = _get_child(node, "identifier")
        if name_node is not None and class_name:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            out.append(Symbol(name=f"{class_name}.{name_node.text.decode('utf-8')}", start=start, end=end))  # type: ignore[attr-defined]

    for child in node.children:  # type: ignore[attr-defined]
        _capture_java_nodes(child, out, class_name)


_JAVA_METHOD_RE = re.compile(
    r"^(?:public|private|protected|static)?\s*(?:\w+\s+)*(\w+)\s*\(",
    re.MULTILINE,
)
_JAVA_CLASS_RE = re.compile(r"^(?:public\s+)?class\s+(\w+)", re.MULTILINE)


def _extract_java_regex(source: str) -> list[Symbol]:
    """Regex-based Java symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()
    class_stack: list[str] = []

    for i, line in enumerate(lines, start=1):
        m = _JAVA_CLASS_RE.match(line)
        if m:
            class_stack.append(m.group(1))
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _JAVA_METHOD_RE.match(line)
        if m and class_stack:
            out.append(Symbol(name=f"{class_stack[-1]}.{m.group(1)}", start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# C/C++
# --------------------------------------------------------------------------- #

def extract_cpp_symbols(source: str) -> list[Symbol]:
    """Extract symbols from C/C++ using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_cpp_tree_sitter(source)
        except Exception:
            pass
    return _extract_cpp_regex(source)


def _extract_cpp_tree_sitter(source: str) -> list[Symbol]:
    """Extract C/C++ symbols via tree-sitter C++ grammar."""
    tree = _parse_ts("cpp", source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("cpp")

    out: list[Symbol] = []
    _capture_cpp_nodes(tree.root_node, out, None)
    return out


def _capture_cpp_nodes(node: object, out: list[Symbol], class_name: Optional[str]) -> None:
    """Recursively walk C++ tree-sitter nodes."""
    node_type = node.type  # type: ignore[attr-defined]

    if node_type in ("class_specifier", "struct_specifier"):
        name_node = _get_child(node, "type_identifier") or _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            cn = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            out.append(Symbol(name=cn, start=start, end=end))
            for child in node.children:  # type: ignore[attr-defined]
                _capture_cpp_nodes(child, out, cn)
        return
    elif node_type == "function_definition":
        # Function name is in declarator > function_declarator > identifier
        declarator = _get_child(node, "declarator")
        name_node = None
        if declarator is not None:
            name_node = _get_child(declarator, "identifier")
        if name_node is None:
            name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            fn_name = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            if class_name:
                fn_name = f"{class_name}.{fn_name}"
            out.append(Symbol(name=fn_name, start=start, end=end))

    for child in node.children:  # type: ignore[attr-defined]
        _capture_cpp_nodes(child, out, class_name)


_CPP_FUNC_RE = re.compile(r"^(?:inline\s+)?(?:void|int|char|float|double|bool|auto|\w+)\s+(\w+)\s*\([^)]*\)", re.MULTILINE)
_CPP_CLASS_RE = re.compile(r"^(?:class|struct)\s+(\w+)", re.MULTILINE)


def _extract_cpp_regex(source: str) -> list[Symbol]:
    """Regex-based C/C++ symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()
    class_stack: list[str] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()

        m = _CPP_CLASS_RE.match(stripped)
        if m:
            class_stack.append(m.group(1))
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        # Class definitions end with };  method bodies end with just }
        if stripped == "};":
            if class_stack:
                class_stack.pop()
            continue

        m = _CPP_FUNC_RE.match(stripped)
        if m:
            fn_name = m.group(1)
            if class_stack:
                fn_name = f"{class_stack[-1]}.{fn_name}"
            out.append(Symbol(name=fn_name, start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# C#
# --------------------------------------------------------------------------- #

def extract_csharp_symbols(source: str) -> list[Symbol]:
    """Extract symbols from C# using tree-sitter or regex fallback."""
    if _TS_AVAILABLE:
        try:
            return _extract_csharp_tree_sitter(source)
        except Exception:
            pass
    return _extract_csharp_regex(source)


def _extract_csharp_tree_sitter(source: str) -> list[Symbol]:
    """Extract C# symbols via tree-sitter C# grammar."""
    tree = _parse_ts("csharp", source.encode("utf-8"))
    if tree is None:
        raise UnsupportedLanguageError("csharp")

    out: list[Symbol] = []
    _capture_csharp_nodes(tree.root_node, out, None)
    return out


def _capture_csharp_nodes(node: object, out: list[Symbol], class_name: Optional[str]) -> None:
    """Recursively walk C# tree-sitter nodes."""
    node_type = node.type  # type: ignore[attr-defined]

    if node_type == "class_declaration":
        name_node = _get_child(node, "identifier")
        if name_node is not None:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            cn = name_node.text.decode("utf-8")  # type: ignore[attr-defined]
            out.append(Symbol(name=cn, start=start, end=end))
            for child in node.children:  # type: ignore[attr-defined]
                _capture_csharp_nodes(child, out, cn)
        return
    elif node_type in ("method_declaration", "constructor_declaration"):
        name_node = _get_child(node, "identifier")
        if name_node is not None and class_name:
            start = name_node.start_point[0] + 1  # type: ignore[attr-defined]
            end = node.end_point[0] + 1  # type: ignore[attr-defined]
            out.append(Symbol(name=f"{class_name}.{name_node.text.decode('utf-8')}", start=start, end=end))  # type: ignore[attr-defined]

    for child in node.children:  # type: ignore[attr-defined]
        _capture_csharp_nodes(child, out, class_name)


_CSHARP_CLASS_RE = re.compile(r"^(?:public\s+)?class\s+(\w+)", re.MULTILINE)
_CSHARP_METHOD_RE = re.compile(
    r"^(?:public|private|protected|internal|static|virtual|override)?\s*(?:\w+\s+)*(\w+)\s*\(",
    re.MULTILINE,
)


def _extract_csharp_regex(source: str) -> list[Symbol]:
    """Regex-based C# symbol extractor (fallback)."""
    out: list[Symbol] = []
    lines = source.splitlines()
    class_stack: list[str] = []

    for i, line in enumerate(lines, start=1):
        m = _CSHARP_CLASS_RE.match(line)
        if m:
            class_stack.append(m.group(1))
            out.append(Symbol(name=m.group(1), start=i, end=i))
            continue

        m = _CSHARP_METHOD_RE.match(line)
        if m and class_stack:
            out.append(Symbol(name=f"{class_stack[-1]}.{m.group(1)}", start=i, end=i))

    return out


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

_EXTRACTORS = {
    "typescript": extract_typescript_symbols,
    "tsx": extract_typescript_symbols,
    "javascript": extract_typescript_symbols,
    "jsx": extract_typescript_symbols,
    "mjs": extract_typescript_symbols,
    "go": extract_go_symbols,
    "rust": extract_rust_symbols,
    "java": extract_java_symbols,
    "c": extract_cpp_symbols,
    "cpp": extract_cpp_symbols,
    "csharp": extract_csharp_symbols,
}


def extract_symbols_for_language(source: str, language: str) -> list[Symbol]:
    """Extract symbols using the appropriate language extractor.

    Raises UnsupportedLanguageError if the language is not supported.
    """
    extractor = _EXTRACTORS.get(language)
    if extractor is None:
        raise UnsupportedLanguageError(f"unsupported language: {language}")
    # Pass language to TS/JS extractors so they use the right tree-sitter grammar
    if language in ("typescript", "tsx", "javascript", "jsx", "mjs"):
        return extractor(source, language=language)
    return extractor(source)