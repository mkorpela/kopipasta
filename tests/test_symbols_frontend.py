from pathlib import Path
from kopipasta.file import extract_symbols


def test_extract_symbols_js_function(tmp_path: Path):
    """Standard JS functions should be extracted with their signatures."""
    js_file = tmp_path / "utils.js"
    js_file.write_text(
        "export function calculateTotal(price, tax) {\n"
        "    const total = price + (price * tax);\n"
        "    return total;\n"
        "}\n"
    )
    assert extract_symbols(str(js_file)) == ["function calculateTotal(price, tax)"]


def test_extract_symbols_ts_function_with_types(tmp_path: Path):
    """TS functions should retain their type hints and return types."""
    ts_file = tmp_path / "api.ts"
    ts_file.write_text(
        "export async function fetchUser(id: string): Promise<User> {\n"
        "    const res = await fetch(`/api/users/${id}`);\n"
        "    return res.json();\n"
        "}\n"
    )
    assert extract_symbols(str(ts_file)) == [
        "async function fetchUser(id: string): Promise<User>"
    ]


def test_extract_symbols_jsx_functional_component(tmp_path: Path):
    """React components must strip massive JSX returns and internal hooks."""
    jsx_file = tmp_path / "Button.jsx"
    jsx_file.write_text(
        "import React, { useState } from 'react';\n"
        "export function Button({ label, onClick }) {\n"
        "    const [hover, setHover] = useState(false);\n"
        "    return (\n"
        "        <button onClick={onClick} onMouseEnter={() => setHover(true)}>\n"
        "            {label}\n"
        "        </button>\n"
        "    );\n"
        "}\n"
    )
    assert extract_symbols(str(jsx_file)) == ["function Button({ label, onClick })"]


def test_extract_symbols_tsx_arrow_component(tmp_path: Path):
    """Arrow function component assignments and Interfaces should be extracted."""
    tsx_file = tmp_path / "Card.tsx"
    tsx_file.write_text(
        "import React from 'react';\n"
        "interface CardProps {\n"
        "    title: string;\n"
        "}\n"
        "export const Card: React.FC<CardProps> = ({ title, children }) => {\n"
        "    return <div className='card'>{title}{children}</div>;\n"
        "};\n"
    )
    assert extract_symbols(str(tsx_file)) == [
        "interface CardProps",
        "const Card: React.FC<CardProps> = ({ title, children }) =>",
    ]


def test_extract_symbols_ts_types_and_interfaces(tmp_path: Path):
    """Types and Interfaces should be captured by name."""
    ts_file = tmp_path / "types.ts"
    ts_file.write_text(
        "export interface UserData {\n"
        "    id: number;\n"
        "    name: string;\n"
        "}\n"
        "export type Status = 'active' | 'inactive';\n"
    )
    assert extract_symbols(str(ts_file)) == ["interface UserData", "type Status"]


def test_extract_symbols_js_class_component(tmp_path: Path):
    """JS Classes should extract constructors and methods, similar to Python."""
    jsx_file = tmp_path / "ErrorBoundary.jsx"
    jsx_file.write_text(
        "class ErrorBoundary extends React.Component {\n"
        "    constructor(props) { super(props); this.state = { hasError: false }; }\n"
        "    static getDerivedStateFromError(error) { return { hasError: true }; }\n"
        "    componentDidCatch(error, info) { logErrorToMyService(error, info); }\n"
        "    render() { if (this.state.hasError) return <h1>Error</h1>; return this.props.children; }\n"
        "}\n"
    )
    assert extract_symbols(str(jsx_file)) == [
        "class ErrorBoundary(React.Component) [constructor, getDerivedStateFromError, componentDidCatch, render]"
    ]


def test_extract_symbols_jsdoc_extraction(tmp_path: Path):
    """JSDoc blocks should be appended as comments, just like Python docstrings."""
    ts_file = tmp_path / "hooks.ts"
    ts_file.write_text(
        "/**\n"
        " * Custom hook to manage authentication state.\n"
        " * @returns AuthContext\n"
        " */\n"
        "export function useAuth() {\n"
        "    return useContext(AuthContext);\n"
        "}\n"
    )
    assert extract_symbols(str(ts_file)) == [
        "function useAuth()  // Custom hook to manage authentication state."
    ]


def test_extract_symbols_ignore_internal_functions(tmp_path: Path):
    """Nested helper functions inside components/functions must be ignored."""
    js_file = tmp_path / "complex.js"
    js_file.write_text(
        "export function mainTask() {\n"
        "    function helper() {\n"
        "        return true;\n"
        "    }\n"
        "    const inlineArrow = () => false;\n"
        "    return helper();\n"
        "}\n"
    )
    assert extract_symbols(str(js_file)) == ["function mainTask()"]


def test_extract_symbols_react_hoc(tmp_path: Path):
    """Components wrapped in memo or forwardRef should be extracted."""
    jsx_file = tmp_path / "Button.jsx"
    jsx_file.write_text(
        "import { memo, forwardRef } from 'react';\n"
        "export const Button = memo(forwardRef((props, ref) => {\n"
        "    return <button ref={ref} {...props} />;\n"
        "}));\n"
    )
    assert extract_symbols(str(jsx_file)) == [
        "const Button = memo(forwardRef((props, ref) =>"
    ]


def test_extract_symbols_export_default(tmp_path: Path):
    """Default exports of functions or HOCs should be extracted."""
    app_file = tmp_path / "App.jsx"
    app_file.write_text(
        "export default function App({ user }) {\n"
        "    return <div>{user.name}</div>;\n"
        "}\n"
    )
    assert extract_symbols(str(app_file)) == ["function App({ user })"]

    footer_file = tmp_path / "Footer.jsx"
    footer_file.write_text(
        "export default memo(function Footer() {\n"
        "    return <footer>Hi</footer>;\n"
        "});\n"
    )
    assert extract_symbols(str(footer_file)) == ["memo(function Footer()"]
