"""`run` executes the target in a child process, in the project's interpreter."""

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def environment(**extra):
    env = dict(os.environ, PYTHONPATH=str(REPO), **extra)
    env.pop("VIRTUAL_ENV", None)
    for name in ("LDR_RUN_CHILD", "LDR_INTERPRETER"):
        env.pop(name, None)
    return env


def start(project, *args, env=None):
    return subprocess.Popen(
        [sys.executable, "-m", "ldr", *args],
        cwd=project,
        env=env or environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def launch(project, *args, env=None, expected_exit=0):
    process = start(project, *args, env=env)
    out, err = process.communicate(timeout=120)
    assert process.returncode == expected_exit, out + err
    return out, err


def last_status(project):
    return json.loads((project / ".ldr" / "last.json").read_bytes())["status"]


def test_the_target_runs_in_a_child_of_the_ldr_process(tmp_path):
    # Every interpreter that starts logs its pid and command line, so the test
    # does not depend on how a virtual-environment launcher nests processes.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    log = tmp_path / "starts.log"
    (hooks / "sitecustomize.py").write_text(
        "import os, sys\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(f\"{os.getpid()}\\t{' '.join(sys.orig_argv)}\\n\")\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("import os\nprint('PID', os.getpid())\n", encoding="utf-8")
    env = environment()
    env["PYTHONPATH"] = os.pathsep.join([str(hooks), str(REPO)])
    out, _ = launch(project, "run", "app.py", env=env)
    target = int(next(l for l in out.splitlines() if l.startswith("PID")).split()[1])
    starts = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
    launcher = [pid for pid, command in starts if " -m ldr run" in command]
    child = [pid for pid, command in starts if " -m ldr.cli run" in command]
    assert len(launcher) == 1 and len(child) == 1, starts
    assert int(child[0]) == target != int(launcher[0])


def test_exit_status_and_output_pass_through_the_child(tmp_path):
    (tmp_path / "app.py").write_text(
        "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    out, err = launch(tmp_path, "run", "app.py", expected_exit=3)
    assert "to stdout" in out
    assert "to stderr" in err


def test_the_target_never_sees_ldr_launch_variables(tmp_path):
    (tmp_path / "app.py").write_text(
        "import os\nprint('SEEN', [n for n in os.environ if n.startswith('LDR_')])\n",
        encoding="utf-8",
    )
    out, _ = launch(tmp_path, "run", "app.py")
    assert "SEEN []" in out


def test_the_interpreter_path_and_version_are_recorded(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    launch(tmp_path, "run", "app.py")
    interpreter = last_status(tmp_path)["interpreter"]
    assert "~" in interpreter["path"] or str(Path.home()) not in sys.executable
    assert str(Path.home()) not in interpreter["path"]
    assert Path(os.path.expanduser(interpreter["path"])).resolve() == Path(sys.executable).resolve()
    assert interpreter["version"].startswith(f"{sys.version_info.major}.{sys.version_info.minor}")
    assert interpreter["selected_by"]


@pytest.fixture(scope="module")
def project_venv(tmp_path_factory):
    root = tmp_path_factory.mktemp("venv-project")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    python = root / ".venv" / bin_dir / ("python.exe" if os.name == "nt" else "python")
    (root / "app.py").write_text("import sys\nprint('EXE', sys.executable)\n", encoding="utf-8")
    return root, python


def test_the_project_environment_is_used_when_ldr_runs_from_elsewhere(project_venv):
    root, python = project_venv
    out, _ = launch(root, "run", "app.py")
    shown = next(l for l in out.splitlines() if l.startswith("EXE")).split(" ", 1)[1]
    assert Path(shown).resolve() == python.resolve()
    interpreter = last_status(root)["interpreter"]
    assert Path(os.path.expanduser(interpreter["path"])).resolve() == python.resolve()
    assert interpreter["selected_by"] == ".venv"


def test_it_refuses_when_ldr_cannot_be_imported_in_the_project_interpreter(project_venv):
    root, python = project_venv
    # The parent finds LDR through sys.path. The project's interpreter has no way to.
    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r}); "
        "from ldr.cli import main; sys.exit(main(['run', 'app.py']))"
    )
    env = environment()
    env.pop("PYTHONPATH")
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "LDR is not installed in" in result.stderr
    assert str(python) in result.stderr
    assert "EXE" not in result.stdout
