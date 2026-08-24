from __future__ import annotations

from pathlib import Path
import sys

from cx_Freeze import Executable, setup

ROOT_DIR = Path(__file__).resolve().parent


build_exe_options = {
    "packages": [
        "search_simulator",
        "matplotlib",
        "pandas",
        "tqdm",
    ],
    "includes": [
        "sqlite3",
        "tkinter",
        "tkinter.ttk",
    ],
    "excludes": [],
    "include_files": [],
    "optimize": 1,
    "include_msvcr": True,
    "build_exe": str(ROOT_DIR / "build" / "search_simulator_exe"),
}

gui_base = "gui" if sys.platform == "win32" else None


setup(
    name="search-simulator",
    version="0.1.0",
    description="AI Werewolf Search Simulator executable",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            script=str(ROOT_DIR / "search_simulator" / "__main__.py"),
            target_name="search_simulator.exe",
            base=gui_base,
        ),
    ],
)
