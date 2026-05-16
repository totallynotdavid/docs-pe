"""Entry point for robot CLI application."""
from __future__ import annotations

import multiprocessing as mp

from robot.app.cli import main


if __name__ == "__main__":
    mp.freeze_support()
    main()
