import csv
import os
import sys
import time
from typing import Dict, List

import utils
from dice import Dice
from indeed import Indeed
from ziprecruiter import ZipRecruiter

FAILED_PATH = os.path.join("data", "Failed Jobs.csv")


def manualAnswerRows(site: str) -> List[Dict[str, str]]:
    if not os.path.exists(FAILED_PATH):
        return []
    with open(FAILED_PATH, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    seen = set()
    picked = []
    for row in rows:
        link = row.get("Link", "")
        reason = row.get("Reason", "")
        if row.get("Site") == site and ("manual answers" in reason or "retry later" in reason) and link and link not in seen:
            seen.add(link)
            picked.append(row)
    return picked


def removeLinks(links: List[str]) -> None:
    if not links or not os.path.exists(FAILED_PATH):
        return
    with open(FAILED_PATH, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return
    header, body = rows[0], rows[1:]
    linkIndex = header.index("Link") if "Link" in header else 7
    body = [r for r in body if len(r) <= linkIndex or r[linkIndex] not in links]
    with open(FAILED_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(body)


def succeeded(bot, before: int) -> bool:
    return bot.countApplied + bot.countAlreadyApplied > before


class RetryDice(Dice):
    def run(self) -> None:
        rows = manualAnswerRows(self.siteName)
        self.displayWriteResults(f"\n Dice | Retrying {len(rows)} failed 'needs manual answers' jobs")
        done = []
        for row in rows:
            before = self.countApplied + self.countAlreadyApplied
            try:
                self.applyToJob(row["Link"])
            except Exception as e:
                self.countCannotApply += 1
                self.record(f"{self.countJobs} | {row.get('Job Title', '')} | {row.get('Company', '')}", f"* 🥵 Error: {str(e)[0:80]}", row["Link"])
            if succeeded(self, before):
                done.append(row["Link"])
        removeLinks(done)
        utils.prGreen(f"Dice retry: {len(done)} of {len(rows)} failed jobs are now applied.")


class RetryIndeed(Indeed):
    def run(self) -> None:
        rows = manualAnswerRows(self.siteName)
        self.displayWriteResults(f"\n Indeed | Retrying {len(rows)} failed 'needs manual answers' jobs")
        done = []
        for row in rows:
            before = self.countApplied + self.countAlreadyApplied
            self.ensureWindow()
            try:
                self.applyToJob(row["Link"])
            except Exception as e:
                self.countCannotApply += 1
                self.record(f"{self.countJobs} | {row.get('Job Title', '')} | {row.get('Company', '')}", f"* 🥵 Error: {str(e)[0:80]}", row["Link"])
            if succeeded(self, before):
                done.append(row["Link"])
        removeLinks(done)
        utils.prGreen(f"Indeed retry: {len(done)} of {len(rows)} failed jobs are now applied.")


class RetryZipRecruiter(ZipRecruiter):
    def run(self) -> None:
        rows = manualAnswerRows(self.siteName)
        self.displayWriteResults(f"\n ZipRecruiter | Retrying {len(rows)} failed 'needs manual answers' jobs")
        done = []
        for row in rows:
            before = self.countApplied + self.countAlreadyApplied
            self.ensureWindow()
            try:
                self.open(row["Link"])
                self.pause(3, 5)
                self.applyToJob(row.get("Job Title", ""), row.get("Company", ""), row.get("Location", ""), row["Link"])
            except Exception as e:
                self.countCannotApply += 1
                self.record(f"{self.countJobs} | {row.get('Job Title', '')} | {row.get('Company', '')}", f"* 🥵 Error: {str(e)[0:80]}", row["Link"])
            if succeeded(self, before):
                done.append(row["Link"])
        removeLinks(done)
        utils.prGreen(f"ZipRecruiter retry: {len(done)} of {len(rows)} failed jobs are now applied.")


def main() -> None:
    start = time.time()
    sites = [a.lower() for a in sys.argv[1:]] or ["dice", "ziprecruiter", "indeed"]
    if "dice" in sites and manualAnswerRows("Dice"):
        RetryDice().start()
    if "ziprecruiter" in sites and manualAnswerRows("ZipRecruiter"):
        RetryZipRecruiter().start()
    if "indeed" in sites and manualAnswerRows("Indeed"):
        RetryIndeed().start()
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
