import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = ROOT.parent / "golden-gate"
REVISION = "b8a6f2552e12aaf92c1549dc88a41c9b7e73f1aa"
CMAKE = ["uv", "tool", "run", "--from", "cmake==3.31.6", "cmake"]


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def main():
    if not SOURCE.exists():
        run(["git", "clone", "--depth", "1", "https://github.com/Google-Health-API/golden-gate.git", str(SOURCE)])
        run(["git", "fetch", "--depth", "1", "origin", REVISION], cwd=SOURCE)
        run(["git", "checkout", "--detach", REVISION], cwd=SOURCE)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    if revision != REVISION:
        raise RuntimeError(f"Expected Golden Gate {REVISION}, found {revision}")
    patch = str(ROOT / "golden-gate.patch")
    applied = subprocess.run(["git", "apply", "--reverse", "--check", patch], cwd=SOURCE, capture_output=True)
    if applied.returncode != 0:
        run(["git", "apply", "--check", patch], cwd=SOURCE)
        run(["git", "apply", patch], cwd=SOURCE)
    run(["git", "submodule", "update", "--init", "--depth", "1", "external/mbedtls"], cwd=SOURCE)
    build = str(ROOT / "build")
    run(CMAKE + ["-S", str(ROOT), "-B", build, "-DCMAKE_BUILD_TYPE=Release"])
    run(CMAKE + ["--build", build, "--target", "fitbit-air-reader", "reader-tests", "--parallel", "6"])
    run(["uv", "tool", "run", "--from", "cmake==3.31.6", "ctest", "--test-dir", build, "--output-on-failure"])


if __name__ == "__main__":
    main()
