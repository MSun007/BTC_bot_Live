"""Local audit runner; no cloud credentials, orders, or messages are used."""
import os
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
if (ROOT / ".audit-deps").exists():
    sys.path.insert(0, str(ROOT / ".audit-deps"))
os.environ["DASHBOARD_SESSION_SECRET"] = "offline-audit-session-secret"
os.environ["SEND_TELEGRAM"] = "false"
os.environ["SEND_EMAIL"] = "false"

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    for path in ROOT.glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    if "--lint" in sys.argv:
        from ruff.__main__ import find_ruff_bin
        result_code = subprocess.call([find_ruff_bin(), "check", "--select", "F821,F822,F823,E9", *map(str, ROOT.glob("*.py"))])
        if result_code:
            sys.exit(result_code)
    import perp_dashboard_app as dashboard
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", dashboard.HTML, re.S)
    output = ROOT / "audit" / "dashboard.js"
    output.parent.mkdir(exist_ok=True)
    output.write_text("\n".join(scripts), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
