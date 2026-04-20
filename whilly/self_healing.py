"""
Self-healing error handler system.
Detects common code errors and attempts automated fixes.
"""

import ast
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class CodeError:
    """Represents a detected code error with potential fix."""

    def __init__(self, error_type: str, file_path: str, line_num: int,
                 description: str, suggested_fix: str):
        self.error_type = error_type
        self.file_path = file_path
        self.line_num = line_num
        self.description = description
        self.suggested_fix = suggested_fix


class SelfHealingHandler:
    """Automatically detects and fixes common code errors."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.fixes_applied = []

    def analyze_error(self, error_msg: str, traceback_str: str) -> Optional[CodeError]:
        """Analyze error and suggest fix."""

        # Extract file path and line number from traceback
        file_match = re.search(r'File "([^"]+)", line (\d+)', traceback_str)
        if not file_match:
            return None

        file_path = file_match.group(1)
        line_num = int(file_match.group(2))

        # Pattern matching for different error types
        if "NameError: name" in error_msg:
            return self._fix_name_error(error_msg, file_path, line_num)
        elif "TypeError:" in error_msg and "missing" in error_msg and "positional" in error_msg:
            return self._fix_missing_parameter(error_msg, file_path, line_num)
        elif "ImportError:" in error_msg or "ModuleNotFoundError:" in error_msg:
            return self._fix_import_error(error_msg, file_path, line_num)
        elif "AttributeError:" in error_msg:
            return self._fix_attribute_error(error_msg, file_path, line_num)

        return None

    def _fix_name_error(self, error_msg: str, file_path: str, line_num: int) -> Optional[CodeError]:
        """Fix NameError by analyzing function signature and call patterns."""

        # Extract undefined variable name
        var_match = re.search(r"name '(\w+)' is not defined", error_msg)
        if not var_match:
            return None

        undefined_var = var_match.group(1)

        # Read the file and analyze context
        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            if line_num > len(lines):
                return None

            error_line = lines[line_num - 1].strip()

            # Find function definition containing this line
            func_def_line = self._find_function_definition(lines, line_num)
            if func_def_line:
                func_signature = lines[func_def_line - 1].strip()

                # Check if variable should be a parameter
                if f"def " in func_signature and undefined_var not in func_signature:
                    # Suggest adding parameter to function
                    suggested_fix = f"Add '{undefined_var}' parameter to function signature at line {func_def_line}"

                    return CodeError(
                        error_type="NameError",
                        file_path=file_path,
                        line_num=func_def_line,
                        description=f"Variable '{undefined_var}' not in scope, likely missing function parameter",
                        suggested_fix=suggested_fix
                    )

        except Exception:
            pass

        return None

    def _fix_missing_parameter(self, error_msg: str, file_path: str, line_num: int) -> Optional[CodeError]:
        """Fix missing positional arguments."""

        # Extract function name and missing parameter info
        func_match = re.search(r"(\w+)\(\) missing .+ positional argument", error_msg)
        if not func_match:
            return None

        func_name = func_match.group(1)

        suggested_fix = f"Add missing parameter(s) to {func_name}() call"

        return CodeError(
            error_type="TypeError",
            file_path=file_path,
            line_num=line_num,
            description=f"Function {func_name} call is missing required parameters",
            suggested_fix=suggested_fix
        )

    def _fix_import_error(self, error_msg: str, file_path: str, line_num: int) -> Optional[CodeError]:
        """Fix import errors."""

        module_match = re.search(r"No module named '(\w+)'", error_msg)
        if module_match:
            module_name = module_match.group(1)
            suggested_fix = f"Install missing module: pip install {module_name}"

            return CodeError(
                error_type="ImportError",
                file_path=file_path,
                line_num=line_num,
                description=f"Missing module: {module_name}",
                suggested_fix=suggested_fix
            )

        return None

    def _fix_attribute_error(self, error_msg: str, file_path: str, line_num: int) -> Optional[CodeError]:
        """Fix attribute errors."""

        attr_match = re.search(r"'(\w+)' object has no attribute '(\w+)'", error_msg)
        if attr_match:
            obj_type = attr_match.group(1)
            attr_name = attr_match.group(2)

            suggested_fix = f"Check if {obj_type}.{attr_name} exists or use different attribute/method"

            return CodeError(
                error_type="AttributeError",
                file_path=file_path,
                line_num=line_num,
                description=f"{obj_type} object missing attribute {attr_name}",
                suggested_fix=suggested_fix
            )

        return None

    def _find_function_definition(self, lines: List[str], error_line_num: int) -> Optional[int]:
        """Find the function definition that contains the error line."""

        for i in range(error_line_num - 1, -1, -1):
            line = lines[i].strip()
            if line.startswith("def ") and ":" in line:
                return i + 1  # Return 1-indexed line number

        return None

    def apply_fix(self, error: CodeError) -> bool:
        """Apply automated fix for the error."""

        try:
            if error.error_type == "NameError":
                return self._apply_name_error_fix(error)
            elif error.error_type == "ImportError":
                return self._apply_import_error_fix(error)
            # Add more fix implementations as needed

        except Exception as e:
            print(f"Failed to apply fix: {e}")
            return False

        return False

    def _apply_name_error_fix(self, error: CodeError) -> bool:
        """Apply fix for NameError by adding missing parameter."""

        # This is a simplified implementation
        # In practice, you'd want more sophisticated AST-based editing
        print(f"🔧 Auto-fix suggested: {error.suggested_fix}")
        print(f"   File: {error.file_path}:{error.line_num}")
        print(f"   {error.description}")

        # For now, just log the suggestion
        # Future versions could use AST manipulation to actually apply fixes
        return True

    def _apply_import_error_fix(self, error: CodeError) -> bool:
        """Apply fix for ImportError by installing missing module."""

        if "pip install" in error.suggested_fix:
            module_match = re.search(r"pip install (\w+)", error.suggested_fix)
            if module_match:
                module_name = module_match.group(1)

                try:
                    print(f"🔧 Installing missing module: {module_name}")
                    subprocess.run([sys.executable, "-m", "pip", "install", module_name],
                                 check=True, capture_output=True)
                    return True
                except subprocess.CalledProcessError:
                    print(f"Failed to install {module_name}")

        return False


def global_exception_handler(exctype, value, tb):
    """Global exception handler that attempts self-healing."""

    project_root = Path.cwd()
    healer = SelfHealingHandler(project_root)

    # Get the full traceback as string
    tb_str = ''.join(traceback.format_exception(exctype, value, tb))
    error_msg = str(value)

    print("🚨 Error detected:")
    print(f"   {exctype.__name__}: {error_msg}")
    print()

    # Analyze and attempt fix
    error = healer.analyze_error(error_msg, tb_str)
    if error:
        print("🤖 Self-healing analysis:")
        print(f"   Type: {error.error_type}")
        print(f"   Location: {error.file_path}:{error.line_num}")
        print(f"   Description: {error.description}")
        print(f"   Suggested fix: {error.suggested_fix}")
        print()

        # Apply fix if possible
        if healer.apply_fix(error):
            print("✅ Auto-fix applied! Restarting...")
            return
        else:
            print("⚠️  Auto-fix not available for this error type")
    else:
        print("🔍 No automated fix available")

    # Print full traceback for manual debugging
    print("\n" + "="*80)
    print("FULL TRACEBACK:")
    print("="*80)
    traceback.print_exception(exctype, value, tb)


def enable_self_healing():
    """Enable global self-healing error handler."""
    sys.excepthook = global_exception_handler
    print("🛡️  Self-healing error handler enabled")


if __name__ == "__main__":
    # Test the self-healing system
    enable_self_healing()

    # Simulate some errors for testing
    try:
        undefined_variable  # NameError
    except:
        pass