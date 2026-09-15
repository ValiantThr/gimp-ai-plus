#!/usr/bin/env python3
"""
Run the GIMP integration tests headlessly.

GIMP 3 registers python-fu-eval as a batch interpreter, so real plugin code
can be exercised against a real Gimp module with no display and no MCP
server:

    gimp-console-3.2 -idf --batch-interpreter=python-fu-eval \\
        -b "exec(open('tests/gimp_integration.py').read())" --quit

This launches that, parses the GIMPTEST| markers out of GIMP's noisy stdout,
and exits non-zero if any test failed.

Usage:
    python3 tests/run_gimp_tests.py [--gimp PATH] [--verbose]

Slow by nature - GIMP scans its plug-ins on startup, so expect a minute or
two. The pure unit suite (tests/run_tests.py) stays fast; run this before
committing changes to mask or coordinate code.
"""

import argparse
import glob
import os
import platform
import subprocess
import sys

MARK = "GIMPTEST|"

# GIMP 3 console binaries, newest-looking first. The version is in the name,
# so glob rather than guess.
CANDIDATE_GLOBS = {
    "Windows": [
        r"%LOCALAPPDATA%\Programs\GIMP 3\bin\gimp-console-*.exe",
        r"%ProgramFiles%\GIMP 3\bin\gimp-console-*.exe",
    ],
    "Darwin": [
        "/Applications/GIMP.app/Contents/MacOS/gimp-console*",
        "/Applications/GIMP-3*.app/Contents/MacOS/gimp-console*",
    ],
    "Linux": [
        "/usr/bin/gimp-console-3*",
        "/usr/local/bin/gimp-console-3*",
        "/opt/gimp*/bin/gimp-console-3*",
    ],
}


def find_gimp_console():
    """Locate a GIMP 3 console binary."""
    for pattern in CANDIDATE_GLOBS.get(platform.system(), []):
        expanded = os.path.expandvars(pattern)
        matches = sorted(glob.glob(expanded), reverse=True)
        # Skip the debug-symbol files Windows ships alongside the binaries.
        matches = [m for m in matches if not m.endswith(".pdb")]
        if matches:
            return matches[0]

    from shutil import which

    for name in ("gimp-console-3.2", "gimp-console-3.0", "gimp-console-3", "gimp-console"):
        found = which(name)
        if found:
            return found
    return None


def run(gimp, verbose=False):
    test_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gimp_integration.py")
    if not os.path.exists(test_file):
        print(f"ERROR: {test_file} not found")
        return 1

    # exec() the file rather than passing its body: keeps quoting sane and
    # gives real filenames in tracebacks. The repo root is injected because
    # exec'd code inherits GIMP's __file__, not this script's.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        f"GIMP_AI_REPO_ROOT = r'{root}'\n"
        f"exec(open(r'{test_file}').read())"
    )
    cmd = [gimp, "-idf", "--batch-interpreter=python-fu-eval", "-b", script, "--quit"]

    env = dict(os.environ)
    # GIMP's stdout is cp1252 on Windows; keep the child from dying on it.
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"Running GIMP integration tests")
    print(f"  {gimp}")
    print("  (GIMP scans plug-ins on startup; this takes a minute or two)")
    print()

    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: GIMP did not finish within 900s")
        return 1

    output = (proc.stdout or "") + (proc.stderr or "")
    if verbose:
        print(output)

    results, begin, end = [], None, None
    for line in output.splitlines():
        if MARK not in line:
            continue
        payload = line[line.index(MARK) + len(MARK):]
        parts = payload.split("|")
        if parts[0] == "BEGIN":
            begin = int(parts[1])
        elif parts[0] == "END":
            end = (int(parts[1]), int(parts[2]))
        else:
            results.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))

    if begin is None:
        print("ERROR: the test script never started inside GIMP.")
        print("Re-run with --verbose to see GIMP's output.")
        if not verbose:
            tail = output.strip().splitlines()[-15:]
            print("\n".join("  " + line for line in tail))
        return 1

    for status, name, detail in results:
        symbol = {"PASS": "[ok]  ", "SKIP": "[skip]"}.get(status, "[FAIL]")
        print(f"  {symbol} {name}")
        if detail:
            print(f"         {detail}")

    print()
    if end is None:
        print("ERROR: the test script did not finish - see output above")
        return 1

    passed, total = end
    if passed == total:
        print(f"All {total} GIMP integration tests passed")
        return 0

    print(f"{total - passed} of {total} GIMP integration tests FAILED")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gimp", help="path to a gimp-console binary")
    parser.add_argument("--verbose", action="store_true", help="show all GIMP output")
    args = parser.parse_args()

    gimp = args.gimp or find_gimp_console()
    if not gimp:
        print("ERROR: could not find a GIMP 3 console binary.")
        print("Pass one explicitly:  python3 tests/run_gimp_tests.py --gimp /path/to/gimp-console-3.2")
        return 1
    if not os.path.exists(gimp) and not os.path.isabs(gimp):
        print(f"ERROR: {gimp} not found")
        return 1

    return run(gimp, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
