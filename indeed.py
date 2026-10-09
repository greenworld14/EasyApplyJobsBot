import os
import re
import csv
import time
from typing import List, Set
from urllib.parse import quote_plus

import config
import utils
from dice import Dice

from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

try:
    from selenium_stealth import stealth as stealth_fn
except ImportError:
    stealth_fn = None


class Indeed(Dice):
    siteName = "Indeed"
    homeUrl = "https://www.indeed.com"
    loginUrl = "https://secure.indeed.com/auth"

    def __init__(self) -> None:
        self.email = getattr(config, "indeedEmail", "")
        self.password = getattr(config, "indeedPassword", "")
        self.countApplied = 0
        self.countJobs = 0
        self.countBlacklisted = 0
        self.countAlreadyApplied = 0
        self.countCannotApply = 0
        self.seenJobs: Set[str] = set()
        self.answeredQuestions: List[str] = []
        self.currentCompany = ""
        self.currentTitle = ""
        self.currentLocation = ""
        self.unansweredQuestions: List[str] = []
        self.answersMtime = self.answersFileMtime()
        self.answers = self.loadAnswers()
        self.driver = self.createIndeedDriver()
        self.cookiesPath = os.path.join(os.getcwd(), "cookies", f"{self.siteName.lower()}_{self.getHash(self.email or 'profile')}.pkl")
        self.appliedUrls: Set[str] = set()
        self.stepRetries: dict = {}

    def createIndeedDriver(self):
        options = webdriver.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--disable-extensions")
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-dev-shm-usage')
        if config.headless:
            options.add_argument("--headless")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option('useAutomationExtension', False)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        
        profile_path = os.path.expanduser("~/.indeed_profile")
        os.makedirs(profile_path, exist_ok=True)
        options.add_argument('--user-data-dir=' + profile_path)
        
        self.clearStaleDriverLock()
        try:
            folder = os.path.dirname(ChromeDriverManager().install())
            driverName = "chromedriver.exe" if os.name == "nt" else "chromedriver"
            driver = webdriver.Chrome(service=ChromeService(os.path.join(folder, driverName)), options=options)
        except Exception:
            driver = webdriver.Chrome(options=options)
        
        driver.set_page_load_timeout(60)
        if stealth_fn:
            try:
                stealth_fn(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32",
                        webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
            except Exception:
                pass
        return driver

    def login(self) -> None:
        self.open(self.loginUrl)
        self.pause(3, 5)
        try:
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
        except Exception:
            pass

    def searchUrl(self, position: str, location: str, page: int) -> str:
        params = [("q", position), ("l", location or ""), ("sc", "0kf:attr(DSQF7);"), ("start", str((page - 1) * 10))]
        fromage = getattr(config, "indeedFromAge", 7)
        if fromage:
            params.append(("fromage", str(fromage)))
        remote = getattr(config, "indeedRemoteOnly", False)
        if remote:
            params.append(("jt", "remote"))
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

    def jobProperties(self) -> List[str]:
        title = ""
        company = ""
        location = ""
        try:
            title = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='jobsearch-JobInfoHeader-title']").text.strip()
        except Exception:
            try:
                title = self.driver.find_element(By.CSS_SELECTOR, "h1").text.strip()
            except Exception:
                pass
        for selector in ["[data-testid='inlineHeader-companyName']", "[data-company-name='true']", ".jobsearch-CompanyInfoContainer a"]:
            try:
                company = self.driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if company:
                    break
            except Exception:
                continue
        for selector in ["[data-testid='inlineHeader-companyLocation']", "[data-testid='job-location']", "#jobLocationText"]:
            try:
                location = self.driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if location:
                    break
            except Exception:
                continue
        return [title, company, location]

    def findApplyButton(self):
        for _ in range(5):
            for selector in ["#indeedApplyButton", "button[id*='indeedApplyButton']", "[data-testid='indeedApplyButton']", ".jobsearch-IndeedApplyButton-newDesign", "button[aria-label*='Apply']"]:
                try:
                    buttons = [b for b in self.driver.find_elements(By.CSS_SELECTOR, selector) if b.is_displayed()]
                    if buttons:
                        return buttons[0]
                except Exception:
                    continue
            
            btns = self.findClickable(["easy apply", "apply now", "apply"], exact=True)
            if btns:
                return btns
            
            time.sleep(1)
        return None

    def alreadyApplied(self) -> bool:
        text = self.pageText()
        if re.search(r"\byou applied\b|applied on \w+ \d+|you've applied|application submitted", text, re.IGNORECASE):
            return True
        try:
            btn = self.findApplyButton()
            if btn:
                label = (btn.text or btn.get_attribute("aria-label") or "").strip().lower()
                if "applied" in label or "submitted" in label:
                    return True
        except Exception:
            pass
        return False

    def scrollToElement(self, element):
        try:
            self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
            self.pause(0.5, 1)
        except Exception:
            pass

    def dismissCookieBanner(self) -> None:
        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "button, a"):
                text = (btn.text or btn.get_attribute("aria-label") or "").lower()
                if "accept" in text and ("cookie" in text or "all" in text):
                    self.click(btn)
                    self.pause(1, 2)
                    return
        except Exception:
            pass

    def getLandingApplyButton(self):
        for selector in ["button[type='submit']", "a[href*='apply']", "button"]:
            buttons = [b for b in self.driver.find_elements(By.CSS_SELECTOR, selector) if b.is_displayed()]
            for btn in buttons:
                text = (btn.text or btn.get_attribute("aria-label") or "").lower()
                if "apply" in text and "save" not in text:
                    return btn
        return None

    def switchToAtsIframe(self) -> bool:
        ats_names = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "icims", "jobvite", "bamboohr"]
        for iframe in self.driver.find_elements(By.TAG_NAME, "iframe"):
            try:
                src = (iframe.get_attribute("src") or "").lower()
                if any(ats in src for ats in ats_names):
                    self.driver.switch_to.frame(iframe)
                    self.pause(2, 3)
                    return True
            except Exception:
                pass
        return False

    def exitAtsIframe(self) -> None:
        try:
            self.driver.switch_to.default_content()
            self.pause(1, 2)
        except Exception:
            pass

    def uploadResumeToFile(self) -> bool:
        path = os.path.abspath(config.resumePath) if getattr(config, "resumePath", "") else ""
        if not path or not os.path.isfile(path):
            return True
        try:
            inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            for inp in inputs:
                label = ""
                try:
                    label_elem = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp.get_attribute('id')}']")
                    label = label_elem.text.lower()
                except Exception:
                    pass
                if "cover" in label or "letter" in label:
                    continue
                try:
                    inp.send_keys(path)
                    self.pause(2, 3)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        return True

    def answerAllQuestions(self) -> None:
        self.fillForm()
        
        inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='tel'], input[type='number'], input:not([type]), textarea")
        for inp in inputs:
            if not inp.is_displayed():
                continue
            try:
                val = inp.get_attribute("value")
                if val:
                    continue
                label_text = ""
                inp_id = inp.get_attribute("id")
                if inp_id:
                    try:
                        label = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']")
                        label_text = label.text.lower()
                    except Exception:
                        pass
                if "search" in label_text or "newsletter" in label_text:
                    continue
                answer = self.resolve_answer(label_text or inp.get_attribute("placeholder") or inp.get_attribute("name") or "")
                if answer:
                    self.scrollToElement(inp)
                    self.typeInto(inp, answer)
            except Exception:
                pass
        
        selects = self.driver.find_elements(By.CSS_SELECTOR, "select")
        for select in selects:
            if not select.is_displayed():
                continue
            try:
                label_text = ""
                select_id = select.get_attribute("id")
                if select_id:
                    try:
                        label = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{select_id}']")
                        label_text = label.text.lower()
                    except Exception:
                        pass
                if "search" in label_text or "newsletter" in label_text:
                    continue
                answer = self.resolve_answer(label_text or select.get_attribute("name") or "")
                if answer:
                    self.scrollToElement(select)
                    from selenium.webdriver.support.ui import Select
                    sel = Select(select)
                    try:
                        sel.select_by_visible_text(answer)
                    except Exception:
                        try:
                            sel.select_by_value(answer)
                        except Exception:
                            pass
            except Exception:
                pass
        
        radios = self.driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
        radio_names = {}
        for radio in radios:
            if not radio.is_displayed():
                continue
            name = radio.get_attribute("name")
            if name not in radio_names:
                radio_names[name] = []
            radio_names[name].append(radio)
        
        for name, radio_group in radio_names.items():
            if any(r.is_selected() for r in radio_group):
                continue
            try:
                label_text = ""
                try:
                    label = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{radio_group[0].get_attribute('id')}']")
                    label_text = label.text.lower()
                except Exception:
                    pass
                answer = self.resolve_answer(label_text or name or "")
                if answer:
                    for radio in radio_group:
                        if answer.lower() in (radio.get_attribute("value") or "").lower() or answer.lower() in (self.driver.find_element(By.CSS_SELECTOR, f"label[for='{radio.get_attribute('id')}']").text.lower() if radio.get_attribute("id") else ""):
                            self.scrollToElement(radio)
                            radio.click()
                            break
                    else:
                        if radio_group:
                            self.scrollToElement(radio_group[0])
                            radio_group[0].click()
            except Exception:
                pass
        
        checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        for checkbox in checkboxes:
            if not checkbox.is_displayed():
                continue
            try:
                if checkbox.is_selected():
                    continue
                label_text = ""
                checkbox_id = checkbox.get_attribute("id")
                if checkbox_id:
                    try:
                        label = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{checkbox_id}']")
                        label_text = label.text.lower()
                    except Exception:
                        pass
                if "agree" in label_text or "consent" in label_text or "confirm" in label_text or "accept" in label_text:
                    self.scrollToElement(checkbox)
                    checkbox.click()
            except Exception:
                pass

    def applyOnEmployerSite(self, url: str) -> bool:
        main_window = self.driver.current_window_handle
        known_windows = list(self.driver.window_handles)
        
        try:
            for _ in range(5):
                btn = self.getLandingApplyButton()
                if btn and "apply" in (btn.text or "").lower():
                    self.scrollToElement(btn)
                    self.click(btn)
                    self.pause(2, 3)
                    self.switchToNewWindow(known_windows)
                    break
                time.sleep(1)
        except Exception:
            pass
        
        self.pause(1, 2)
        self.dismissCookieBanner()
        self.switchToAtsIframe()
        self.uploadResumeToFile()
        
        step_count = 0
        while step_count < 15:
            step_count += 1
            self.answerAllQuestions()
            
            next_btn = self.findClickable(["continue", "next", "proceed", "forward"], exact=True)
            submit_btn = self.findClickable(["submit", "send application", "apply"], exact=True)
            
            if submit_btn:
                self.scrollToElement(submit_btn)
                self.click(submit_btn)
                self.pause(3, 5)
                text = self.pageText()
                if "confirm" in text or "thank" in text or "success" in text or "application received" in text:
                    return True
                if "error" in text or "invalid" in text:
                    return False
            elif next_btn:
                self.scrollToElement(next_btn)
                self.click(next_btn)
                self.pause(2, 3)
            else:
                break
        
        return "thank" in self.pageText() or "success" in self.pageText() or "confirm" in self.pageText()

    def saveUnansweredQuestion(self, question: str) -> None:
        if not question or not question.strip():
            return
        try:
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", "Manual Answer Questions.csv")
            seen = set()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    for r in csv.reader(f):
                        if r and len(r) > 6:
                            seen.add((r[0].strip().lower(), r[6].strip() if len(r) > 6 else ""))
            
            url = self.currentUrl()
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            q_clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", question).strip()
            key = (q_clean.lower(), url.strip())
            
            if key not in seen:
                newFile = not os.path.exists(path)
                with open(path, "a", encoding="utf-8-sig" if newFile else "utf-8", newline="") as f:
                    if newFile:
                        f.write("Question,Times,Last Seen,Site,Company,Job Title,Link,Answer In additionalQuestions.yaml,Status\n")
                    writer = csv.writer(f)
                    writer.writerow([q_clean, "1", now, self.siteName, self.currentCompany, self.currentTitle, url, "", ""])
        except Exception:
            pass

    def applyToJob(self, url: str) -> None:
        self.open(url)
        self.pause(2, 4)
        self.countJobs += 1
        self.answeredQuestions = []
        self.stepRetries = {}
        
        title, company, location = self.jobProperties()
        self.currentTitle, self.currentCompany, self.currentLocation = title, company, location
        
        reason = self.isBlacklisted(title, company) or self.techBlacklisted(title)
        if reason:
            self.countBlacklisted += 1
            print(f"{self.countJobs} | {title} | {company} | {location} | blacklisted")
            return
        
        if url in self.appliedUrls or self.alreadyApplied():
            self.countAlreadyApplied += 1
            print(f"{self.countJobs} | {title} | {company} | {location} | already_applied")
            return
        
        apply_btn = self.findApplyButton()
        company_site_btn = self.findClickable(["apply on company site", "apply on employer site"], exact=True)
        
        if apply_btn is None and company_site_btn is None:
            self.countCannotApply += 1
            print(f"{self.countJobs} | {title} | {company} | {location} | no_button")
            return
        
        main_window = self.driver.current_window_handle
        known_windows = list(self.driver.window_handles)
        
        result = "failed"
        if apply_btn and self.isElementDisplayed(apply_btn):
            self.scrollToElement(apply_btn)
            self.click(apply_btn)
            self.pause(3, 4)
            self.switchToNewWindow(known_windows)
            
            for i in range(30):
                current_url = self.currentUrl().lower()
                if "smartapply.indeed.com" in current_url or "indeedapply" in current_url:
                    self.pause(2, 3)
                    break
                self.waitForChallenge()
                time.sleep(1)
            
            if "indeed.com" in self.currentUrl().lower():
                step_count = 0
                while step_count < 25:
                    step_count += 1
                    self.answerAllQuestions()
                    self.pause(1, 2)
                    
                    next_btn = self.findClickable(["continue", "next", "review your application", "continue to review", "apply anyway", "continue applying"], exact=True)
                    submit_btn = self.findClickable(["submit your application", "submit application", "submit"], exact=True)
                    
                    if submit_btn:
                        self.scrollToElement(submit_btn)
                        self.click(submit_btn)
                        self.pause(4, 6)
                        current_url = self.currentUrl().lower()
                        page_text = self.pageText().lower()
                        if "post-apply" in current_url or "your application has been submitted" in page_text or "you've applied" in page_text or "application submitted" in page_text or "thank" in page_text or "success" in page_text:
                            result = "applied"
                        break
                    elif next_btn:
                        self.scrollToElement(next_btn)
                        self.click(next_btn)
                        self.pause(2, 3)
                    else:
                        break
        elif company_site_btn:
            if self.applyOnEmployerSite(url):
                result = "applied"
            else:
                result = "failed"
        
        for q in self.collectUnanswered():
            self.saveUnansweredQuestion(q)
        
        self.closeExtraWindows(main_window)
        
        if result == "applied":
            self.applied()
            self.appliedUrls.add(url)
            print(f"{self.countJobs} | {title} | {company} | {location} | applied")
        else:
            self.countCannotApply += 1
            print(f"{self.countJobs} | {title} | {company} | {location} | failed")

    def isElementDisplayed(self, element) -> bool:
        try:
            return element.is_displayed()
        except Exception:
            return False

    def run(self) -> None:
        positions = getattr(config, "indeedPositions", getattr(config, "keywords", []))
        locations = getattr(config, "indeedLocations", config.searchLocations)
        max_pages = getattr(config, "indeedMaxPages", config.maxPagesPerSearch)
        
        resume_path = getattr(config, "resumePath", "")
        if resume_path and not os.path.isfile(os.path.abspath(resume_path)):
            utils.prRed(f"❌ resumePath not found: {resume_path}")
            return
        
        for position in positions:
            for location in locations:
                for page in range(1, max_pages + 1):
                    try:
                        self.open(self.searchUrl(position, location, page))
                        self.pause(3, 5)
                        links = [link for link in self.collectJobLinks() if link not in self.seenJobs]
                    except Exception:
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
                            print(f"{self.countJobs} | {self.currentTitle} | {self.currentCompany} | {self.currentLocation} | failed")
                        
                        if self.reachedCap():
                            break
                    
                    if self.reachedCap():
                        break
                
                if self.reachedCap():
                    break
        
        print(f"\n=== Summary ===")
        print(f"Applied: {self.countApplied}")
        print(f"Failed: {self.countCannotApply}")
        print(f"Blacklisted: {self.countBlacklisted}")
        print(f"Already Applied: {self.countAlreadyApplied}")


def main() -> None:
    start = time.time()
    Indeed().start()
    utils.prYellow("---Took: " + str(round((time.time() - start) / 60)) + " minute(s).")


if __name__ == "__main__":
    main()
