"""Tests for merge_train.lang_extractors: multi-language symbol extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from merge_train.lang_extractors import (
    extract_csharp_symbols,
    extract_cpp_symbols,
    extract_go_symbols,
    extract_java_symbols,
    extract_rust_symbols,
    extract_symbols_for_language,
    extract_typescript_symbols,
)
from merge_train.symbols import Symbol

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------------- #
# TypeScript / JavaScript
# --------------------------------------------------------------------------- #


def test_extract_typescript_symbols_function():
    src = "function foo() {\n    return 1;\n}\n"
    syms = extract_typescript_symbols(src)
    assert any(s.name == "foo" for s in syms)


def test_extract_typescript_symbols_class():
    src = "class MyClass {\n    method() {}\n}\n"
    syms = extract_typescript_symbols(src)
    names = [s.name for s in syms]
    assert "MyClass" in names


def test_extract_typescript_from_fixture():
    src = read_fixture("sample.ts")
    syms = extract_typescript_symbols(src)
    names = [s.name for s in syms]
    assert "add" in names
    assert "Calculator" in names
    assert "fetchUser" in names


def test_extract_typescript_ranges():
    src = "function foo() {\n    return 1;\n}\n\nfunction bar() {\n    return 2;\n}\n"
    syms = extract_typescript_symbols(src)
    foo = next(s for s in syms if s.name == "foo")
    bar = next(s for s in syms if s.name == "bar")
    # start line is correctly identified
    assert foo.start == 1
    assert bar.start == 5
    # end line: with regex fallback, end == start (no block tracking)


def test_extract_typescript_arrow_function():
    src = "export const multiply = (x: number, y: number): number => x * y;\n"
    syms = extract_typescript_symbols(src)
    # Arrow functions with const binding are captured as "multiply"
    assert any(s.name == "multiply" for s in syms)


# --------------------------------------------------------------------------- #
# Go
# --------------------------------------------------------------------------- #


def test_extract_go_symbols_function():
    src = "func Add(a int, b int) int {\n    return a + b\n}\n"
    syms = extract_go_symbols(src)
    assert any(s.name == "Add" for s in syms)


def test_extract_go_symbols_method():
    src = "func (c *Config) GetHost() string {\n    return c.Host\n}\n"
    syms = extract_go_symbols(src)
    assert any(s.name == "Config.GetHost" for s in syms)


def test_extract_go_symbols_type():
    src = "type User struct {\n    Name string\n}\n"
    syms = extract_go_symbols(src)
    assert any(s.name == "User" for s in syms)


def test_extract_go_from_fixture():
    src = read_fixture("sample.go")
    syms = extract_go_symbols(src)
    names = [s.name for s in syms]
    assert "Add" in names
    assert "Config.GetHost" in names
    assert "User" in names
    assert "User.Greet" in names
    assert "Calculator.Add" in names


def test_extract_go_ranges():
    src = "func foo() {}\n\nfunc bar() {}\n"
    syms = extract_go_symbols(src)
    foo = next(s for s in syms if s.name == "foo")
    bar = next(s for s in syms if s.name == "bar")
    assert foo.start == 1
    assert bar.start == 3


# --------------------------------------------------------------------------- #
# Rust
# --------------------------------------------------------------------------- #


def test_extract_rust_symbols_function():
    src = "fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
    syms = extract_rust_symbols(src)
    assert any(s.name == "add" for s in syms)


def test_extract_rust_symbols_struct():
    src = "struct Calculator {\n    value: i32,\n}\n"
    syms = extract_rust_symbols(src)
    assert any(s.name == "Calculator" for s in syms)


def test_extract_rust_symbols_impl_method():
    # impl block with struct defined first - both type name and method emitted
    src = "struct Calculator {\n    value: i32,\n}\n\nimpl Calculator {\n    fn add(&mut self, amount: i32) {}\n}\n"
    syms = extract_rust_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert any("Calculator.add" in n for n in names)


def test_extract_rust_from_fixture():
    src = read_fixture("sample.rs")
    syms = extract_rust_symbols(src)
    names = [s.name for s in syms]
    # Calculator is defined as struct before impl
    assert "Calculator" in names
    # add is a top-level standalone function (not in impl)
    assert "add" in names


def test_extract_rust_ranges():
    src = "fn foo() {}\n\nfn bar() {}\n"
    syms = extract_rust_symbols(src)
    foo = next(s for s in syms if s.name == "foo")
    bar = next(s for s in syms if s.name == "bar")
    assert foo.start == 1
    assert bar.start == 3


# --------------------------------------------------------------------------- #
# Java
# --------------------------------------------------------------------------- #


def test_extract_java_symbols_class():
    src = "public class Calculator {\n    public void add(int amount) {}\n}\n"
    syms = extract_java_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert any("Calculator.add" in n for n in names)


def test_extract_java_symbols_static_method():
    src = "public class Calc {\n    public static int multiply(int a, int b) {\n        return a * b;\n    }\n}\n"
    syms = extract_java_symbols(src)
    names = [s.name for s in syms]
    assert "Calc" in names
    assert any("Calc.multiply" in n for n in names)


def test_extract_java_from_fixture():
    src = read_fixture("sample.java")
    syms = extract_java_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert "Config" in names


def test_extract_java_ranges():
    src = "class Foo {}\n\nclass Bar {}\n"
    syms = extract_java_symbols(src)
    foo = next(s for s in syms if s.name == "Foo")
    bar = next(s for s in syms if s.name == "Bar")
    assert foo.start == 1
    assert bar.start == 3


# --------------------------------------------------------------------------- #
# C/C++
# --------------------------------------------------------------------------- #


def test_extract_cpp_symbols_function():
    src = "int add(int a, int b) {\n    return a + b;\n}\n"
    syms = extract_cpp_symbols(src)
    assert any(s.name == "add" for s in syms)


def test_extract_cpp_symbols_class():
    src = "class Calculator {\npublic:\n    void add(int amount) {}\n};\n"
    syms = extract_cpp_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert any("Calculator.add" in n for n in names)


def test_extract_cpp_from_fixture():
    src = read_fixture("sample.cpp")
    syms = extract_cpp_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert "Config" in names


def test_extract_cpp_ranges():
    src = "void foo() {}\n\nvoid bar() {}\n"
    syms = extract_cpp_symbols(src)
    foo = next(s for s in syms if s.name == "foo")
    bar = next(s for s in syms if s.name == "bar")
    assert foo.start == 1
    assert bar.start == 3


# --------------------------------------------------------------------------- #
# C#
# --------------------------------------------------------------------------- #


def test_extract_csharp_symbols_class():
    src = "public class Calculator {\n    public void Add(int amount) {}\n}\n"
    syms = extract_csharp_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert any("Calculator.Add" in n for n in names)


def test_extract_csharp_from_fixture():
    src = read_fixture("sample.cs")
    syms = extract_csharp_symbols(src)
    names = [s.name for s in syms]
    assert "Calculator" in names
    assert "Config" in names


def test_extract_csharp_ranges():
    src = "class Foo {}\n\nclass Bar {}\n"
    syms = extract_csharp_symbols(src)
    foo = next(s for s in syms if s.name == "Foo")
    bar = next(s for s in syms if s.name == "Bar")
    assert foo.start == 1
    assert bar.start == 3


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


def test_extract_symbols_for_language_typescript():
    src = "function foo() {}"
    syms = extract_symbols_for_language(src, "typescript")
    assert any(s.name == "foo" for s in syms)


def test_extract_symbols_for_language_go():
    src = "func foo() {}"
    syms = extract_symbols_for_language(src, "go")
    assert any(s.name == "foo" for s in syms)


def test_extract_symbols_for_language_rust():
    src = "fn foo() {}"
    syms = extract_symbols_for_language(src, "rust")
    assert any(s.name == "foo" for s in syms)


def test_extract_symbols_for_language_java():
    src = "class Foo {}"
    syms = extract_symbols_for_language(src, "java")
    assert any(s.name == "Foo" for s in syms)


def test_extract_symbols_for_language_cpp():
    src = "void foo() {}"
    syms = extract_symbols_for_language(src, "cpp")
    assert any(s.name == "foo" for s in syms)


def test_extract_symbols_for_language_csharp():
    src = "class Foo {}"
    syms = extract_symbols_for_language(src, "csharp")
    assert any(s.name == "Foo" for s in syms)


def test_extract_symbols_for_language_c():
    src = "void foo() {}"
    syms = extract_symbols_for_language(src, "c")
    assert any(s.name == "foo" for s in syms)


def test_extract_symbols_for_language_unknown_raises():
    from merge_train.symbols import UnsupportedLanguageError

    with pytest.raises(UnsupportedLanguageError):
        extract_symbols_for_language("x = 1", "unknown_language")


def test_extract_java_regex_indentation_and_class_pop():
    from merge_train.lang_extractors import _extract_java_regex

    src = (
        "  public class Foo {\n"
        "      public void bar() {\n"
        "          if (x) {\n"
        "          }\n"
        "      }\n"
        "  }\n"
        "  class Bar {\n"
        "      public void baz() {}\n"
        "  }\n"
    )
    syms = _extract_java_regex(src)
    names = [s.name for s in syms]
    assert "Foo" in names
    assert "Foo.bar" in names
    assert "Bar" in names
    assert "Bar.baz" in names
    assert "Foo.baz" not in names
    assert "Bar.bar" not in names


def test_extract_csharp_regex_indentation_and_class_pop():
    from merge_train.lang_extractors import _extract_csharp_regex

    src = (
        "  public class Foo\n"
        "  {\n"
        "      public void Bar()\n"
        "      {\n"
        "          if (x)\n"
        "          {\n"
        "          }\n"
        "      }\n"
        "  }\n"
        "  class Bar\n"
        "  {\n"
        "      public void Baz() {}\n"
        "  }\n"
    )
    syms = _extract_csharp_regex(src)
    names = [s.name for s in syms]
    assert "Foo" in names
    assert "Foo.Bar" in names
    assert "Bar" in names
    assert "Bar.Baz" in names
    assert "Foo.Baz" not in names
    assert "Bar.Bar" not in names


# --------------------------------------------------------------------------- #
# TypeScript -- additional coverage: interface, type alias, export, async arrow
# --------------------------------------------------------------------------- #


def test_extract_typescript_interface():
    src = "interface UserService {\n    getUser(id: string): User;\n}\n"
    syms = extract_typescript_symbols(src)
    names = [s.name for s in syms]
    assert "UserService" in names


def test_extract_typescript_type_alias():
    src = "type UserId = string;\ntype Handler = (req: Request) => void;\n"
    syms = extract_typescript_symbols(src)
    names = [s.name for s in syms]
    assert "UserId" in names
    assert "Handler" in names


def test_extract_typescript_exported_function():
    src = (
        "export function processRequest(req: Request): Response {\n    return {};\n}\n"
    )
    syms = extract_typescript_symbols(src)
    assert any(s.name == "processRequest" for s in syms)


def test_extract_typescript_async_arrow_function():
    src = (
        "const fetchData = async (url: string) => {\n    return await fetch(url);\n};\n"
    )
    syms = extract_typescript_symbols(src)
    assert any(s.name == "fetchData" for s in syms)


def test_extract_typescript_class_and_interface_together():
    src = (
        "interface Greetable {\n"
        "    greet(): string;\n"
        "}\n"
        "class Greeter implements Greetable {\n"
        "    greet() { return 'hello'; }\n"
        "}\n"
    )
    syms = extract_typescript_symbols(src)
    names = [s.name for s in syms]
    assert "Greetable" in names
    assert "Greeter" in names


# --------------------------------------------------------------------------- #
# Go -- additional coverage: plain func, pointer receiver, interface type
# --------------------------------------------------------------------------- #


def test_extract_go_plain_function_no_receiver():
    src = "func Compute(x int, y int) int {\n    return x + y\n}\n"
    syms = extract_go_symbols(src)
    assert any(s.name == "Compute" for s in syms)


def test_extract_go_method_pointer_receiver():
    src = (
        "func (s *Server) HandleRequest(w http.ResponseWriter, r *http.Request) {\n}\n"
    )
    syms = extract_go_symbols(src)
    assert any(s.name == "Server.HandleRequest" for s in syms)


def test_extract_go_interface_type():
    src = "type Stringer interface {\n    String() string\n}\n"
    syms = extract_go_symbols(src)
    assert any(s.name == "Stringer" for s in syms)


def test_extract_go_struct_and_interface_together():
    src = (
        "type Writer interface {\n"
        "    Write(p []byte) (int, error)\n"
        "}\n"
        "type FileWriter struct {\n"
        "    path string\n"
        "}\n"
    )
    syms = extract_go_symbols(src)
    names = [s.name for s in syms]
    assert "Writer" in names
    assert "FileWriter" in names


# --------------------------------------------------------------------------- #
# Routing -- .ts / .tsx / .go file extensions routed correctly
# --------------------------------------------------------------------------- #


def test_language_for_path_ts():
    from merge_train.symbols import language_for_path

    assert language_for_path("src/utils/helpers.ts") == "typescript"


def test_language_for_path_tsx():
    from merge_train.symbols import language_for_path

    assert language_for_path("components/Button.tsx") == "tsx"


def test_language_for_path_go():
    from merge_train.symbols import language_for_path

    assert language_for_path("cmd/server/main.go") == "go"


def test_is_supported_path_ts_tsx_go():
    from merge_train.symbols import is_supported_path

    assert is_supported_path("app.ts") is True
    assert is_supported_path("App.tsx") is True
    assert is_supported_path("main.go") is True


def test_extract_symbols_for_language_tsx():
    src = "class AppComponent {\n    render() { return null; }\n}\n"
    syms = extract_symbols_for_language(src, "tsx")
    assert any(s.name == "AppComponent" for s in syms)


def test_extract_symbols_for_language_ts_interface():
    src = "interface Config {\n    host: string;\n    port: number;\n}\n"
    syms = extract_symbols_for_language(src, "typescript")
    assert any(s.name == "Config" for s in syms)


def test_extract_symbols_for_language_go_func_and_type():
    src = "type Service struct {}\nfunc NewService() *Service { return &Service{} }\n"
    syms = extract_symbols_for_language(src, "go")
    names = [s.name for s in syms]
    assert "Service" in names
    assert "NewService" in names
