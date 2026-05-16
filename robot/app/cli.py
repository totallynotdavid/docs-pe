from __future__ import annotations

import logging
import multiprocessing as mp

from robot.app.config import load_config
from robot.app.run import run
from robot.obs.events import RUN_START
from robot.obs.logging import configure_logging, kv, new_run_id


def main(argv: list[str] | None = None) -> None:
    cfg = load_config(argv)
    run_id = new_run_id()

    configure_logging(debug=cfg.debug, run_id=run_id)
    logging.getLogger(__name__).info("%s %s", RUN_START, kv(run_id=run_id))

    run(cfg, run_id=run_id)


if __name__ == "__main__":
    mp.freeze_support()
    main()
