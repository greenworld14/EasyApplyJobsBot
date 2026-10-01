import time

import config
import utils


def main() -> None:
    start = time.time()
    if config.runDice:
        from dice import Dice
        Dice().start()
    if config.runZipRecruiter:
        from ziprecruiter import ZipRecruiter
        ZipRecruiter().start()
    if not config.runDice and not config.runZipRecruiter:
        utils.prRed("❌ Enable runDice or runZipRecruiter in config.py.")
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
