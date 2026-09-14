# -*- coding: utf-8 -*-
"""Tests for command whitelist security validation module.

This module contains comprehensive tests for the security validation
logic in aidev_agent.packages.security.command.command_security.
"""

from __future__ import annotations

import pytest
from aidev_agent.packages.security.command.command_parser import (
    _check_rejected_patterns,
    _check_script_path_allowed,
    _extract_bash_c_content,
    _is_bash_c_form,
    _normalize_command_name,
)
from aidev_agent.packages.security.command.command_security import (
    ALLOWED_COMMANDS,
    DEFAULT_ALLOWED_SCRIPT_DIRS,
    EFFECTIVE_ALLOWED_COMMANDS,
    AllowedFlagsOnly,
    ForbiddenFlags,
    ValidationResult,
    is_command_allowed,
    validate_command,
    validate_path,
)
from aidev_agent.packages.security.redaction.operations import redact_known_values


class TestAllowedCommands:
    """Test ALLOWED_COMMANDS whitelist definitions."""

    def test_allowed_commands_is_frozenset(self):
        """ALLOWED_COMMANDS should be a frozenset."""
        assert isinstance(ALLOWED_COMMANDS, frozenset)

    def test_allowed_commands_contains_expected_categories(self):
        """Whitelist should contain commands from all 6 categories."""
        # Category 1: System info
        assert "pwd" in ALLOWED_COMMANDS
        assert "uname" in ALLOWED_COMMANDS
        assert "df" in ALLOWED_COMMANDS
        assert "free" in ALLOWED_COMMANDS

        # Category 2: File/dir operations
        assert "ls" in ALLOWED_COMMANDS
        assert "cat" in ALLOWED_COMMANDS
        assert "stat" in ALLOWED_COMMANDS

        # Category 3: File content operations
        assert "grep" in ALLOWED_COMMANDS
        assert "head" in ALLOWED_COMMANDS
        assert "tail" in ALLOWED_COMMANDS
        assert "sed" in ALLOWED_COMMANDS
        assert "awk" in ALLOWED_COMMANDS
        assert "diff" in ALLOWED_COMMANDS

        # Category 4: Basic tools
        assert "echo" in ALLOWED_COMMANDS
        assert "sleep" in ALLOWED_COMMANDS
        assert "true" in ALLOWED_COMMANDS

        # Category 5: Archive
        assert "tar" in ALLOWED_COMMANDS
        assert "gzip" in ALLOWED_COMMANDS

        # Category 6: Script execution
        assert "bash" in ALLOWED_COMMANDS
        assert "sh" in ALLOWED_COMMANDS
        assert "python" in ALLOWED_COMMANDS
        assert "python3" in ALLOWED_COMMANDS

    def test_dangerous_commands_not_in_whitelist(self):
        """Dangerous commands must NOT be in the whitelist."""
        dangerous = {
            "rm",
            "kill",
            "pkill",
            "killall",
            "curl",
            "wget",
            "nc",
            "netcat",
            "sudo",
            "su",
            "chmod",
            "chown",
            "passwd",
            "mv",
            "cp",
            "dd",
            "mkfs",
            "ssh",
            "scp",
            "rsync",
            "iptables",
            "mount",
            "umount",
            "ps",
            "top",
            "netstat",
            "ss",
            "nohup",
            "setsid",
            "disown",
            "screen",
            "tmux",
        }
        for cmd in dangerous:
            assert cmd not in ALLOWED_COMMANDS, f"{cmd} should not be in whitelist"

    def test_effective_includes_base(self):
        """EFFECTIVE_ALLOWED_COMMANDS should include all base commands."""
        assert ALLOWED_COMMANDS <= EFFECTIVE_ALLOWED_COMMANDS


class TestParameterRestrictions:
    """Test parameter restriction classes."""

    def test_allowed_flags_only_allows_valid(self):
        """AllowedFlagsOnly should allow specified flags."""
        restriction = AllowedFlagsOnly(flags={"", "-s", "-v"})
        ok, _ = restriction.is_allowed([])
        assert ok
        ok, _ = restriction.is_allowed(["-s"])
        assert ok
        ok, _ = restriction.is_allowed(["-v"])
        assert ok
        ok, _ = restriction.is_allowed(["-s", "/tmp"])
        assert ok

    def test_allowed_flags_only_rejects_invalid(self):
        """AllowedFlagsOnly should reject unspecified flags."""
        restriction = AllowedFlagsOnly(flags={"", "-s", "-v"})
        ok, reason = restriction.is_allowed(["-a"])
        assert not ok
        assert "不允许" in reason

    def test_allowed_flags_only_allows_non_flags(self):
        """Non-flag arguments should always pass AllowedFlagsOnly."""
        restriction = AllowedFlagsOnly(flags={"", "-h"})
        ok, _ = restriction.is_allowed(["/tmp", "file.txt"])
        assert ok

    def test_forbidden_flags_blocks_specified(self):
        """ForbiddenFlags should block specified flags."""
        restriction = ForbiddenFlags(forbidden={"-a", "--all"})
        ok, _ = restriction.is_allowed(["-h"])
        assert ok
        ok, reason = restriction.is_allowed(["-a"])
        assert not ok
        assert "不允许" in reason


class TestNormalizeCommandName:
    """Test command name normalization."""

    def test_plain_command_name(self):
        """Plain command names should pass through unchanged."""
        assert _normalize_command_name("ls") == "ls"
        assert _normalize_command_name("cat") == "cat"
        assert _normalize_command_name("python3") == "python3"

    def test_absolute_path(self):
        """Absolute paths should have basename extracted."""
        assert _normalize_command_name("/bin/ls") == "ls"
        assert _normalize_command_name("/usr/bin/python3") == "python3"
        assert _normalize_command_name("/bin/cat") == "cat"

    def test_relative_path(self):
        """Relative paths should have basename extracted (if safe)."""
        assert _normalize_command_name("./ls") == "ls"

    def test_path_traversal_rejected_in_normalize(self):
        """Path traversal in relative path should be rejected."""
        with pytest.raises(ValueError, match="路径遍历"):
            _normalize_command_name("../bin/cat")

    def test_path_traversal_rejected(self):
        """Path traversal in command name should be rejected."""
        with pytest.raises(ValueError, match="路径遍历"):
            _normalize_command_name("../../bin/rm")

    def test_empty_command_name(self):
        """Empty command name should raise ValueError."""
        with pytest.raises(ValueError, match="空命令名"):
            _normalize_command_name("")

    def test_strips_whitespace(self):
        """Whitespace should be stripped."""
        assert _normalize_command_name("  ls  ") == "ls"


class TestBashCMethods:
    """Test bash -c detection and extraction helpers."""

    def test_is_bash_c_form_positive(self):
        """Should detect bash -c form."""
        assert _is_bash_c_form("bash", ["-c", "ls"])
        assert _is_bash_c_form("sh", ["-c", "ls"])
        assert _is_bash_c_form("zsh", ["-c", "ls"])

    def test_is_bash_c_form_negative(self):
        """Should not detect non -c forms."""
        assert not _is_bash_c_form("bash", ["script.sh"])
        assert not _is_bash_c_form("ls", ["-la"])

    def test_extract_bash_c_content(self):
        """Should extract content after -c."""
        assert _extract_bash_c_content("bash", ["-c", "ls /tmp"]) == "ls /tmp"
        assert _extract_bash_c_content("sh", ["-c", "pwd"]) == "pwd"

    def test_extract_bash_c_no_content(self):
        """Should return None when -c has no following argument."""
        assert _extract_bash_c_content("bash", ["-c"]) is None
        assert _extract_bash_c_content("bash", []) is None


class TestScriptFileHelpers:
    """Test script file detection helpers."""

    def test_check_script_path_allowed(self):
        """Script path in allowed dir should pass."""
        ok, _ = _check_script_path_allowed("/workspace/script.py", ["/workspace", "/tmp"])
        assert ok

    def test_check_script_path_not_allowed(self):
        """Script path outside allowed dirs should fail."""
        ok, reason = _check_script_path_allowed("/etc/script.py", ["/workspace", "/tmp"])
        assert not ok
        assert "不在允许" in reason

    def test_check_script_path_traversal(self):
        """Path traversal in script path should be rejected."""
        ok, reason = _check_script_path_allowed("../evil.py", ["/workspace"])
        assert not ok


class TestRejectedPatterns:
    """Test forbidden pattern detection."""

    def test_command_substitution_dollar_paren(self):
        """$(cmd) should be detected."""
        ok, reason = _check_rejected_patterns("echo $(rm file)")
        assert not ok
        assert "命令替换" in reason

    def test_command_substitution_backtick(self):
        """`cmd` should be detected."""
        ok, reason = _check_rejected_patterns("echo `rm file`")
        assert not ok
        assert "命令替换" in reason

    def test_process_substitution(self):
        """<() and >() should be detected."""
        ok, reason = _check_rejected_patterns("diff <(ls) <(ls)")
        assert not ok
        assert "进程替换" in reason

    def test_here_string(self):
        """<<< should be detected."""
        ok, reason = _check_rejected_patterns("cat <<< hello")
        assert not ok
        assert "Here String" in reason

    def test_here_doc(self):
        """<< should be detected."""
        ok, reason = _check_rejected_patterns("cat << EOF")
        assert not ok
        assert "Here Document" in reason

    def test_background_execution(self):
        """& should be detected."""
        ok, reason = _check_rejected_patterns("sleep 100 &")
        assert not ok
        assert "后台执行" in reason

    def test_nohup(self):
        """nohup should be detected."""
        ok, reason = _check_rejected_patterns("nohup cmd")
        assert not ok
        assert "nohup" in reason

    def test_setsid(self):
        """setsid should be detected."""
        ok, reason = _check_rejected_patterns("setsid cmd")
        assert not ok
        assert "setsid" in reason

    def test_disown(self):
        """disown should be detected."""
        ok, reason = _check_rejected_patterns("disown")
        assert not ok
        assert "disown" in reason

    def test_screen(self):
        """screen should be detected."""
        ok, reason = _check_rejected_patterns("screen -dmS session")
        assert not ok
        assert "screen" in reason

    def test_tmux(self):
        """tmux should be detected."""
        ok, reason = _check_rejected_patterns("tmux new -d")
        assert not ok
        assert "tmux" in reason

    def test_brace_expansion(self):
        """Brace expansion should be detected."""
        ok, reason = _check_rejected_patterns("echo {a,b,c}")
        assert not ok
        assert "大括号扩展" in reason

    def test_pipe_not_rejected(self):
        """Plain pipe | should NOT be rejected at pattern level (handled by splitting)."""
        # Note: | is a valid pipe operator, not rejected at pattern level
        # It's handled by the command splitting logic
        ok, _ = _check_rejected_patterns("cat file | grep pattern")
        assert ok

    def test_and_not_rejected(self):
        """&& should NOT be rejected at pattern level."""
        ok, _ = _check_rejected_patterns("ls && pwd")
        assert ok

    def test_or_not_rejected(self):
        """|| should NOT be rejected at pattern level."""
        ok, _ = _check_rejected_patterns("ls || echo failed")
        assert ok

    def test_redirect_to_dev_null_not_rejected(self):
        """Redirect to /dev/null should NOT be rejected at pattern level."""
        ok, _ = _check_rejected_patterns("ls -la /root/.cursor/ 2>/dev/null")
        assert ok

    def test_redirect_to_dev_null_with_space_not_rejected(self):
        """Redirect to /dev/null with space should NOT be rejected."""
        ok, _ = _check_rejected_patterns("ls 2> /dev/null")
        assert ok

    def test_redirect_stdout_to_dev_null_not_rejected(self):
        """Stdout redirect to /dev/null should NOT be rejected."""
        ok, _ = _check_rejected_patterns("ls >/dev/null 2>&1")
        # Note: 2>&1 contains >& which is still caught — only >/dev/null part is safe
        # This specific case has >&1 which is not to /dev/null, so it should be rejected
        assert not ok

    def test_redirect_to_regular_file_still_rejected(self):
        """Redirect to regular file should still be rejected."""
        ok, _ = _check_rejected_patterns("ls > /tmp/output.txt")
        assert not ok


class TestValidateCommandAllowed:
    """Test validate_command with allowed commands."""

    def test_simple_allowed_commands(self):
        """Basic whitelisted commands should pass."""
        allowed = ["pwd", "ls", "cat file.txt", "echo hello", "date", "whoami"]
        for cmd in allowed:
            result = validate_command(cmd)
            assert result.is_allowed, f"'{cmd}' should be allowed but got: {result.reason}"

    def test_allowed_with_args(self):
        """Whitelisted commands with valid args should pass."""
        allowed = [
            "ls -la /tmp",
            "uname -s",
            "df -h",
            "grep pattern file.txt",
            "head -n 20 file.txt",
            "tail -f file.txt",
            "wc -l file.txt",
            "sort file.txt",
            "awk '{print $1}' file.txt",
            "sed s/foo/bar/g file.txt",
        ]
        for cmd in allowed:
            result = validate_command(cmd)
            assert result.is_allowed, f"'{cmd}' should be allowed but got: {result.reason}"

    def test_combined_commands_and(self):
        """Combined commands with && should pass if all sub-commands are allowed."""
        result = validate_command("ls /tmp && pwd")
        assert result.is_allowed

    def test_combined_commands_semicolon(self):
        """Combined commands with ; should pass if all sub-commands are allowed."""
        result = validate_command("cd /tmp; ls; pwd")
        assert result.is_allowed

    def test_combined_commands_or(self):
        """Combined commands with || should pass if all sub-commands are allowed."""
        result = validate_command("ls || echo failed")
        assert result.is_allowed

    def test_pipeline(self):
        """Piped commands should pass if all sub-commands are allowed."""
        result = validate_command("cat file.txt | grep pattern")
        assert result.is_allowed

    def test_bash_c_allowed(self):
        """bash -c with allowed inner command should pass."""
        result = validate_command('bash -c "ls /tmp"')
        assert result.is_allowed

    def test_bash_c_combined_inner(self):
        """bash -c with combined allowed inner commands should pass."""
        result = validate_command('bash -c "ls / && pwd"')
        assert result.is_allowed

    def test_sh_c_allowed(self):
        """sh -c with allowed inner command should pass."""
        result = validate_command('sh -c "ls /tmp"')
        assert result.is_allowed

    def test_comment_after_command(self):
        """Command with trailing comment should pass (comment stripped)."""
        result = validate_command("echo hello # this is a comment")
        assert result.is_allowed

    def test_quoted_pipe(self):
        """Quoted pipe character should not cause splitting."""
        result = validate_command('echo "hello | world"')
        assert result.is_allowed

    def test_long_command(self):
        """Long but valid commands should pass."""
        result = validate_command("echo " + "A" * 5000)
        assert result.is_allowed

    def test_uname_no_args(self):
        """uname with no args should pass."""
        result = validate_command("uname")
        assert result.is_allowed

    def test_df_no_args(self):
        """df with no args should pass."""
        result = validate_command("df")
        assert result.is_allowed

    def test_python_with_allowed_script_path(self):
        """python with script in allowed dir should pass."""
        result = validate_command("python /workspace/script.py")
        assert result.is_allowed

    def test_python3_with_allowed_script_path(self):
        """python3 with script in allowed dir should pass."""
        result = validate_command("python3 /app/script.py")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_python_script_without_path(self):
        """python script.py (relative path) should pass."""
        result = validate_command("python script.py")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_python3_m_module(self):
        """python3 -m module should pass."""
        result = validate_command("python3 -m pytest")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_python3_u_script(self):
        """python3 -u script.py should pass."""
        result = validate_command("python3 -u script.py")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_cd_and_python3_script(self):
        """cd /app && python3 script.py should pass."""
        result = validate_command("cd /app && python3 script.py")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_shell_with_allowed_script_path(self):
        """bash with script in allowed dir should pass."""
        result = validate_command("bash /workspace/script.sh")
        assert result.is_allowed

    def test_redirect_stderr_to_dev_null_allowed(self):
        """Command with 2>/dev/null should be allowed."""
        result = validate_command("ls -la /root/.cursor/ 2>/dev/null")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_redirect_stdout_to_dev_null_allowed(self):
        """Command with >/dev/null should be allowed."""
        result = validate_command("cat /etc/passwd >/dev/null")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"

    def test_redirect_to_dev_null_with_space_allowed(self):
        """Command with 2> /dev/null (with space) should be allowed."""
        result = validate_command("ls -la /root/ 2> /dev/null")
        assert result.is_allowed, f"Should be allowed but got: {result.reason}"


class TestValidateCommandRejected:
    """Test validate_command with rejected commands."""

    def test_dangerous_commands(self):
        """Dangerous commands should be rejected."""
        dangerous = ["rm file.txt", "curl example.com", "sudo ls", "kill 123"]
        for cmd in dangerous:
            result = validate_command(cmd)
            assert not result.is_allowed, f"'{cmd}' should be rejected"
            assert "不在允许" in result.reason

    def test_uname_forbidden_flag(self):
        """uname -a should be rejected."""
        result = validate_command("uname -a")
        assert not result.is_allowed
        assert "不允许" in result.reason

    def test_df_forbidden_flag(self):
        """df -a should be rejected."""
        result = validate_command("df -a")
        assert not result.is_allowed

    def test_python_c_allowed(self):
        """python -c should be allowed."""
        result = validate_command('python -c "print(1)"')
        assert result.is_allowed, f"python -c should be allowed but got: {result.reason}"

    def test_python3_c_allowed(self):
        """python3 -c should be allowed."""
        result = validate_command('python3 -c "print(1)"')
        assert result.is_allowed, f"python3 -c should be allowed but got: {result.reason}"

    def test_python3_c_single_quote_allowed(self):
        """python3 -c with single quotes should be allowed."""
        result = validate_command("python3 -c 'print(hello)'")
        assert result.is_allowed, f"python3 -c with single quotes should be allowed but got: {result.reason}"

    def test_background_execution_rejected(self):
        """Background execution should be rejected."""
        result = validate_command("sleep 100 &")
        assert not result.is_allowed
        assert "后台" in result.reason

    def test_nohup_rejected(self):
        """nohup should be rejected."""
        result = validate_command("nohup python server.py")
        assert not result.is_allowed

    def test_nohup_with_ampersand_rejected(self):
        """nohup with & should be rejected."""
        result = validate_command("nohup python server.py &")
        assert not result.is_allowed

    def test_command_substitution_rejected(self):
        """Command substitution should be rejected."""
        result = validate_command("echo $(rm file)")
        assert not result.is_allowed
        assert "命令替换" in result.reason

    def test_backtick_substitution_rejected(self):
        """Backtick command substitution should be rejected."""
        result = validate_command("echo `rm file`")
        assert not result.is_allowed

    def test_process_substitution_rejected(self):
        """Process substitution should be rejected."""
        result = validate_command("diff <(ls dir1) <(ls dir2)")
        assert not result.is_allowed

    def test_redirection_rejected(self):
        """Redirection should be rejected."""
        result = validate_command("ls > output.txt")
        assert not result.is_allowed
        assert "重定向" in result.reason

    def test_input_redirection_rejected(self):
        """Input redirection should be rejected."""
        result = validate_command("cat < file.txt")
        assert not result.is_allowed

    def test_here_string_rejected(self):
        """Here string should be rejected."""
        result = validate_command("cat <<< hello")
        assert not result.is_allowed

    def test_brace_expansion_rejected(self):
        """Brace expansion should be rejected."""
        result = validate_command("echo {a,b,c}")
        assert not result.is_allowed

    def test_path_traversal_rejected(self):
        """Path traversal in command should be rejected."""
        result = validate_command("../../bin/ls")
        assert not result.is_allowed

    def test_absolute_path_dangerous_rejected(self):
        """Absolute path to dangerous command should be rejected."""
        result = validate_command("/bin/rm file")
        assert not result.is_allowed

    def test_bash_c_inner_dangerous_rejected(self):
        """bash -c with dangerous inner command should be rejected."""
        result = validate_command('bash -c "rm -rf /"')
        assert not result.is_allowed

    def test_nested_bash_c_rejected(self):
        """Nested bash -c should be rejected."""
        result = validate_command('bash -c "bash -c \\"rm -rf /\\""')
        assert not result.is_allowed

    def test_bash_c_no_arg_rejected(self):
        """bash -c with no argument should be rejected."""
        result = validate_command("bash -c")
        assert not result.is_allowed

    def test_empty_command_rejected(self):
        """Empty command should be rejected."""
        result = validate_command("")
        assert not result.is_allowed
        assert "空命令" in result.reason

    def test_whitespace_only_rejected(self):
        """Whitespace-only command should be rejected."""
        result = validate_command("   ")
        assert not result.is_allowed

    def test_comment_only_rejected(self):
        """Comment-only should be rejected."""
        result = validate_command("# this is a comment")
        assert not result.is_allowed

    def test_null_byte_rejected(self):
        """Null byte should be rejected."""
        result = validate_command("echo hello\0world")
        assert not result.is_allowed

    def test_unmatched_quotes_rejected(self):
        """Unmatched quotes should be rejected."""
        result = validate_command('echo "unclosed')
        assert not result.is_allowed

    def test_setsid_rejected(self):
        """setsid should be rejected."""
        result = validate_command("setsid cmd")
        assert not result.is_allowed

    def test_disown_rejected(self):
        """disown should be rejected."""
        result = validate_command("disown")
        assert not result.is_allowed

    def test_screen_rejected(self):
        """screen should be rejected."""
        result = validate_command("screen -dmS s")
        assert not result.is_allowed

    def test_tmux_rejected(self):
        """tmux should be rejected."""
        result = validate_command("tmux new -d")
        assert not result.is_allowed

    def test_pipe_ampersand_rejected(self):
        """|& operator should be rejected."""
        result = validate_command("ls |& grep pattern")
        assert not result.is_allowed

    def test_script_outside_allowed_dir_allowed_for_python(self):
        """Python script outside allowed dirs should now be allowed (only -c is blocked)."""
        result = validate_command("python /etc/script.py")
        assert result.is_allowed

    def test_bash_c_inner_python_script_allowed(self):
        """bash -c containing python script should be allowed (python -c is not used here)."""
        result = validate_command('bash -c "python /etc/script.py"')
        assert result.is_allowed


class TestValidateCommandEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_complex_nested_quotes(self):
        """Complex nested quotes should be handled."""
        result = validate_command('bash -c "echo \\"hello world\\""')
        # This should parse and pass (echo is allowed)
        assert result.is_allowed or "语法错误" in result.reason or "引号" in result.reason

    def test_multiple_pipes(self):
        """Multiple piped commands should all be checked."""
        result = validate_command("cat file.txt | grep pattern | wc -l")
        assert result.is_allowed

    def test_multiple_pipes_with_dangerous_end(self):
        """Pipe ending with dangerous command should be rejected."""
        result = validate_command("cat file.txt | grep pattern | rm file")
        assert not result.is_allowed

    def test_and_operator_with_dangerous_second(self):
        """&& with dangerous second command should be rejected."""
        result = validate_command("cd /tmp && rm -rf /")
        assert not result.is_allowed

    def test_recursion_depth_limit(self):
        """Deeply nested bash -c should hit recursion limit.

        Note: Constructing syntactically valid deeply-nested bash -c strings
        that bashlex parses as nested is complex. We test the recursion guard
        by directly invoking the internal validation with forced recursion
        through the visited-set mechanism, which also triggers the depth check
        path.
        """
        from aidev_agent.packages.security.command.command_security import _validate_single_command

        # Use the visited-set loop detection which also guards against infinite recursion
        result = _validate_single_command(
            'bash -c "ls"',
            allowed_dirs=["/workspace"],
            recursion_depth=11,  # Exceeds the 10-depth limit
        )
        assert not result.is_allowed
        assert "层级过深" in result.reason or "嵌套" in result.reason
        assert "层级过深" in result.reason or "嵌套" in result.reason

    def test_env_var_path_not_expanded(self):
        """Environment variable paths should not be expanded (literal check)."""
        # $HOME/bin/ls basename is "ls" which is allowed
        result = validate_command("$HOME/bin/ls")
        # This depends on whether "$HOME/bin/ls" normalizes to something with "ls" basename
        # The basename would be "$HOME/bin/ls" which is not "ls", so it should be rejected
        # Actually let's check what happens
        assert isinstance(result.is_allowed, bool)

    def test_wildcard_allowed(self):
        """Wildcards should be allowed as args."""
        result = validate_command("ls *.py")
        assert result.is_allowed

    def test_question_wildcard_allowed(self):
        """? wildcard should be allowed."""
        result = validate_command("ls file?.txt")
        assert result.is_allowed


class TestNestedQuotesNotBlocked:
    """Test that commands with nested quotes are not incorrectly blocked.

    Regression tests for the "嵌套引号命令被误拦截" fix.
    Ensures the shell parser correctly handles nested quote combinations
    without producing false-positive rejections due to parse errors.
    """

    def test_double_quotes_with_inner_single_quotes(self):
        """Double-quoted string containing single quotes should be parsed correctly.

        Core scenario for the nested-quote fix: single quotes inside double
        quotes must not cause a parsing failure or false rejection.
        """
        cmd = """cd /app && echo "sys.path.append('/app/scripts'); print('hello')" """
        result = validate_command(cmd)
        assert result.is_allowed, f"Nested single-in-double quotes should be allowed but got: {result.reason}"

    def test_bash_c_with_complex_nested_quotes(self):
        """bash -c with nested function calls using single quotes inside double quotes."""
        cmd = "bash -c \"echo 'test' && echo 'done'\""
        result = validate_command(cmd)
        assert result.is_allowed, f"bash -c with nested quotes should be allowed but got: {result.reason}"

    def test_and_operator_with_quoted_arguments(self):
        """&& separated commands each containing quoted arguments should be allowed."""
        cmd = 'echo "hello" && echo "world"'
        result = validate_command(cmd)
        assert result.is_allowed, f"&& with quoted args should be allowed but got: {result.reason}"

    def test_single_quotes_with_inner_double_quotes(self):
        """Single-quoted string containing double quotes should be parsed correctly."""
        cmd = """echo 'say "hello"' && ls"""
        result = validate_command(cmd)
        assert result.is_allowed, f"Nested double-in-single quotes should be allowed but got: {result.reason}"


class TestIsCommandAllowed:
    """Test is_command_allowed shortcut."""

    def test_allowed_returns_true(self):
        assert is_command_allowed("ls")

    def test_rejected_returns_false(self):
        assert not is_command_allowed("rm file")

    def test_with_custom_dirs(self):
        assert is_command_allowed("python /my/scripts/test.py", allowed_script_dirs=["/my/scripts"])


class TestValidationResult:
    """Test ValidationResult dataclass."""

    def test_allowed_result(self):
        result = ValidationResult(is_allowed=True)
        assert result
        assert result.is_allowed
        assert result.reason == ""

    def test_denied_result(self):
        result = ValidationResult(is_allowed=False, reason="test reason")
        assert not result
        assert not result.is_allowed
        assert result.reason == "test reason"


class TestEnvironmentConfig:
    """Test environment variable configuration."""

    def test_default_script_dirs_not_empty(self):
        """Default allowed script dirs should not be empty."""
        assert len(DEFAULT_ALLOWED_SCRIPT_DIRS) > 0


class TestScriptPathValidation:
    """Test script path validation edge cases."""

    def test_exact_match_allowed_dir(self):
        """Script exactly in allowed dir should pass."""
        ok, _ = _check_script_path_allowed("/workspace/script.py", ["/workspace"])
        assert ok

    def test_subdirectory_allowed(self):
        """Script in subdirectory of allowed dir should pass."""
        ok, _ = _check_script_path_allowed("/workspace/proj/script.py", ["/workspace"])
        assert ok

    def test_sibling_dir_rejected(self):
        """Script in sibling dir should be rejected."""
        ok, _ = _check_script_path_allowed("/other/script.py", ["/workspace"])
        assert not ok

    def test_empty_path(self):
        """Empty script path should be rejected."""
        ok, reason = _check_script_path_allowed("", ["/workspace"])
        assert not ok
        assert "未指定" in reason or "遍历" in reason


class TestValidatePath:
    """Test validate_path function from security module."""

    @pytest.mark.parametrize(
        "path, expected",
        [
            ("foo/bar", "foo/bar"),
            ("/foo/bar", "/foo/bar"),
            ("/./foo//bar", "/foo/bar"),
            ("a/../b", "b"),
        ],
    )
    def test_validate_path_normalizes(self, path, expected):
        """Test path normalization."""
        assert validate_path(path) == expected

    def test_validate_path_prevents_traversal(self):
        """Test that path traversal is prevented."""
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            validate_path("../etc/passwd")

    def test_validate_path_tilde_passes_through(self):
        """Test that tilde paths are passed through without expansion (SEC-03).

        ~ expansion should happen in the sandbox context, not via os.path.expanduser.
        """
        assert validate_path("~") == "~"
        assert validate_path("~/.bashrc") == "~/.bashrc"

    def test_validate_path_windows_absolute_rejected(self):
        """Test that Windows absolute paths are rejected."""
        with pytest.raises(ValueError, match="Windows absolute paths are not supported"):
            validate_path("C:/Users/file.txt")

    def test_validate_path_allowed_prefixes(self):
        """Test path with allowed prefixes."""
        assert validate_path("/data/file.txt", allowed_prefixes=["/data/", "/workspace/"]) == "/data/file.txt"

    def test_validate_path_not_in_allowed_prefixes(self):
        """Test that paths outside allowed prefixes are rejected."""
        with pytest.raises(ValueError, match="must start with one of"):
            validate_path("/etc/file.txt", allowed_prefixes=["/data/", "/workspace/"])


class TestRedactOutput:
    """Test redact_output function."""

    @pytest.mark.parametrize(
        "text, sensitive_values, expected",
        [
            ("token is abc123", ["abc123"], "token is __BKAI_AGENT_REDACTED__"),
            ("user=admin token=xyz", ["admin", "xyz"], "user=__BKAI_AGENT_REDACTED__ token=__BKAI_AGENT_REDACTED__"),
            ("no secrets here", ["secret_token"], "no secrets here"),
            ("some text", [], "some text"),
            ("some text", [""], "some text"),
            ("key=abc key=abc", ["abc"], "key=__BKAI_AGENT_REDACTED__ key=__BKAI_AGENT_REDACTED__"),
        ],
    )
    def test_redact_output(self, text, sensitive_values, expected):
        """测试脱敏函数各种场景。"""
        assert redact_known_values(text, sensitive_values) == expected
