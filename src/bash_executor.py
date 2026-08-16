import logging
import os
import re
import subprocess
import sys
import warnings
from hashlib import md5
from pathlib import Path
from string import Template
from types import SimpleNamespace
from typing import Any, Callable, ClassVar, Dict, List, Optional, Union
from typing_extensions import ParamSpec

from autogen.coding.func_with_reqs import (
    FunctionWithRequirements,
    FunctionWithRequirementsStr,
    _build_python_functions_file,
    to_stub,
)
from autogen.code_utils import PYTHON_VARIANTS, TIMEOUT_MSG, WIN32, _cmd
from autogen.coding import LocalCommandLineCodeExecutor
from autogen.coding.base import CodeBlock, CodeExecutor, CodeExtractor, CommandLineCodeResult
from autogen.coding.markdown_code_extractor import MarkdownCodeExtractor
from autogen.coding.utils import _get_file_name_from_content, silence_pip

import tools
import langfuse_tracing as tracing


class BashCodeExecutor(LocalCommandLineCodeExecutor):
    def sanitize_command(self, lang: str, code: str) -> None:
        """
        Sanitize the code block to prevent dangerous commands.
        This approach acknowledges that while Docker or similar
        containerization/sandboxing technologies provide a robust layer of security,
        not all users may have Docker installed or may choose not to use it.
        Therefore, having a baseline level of protection helps mitigate risks for users who,
        either out of choice or necessity, run code outside of a sandboxed environment.
        """
        dangerous_patterns = [
            # (r"\brm\s+-rf\b", "Use of 'rm -rf' command is not allowed."),
            (r"\bmv\b.*?\s+/dev/null", "Moving files to /dev/null is not allowed."),
            (r"\bdd\b", "Use of 'dd' command is not allowed."),
            (r">\s*/dev/sd[a-z][1-9]?", "Overwriting disk blocks directly is not allowed."),
            (r":\(\)\{\s*:\|\:&\s*\};:", "Fork bombs are not allowed."),
        ]
        if lang in ["bash", "shell", "sh"]:
            for pattern, message in dangerous_patterns:
                if re.search(pattern, code):
                    raise ValueError(f"Potentially dangerous command detected: {message}")
                
    def _execute_code_dont_check_setup(self, code_blocks: List[CodeBlock]) -> CommandLineCodeResult:
        logs_all = ""
        file_names = []
        for code_block in code_blocks:
            lang, code = code_block.language, code_block.code
            lang = lang.lower()

            self.sanitize_command(lang, code)
            code = silence_pip(code, lang)

            if lang in PYTHON_VARIANTS:
                lang = "python"

            if WIN32 and lang in ["sh", "shell"]:
                lang = "ps1"

            if lang not in self.SUPPORTED_LANGUAGES:
                # In case the language is not supported, we return an error message.
                exitcode = 1
                logs_all += "\n" + f"unknown language {lang}"
                break

            execute_code = self.execution_policies.get(lang, False)
            try:
                # Check if there is a filename comment
                filename = _get_file_name_from_content(code, self._work_dir)
            except ValueError:
                return CommandLineCodeResult(exit_code=1, output="Filename is not in the workspace")

            if filename is None:
                # create a file with an automatically generated name
                code_hash = md5(code.encode()).hexdigest()
                filename = f"tmp_code_{code_hash}.{'py' if lang.startswith('python') else lang}"
            filename = filename.replace("/", "_")  # Only modify the filename
            written_file = (self._work_dir / filename).resolve()

            with written_file.open("w", encoding="utf-8") as f:
                f.write(code)
            file_names.append(written_file)
            if not execute_code:
                # Just return a message that the file is saved.
                logs_all += f"Code saved to {str(written_file)}\n"
                exitcode = 0
                continue

            program = _cmd(lang)
            cmd = [program, str(written_file.absolute())]
            env = os.environ.copy()

            if self._virtual_env_context:
                virtual_env_abs_path = os.path.abspath(self._virtual_env_context.bin_path)
                path_with_virtualenv = rf"{virtual_env_abs_path}{os.pathsep}{env['PATH']}"
                env["PATH"] = path_with_virtualenv
                if WIN32:
                    activation_script = os.path.join(virtual_env_abs_path, "activate.bat")
                    cmd = [activation_script, "&&", *cmd]

            # A span per command: what the agent ran, and how it went. This is
            # where most of a failed build is explained, so it is worth having
            # next to the LLM calls in the trace.
            with tracing.step("bash", as_type="tool", input=code,
                              metadata={"language": lang, "timeout": self._timeout}) as cmd_span:
                try:
                    result = subprocess.run(
                        cmd, cwd=self._work_dir, capture_output=True, text=True, timeout=float(self._timeout), env=env
                    )
                except subprocess.TimeoutExpired:
                    tracing.update(cmd_span, output=TIMEOUT_MSG, level="WARNING",
                                   status_message="timed out")
                    logs_all += "\n" + TIMEOUT_MSG
                    # Same exit code as the timeout command on linux.
                    exitcode = 124
                    break

                tracing.update(
                    cmd_span,
                    output=result.stdout if result.returncode == 0 else result.stderr,
                    metadata={"exit_code": result.returncode},
                    level="ERROR" if result.returncode != 0 else None,
                )


            ###########################################
            ##            My Modifications           ##
            ###########################################
            exitcode = result.returncode
            if exitcode == 0:
                if 'ls ' in code:
                    logs_all += result.stdout
                # if 'cat' in code:
                #     logs_all += tools.summarize_text(result.stdout)
                else:
                    logs_all += 'Ran successfully, '
            if exitcode != 0:
                logs_all += result.stderr
            ###########################################

            if exitcode != 0:
                break

        code_file = str(file_names[0]) if len(file_names) > 0 else None
        return CommandLineCodeResult(exit_code=exitcode, output=logs_all, code_file=code_file)



if __name__ == "__main__":
    # Example Usage
    # code_blocks = [
    #     CodeBlock(language="bash", code="# Set PYTHONPATH to include the V8 tools/mb directory (this resolves the missing 'gn_helpers' module) \n\n export PYTHONPATH=/app/k8s_compiled_repos/v8/tools/mb:$PYTHONPATH"),
    #     CodeBlock(language="bash", code="echo $PYTHONPATH"),
        
    # ]
    
    v8_o3_messages = """
```bash
# Set PYTHONPATH to include the V8 tools/mb directory (this resolves the missing 'gn_helpers' module)
export PYTHONPATH=/app/k8s_compiled_repos/v8/tools/mb:$PYTHONPATH
```
    """
    # print(messages)
    redis_4o_messages = """
```bash
# Navigate to the cloned Redis repository directory
cd /app/compiled_repos/redis

# Set PYTHONPATH to include the V8 tools/mb directory (this resolves the missing 'gn_helpers' module)
export PYTHONPATH=/app/k8s_compiled_repos/v8/tools/mb:$PYTHONPATH
```    
    """
    try:
        messages = v8_o3_messages
        code_extractor = MarkdownCodeExtractor()
        code_blocks = code_extractor.extract_code_blocks(messages)
        # code_blocks = CodeExtractor.extract_code_blocks(code)
        bash_executor = BashCodeExecutor(timeout=50)
        result = bash_executor.execute_code_blocks(code_blocks)
        print(result)
        print("*"*50)
    except Exception as e:
        print(e)
    # messages = redis_4o_messages    
    # code_extractor = MarkdownCodeExtractor()
    # code_blocks = code_extractor.extract_code_blocks(messages)
    # # code_blocks = CodeExtractor.extract_code_blocks(code)
    # bash_executor = BashCodeExecutor(timeout=50)
    # result = bash_executor.execute_code_blocks(code_blocks)
    # print(result)    
    
    


# import re

# CODE_BLOCK_PATTERN = r"```[ \t]*(\w+)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```"

# print("v8_o3_messages:", re.findall(CODE_BLOCK_PATTERN, v8_o3_messages, flags=re.DOTALL))
# print("redis_4o_messages:", re.findall(CODE_BLOCK_PATTERN, redis_4o_messages, flags=re.DOTALL))
# print("v8_o3_messages:", repr(v8_o3_messages))
# print("redis_4o_messages:", repr(redis_4o_messages))
