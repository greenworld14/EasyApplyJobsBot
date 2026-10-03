import os
import re
import time
from typing import List, Optional
from urllib.parse import quote_plus

import config
import utils
from jobsite import JobSiteBot

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement


class Indeed(JobSiteBot):
    siteName = "Indeed"
    homeUrl = "https://www.indeed.com"
    protectedUrl = "https://myjobs.indeed.com/applied"
    loginUrl = "https://secure.indeed.com/auth"
    loginMarkers = ["secure.indeed.com/auth", "/account/login", "signin", "sign-in"]
    applyButtonSelectors = ["#indeedApplyButton", "button[id*='indeedApplyButton']", "[data-testid='indeedApplyButton']", "[data-testid='indeedApplyButton-test']", ".jobsearch-IndeedApplyButton-newDesign", "span[data-indeed-apply-joburl] button"]
    submitTexts = ["submit your application", "submit application", "submit"]
    nextTexts = JobSiteBot.nextTexts + ["review your application", "continue to review", "apply anyway", "continue applying"]
    successTexts = JobSiteBot.successTexts + ["your application has been submitted", "application has been submitted", "you've applied to this job"]

    def __init__(self) -> None:
        super().__init__(getattr(config, "indeedEmail", ""), getattr(config, "indeedPassword", ""))

    def login(self) -> None:
        self.open(self.loginUrl)
        self.pause(3, 5)
        emailInput = self.driver.find_element(By.CSS_SELECTOR, "input[type='email'], input[name='__email']")
        self.typeInto(emailInput, self.email)
        self.clickText(["continue", "next"], exact=True)
        self.pause(4, 6)
        passwords = [p for p in self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if p.is_displayed()]
        if not passwords:
            self.clickText(["sign in with login code instead", "log in with a password instead", "sign in with password", "use password"])
            self.pause(2, 4)
            passwords = [p for p in self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if p.is_displayed()]
        if passwords and self.password:
            self.typeInto(passwords[0], self.password)
            self.clickText(["sign in", "log in", "continue"], exact=True)
            self.pause(6, 9)

    def fromAge(self) -> str:
        return {"Past 24 hours": "1", "Past 3 days": "3", "Past Week": "7", "Past Month": "14"}.get(config.datePosted[0], "")

    def searchUrl(self, keyword: str, location: str, page: int) -> str:
        params = [("q", keyword), ("l", location or ""), ("sc", "0kf:attr(DSQF7);"), ("start", str((page - 1) * 10))]
        if self.fromAge():
            params.append(("fromage", self.fromAge()))
        return "https://www.indeed.com/jobs?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params)

    def collectJobLinks(self) -> List[str]:
        keys = []
        for element in self.driver.find_elements(By.CSS_SELECTOR, "a[data-jk], [data-jk]"):
            jk = element.get_attribute("data-jk") or ""
            if jk and jk not in keys:
                keys.append(jk)
        for anchor in self.driver.find_elements(By.CSS_SELECTOR, "a.jcs-JobTitle, a[href*='jk=']"):
            match = re.search(r"[?&]jk=([0-9a-f]+)", anchor.get_attribute("href") or "")
            if match and match.group(1) not in keys:
                keys.append(match.group(1))
        return [f"https://www.indeed.com/viewjob?jk={jk}" for jk in keys]

    def firstText(self, selectors: List[str]) -> str:
        for selector in selectors:
            try:
                text = self.driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if text:
                    return text.split("\n")[0].strip()
            except Exception:
                continue
        return ""

    def jobProperties(self) -> List[str]:
        title = self.firstText(["[data-testid='jobsearch-JobInfoHeader-title']", "h1.jobsearch-JobInfoHeader-title", "h1"])
        title = re.sub(r"\s*-\s*job post$", "", title, flags=re.I)
        company = self.firstText(["[data-testid='inlineHeader-companyName']", "[data-company-name='true']", "[data-testid='jobsearch-CompanyInfoContainer'] a", ".jobsearch-CompanyInfoContainer a"])
        location = self.firstText(["[data-testid='inlineHeader-companyLocation']", "[data-testid='job-location']", "[data-testid='jobsearch-JobInfoHeader-companyLocation']", "#jobLocationText"])
        if not (title and company):
            try:
                parts = [p.strip() for p in re.split(r"\s+-\s+", re.sub(r"\s*[|-]\s*Indeed(\.com)?\s*$", "", self.driver.title or "", flags=re.I)) if p.strip()]
            except Exception:
                parts = []
            if parts:
                title = title or parts[0]
                rest = parts[1:]
                if rest and re.search(r",\s*[A-Z]{2}\b|\bremote\b|\bhybrid\b|\d{5}", rest[-1], flags=re.I):
                    location = location or rest.pop()
                company = company or (rest[0] if rest else "")
        return [title, company, location]

    def alreadyApplied(self) -> bool:
        text = self.pageText()
        if re.search(r"\byou applied\b|applied on \w+ \d+|you've applied", text):
            return True
        for selector in self.applyButtonSelectors:
            for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if button.is_displayed() and (button.text or "").strip().lower().startswith("applied"):
                        return True
                except Exception:
                    continue
        return False

    def applyButton(self) -> Optional[WebElement]:
        for _ in range(5):
            for selector in self.applyButtonSelectors:
                for button in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        label = (button.text or button.get_attribute("aria-label") or "").strip().lower()
                        if button.is_displayed() and button.is_enabled() and "company site" not in label and not label.startswith("applied"):
                            return button
                    except Exception:
                        continue
            button = self.findClickable(["apply now", "easily apply"], exact=True)
            if button is not None:
                return button
            time.sleep(1.5)
        return None

    def externalApply(self) -> bool:
        return self.findClickable(["apply on company site", "apply on employer site"]) is not None

    def onApplyFlow(self) -> bool:
        url = self.currentUrl().lower()
        return "smartapply.indeed.com" in url or "indeedapply" in url

    def waitForApplyFlow(self, seconds: int = 25) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.waitForChallenge()
            if self.onApplyFlow():
                self.pause(2, 3)
                return True
            time.sleep(1)
        return False

    def submittedPage(self) -> bool:
        return "post-apply" in self.currentUrl().lower() or any(t in self.pageText() for t in self.successTexts)

    def prepareResume(self) -> None:
        if "resume" not in self.currentUrl().lower() and "resume" not in self.pageText()[:4000]:
            return
        try:
            radios = self.driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name*='resume' i], [data-testid*='resume'] input[type='radio'], [data-testid*='resume-selection'] input")
            if radios:
                if not any(r.is_selected() for r in radios):
                    self.driver.execute_script("arguments[0].click();", radios[0])
                    self.pause(1, 2)
                return
            cards = [c for c in self.driver.find_elements(By.CSS_SELECTOR, "[data-testid*='resume-selection'][role='button'], [data-testid*='resume'][role='radio']") if c.is_displayed()]
            if cards and not any((c.get_attribute("aria-checked") or "") == "true" for c in cards):
                self.click(cards[0])
                self.pause(1, 2)
                return
            path = os.path.abspath(config.resumePath) if config.resumePath else ""
            uploads = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            if uploads and path and os.path.isfile(path):
                uploads[0].send_keys(path)
                self.pause(4, 6)
        except Exception:
            pass

    def waitForPage(self) -> None:
        for _ in range(12):
            text = self.pageText()
            if "something went wrong" in text:
                retry = self.findClickable(["try again"], exact=True)
                if retry is not None:
                    self.click(retry)
                    self.pause(3, 5)
                    continue
            if re.search(r"preparing (your )?review|preparing your application|submitting your application|loading\.\.\.", text) and self.findClickable(self.nextTexts + self.submitTexts, exact=True) is None:
                time.sleep(2)
                continue
            return

    def fillForm(self) -> None:
        self.waitForPage()
        self.prepareResume()
        super().fillForm()

    def applyToJob(self, url: str) -> None:
        self.open(url)
        self.pause(2, 4)
        self.countJobs += 1
        self.answeredQuestions = []
        problem = self.pageLoadProblem()
        if problem:
            self.countCannotApply += 1
            self.record(f"{self.countJobs} |  |  | ", f"* 🥵 Page not loaded, retry later: {problem}", url)
            return
        title, company, location = self.jobProperties()
        self.currentTitle, self.currentCompany, self.currentLocation = title, company, location
        properties = f"{self.countJobs} | {title} | {company} | {location}"

        reason = self.isBlacklisted(title, company)
        if reason:
            self.countBlacklisted += 1
            self.record(properties + f" ({reason})", "* 🤬 Blacklisted Job, skipped!", url)
            return

        if self.alreadyApplied():
            self.countAlreadyApplied += 1
            self.record(properties, "* 🥳 Already applied!", url)
            return

        button = self.applyButton()
        if button is None:
            self.countCannotApply += 1
            reason = "* 🥵 Applies on company site (not Indeed Easy Apply)" if self.externalApply() else "* 🥵 No Easily apply button"
            self.record(properties, reason, url)
            return

        mainWindow = self.driver.current_window_handle
        knownWindows = list(self.driver.window_handles)
        self.click(button)
        self.pause(2, 3)
        self.switchToNewWindow(knownWindows)
        if not self.waitForApplyFlow():
            self.countCannotApply += 1
            if "indeed.com" not in self.currentUrl().lower():
                self.record(properties, "* 🥵 Applies on company site (not Indeed Easy Apply)", url)
            else:
                self.record(properties, "* 🥵 Indeed apply form did not open", url)
            self.closeExtraWindows(mainWindow)
            return

        result = self.completeApplication(12)
        for qa in self.answeredQuestions:
            self.displayWriteResults(f"      ❓ {qa}")
        if result == "failed":
            self.waitForPage()
            if self.submittedPage():
                result = "applied"
            elif self.findClickable(self.submitTexts, exact=True) is not None and not self.collectUnanswered():
                result = self.runApplicationSteps(3)
                if result == "failed" and self.submittedPage():
                    result = "applied"

        if result == "applied":
            self.applied()
            self.record(properties, "* 🥳 Just Applied to this job", url)
        elif result == "dry":
            self.record(properties, "* 🧪 DRY RUN - Would apply to this job", url)
        else:
            self.countCannotApply += 1
            self.record(properties, "* 🥵 Cannot apply to this Job! (needs manual answers)", url)
        self.closeExtraWindows(mainWindow)

    def run(self) -> None:
        for location in config.searchLocations:
            for keyword in config.keywords:
                self.displayWriteResults(f"\n Indeed | Category: {keyword}, Location: {location}")
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
                        if self.reachedCap():
                            return
                utils.prYellow(f"Indeed | {keyword}, {location}: applied {self.countApplied} of {self.countJobs} jobs so far.")


def main() -> None:
    start = time.time()
    Indeed().start()
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
