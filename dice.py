import os
import re
import time
from typing import List
from urllib.parse import quote_plus

import config
import utils
from jobsite import JobSiteBot

from selenium.webdriver.common.by import By


class Dice(JobSiteBot):
    siteName = "Dice"
    homeUrl = "https://www.dice.com"
    protectedUrl = "https://www.dice.com/dashboard/profiles"
    loginUrl = "https://www.dice.com/dashboard/login"

    def __init__(self) -> None:
        super().__init__(config.diceEmail, config.dicePassword)

    def login(self) -> None:
        self.open(self.loginUrl)
        self.pause(3, 5)
        emailInput = self.driver.find_element(By.CSS_SELECTOR, "input[name='email'], input[type='email']")
        self.typeInto(emailInput, self.email)
        self.clickText(["continue", "next", "sign in"], exact=True)
        self.pause(3, 5)
        passwordInput = self.driver.find_element(By.CSS_SELECTOR, "input[name='password'], input[type='password']")
        self.typeInto(passwordInput, self.password)
        self.clickText(["sign in", "log in", "login", "continue"], exact=True)
        self.pause(8, 12)

    def postedDate(self) -> str:
        return {"Past 24 hours": "ONE", "Past 3 days": "THREE", "Past Week": "SEVEN"}.get(config.datePosted[0], "")

    def employmentTypes(self) -> str:
        mapping = {"Full-time": "FULLTIME", "Part-time": "PARTTIME", "Contract": "CONTRACTS", "Third Party": "THIRD_PARTY"}
        return "|".join(dict.fromkeys(mapping[t] for t in config.jobType if t in mapping))

    def workplaceTypes(self, location: str) -> str:
        mapping = {"On-site": "On-Site", "Remote": "Remote", "Hybrid": "Hybrid"}
        if location.lower() == "remote":
            return "Remote"
        return "|".join(mapping[r] for r in config.remote if r in mapping)

    def searchUrl(self, keyword: str, location: str, page: int) -> str:
        params = [("q", keyword), ("filters.easyApply", "true"), ("page", str(page)), ("pageSize", "20"), ("language", "en")]
        if location and location.lower() != "remote":
            params.append(("location", location))
        if self.postedDate():
            params.append(("filters.postedDate", self.postedDate()))
        if self.employmentTypes():
            params.append(("filters.employmentType", self.employmentTypes()))
        if self.workplaceTypes(location):
            params.append(("filters.workplaceTypes", self.workplaceTypes(location)))
        return "https://www.dice.com/jobs?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params)

    def collectJobLinks(self) -> List[str]:
        links = []
        for anchor in self.driver.find_elements(By.CSS_SELECTOR, "a[href*='/job-detail/']"):
            href = (anchor.get_attribute("href") or "").split("?")[0]
            if href and href not in links:
                links.append(href)
        for anchor in self.driver.find_elements(By.CSS_SELECTOR, "a.card-title-link[id]"):
            href = "https://www.dice.com/job-detail/" + anchor.get_attribute("id")
            if href not in links:
                links.append(href)
        return links

    def jobProperties(self) -> List[str]:
        title = company = location = ""
        try:
            title = self.driver.find_element(By.CSS_SELECTOR, "h1").text.strip()
        except Exception:
            pass
        for selector in ["[data-cy='companyNameLink']", "a[href*='/company-profile/']", "[data-testid='companyName']", "[data-cy='companyName']"]:
            try:
                company = self.driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if company:
                    break
            except Exception:
                continue
        for selector in ["[data-cy='location']", "[data-testid='job-location']", "li[data-cy='location']"]:
            try:
                location = self.driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if location:
                    break
            except Exception:
                continue
        return [title, company, location]

    def applyButton(self):
        for _ in range(5):
            try:
                buttons = [b for b in self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='apply-button']") if b.is_displayed()]
                button = buttons[0] if buttons else self.findClickable(["easy apply", "apply now", "applied"])
                if button is not None:
                    button.text
                return button
            except Exception:
                time.sleep(1.5)
        return None

    def isAppliedLabel(self, button) -> bool:
        try:
            label = (button.text or button.get_attribute("aria-label") or "").strip().lower()
        except Exception:
            button = self.applyButton()
            label = (button.text or "").strip().lower() if button is not None else ""
        return label.startswith("applied") or label == "application submitted"

    def onAppliedPage(self) -> bool:
        if self.currentUrl().rstrip("/").endswith("/wizard/applied"):
            return True
        text = self.pageText()
        return "already applied to this job" in text or "we have your application on file" in text

    def confirmApplied(self, url: str) -> bool:
        jobId = url.rstrip("/").split("/")[-1]
        for _ in range(2):
            self.open(f"https://www.dice.com/job-applications/{jobId}/wizard")
            self.pause(4, 6)
            if self.onAppliedPage():
                return True
            self.open(url)
            self.pause(3, 5)
            button = self.applyButton()
            if button is not None and self.isAppliedLabel(button):
                return True
        return False

    def uploadResume(self) -> bool:
        if "upload your resume" not in self.pageText():
            return True
        path = os.path.abspath(config.resumePath) if config.resumePath else ""
        if not path or not os.path.isfile(path):
            return False
        inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        if not inputs:
            return False
        inputs[0].send_keys(path)
        self.pause(4, 6)
        return os.path.basename(path).lower() in self.pageText() or "upload your resume" not in self.pageText()

    def applyToJob(self, url: str) -> None:
        self.open(url)
        self.pause(2, 4)
        self.countJobs += 1
        title, company, location = self.jobProperties()
        self.currentTitle, self.currentCompany, self.currentLocation = title, company, location
        properties = f"{self.countJobs} | {title} | {company} | {location}"

        reason = self.isBlacklisted(title, company)
        if reason:
            self.countBlacklisted += 1
            self.record(properties + f" ({reason})", "* 🤬 Blacklisted Job, skipped!", url)
            return

        button = self.applyButton()
        if button is None:
            self.countCannotApply += 1
            self.record(properties, "* 🥵 No Easy Apply button", url)
            return
        if self.isAppliedLabel(button):
            self.countAlreadyApplied += 1
            self.record(properties, "* 🥳 Already applied!", url)
            return

        mainWindow = self.driver.current_window_handle
        knownWindows = list(self.driver.window_handles)
        jobId = url.rstrip("/").split("/")[-1]
        try:
            href = button.get_attribute("href") or ""
        except Exception:
            href = ""
        if "/job-applications/" in href and "login" not in href:
            self.open(href)
        elif "login" in href:
            self.open(f"https://www.dice.com/job-applications/{jobId}/wizard")
        else:
            self.open(f"https://www.dice.com/job-applications/{jobId}/wizard")
        self.pause(2, 4)
        self.switchToNewWindow(knownWindows)
        if self.onLoginPage():
            self.countCannotApply += 1
            self.record(properties, "* 🥵 Not logged in, cannot open application", url)
            self.closeExtraWindows(mainWindow)
            return

        if self.onAppliedPage():
            self.countAlreadyApplied += 1
            self.record(properties, "* 🥳 Already applied!", url)
            self.closeExtraWindows(mainWindow)
            return

        if not self.uploadResume():
            self.countCannotApply += 1
            self.record(properties, "* 🥵 Resume required - set resumePath in config.py or add a resume to your Dice profile", url)
            self.closeExtraWindows(mainWindow)
            self.resumeMissing = True
            return

        self.answeredQuestions = []
        result = self.completeApplication()
        for qa in self.answeredQuestions:
            self.displayWriteResults(f"      ❓ {qa}")
        if result == "failed" and self.onAppliedPage():
            result = "applied"
        if result == "applied":
            self.closeExtraWindows(mainWindow)
            if self.confirmApplied(url):
                self.applied()
                self.record(properties, "* 🥳 Just Applied to this job (confirmed on Dice)", url)
            else:
                self.countCannotApply += 1
                self.record(properties, "* ⚠️ Submitted but Dice does not show it as applied - check manually", url)
            return
        elif result == "dry":
            self.record(properties, "* 🧪 DRY RUN - Would apply to this job", url)
        else:
            self.countCannotApply += 1
            step = re.search(r"step \d+ of \d+", self.pageText())
            where = f" - stuck at {step.group(0)}" if step else ""
            self.record(properties, f"* 🥵 Cannot apply to this Job! (needs manual answers{where})", url)
        self.closeExtraWindows(mainWindow)

    def run(self) -> None:
        self.resumeMissing = False
        if config.resumePath and not os.path.isfile(os.path.abspath(config.resumePath)):
            utils.prRed(f"❌ resumePath file not found: {config.resumePath}")
            return
        for location in config.searchLocations:
            for keyword in config.keywords:
                self.displayWriteResults(f"\n Dice | Category: {keyword}, Location: {location}")
                for page in range(1, config.maxPagesPerSearch + 1):
                    try:
                        self.open(self.searchUrl(keyword, location, page))
                        self.pause(3, 5)
                        links = [link for link in self.collectJobLinks() if link not in self.seenJobs]
                    except Exception as e:
                        self.displayWriteResults(f"* 🥵 Could not load search page {page}: {str(e)[0:80]}")
                        self.recoverRenderer()
                        continue
                    if not links:
                        break
                    for link in links:
                        self.seenJobs.add(link)
                        self.ensureWindow()
                        try:
                            self.applyToJob(link)
                        except Exception as e:
                            self.countCannotApply += 1
                            self.record(f"{self.countJobs} | {self.currentTitle} | {self.currentCompany}", f"* 🥵 Error: {str(e)[0:80]}", link)
                        if self.resumeMissing:
                            utils.prRed("❌ Dice requires a resume. Set resumePath in config.py to your resume file and run again.")
                            return
                        if self.reachedCap():
                            return
                utils.prYellow(f"Dice | {keyword}, {location}: applied {self.countApplied} of {self.countJobs} jobs so far.")


def main() -> None:
    start = time.time()
    Dice().start()
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
