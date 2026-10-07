import re
import time
from typing import List, Optional
from urllib.parse import quote_plus

import config
import utils
from jobsite import JobSiteBot

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement


class ZipRecruiter(JobSiteBot):
    siteName = "ZipRecruiter"
    homeUrl = "https://www.ziprecruiter.com"
    protectedUrl = "https://www.ziprecruiter.com/candidate/my-jobs"
    loginUrl = "https://www.ziprecruiter.com/authn/login"
    cardSelectors = ["article[id^='job-card-']", "div.job_result_two_pane", "article.job_result", "[data-testid='job-card']", "div[data-job-id]"]
    quickApplyTexts = ["1-click apply", "1 click apply", "one-click apply", "quick apply", "easy apply"]

    def __init__(self) -> None:
        super().__init__(config.zipRecruiterEmail, config.zipRecruiterPassword)

    def login(self) -> None:
        self.open(self.loginUrl)
        self.pause(3, 5)
        emailInput = self.driver.find_element(By.CSS_SELECTOR, "input[type='email'], input[name='email']")
        self.typeInto(emailInput, self.email)
        self.clickText(["continue", "next", "log in", "sign in"], exact=True)
        self.pause(3, 5)
        passwordInputs = [p for p in self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if p.is_displayed()]
        if not passwordInputs:
            self.clickText(["sign in with password", "use password", "log in with password", "enter password instead"])
            self.pause(2, 4)
            passwordInputs = [p for p in self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if p.is_displayed()]
        if passwordInputs:
            self.typeInto(passwordInputs[0], self.password)
            self.clickText(["log in", "sign in", "continue", "submit"], exact=True)
            self.pause(8, 12)

    def days(self) -> str:
        return {"Past 24 hours": "1", "Past 3 days": "5", "Past Week": "10", "Past Month": "30"}.get(config.datePosted[0], "")

    def salary(self) -> str:
        digits = re.sub(r"[^0-9]", "", config.salary[0]) if config.salary else ""
        return digits

    def employmentParams(self) -> List[tuple]:
        mapping = {"Full-time": "full_time", "Part-time": "part_time", "Contract": "contract", "Temporary": "temporary", "Intership": "internship"}
        return [("refine_by_employment", "employment_type:" + mapping[t]) for t in config.jobType if t in mapping]

    def searchUrl(self, keyword: str, location: str, page: int) -> str:
        params = [("search", keyword), ("refine_by_apply_type", "has_zipapply"), ("page", str(page))]
        if location and location.lower() != "remote":
            params.append(("location", location))
        if location.lower() == "remote" or config.remote == ["Remote"]:
            params.append(("refine_by_location_type", "only_remote"))
        if self.days():
            params.append(("days", self.days()))
        if self.salary():
            params.append(("refine_by_salary", self.salary()))
        params.extend(self.employmentParams())
        return "https://www.ziprecruiter.com/jobs-search?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params)

    def findCards(self) -> List[WebElement]:
        for selector in self.cardSelectors:
            cards = [c for c in self.driver.find_elements(By.CSS_SELECTOR, selector) if c.is_displayed()]
            if cards:
                return cards
        return []

    def cardKey(self, card: WebElement) -> str:
        for attr in ["id", "data-job-id", "data-jid"]:
            value = card.get_attribute(attr)
            if value:
                return value
        return card.text[:120]

    def cardProperties(self, card: WebElement) -> List[str]:
        title = company = location = ""
        try:
            title = card.find_element(By.CSS_SELECTOR, "h2, h3, [data-testid='job-title']").text.strip()
        except Exception:
            pass
        for selector in ["[data-testid='job-card-company']", "a.company_name", "a[href*='/co/']", ".company_name"]:
            try:
                company = card.find_element(By.CSS_SELECTOR, selector).text.strip()
                if company:
                    break
            except Exception:
                continue
        for selector in ["[data-testid='job-card-location']", ".company_location", ".location"]:
            try:
                location = card.find_element(By.CSS_SELECTOR, selector).text.strip()
                if location:
                    break
            except Exception:
                continue
        lines = [l.strip() for l in card.text.split("\n") if l.strip()]
        if not title and lines:
            title = lines[0]
        if not company and len(lines) > 1:
            company = lines[1]
        return [title, company, location]

    def openCard(self, card: WebElement) -> Optional[str]:
        target = None
        for selector in ["h2 a", "h2 button", "h3 a", "a[href*='jid=']", "a[href*='/c/']", "a[href*='/k/']"]:
            found = [f for f in card.find_elements(By.CSS_SELECTOR, selector) if "apply" not in (f.text or "").lower()]
            if found:
                target = found[0]
                break
        href = target.get_attribute("href") if target is not None else None
        cardId = card.get_attribute("id") or ""
        before = self.currentUrl()
        self.click(target if target is not None else card)
        deadline = time.time() + 8
        while time.time() < deadline and self.currentUrl() == before:
            time.sleep(0.5)
        self.pause(1, 2)
        if href and "/jobs-search" not in href:
            return href
        if cardId.startswith("job-card-"):
            return self.jobLink(cardId[len("job-card-"):])
        match = re.search(r"[?&]lk=([^&]+)", self.currentUrl())
        if match:
            return self.jobLink(match.group(1))
        return self.currentUrl()

    def jobLink(self, lk: str) -> str:
        current = self.currentUrl()
        search = re.search(r"[?&]search=([^&]+)", current)
        location = re.search(r"[?&]location=([^&]+)", current)
        params = []
        if search:
            params.append("search=" + search.group(1))
        if location:
            params.append("location=" + location.group(1))
        params.append("lk=" + lk)
        return "https://www.ziprecruiter.com/jobs-search?" + "&".join(params)

    def applyToJob(self, title: str, company: str, location: str, url: str) -> None:
        self.countJobs += 1
        self.currentTitle, self.currentCompany, self.currentLocation = title, company, location
        self.answeredQuestions = []
        properties = f"{self.countJobs} | {title} | {company} | {location}"

        reason = self.isBlacklisted(title, company) or self.techBlacklisted(title)
        if reason:
            self.countBlacklisted += 1
            self.record(properties + f" ({reason})", "* 🤬 Blacklisted Job, skipped!", url)
            return

        if self.appliedBadge():
            self.countAlreadyApplied += 1
            self.record(properties, "* 🥳 Already applied!", url)
            return

        button = None
        deadline = time.time() + 15
        while time.time() < deadline:
            button = self.findClickable(self.quickApplyTexts) or self.findClickable(["continue", "continue application", "finish applying", "finish application"], exact=True)
            if button is not None:
                break
            if self.appliedBadge():
                self.countAlreadyApplied += 1
                self.record(properties, "* 🥳 Already applied!", url)
                return
            if self.findClickable(["apply", "apply now", "apply on company site"], exact=True) is not None:
                break
            time.sleep(1)
        if button is None:
            self.countCannotApply += 1
            self.record(properties, "* 🥵 No 1-Click / Quick Apply button", url)
            return
        try:
            label = (button.text or "").strip().lower()
        except Exception:
            label = ""

        if config.dryRun and not label.startswith(("continue", "finish")):
            self.record(properties, "* 🧪 DRY RUN - Would apply to this job (1-Click)", url)
            return

        mainWindow = self.driver.current_window_handle
        knownWindows = list(self.driver.window_handles)
        self.click(button)
        self.pause(2, 3)
        self.switchToNewWindow(knownWindows)
        self.waitForForm()

        if config.dryRun:
            self.fillForm()
            for qa in self.answeredQuestions:
                self.displayWriteResults(f"      ❓ {qa}")
            self.record(properties, "* 🧪 DRY RUN - Questions answered, not submitted", url)
            self.closeDialog()
            self.closeExtraWindows(mainWindow)
            return

        if self.appliedOnPage():
            result = "applied"
        else:
            result = self.completeApplication()
            for qa in self.answeredQuestions:
                self.displayWriteResults(f"      ❓ {qa}")
            if result == "failed":
                self.waitForPage()
                if self.appliedOnPage():
                    result = "applied"
                elif self.findClickable(["submit", "submit application"], exact=True) is not None and not self.collectUnanswered():
                    result = self.runApplicationSteps(3)
                    if result == "failed" and self.appliedOnPage():
                        result = "applied"
            elif result != "applied" and self.dialogScope() is None and self.appliedOnPage():
                result = "applied"

        if result == "applied":
            self.applied()
            self.record(properties, "* 🥳 Just Applied to this job", url)
        else:
            self.countCannotApply += 1
            self.record(properties, "* 🥵 Cannot apply to this Job! (needs manual answers)", url)
            self.closeDialog()
        self.closeExtraWindows(mainWindow)

    def waitForForm(self, seconds: int = 20) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            scope = self.dialogScope()
            if scope is not None:
                try:
                    fields = scope.find_elements(By.CSS_SELECTOR, "input:not([type='hidden']):not([type='submit']), textarea, select, [role='combobox'], [role='radiogroup']")
                    if any(f.is_displayed() for f in fields):
                        time.sleep(1.5)
                        return
                except Exception:
                    pass
            if self.appliedOnPage():
                return
            time.sleep(1)

    APPLIED_BADGE_JS = r"""
for (const el of document.querySelectorAll('button, a, span, div, p, [role=button]')) {
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0 || el.children.length > 2) continue;
  const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
  if (t.length < 40 && /^(applied!?|application sent|you applied|applied on .+|applied \d.*)$/.test(t)) return true;
}
return false;
"""

    def appliedBadge(self) -> bool:
        try:
            return bool(self.driver.execute_script(self.APPLIED_BADGE_JS))
        except Exception:
            return False

    def appliedOnPage(self) -> bool:
        if self.appliedBadge():
            return True
        return any(t in self.pageText() for t in self.successTexts)

    def writeAnswers(self) -> None:
        for qa in self.answeredQuestions:
            self.displayWriteResults(f"      ❓ {qa}")
        self.answeredQuestions = []

    def closeDialog(self) -> None:
        scope = self.dialogScope()
        if scope is None:
            return
        closer = self.findClickable(["close", "cancel", "×", "dismiss"], exact=True, scope=scope)
        if closer is not None:
            self.click(closer)
            self.pause(1, 2)

    def run(self) -> None:
        for location in config.searchLocations:
            for keyword in config.keywords:
                self.displayWriteResults(f"\n ZipRecruiter | Category: {keyword}, Location: {location}")
                for page in range(1, config.maxPagesPerSearch + 1):
                    pageUrl = self.searchUrl(keyword, location, page)
                    try:
                        self.open(pageUrl)
                        self.pause(3, 5)
                        total = len(self.findCards())
                    except Exception as e:
                        self.displayWriteResults(f"* 🥵 Could not load search page {page}: {str(e)[0:80]}")
                        self.recoverRenderer()
                        continue
                    newOnPage = 0
                    for index in range(total):
                        cards = self.findCards()
                        if index >= len(cards):
                            break
                        card = cards[index]
                        key = self.cardKey(card)
                        if key in self.seenJobs:
                            continue
                        self.seenJobs.add(key)
                        newOnPage += 1
                        self.ensureWindow()
                        try:
                            title, company, jobLocation = self.cardProperties(card)
                            url = self.openCard(card)
                            self.applyToJob(title, company, jobLocation, url)
                        except Exception as e:
                            self.countCannotApply += 1
                            self.record(f"{self.countJobs} | {self.currentTitle} | {self.currentCompany}", f"* 🥵 Error: {str(e)[0:80]}", self.currentUrl())
                        if "jobs-search" not in self.currentUrl() or self.dialogScope() is not None:
                            self.open(pageUrl)
                            self.pause(2, 4)
                        if self.reachedCap():
                            return
                    if newOnPage == 0:
                        break
                utils.prYellow(f"ZipRecruiter | {keyword}, {location}: applied {self.countApplied} of {self.countJobs} jobs so far.")


def main() -> None:
    start = time.time()
    ZipRecruiter().start()
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
