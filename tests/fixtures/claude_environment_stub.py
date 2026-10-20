"""A harmless Claude CLI stub for real SDK process-boundary tests.

It records only whether selected sentinel names are present. It never serializes an
environment value, opens a network connection, or speaks to a model.
"""

import json
import sys
from pathlib import Path
from typing import Any

SELECTED_NAMES = (
    "G04_SECRET_SENTINEL",
    "ANTHROPIC_BASE_URL",
    "G04_NOTIFICATION_SENTINEL",
    "AGENT_SLACK_WEBHOOK",
    "CLAUDE_SESSION_ID",
    "AGENT_DISPATCH_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
)


def write_stub(directory: Path, *, mode: str = "result") -> tuple[Path, Path]:
    """Create an executable `claude` and its presence-only capture path."""
    capture = directory / "claude-environment.jsonl"
    script = directory / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, subprocess, time\n"
        "import re\n"
        f"capture = {str(capture)!r}\n"
        f"names = {SELECTED_NAMES!r}\n"
        "kind = 'version' if any(arg in ('-v', '--version') for arg in sys.argv[1:]) else 'worker'\n"
        f"mode = {mode!r}\n"
        "def log(row):\n"
        "    fd = os.open(capture, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0))\n"
        "    try: os.write(fd, (json.dumps(row) + '\\n').encode())\n"
        "    finally: os.close(fd)\n"
        "log({'kind': kind, 'pid': os.getpid(), 'stdio_permission': '--permission-prompt-tool' in sys.argv, 'present': {name: name in os.environ for name in names}})\n"
        "if kind == 'version':\n"
        "    if mode == 'version-hold': time.sleep(60)\n"
        "    sys.stdout.write(('unknown' if mode == 'version-invalid' else '1.9.0' if mode == 'version-old' else '2.0.0') + '\\n')\n"
        "    raise SystemExit(0)\n"
        "if mode in ('cancel', 'connect-failure'):\n"
        "    child_code = \"import os, json, time; fd=os.open(\" + repr(capture) + \",os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_BINARY',0)); os.write(fd,(json.dumps({'kind':'descendant','pid':os.getpid(),'present':{n:n in os.environ for n in \" + repr(names) + \"}})+chr(10)).encode()); os.close(fd); time.sleep(60)\"\n"
        "    subprocess.Popen([sys.executable, '-c', child_code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    for _ in range(2000):\n"
        "        if '\"descendant\"' in open(capture, encoding='utf-8').read(): break\n"
        "        time.sleep(0.01)\n"
        "    time.sleep(0.1)\n"
        "if mode == 'hold':\n"
        "    while not os.path.exists(capture + '.release'): time.sleep(0.01)\n"
        "if mode == 'connect-failure':\n"
        "    raise SystemExit(73)\n"
        "request = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'type': 'control_response', 'response': {'subtype': 'success', "
        "'request_id': request['request_id'], 'response': {}}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "user = sys.stdin.readline()\n"
        "if mode == 'cancel':\n"
        "    control = json.loads(sys.stdin.readline())\n"
        "    log({'kind': 'interrupted', 'present': {name: name in os.environ for name in names}})\n"
        "    sys.stdout.write(json.dumps({'type': 'control_response', 'response': {'subtype': 'success', "
        "'request_id': control['request_id'], 'response': {}}}) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    raise SystemExit(0)\n"
        "if mode == 'callback':\n"
        "    sys.stdout.write(json.dumps({'type':'control_request','request_id':'permission-fixture','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'curl https://example.invalid'},'tool_use_id':'fixture-tool'}})+'\\n')\n"
        "    sys.stdout.flush()\n"
        "    response = json.loads(sys.stdin.readline())\n"
        "    log({'kind':'permission','behavior':response['response']['response']['behavior']})\n"
        "if user:\n"
        "    content = json.loads(user)['message']['content']\n"
        "    match = re.search(r'^nonce: (\\S+)$', content, re.M)\n"
        "    nonce = match.group(1) if match else 'fixture'\n"
        "    result = {'nonce': nonce, 'status': 'blocked', 'summary': 'fixture stopped', "
        "'branch': 'fixture', 'commits': [], 'files_touched': [], 'commands': [], "
        "'open_questions': [], 'next_steps': [], 'notes': ''}\n"
        "    sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, "
        "'duration_ms': 0, 'duration_api_ms': 0, 'session_id': 'fixture', 'total_cost_usd': 0, "
        "'num_turns': 0, 'result': 'fixture', 'terminal_reason': 'completed', "
        "'structured_output': result}) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    if sys.platform == "win32":
        return _windows_launcher(directory / "claude.exe", script), capture
    return script, capture


def _windows_launcher(exe: Path, script: Path) -> Path:
    """CreateProcess cannot run a shebang script, and a .cmd shim cannot take the 20k+ characters of
    arguments a dispatch passes (cmd.exe stops at 8191). Copy a uv console-script launcher from the
    running environment and point its embedded script at the stub."""
    if sys.platform != "win32":
        raise NotImplementedError("the launcher is Windows-only")
    import ctypes
    import io
    import shutil
    import zipfile
    from ctypes import wintypes

    shutil.copyfile(Path(sys.executable).parent / "pytest.exe", exe)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "__main__.py",
            f"import runpy, sys\nsys.argv[0] = {str(script)!r}\nrunpy.run_path(sys.argv[0], run_name='__main__')\n",
        )
    data = payload.getvalue()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    handle = kernel.BeginUpdateResourceW(str(exe), False)
    rcdata = ctypes.cast(10, wintypes.LPCWSTR)
    if not handle or not kernel.UpdateResourceW(handle, rcdata, "UV_SCRIPT_DATA", 0, data, len(data)):
        raise OSError(ctypes.get_last_error(), "could not write the stub launcher")
    if not kernel.EndUpdateResourceW(handle, False):
        raise OSError(ctypes.get_last_error(), "could not write the stub launcher")
    return exe


def captures(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if line.endswith("\n")
    ]
