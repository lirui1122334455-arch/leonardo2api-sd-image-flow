#!/usr/bin/env python
"""Build a free "sourceless .pyc" public release.

This is not strong encryption. It is a lightweight free obfuscation/release
format: most Python source files are compiled to legacy .pyc files and the
original .py files are not copied. Python can import these .pyc files directly.

Only these source files are intentionally kept readable in the public release:
  - src/api/routes.py
  - src/services/veo_workflow_executor.py

The generated release is tied to the CPython major/minor version used to build
it. For example, if built by Python 3.13, users should run it with CPython
3.13.x on both Windows and Ubuntu.
"""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT.parent / "fpbrowser2api_public"

KEEP_SOURCE = {
    Path("src/api/routes.py"),
    Path("src/services/veo_workflow_executor.py"),
}

COPY_ROOT_FILES = [
    ".gitignore",
    "requirements.txt",
    "fpbrowser2api_service.ps1",
    "fpbrowser2api_service.sh",
    "api接口.md",
    "seedance-nana-veo接口.md",
]

COPY_DIRS = [
    "static",
    "rules",
]

RUNTIME_DIRS = [
    "data",
    "logs",
    "analyze",
]


def rel_posix(path: Path) -> str:
    return path.as_posix()


def ensure_safe_output_dir(output: Path) -> Path:
    output = output.expanduser().resolve()
    root = PROJECT_ROOT.resolve()

    if output == root:
        raise SystemExit(f"Refuse to use project root as output: {output}")

    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SystemExit(
            "Refuse to place output inside the source project, because it may "
            f"be deleted during rebuild: {output}"
        )

    # Extra guard against catastrophic paths.
    if output.anchor == str(output):
        raise SystemExit(f"Refuse to use filesystem root as output: {output}")

    return output


def clean_output(output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return

    def ignore(_dir: str, names: list[str]) -> set[str]:
        ignored = {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }
        return {name for name in names if name in ignored or name.endswith(".pyc")}

    shutil.copytree(src, dst, ignore=ignore)


def compile_to_legacy_pyc(src: Path, dst_pyc: Path) -> None:
    """Compile src to module.pyc / __init__.pyc style path."""
    dst_pyc.parent.mkdir(parents=True, exist_ok=True)
    rel = src.relative_to(PROJECT_ROOT)
    py_compile.compile(
        str(src),
        cfile=str(dst_pyc),
        dfile=rel_posix(rel),
        doraise=True,
        optimize=0,
    )


def write_main_stub(output: Path) -> None:
    major, minor = sys.version_info[:2]
    stub = f'''\
"""FPBrowser2API public launcher.

This release contains sourceless .pyc modules. It must be run with CPython
{major}.{minor}.x, the same major/minor Python version used to build it.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


REQUIRED = ({major}, {minor})


def _run_pyc() -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != REQUIRED:
        raise SystemExit(
            "This fpbrowser2api release was built for CPython "
            f"{{REQUIRED[0]}}.{{REQUIRED[1]}}.x, but current Python is "
            f"{{sys.version.split()[0]}}. Please install/use CPython "
            f"{{REQUIRED[0]}}.{{REQUIRED[1]}}.x."
        )

    pyc = pathlib.Path(__file__).with_suffix(".pyc")
    spec = importlib.util.spec_from_file_location("__main__", pyc)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {{pyc}}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["__main__"] = module
    spec.loader.exec_module(module)


if __name__ == "__main__":
    _run_pyc()
'''
    (output / "main.py").write_text(stub, encoding="utf-8", newline="\n")


def build_python_payload(output: Path) -> None:
    # Root entrypoint: original main.py -> main.pyc, plus a tiny readable
    # launcher main.py because the service scripts start "python main.py".
    compile_to_legacy_pyc(PROJECT_ROOT / "main.py", output / "main.pyc")
    write_main_stub(output)

    src_root = PROJECT_ROOT / "src"
    for src in sorted(src_root.rglob("*")):
        if "__pycache__" in src.parts:
            continue
        rel = src.relative_to(PROJECT_ROOT)
        dst = output / rel

        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue

        if src.suffix == ".py":
            if rel in KEEP_SOURCE:
                copy_file(src, dst)
            else:
                compile_to_legacy_pyc(src, dst.with_suffix(".pyc"))
        else:
            copy_file(src, dst)


def copy_public_assets(output: Path) -> None:
    for name in COPY_ROOT_FILES:
        src = PROJECT_ROOT / name
        if src.exists():
            copy_file(src, output / name)

    # config: publish only the example file. Do NOT publish setting.toml.
    config_out = output / "config"
    config_out.mkdir(parents=True, exist_ok=True)
    example = PROJECT_ROOT / "config" / "setting_example.toml"
    if example.exists():
        copy_file(example, config_out / "setting_example.toml")

    for dirname in COPY_DIRS:
        copy_tree(PROJECT_ROOT / dirname, output / dirname)

    for dirname in RUNTIME_DIRS:
        (output / dirname).mkdir(parents=True, exist_ok=True)
        keep = output / dirname / ".gitkeep"
        keep.write_text("", encoding="utf-8")

    # Avoid committing generated runtime files from users.
    public_gitignore = output / ".gitignore"
    extra_ignore = textwrap.dedent(
        """

        # Runtime files generated by fpbrowser2api
        /config/setting.toml
        /venv/
        /.venv/
        /app.log
        /logs.txt
        /logs/*
        !/logs/.gitkeep
        /analyze/*
        !/analyze/.gitkeep
        /data/*.db
        /data/*.db-shm
        /data/*.db-wal
        /data/logs/
        /fpbrowser2api.pid
        /fpbrowser2api.out
        /fpbrowser2api.out.err
        /proxies.txt
        __pycache__/
        *.py[cod]
        """
    ).strip()
    old = public_gitignore.read_text(encoding="utf-8") if public_gitignore.exists() else ""
    public_gitignore.write_text(old.rstrip() + "\n\n" + extra_ignore + "\n", encoding="utf-8")


def write_public_readme(output: Path) -> None:
    major, minor = sys.version_info[:2]
    readme = f"""\
# FPBrowser2API Public Release

这是 `fpbrowser2api` 的免费混淆发布版：大多数 Python 文件以 `.pyc` 无源码形式发布；
以下两个文件按需求保留源码：

- `src/api/routes.py`
- `src/services/veo_workflow_executor.py`

> 注意：`.pyc` 不是强加密，只是让普通用户看不到直接源码，适合“不主动逆向”的发布场景。

## Python 版本要求

本发布包由 **CPython {major}.{minor}.x** 构建，用户也必须使用 **CPython {major}.{minor}.x**。

Windows 和 Ubuntu 都可以运行同一份 `.pyc`，但 Python 主版本/次版本必须一致。

## Windows 安装运行

```powershell
git clone <你的仓库地址> fpbrowser2api
cd fpbrowser2api

py -{major}.{minor} -m venv venv
.\\venv\\Scripts\\activate

pip install -r requirements.txt

Copy-Item .\\config\\setting_example.toml .\\config\\setting.toml

powershell -ExecutionPolicy Bypass -File .\\fpbrowser2api_service.ps1 start
```

## Ubuntu 安装运行

```bash
git clone <你的仓库地址> fpbrowser2api
cd fpbrowser2api

python{major}.{minor} -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp config/setting_example.toml config/setting.toml

chmod +x ./fpbrowser2api_service.sh
./fpbrowser2api_service.sh start
```

默认监听地址请查看 `config/setting.toml` 的 `[server]` 配置。
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")


def verify_release(output: Path) -> None:
    leaked_setting = output / "config" / "setting.toml"
    if leaked_setting.exists():
        raise SystemExit(f"Secret config must not be published: {leaked_setting}")

    src_py = {
        p.relative_to(output)
        for p in (output / "src").rglob("*.py")
        if "__pycache__" not in p.parts
    }
    expected = KEEP_SOURCE
    if src_py != expected:
        raise SystemExit(
            "Unexpected readable .py files under src:\n"
            f"  expected: {sorted(map(str, expected))}\n"
            f"  actual:   {sorted(map(str, src_py))}"
        )

    required = [
        output / "main.py",
        output / "main.pyc",
        output / "src" / "core" / "config.pyc",
        output / "src" / "api" / "routes.py",
        output / "src" / "services" / "veo_workflow_executor.py",
        output / "config" / "setting_example.toml",
        output / "requirements.txt",
        output / "fpbrowser2api_service.ps1",
        output / "fpbrowser2api_service.sh",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit("Release is missing required files:\n" + "\n".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build fpbrowser2api public .pyc release")
    parser.add_argument(
        "-O",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output directory, default: {DEFAULT_OUTPUT}",
    )
    args = parser.parse_args()

    output = ensure_safe_output_dir(args.output)
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Output dir:   {output}")
    print(f"Python:       {sys.version.split()[0]} ({sys.implementation.name})")

    if sys.implementation.name != "cpython":
        raise SystemExit("This release method requires CPython.")

    clean_output(output)
    copy_public_assets(output)
    build_python_payload(output)
    write_public_readme(output)
    verify_release(output)

    sh = output / "fpbrowser2api_service.sh"
    if sh.exists():
        try:
            os.chmod(sh, os.stat(sh).st_mode | 0o755)
        except OSError:
            pass

    print("\nOK: public .pyc release generated.")
    print(f"Readable source kept: {', '.join(sorted(map(str, KEEP_SOURCE)))}")
    print(f"Next: cd {output} && git init && git add . && git commit -m \"release\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
