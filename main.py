import time

import config
import utils


def main() -> None:
    start = time.time()
    runIndeed = getattr(config, "runIndeed", False)
    if config.runDice:
        from dice import Dice
        Dice().start()
    if config.runZipRecruiter:
        from ziprecruiter import ZipRecruiter
        ZipRecruiter().start()
    if runIndeed:
        from indeed import Indeed
        Indeed().start()
    if not config.runDice and not config.runZipRecruiter and not runIndeed:
        utils.prRed("❌ Enable runDice, runZipRecruiter or runIndeed in config.py.")
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
