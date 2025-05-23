# kopipasta/import_parser.py

import os
import re
import json
import ast
from typing import Dict, List, Optional, Set, Tuple

# --- Global Cache for tsconfig.json data ---
# Key: absolute path to tsconfig.json file
# Value: Tuple (absolute_base_url: Optional[str], alias_paths_map: Dict[str, List[str]])
_tsconfig_configs_cache: Dict[str, Tuple[Optional[str], Dict[str, List[str]]]] = {}


# --- TypeScript Alias and Import Resolution ---

def find_relevant_tsconfig_path(file_path_abs: str, project_root_abs: str) -> Optional[str]:
    """
    Finds the most relevant tsconfig.json by searching upwards from the file's directory,
    stopping at project_root_abs.
    Searches for 'tsconfig.json' first, then 'tsconfig.*.json' in each directory.
    """
    current_dir = os.path.dirname(os.path.normpath(file_path_abs))
    project_root_abs_norm = os.path.normpath(project_root_abs)

    while current_dir.startswith(project_root_abs_norm) and len(current_dir) >= len(project_root_abs_norm):
        potential_tsconfig = os.path.join(current_dir, "tsconfig.json")
        if os.path.isfile(potential_tsconfig):
            return os.path.normpath(potential_tsconfig)

        try:
            variant_tsconfigs = sorted([
                f for f in os.listdir(current_dir)
                if f.startswith("tsconfig.") and f.endswith(".json") and
                   os.path.isfile(os.path.join(current_dir, f))
            ])
            if variant_tsconfigs:
                return os.path.normpath(os.path.join(current_dir, variant_tsconfigs[0]))
        except OSError:
            pass

        if current_dir == project_root_abs_norm:
            break
        
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break
        current_dir = parent_dir
    return None


def load_tsconfig_config(tsconfig_path_abs: str) -> Tuple[Optional[str], Dict[str, List[str]]]:
    """
    Loads baseUrl and paths from a specific tsconfig.json.
    Caches results.
    Returns (absolute_base_url, paths_map).
    """
    if tsconfig_path_abs in _tsconfig_configs_cache:
        return _tsconfig_configs_cache[tsconfig_path_abs]

    if not os.path.isfile(tsconfig_path_abs):
        _tsconfig_configs_cache[tsconfig_path_abs] = (None, {})
        return None, {}
        
    try:
        with open(tsconfig_path_abs, 'r', encoding='utf-8') as f:
            content = f.read()
            content = re.sub(r"//.*?\n", "\n", content) 
            content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
            config = json.loads(content)
        
        compiler_options = config.get("compilerOptions", {})
        tsconfig_dir = os.path.dirname(tsconfig_path_abs)
        base_url_from_config = compiler_options.get("baseUrl", ".") 
        abs_base_url = os.path.normpath(os.path.join(tsconfig_dir, base_url_from_config))
        
        paths = compiler_options.get("paths", {})
        processed_paths = {key: (val if isinstance(val, list) else [val]) for key, val in paths.items()}

        # print(f"DEBUG: Loaded config from {os.path.relpath(tsconfig_path_abs)}: effective abs_baseUrl='{abs_base_url}', {len(processed_paths)} path alias(es).")
        _tsconfig_configs_cache[tsconfig_path_abs] = (abs_base_url, processed_paths)
        return abs_base_url, processed_paths
    except Exception as e:
        print(f"Warning: Could not parse {os.path.relpath(tsconfig_path_abs)}: {e}")
        _tsconfig_configs_cache[tsconfig_path_abs] = (None, {})
        return None, {}


def _probe_ts_path_candidates(candidate_base_path_abs: str) -> Optional[str]:
    """
    Given a candidate base absolute path, tries to find a corresponding file.
    """
    possible_extensions = ['.ts', '.tsx', '.js', '.jsx', '.json']
    
    if os.path.isfile(candidate_base_path_abs):
        return candidate_base_path_abs

    stem, original_ext = os.path.splitext(candidate_base_path_abs)
    base_for_ext_check = stem if original_ext.lower() in possible_extensions else candidate_base_path_abs

    for ext in possible_extensions:
        path_with_ext = base_for_ext_check + ext
        if os.path.isfile(path_with_ext):
            return path_with_ext
            
    if os.path.isdir(base_for_ext_check):
        for ext in possible_extensions:
            index_file_path = os.path.join(base_for_ext_check, "index" + ext)
            if os.path.isfile(index_file_path):
                return index_file_path
    return None


def resolve_ts_import_path(
    import_str: str, 
    current_file_dir_abs: str, 
    abs_base_url: Optional[str], 
    alias_map: Dict[str, List[str]]
) -> Optional[str]:
    """
    Resolves a TypeScript import string to an absolute file path.
    """
    candidate_targets_abs: List[str] = []
    sorted_alias_keys = sorted(alias_map.keys(), key=len, reverse=True)
    alias_matched_and_resolved = False

    for alias_pattern in sorted_alias_keys:
        alias_prefix_pattern = alias_pattern.replace("/*", "")
        if import_str.startswith(alias_prefix_pattern):
            import_suffix = import_str[len(alias_prefix_pattern):]
            for mapping_path_template_list in alias_map[alias_pattern]:
                for mapping_path_template in (mapping_path_template_list if isinstance(mapping_path_template_list, list) else [mapping_path_template_list]):
                    if "/*" in alias_pattern :
                        resolved_relative_to_base = mapping_path_template.replace("*", import_suffix, 1)
                    else:
                        resolved_relative_to_base = mapping_path_template
                    if abs_base_url:
                        abs_candidate = os.path.normpath(os.path.join(abs_base_url, resolved_relative_to_base))
                        candidate_targets_abs.append(abs_candidate)
                    else:
                        print(f"Warning: TS Alias '{alias_pattern}' used, but no abs_base_url for context of '{current_file_dir_abs}'.")
            if candidate_targets_abs:
                alias_matched_and_resolved = True
                break

    if not alias_matched_and_resolved and import_str.startswith('.'):
        abs_candidate = os.path.normpath(os.path.join(current_file_dir_abs, import_str))
        candidate_targets_abs.append(abs_candidate)
    elif not alias_matched_and_resolved and abs_base_url and not import_str.startswith('.'):
        abs_candidate = os.path.normpath(os.path.join(abs_base_url, import_str))
        candidate_targets_abs.append(abs_candidate)

    for cand_abs_path in candidate_targets_abs:
        resolved_file = _probe_ts_path_candidates(cand_abs_path)
        if resolved_file:
            return resolved_file
    return None


def parse_typescript_imports(
    file_content: str, 
    file_path_abs: str,
    project_root_abs: str
) -> Set[str]:
    resolved_imports_abs_paths = set()
    relevant_tsconfig_abs_path = find_relevant_tsconfig_path(file_path_abs, project_root_abs)
    
    abs_base_url, alias_map = None, {}
    if relevant_tsconfig_abs_path:
        abs_base_url, alias_map = load_tsconfig_config(relevant_tsconfig_abs_path)
    else:
        # print(f"Warning: No tsconfig.json found for {os.path.relpath(file_path_abs, project_root_abs)}. Import resolution might be limited.")
        abs_base_url = project_root_abs 

    import_regex = re.compile(
        r"""
        (?:import|export)
        (?:\s+(?:type\s+)?(?:[\w*{}\s,\[\]:\."'`-]+)\s+from)?
        \s*['"`]([^'"\n`]+?)['"`]
        |require\s*\(\s*['"`]([^'"\n`]+?)['"`]\s*\)
        |import\s*\(\s*['"`]([^'"\n`]+?)['"`]\s*\)
        """,
        re.VERBOSE | re.MULTILINE
    )
    
    current_file_dir_abs = os.path.dirname(file_path_abs)

    for match in import_regex.finditer(file_content):
        import_str_candidate = next((g for g in match.groups() if g is not None), None)
        if import_str_candidate:
            is_likely_external = (
                not import_str_candidate.startswith(('.', '/')) and
                not any(import_str_candidate.startswith(alias_pattern.replace("/*", "")) for alias_pattern in alias_map) and
                not (abs_base_url and os.path.exists(os.path.join(abs_base_url, import_str_candidate))) and
                (import_str_candidate.count('/') == 0 or (import_str_candidate.startswith('@') and import_str_candidate.count('/') == 1)) and
                '.' not in import_str_candidate.split('/')[0]
            )
            if is_likely_external:
                continue

            resolved_abs_path = resolve_ts_import_path(
                import_str_candidate, 
                current_file_dir_abs, 
                abs_base_url, 
                alias_map
            )
            
            if resolved_abs_path:
                norm_resolved_path = os.path.normpath(resolved_abs_path)
                if norm_resolved_path.startswith(os.path.normpath(project_root_abs)):
                    resolved_imports_abs_paths.add(norm_resolved_path)
    return resolved_imports_abs_paths


# --- Python Import Resolution ---

def resolve_python_import(
    module_name_parts: List[str], 
    current_file_dir_abs: str, 
    project_root_abs: str, 
    level: int
) -> Optional[str]:
    base_path_to_search = ""
    if level > 0:
        base_path_to_search = current_file_dir_abs
        for _ in range(level - 1):
            base_path_to_search = os.path.dirname(base_path_to_search)
    else:
        base_path_to_search = project_root_abs

    candidate_rel_path = os.path.join(*module_name_parts)
    potential_abs_path = os.path.join(base_path_to_search, candidate_rel_path)
    
    py_file = potential_abs_path + ".py"
    if os.path.isfile(py_file):
        return os.path.normpath(py_file)
    
    init_file = os.path.join(potential_abs_path, "__init__.py")
    if os.path.isdir(potential_abs_path) and os.path.isfile(init_file):
        return os.path.normpath(init_file)

    if level == 0 and base_path_to_search == project_root_abs:
        src_base_path = os.path.join(project_root_abs, "src")
        if os.path.isdir(src_base_path):
            potential_abs_path_src = os.path.join(src_base_path, candidate_rel_path)
            py_file_src = potential_abs_path_src + ".py"
            if os.path.isfile(py_file_src):
                return os.path.normpath(py_file_src)
            init_file_src = os.path.join(potential_abs_path_src, "__init__.py")
            if os.path.isdir(potential_abs_path_src) and os.path.isfile(init_file_src):
                return os.path.normpath(init_file_src)
    return None


def parse_python_imports(file_content: str, file_path_abs: str, project_root_abs: str) -> Set[str]:
    resolved_imports = set()
    current_file_dir_abs = os.path.dirname(file_path_abs)
    
    try:
        tree = ast.parse(file_content, filename=file_path_abs)
    except SyntaxError:
        # print(f"Warning: Syntax error in {file_path_abs}, cannot parse Python imports.")
        return resolved_imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_parts = alias.name.split('.')
                resolved = resolve_python_import(module_parts, current_file_dir_abs, project_root_abs, level=0)
                if resolved and os.path.exists(resolved) and os.path.normpath(resolved).startswith(os.path.normpath(project_root_abs)):
                    resolved_imports.add(os.path.normpath(resolved))
        elif isinstance(node, ast.ImportFrom):
            level_to_resolve = node.level
            if node.module:
                module_parts = node.module.split('.')
                resolved = resolve_python_import(module_parts, current_file_dir_abs, project_root_abs, level_to_resolve)
                if resolved and os.path.exists(resolved) and os.path.normpath(resolved).startswith(os.path.normpath(project_root_abs)):
                    resolved_imports.add(os.path.normpath(resolved))
            else:
                for alias in node.names:
                    item_name_parts = alias.name.split('.')
                    resolved = resolve_python_import(item_name_parts, current_file_dir_abs, project_root_abs, level=level_to_resolve)
                    if resolved and os.path.exists(resolved) and os.path.normpath(resolved).startswith(os.path.normpath(project_root_abs)):
                        resolved_imports.add(os.path.normpath(resolved))
    return resolved_imports

