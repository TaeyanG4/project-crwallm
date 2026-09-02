"""Build the desktop application, then make it prove it works.

    uv run python packaging/build.py

Three steps, and the third is the point: a packaged app fails by leaving
something out, and nothing about the build succeeding tells you whether it
did. So the build runs the executable's own ``--self-test`` and refuses to
call the result a release if it does not come back clean.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = ROOT / "dist" / "CRWALLM"
EXE = DIST / "CRWALLM.exe"


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


def main() -> int:
    if sys.platform != "win32":
        print("이 빌드는 Windows 전용입니다.", file=sys.stderr)
        return 1

    step("아이콘")
    subprocess.run([sys.executable, str(HERE / "make_icon.py")], check=True)

    step("PyInstaller")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(HERE / "crwallm.spec"),
            "--noconfirm",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(ROOT / "build"),
            "--log-level",
            "WARN",
        ],
        check=True,
        cwd=ROOT,
    )

    if not EXE.exists():
        print(f"빌드가 끝났는데 실행 파일이 없습니다: {EXE}", file=sys.stderr)
        return 1

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    files = sum(1 for f in DIST.rglob("*") if f.is_file())
    print(f"{DIST}  {size / 1e6:.1f} MB, {files}개 파일")

    step("자체 점검")
    report = Path(tempfile.gettempdir()) / "crwallm-self-test.txt"
    report.unlink(missing_ok=True)
    # --quiet: the message box is modal, and a build that waits for a click is
    # a build that never finishes on a machine nobody is sitting at.
    result = subprocess.run([str(EXE), "--self-test", "--quiet"], timeout=180)
    if report.exists():
        print(report.read_text(encoding="utf-8"))

    if result.returncode != 0:
        print("\n점검 실패. 이 빌드는 내보내지 마세요.", file=sys.stderr)
        return result.returncode

    step("완료")
    print(f"설치하려면:  powershell -ExecutionPolicy Bypass -File {HERE / 'install.ps1'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
