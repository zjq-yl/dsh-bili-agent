"""Exercise the PowerShell installer with a real pip-less venv, without network."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows installer")
class SetupTests(unittest.TestCase):
    def test_activated_pipless_environment_is_bootstrapped_and_reused(self):
        policy = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
            check=True, capture_output=True, timeout=15,
        )
        if policy.stdout.strip() == b"Restricted":
            self.skipTest("PowerShell execution policy prohibits scripts; policy is not changed by tests")
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="bili-setup-") as directory:
            project = Path(directory)
            environment = project / ".venv"
            interpreter = environment / "Scripts/python.exe"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(environment)],
                check=True, capture_output=True, timeout=60,
            )
            scripts = project / "scripts"
            scripts.mkdir()
            shutil.copyfile(root / "scripts/setup-local.ps1", scripts / "setup-local.ps1")
            (project / "requirements").mkdir()
            # Empty dependency lists exercise real pip without downloading anything.
            for name in ("base", "audio"):
                (project / f"requirements/{name}.txt").write_text("", encoding="utf-8")
            (scripts / "check-runtime.py").write_text(
                "import sys, pip\n"
                "from pathlib import Path\n"
                "assert Path(sys.prefix).resolve() == Path(__file__).resolve().parents[1] / '.venv'\n"
                "assert '--audio' in sys.argv\n",
                encoding="utf-8",
            )
            env = {**os.environ, "VIRTUAL_ENV": str(environment), "PIP_NO_INDEX": "1"}
            env["PATH"] = str(interpreter.parent) + os.pathsep + os.environ.get("PATH", "")
            command = [
                "powershell", "-NoProfile", "-NonInteractive", "-File",
                str(scripts / "setup-local.ps1"), "-WithAudio",
            ]
            first = subprocess.run(command, cwd=project, env=env, capture_output=True, timeout=120)
            self.assertEqual(first.returncode, 0, (first.stdout + first.stderr).decode(errors="replace"))
            self.assertIn(b"using ensurepip (offline)", first.stdout)
            self.assertIn(b"Ready.", first.stdout)
            subprocess.run([str(interpreter), "-m", "pip", "--version"], check=True, capture_output=True)

            sentinel = environment / "preserve-existing-environment.txt"
            sentinel.write_text("keep", encoding="utf-8")
            second = subprocess.run(command, cwd=project, env=env, capture_output=True, timeout=120)
            self.assertEqual(second.returncode, 0, (second.stdout + second.stderr).decode(errors="replace"))
            self.assertNotIn(b"using ensurepip (offline)", second.stdout)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
