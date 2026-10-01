import csv
import hashlib
import os
import pickle
import random
import re
import sys
import time
from typing import Dict, List, Optional

import config
import constants
import utils

sys.stdout.reconfigure(encoding='utf-8')

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

try:
    from selenium_stealth import stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

try:
    import yaml
except ImportError:
    yaml = None


FIND_CLICKABLE_JS = """
const texts = arguments[0].map(t => t.toLowerCase());
const exact = arguments[1];
const scope = arguments[2] || document;
function visible(el) {
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const s = window.getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none';
}
function label(el) {
  if (el.tagName === 'INPUT') return (el.value || '').trim();
  return (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
}
function search(root) {
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName;
    const isClickable = tag === 'BUTTON' || tag === 'A' || el.getAttribute('role') === 'button'
      || (tag === 'INPUT' && ['submit', 'button'].includes((el.type || '').toLowerCase()));
    if (isClickable && !el.disabled && el.getAttribute('aria-disabled') !== 'true' && visible(el)) {
      const t = label(el).toLowerCase().replace(/\\s+/g, ' ');
      if (t && texts.some(x => exact ? t === x : t.includes(x))) return el;
    }
    if (el.shadowRoot) {
      const found = search(el.shadowRoot);
      if (found) return found;
    }
  }
  return null;
}
return search(scope);
"""

PAGE_TEXT_JS = """
function collect(root) {
  let out = '';
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) out += ' ' + collect(el.shadowRoot);
  }
  return out;
}
return (document.body ? document.body.innerText : '') + ' ' + collect(document);
"""


class JobSiteBot:
    siteName = ""
    homeUrl = ""

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password
        self.countApplied = 0
        self.countJobs = 0
        self.countBlacklisted = 0
        self.countAlreadyApplied = 0
        self.countCannotApply = 0
        self.seenJobs = set()
        self.answeredQuestions: List[str] = []
        self.currentCompany = ""
        self.currentTitle = ""
        self.currentLocation = ""
        self.unansweredQuestions: List[str] = []
        self.answers = self.loadAnswers()
        self.driver = self.createDriver()
        self.cookiesPath = os.path.join(os.getcwd(), "cookies", f"{self.siteName.lower()}_{self.getHash(email or 'profile')}.pkl")

    def createDriver(self) -> webdriver.Chrome:
        options = utils.chromeBrowserOptions()
        options.page_load_strategy = "eager"
        try:
            folder = os.path.dirname(ChromeDriverManager().install())
            driverName = "chromedriver.exe" if os.name == "nt" else "chromedriver"
            driver = webdriver.Chrome(service=ChromeService(os.path.join(folder, driverName)), options=options)
        except Exception:
            driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(60)
        if STEALTH_AVAILABLE:
            try:
                stealth(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32",
                        webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
            except Exception:
                pass
        return driver

    def loadAnswers(self) -> Dict[str, Dict[str, str]]:
        empty = {"inputField": {}, "radio": {}, "dropdown": {}, "checkbox": {}}
        if yaml is None or not os.path.exists("additionalQuestions.yaml"):
            return empty
        try:
            with open("additionalQuestions.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key in empty:
                empty[key] = {str(k): str(v) for k, v in (data.get(key) or {}).items() if v is not None}
        except Exception as e:
            utils.prRed("❌ additionalQuestions.yaml has a format error, saved answers are NOT being used: " + " ".join(str(e).split())[0:160])
        return empty

    def currentUrl(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def rendererAlive(self) -> bool:
        try:
            self.driver.execute_script("return 1")
            return True
        except Exception:
            return False

    def restartDriver(self) -> None:
        utils.prYellow(f"🔄 {self.siteName} browser stopped responding, restarting Chrome...")
        try:
            self.driver.quit()
        except Exception:
            pass
        self.driver = self.createDriver()
        try:
            self.driver.get(self.homeUrl)
            time.sleep(3)
            self.loadCookies()
        except Exception:
            pass

    def recoverRenderer(self) -> None:
        if self.rendererAlive():
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
            return
        self.restartDriver()

    def open(self, url: str) -> None:
        for attempt in range(2):
            try:
                self.driver.get(url)
                break
            except Exception:
                self.recoverRenderer()
                if self.currentUrl().split("#")[0].rstrip("/") == url.split("#")[0].rstrip("/") and self.rendererAlive():
                    break
                if attempt == 0 and config.displayWarnings:
                    utils.prYellow(f"⚠️ Page load timed out, retrying: {url[:80]}")
        self.waitForChallenge()
        self.waitForSessionRedirect()

    def waitForSessionRedirect(self, seconds: int = 40) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            url = self.currentUrl().lower()
            if not (self.onLoginPage() and "redirect" in url):
                return
            time.sleep(2)

    def onChallenge(self) -> bool:
        try:
            title = (self.driver.title or "").lower()
            return "just a moment" in title or "attention required" in title or "security check" in title
        except Exception:
            return False

    def waitForChallenge(self) -> None:
        if not self.onChallenge():
            return
        utils.prYellow(f"👉 {self.siteName} is showing a security check. Solve it in the browser window, waiting up to {config.manualLoginWaitSeconds} seconds...")
        deadline = time.time() + config.manualLoginWaitSeconds
        while time.time() < deadline and self.onChallenge():
            time.sleep(3)

    def getHash(self, string: str) -> str:
        return hashlib.md5(string.encode("utf-8")).hexdigest()

    def pause(self, low: float = 1, high: Optional[float] = None) -> None:
        time.sleep(random.uniform(low, high or constants.botSpeed))

    def loadCookies(self) -> None:
        if len(config.chromeProfilePath) > 0 or not os.path.exists(self.cookiesPath):
            return
        try:
            with open(self.cookiesPath, "rb") as f:
                cookies = pickle.load(f)
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    continue
            self.driver.refresh()
        except Exception:
            pass

    def saveCookies(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cookiesPath), exist_ok=True)
            with open(self.cookiesPath, "wb") as f:
                pickle.dump(self.driver.get_cookies(), f)
        except Exception:
            pass

    protectedUrl = ""
    loginMarkers = ["login", "signin", "sign-in", "authn", "/auth"]

    def onLoginPage(self) -> bool:
        url = self.currentUrl().lower()
        return any(marker in url for marker in self.loginMarkers)

    def isLoggedIn(self) -> bool:
        try:
            self.open(self.protectedUrl)
            self.pause(3, 5)
            return not self.onLoginPage()
        except Exception:
            return False

    def login(self) -> None:
        raise NotImplementedError

    def ensureLoggedIn(self) -> bool:
        self.open(self.homeUrl)
        self.pause(2, 4)
        self.loadCookies()
        if self.isLoggedIn():
            utils.prGreen(f"✅ Logged in {self.siteName}.")
            return True
        if self.email and self.password:
            utils.prYellow(f"🔄 Trying to log in {self.siteName}...")
            try:
                self.login()
            except Exception as e:
                if config.displayWarnings:
                    utils.prYellow("⚠️ Login step failed: " + str(e)[0:80])
        if not self.isLoggedIn():
            utils.prYellow(f"👉 Finish logging in {self.siteName} in the browser window (captcha / code). Waiting up to {config.manualLoginWaitSeconds} seconds...")
            deadline = time.time() + config.manualLoginWaitSeconds
            while time.time() < deadline:
                time.sleep(5)
                if not self.onLoginPage() and self.isLoggedIn():
                    break
        if self.isLoggedIn():
            utils.prGreen(f"✅ Logged in {self.siteName}.")
            self.saveCookies()
            return True
        utils.prRed(f"❌ Could not log in {self.siteName}. Check credentials in config.py.")
        return False

    def findClickable(self, texts: List[str], exact: bool = False, scope: Optional[WebElement] = None) -> Optional[WebElement]:
        try:
            return self.driver.execute_script(FIND_CLICKABLE_JS, texts, exact, scope)
        except Exception:
            return None

    def click(self, element: WebElement) -> bool:
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.3)
            element.click()
            return True
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
                return True
            except Exception:
                return False

    def clickText(self, texts: List[str], exact: bool = False) -> bool:
        element = self.findClickable(texts, exact)
        return element is not None and self.click(element)

    def closeExtraWindows(self, mainWindow: str) -> None:
        try:
            handles = self.driver.window_handles
        except Exception:
            self.ensureWindow()
            return
        if mainWindow not in handles:
            self.ensureWindow()
            return
        for handle in handles:
            if handle != mainWindow:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except Exception:
                    pass
        try:
            self.driver.switch_to.window(mainWindow)
        except Exception:
            self.ensureWindow()

    def ensureWindow(self) -> None:
        try:
            self.driver.current_window_handle
            return
        except Exception:
            pass
        try:
            handles = self.driver.window_handles
            if handles:
                self.driver.switch_to.window(handles[0])
                return
        except Exception:
            pass
        self.restartDriver()

    def switchToNewWindow(self, knownWindows: List[str]) -> None:
        for handle in self.driver.window_handles:
            if handle not in knownWindows:
                self.driver.switch_to.window(handle)
                return

    def pageText(self) -> str:
        try:
            return (self.driver.execute_script(PAGE_TEXT_JS) or "").lower()
        except Exception:
            return ""

    def typeInto(self, element: WebElement, value: str) -> None:
        element.clear()
        element.send_keys(value)
        time.sleep(0.3)

    def questionText(self, element: WebElement) -> str:
        try:
            return (self.driver.execute_script("""
const el = arguments[0];
let parts = [];
if (el.id) {
  const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
  if (l) parts.push(l.innerText);
}
const ids = (el.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
for (const id of ids) { const n = document.getElementById(id); if (n) parts.push(n.innerText); }
parts.push(el.getAttribute('aria-label') || '', el.getAttribute('placeholder') || '', el.getAttribute('name') || '');
let p = el.closest('fieldset, [role=radiogroup], [role=group], .form-group, .question, li, div');
if (p) { const legend = p.querySelector('legend, label, h3, h4, p, span'); if (legend) parts.push(legend.innerText); }
return parts.join(' ');
""", element) or "").lower()
        except Exception:
            return ""

    def matchAnswer(self, section: str, question: str) -> Optional[str]:
        best = None
        for key, value in self.answers.get(section, {}).items():
            k = key.lower()
            if k and k in question and (best is None or len(k) > len(best[0])):
                best = (k, value)
        return best[1] if best else None

    def phoneNumber(self) -> str:
        if getattr(config, "Phone", "").strip():
            return config.Phone.strip()
        return self.answers["inputField"].get("Phone Number", "").strip()

    def fillForm(self) -> None:
        self.fillInputs()
        self.fillSelects()
        self.fillComboboxes()
        self.fillChoiceGroups()
        self.fillRadios()

    def dialogScope(self) -> Optional[WebElement]:
        try:
            return self.driver.execute_script("""
const dialogs = [...document.querySelectorAll('[role=dialog], [aria-modal=true], dialog[open]')].filter(d => {
  const r = d.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && (d.innerText || '').trim().length > 0;
});
return dialogs.length ? dialogs[dialogs.length - 1] : null;
""")
        except Exception:
            return None

    def labelFor(self, element: WebElement) -> str:
        try:
            return (self.driver.execute_script("""
const el = arguments[0];
if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText; }
const ids = (el.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
let t = ''; for (const id of ids) { const n = document.getElementById(id); if (n) t += ' ' + n.innerText; }
if (t.trim()) return t;
const wrap = el.closest('label'); if (wrap) return wrap.innerText;
return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
""", element) or "").strip().lower()
        except Exception:
            return ""

    def profileAnswer(self, q: str) -> Optional[str]:
        if "email" in q:
            return None
        if re.search(r"street address|address line 1|address 1\b|mailing address|home address|current address|full address|residential address|\baddress\b", q) and not re.search(r"address line 2|address 2\b|apt|suite|unit", q):
            return config.fullAddress
        if re.search(r"address line 2|address 2\b|apt|suite|unit number", q):
            return None
        if re.search(r"zip|postal", q):
            return config.zipCode
        if re.search(r"\bcity\b|\btown\b", q) and not re.search(r"state|country", q):
            return config.city
        if re.search(r"\bstate\b|province|region", q) and not re.search(r"united states|statement|time zone", q):
            return config.state
        if re.search(r"\bcountry\b", q) and not re.search(r"code", q):
            return config.country
        if re.search(r"where are you (currently )?(located|based)|current location|your location|location of residence|where do you (live|reside)|city and state|city, state", q):
            return config.fullAddress
        if re.search(r"preferred (first )?name|what should we call you|nickname", q):
            return config.firstName
        if re.search(r"\bfirst name\b|given name", q):
            return config.firstName
        if re.search(r"\blast name\b|surname|family name", q):
            return config.lastName
        if re.search(r"full name|legal name|your name|^name$|^\s*name\b", q):
            return f"{config.firstName} {config.lastName}"
        if re.search(r"how did you (hear|find|learn)|where did you (hear|find|see)|source of (this )?application|referral source", q):
            return self.siteName
        if re.search(r"(current|most recent|present|latest) (employer|company)|where do you (currently )?work|employer name", q):
            return config.currentEmployer
        if re.search(r"(current|most recent|present|latest) (job )?(title|position|role)", q):
            return config.currentJobTitle
        if re.search(r"highest (level of )?(education|degree)|education level|degree (obtained|earned|level)|what degree", q):
            return config.educationLevel
        if re.search(r"(school|college|university|institution)( name| attended)?", q) and not re.search(r"high school diploma", q):
            return config.school
        if re.search(r"graduat\w* (year|date)|year (of )?graduat|when did you graduate", q):
            return config.graduationYear
        if re.search(r"field of study|major|area of study", q):
            return config.fieldOfStudy or None
        if re.search(r"certif", q) and not re.search(r"i certify|certify that", q):
            return config.certifications
        if re.search(r"language(s)? (do you )?speak|spoken language|languages? spoken|fluent in", q):
            return config.languages
        if re.search(r"reference", q):
            return config.referencesAnswer
        if re.search(r"pronoun", q):
            return None
        if re.search(r"gpa|grade point", q):
            return None
        if re.search(r"skills|tech(nology)? stack|technologies|tools (do you|you) (use|know)|programming languages", q) and not re.search(r"\byears?\b", q):
            return config.skillsSummary
        if re.search(r"project|accomplishment|achievement|proud of", q):
            return config.projectAnswer
        if re.search(r"why should we hire|strength|what makes you|what sets you apart|unique|value (would|will) you bring", q):
            return config.strengthsAnswer.format(company=self.currentCompany or "your company", title=self.currentTitle or "this role")
        if re.search(r"anything else|additional (information|comments|details)|other information|is there anything", q):
            return config.aboutMeAnswer.format(company=self.currentCompany or "your company", title=self.currentTitle or "this role")
        if re.search(r"willing to travel|travel (up to|requirement)|able to travel", q):
            return "Yes"
        if re.search(r"relocat", q):
            return config.willingToRelocate
        return None

    def previousQuestion(self, field: WebElement) -> str:
        try:
            return (self.driver.execute_script(r"""
const field = arguments[0];
const root = field.closest('[role=dialog], form') || document;
const nodes = [...root.querySelectorAll('[role=radiogroup], [role=group], fieldset, select, button[role=combobox], label, legend')];
let prev = '';
for (const n of nodes) {
  if (n.contains(field) || !(n.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
  let t = '';
  const ids = (n.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  for (const id of ids) { const e = document.getElementById(id); if (e) t += ' ' + e.innerText; }
  if (!t.trim() && n.id) { const l = document.querySelector('label[for="' + CSS.escape(n.id) + '"]'); if (l) t = l.innerText; }
  if (!t.trim() && (n.tagName === 'LABEL' || n.tagName === 'LEGEND') && !n.querySelector('input')) t = n.innerText;
  t = t.replace(/\s+/g, ' ').trim();
  if (t && !/^(if other|please (specify|provide))/i.test(t)) prev = t;
}
return prev;
""", field) or "").lower()
        except Exception:
            return ""

    def openAnswer(self, question: str) -> Optional[str]:
        q = question.lower()
        fmt = {"company": self.currentCompany or "your company", "title": self.currentTitle or "this role"}
        profile = self.profileAnswer(q)
        if profile is not None:
            return profile
        if "linkedin" in q:
            return self.answers["inputField"].get("Linkedin")
        if "github" in q or "portfolio" in q or "website" in q or "personal site" in q:
            return self.answers["inputField"].get("Website")
        if re.search(r"salary|compensation|\bpay\b|hourly rate|pay rate|\brate\b|wage", q):
            if re.search(r"\balign|within (the|our|your|this) (range|budget)|comfortable with (the|our|this)|ok(ay)? with (the|our|this)|fall within", q):
                return config.salaryAlignAnswer
            return self.payNumber(q)
        if "time zone" in q or "timezone" in q:
            return config.timeZoneAnswer
        if "fun fact" in q or "hobb" in q or "outside of work" in q:
            return config.funFactAnswer
        if re.search(r"\bwhy\b", q) and re.search(r"passionate|interest|join|excite|want to work|apply|this role|this position|us\b", q):
            return config.whyCompanyAnswer.format(**fmt)
        if re.search(r"cover letter|tell us about yourself|introduce yourself|about you\b|summary|describe your experience|relevant experience", q):
            return config.aboutMeAnswer.format(**fmt)
        if re.search(r"start date|when can you start|notice period|availability|available to start", q):
            return config.availabilityAnswer
        if re.search(r"where are you (located|based)|current location|which city|what city", q):
            return self.answers["inputField"].get("City")
        if re.search(r"authoriz|legally|eligible to work", q):
            return "Yes"
        if "sponsor" in q:
            return "No"
        return None

    def payNumber(self, question: str) -> str:
        if re.search(r"hour|/hr|per hr|hourly", question.lower()):
            return str(config.desiredHourlyPay)
        return str(config.desiredPay)

    def fieldInvalid(self, field: WebElement) -> bool:
        try:
            return bool(self.driver.execute_script("""
const el = arguments[0];
el.dispatchEvent(new Event('blur', {bubbles: true}));
if (el.getAttribute('aria-invalid') === 'true') return true;
if (el.validity && !el.validity.valid) return true;
const box = el.closest('div, label, fieldset');
const text = box && box.parentElement ? box.parentElement.innerText.toLowerCase() : '';
return /enter a (valid )?number|must be a number|numbers only|invalid/.test(text);
""", field))
        except Exception:
            return False

    def isNumericField(self, field: WebElement) -> bool:
        try:
            kind = (field.get_attribute("type") or "").lower()
            mode = (field.get_attribute("inputmode") or "").lower()
            pattern = field.get_attribute("pattern") or ""
            return kind == "number" or mode in ("numeric", "decimal") or "\\d" in pattern or "[0-9]" in pattern
        except Exception:
            return False

    def fillComboboxes(self) -> None:
        scope = self.dialogScope()
        root = scope if scope is not None else self.driver
        for box in root.find_elements(By.CSS_SELECTOR, "button[role='combobox'], div[role='combobox'][tabindex]"):
            try:
                if not box.is_displayed():
                    continue
                current = (box.text or "").strip().lower()
                if current and not re.search(r"^(select|choose|please|--)", current):
                    continue
                question = self.labelFor(box) or self.questionText(box)
                self.click(box)
                time.sleep(1)
                options = [o for o in self.driver.find_elements(By.CSS_SELECTOR, "[role='option']") if o.is_displayed()]
                labels = [(o.text or "").strip() for o in options]
                pairs = [(o, l) for o, l in zip(options, labels) if l and not re.search(r"^(select|choose|please)", l.lower())]
                if not pairs:
                    self.click(box)
                    continue
                index = self.pickSingle(question, [l for _, l in pairs])
                if index is None:
                    index = 0
                self.click(pairs[index][0])
                time.sleep(0.5)
                self.answeredQuestions.append(f"{question[:90]} -> {pairs[index][1]}")
            except Exception:
                continue

    def fillInputs(self) -> None:
        fields = self.driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='tel'], input[type='email'], input[type='number'], input:not([type]), textarea")
        for field in fields:
            try:
                if not field.is_displayed() or not field.is_enabled() or (field.get_attribute("value") or "").strip():
                    continue
                label = self.labelFor(field)
                question = label or self.questionText(field)
                inputType = (field.get_attribute("type") or "").lower()
                value = None
                if inputType == "tel" or "phone" in question or "mobile" in question:
                    value = self.phoneNumber()
                elif inputType == "email" or "email" in question:
                    value = self.email or config.Email
                if not value and re.search(r"if other|please (specify|provide|explain|describe)|other \(please|if yes, (please )?(specify|explain)", question) and len(question) < 80:
                    previous = self.previousQuestion(field)
                    value = (self.openAnswer(previous) if previous else None) or ("No" if previous and re.search(r"refer|family|relative", previous) else None)
                if not value:
                    value = self.openAnswer(question)
                if not value and "year" in question and re.search(r"\b(manag\w*|lead\w*|supervis\w*)\b", question):
                    value = str(config.yearsLeadingTeams)
                if not value:
                    value = self.matchAnswer("inputField", question)
                if not value and (inputType == "number" or "year" in question or "experience" in question):
                    value = str(config.yearsOfExperience)
                if not value and field.tag_name.lower() == "textarea":
                    value = config.genericAnswer.format(company=self.currentCompany or "your company", title=self.currentTitle or "this role")
                if value and self.isNumericField(field) and not re.fullmatch(r"[\d.,]+", str(value).strip()):
                    value = self.payNumber(question) if re.search(r"salary|pay|rate|compensation|wage", question) else str(config.yearsOfExperience)
                if value:
                    self.typeInto(field, str(value))
                    if not re.fullmatch(r"[\d.,]+", str(value).strip()) and self.fieldInvalid(field):
                        value = self.payNumber(question) if re.search(r"salary|pay|rate|compensation|wage", question) else str(config.yearsOfExperience)
                        self.typeInto(field, value)
                    self.answeredQuestions.append(f"{question[:90]} -> {str(value)[:60]}")
            except Exception:
                continue

    def fillSelects(self) -> None:
        for element in self.driver.find_elements(By.TAG_NAME, "select"):
            try:
                if not element.is_displayed():
                    continue
                select = Select(element)
                current = select.first_selected_option.text.strip().lower() if select.all_selected_options else ""
                if current and "select" not in current and current not in ("", "-", "--", "choose", "please choose"):
                    continue
                question = self.questionText(element)
                answer = self.matchAnswer("dropdown", question)
                chosen = False
                if answer:
                    for option in select.options:
                        if answer.lower() in option.text.lower():
                            select.select_by_visible_text(option.text)
                            chosen = True
                            break
                if not chosen and len(select.options) > 1:
                    real = [o for o in select.options if o.text.strip() and not re.search(r"^(select|choose|please|--)", o.text.strip().lower())]
                    index = self.pickSingle(question, [o.text.strip() for o in real]) if real else None
                    target = real[index] if index is not None and index < len(real) else (real[0] if real else select.options[1])
                    select.select_by_visible_text(target.text)
                    self.answeredQuestions.append(f"{question[:90]} -> {target.text.strip()}")
            except Exception:
                continue

    CHOICE_GROUPS_JS = r"""
const groups = [];
for (const g of document.querySelectorAll('[role=radiogroup], [role=group], fieldset')) {
  const inputs = [...g.querySelectorAll('input[type=radio], input[type=checkbox]')];
  if (!inputs.length) continue;
  let question = '';
  const ids = (g.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  for (const id of ids) { const n = document.getElementById(id); if (n) question += ' ' + n.innerText; }
  if (!question.trim()) { const l = g.querySelector('legend'); question = l ? l.innerText : (g.innerText || '').split('\n')[0]; }
  const options = inputs.map(i => {
    let l = i.closest('label');
    if (!l && i.id) l = document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
    return (l ? l.innerText : (i.value || '')).trim();
  });
  groups.push([question.trim(), inputs, options, inputs[0].type === 'checkbox']);
}
return groups;
"""

    def hasSkill(self, text: str) -> bool:
        text = text.lower()
        return any(re.search(r"(?<![a-z0-9])" + re.escape(skill.lower()) + r"(?![a-z0-9])", text) for skill in config.skills)

    def optionTokens(self, label: str) -> List[str]:
        label = re.sub(r"\(.*?\)", "", label.lower())
        label = re.sub(r"\b(only|experience|with|in)\b", " ", label)
        return [t.strip() for t in re.split(r"\+|/|,|&|\band\b", label) if t.strip()]

    def optionMatchesSkills(self, label: str) -> bool:
        tokens = self.optionTokens(label)
        return bool(tokens) and all(self.hasSkill(t) for t in tokens)

    EEO_PATTERN = r"hispanic|latino|ethnicity|\brace\b|racial|gender|\bsex\b|veteran|disabilit|pronoun|sexual orientation|transgender|demographic|self-identif|eeo"
    DECLINE_PATTERN = r"decline|not wish|don't wish|do not wish|prefer not|choose not|not to answer|not to disclose|rather not|i don't want"

    def isEeo(self, question: str, options: List[str]) -> bool:
        return bool(re.search(self.EEO_PATTERN, (question + " " + " ".join(options)).lower()))

    def eeoPick(self, question: str, options: List[str], single: bool) -> Optional[int]:
        lowered = [o.lower() for o in options]
        text = (question + " " + " ".join(options)).lower()
        own = self.matchAnswer("radio", question) or self.matchAnswer("dropdown", question) or self.matchAnswer("checkbox", question)
        if not own:
            for key, value in config.eeoAnswers.items():
                if value and re.search(key, text):
                    own = value
                    break
        if own:
            for i, o in enumerate(lowered):
                if o == own.lower():
                    return i
        for i, o in enumerate(lowered):
            if re.search(self.DECLINE_PATTERN, o):
                return i
        return None

    def ruleAnswer(self, question: str) -> Optional[str]:
        q = question.lower()
        if re.search(r"(ever|previously|formerly|currently|have you) (been )?(employed|worked) (by|for|at)|former employee|current employee|worked for us|employed by us|previously applied|applied (here|to us) before", q):
            return "no"
        if re.search(r"referr|referred", q):
            return "no"
        if re.search(r"relative|family member|related to (anyone|an employee)", q):
            return "no"
        if re.search(r"non-?compete|non-?solicit", q):
            return "no"
        if re.search(r"convicted|felony|criminal", q):
            return "no"
        if re.search(r"\bwaive\b|acknowledge|i agree|do you agree|consent|certify|attest|i understand|accurate and complete|true and complete", q):
            return "yes"
        if re.search(r"18 years|over the age|at least 18", q):
            return "yes"
        if re.search(r"how did you (hear|find|learn)|where did you (hear|find|see)|source", q):
            return self.siteName.lower()
        if re.search(r"highest (level of )?(education|degree)|education level|degree", q):
            return "bachelor"
        if re.search(r"english", q) and re.search(r"proficien|level|fluen", q):
            return "native"
        if re.search(r"willing to travel|able to travel|travel (up to|requirement)", q):
            return "yes"
        if re.search(r"commut|on-?site|in office|in-office|hybrid", q) and re.search(r"willing|able|comfortable|can you", q):
            return "yes"
        if re.search(r"background|drug (test|screen)", q):
            return "yes"
        if re.search(r"(currently|do you) (live|reside) in|located in|based in", q):
            return "yes" if re.search(r"new york|\bny\b|united states|\bus\b|usa|east", q) else None
        if re.search(r"bachelor", q):
            return "yes"
        if "authoriz" in q or "legally" in q or "eligible to work" in q or "right to work" in q:
            if "sponsor" in q and not re.search(r"without|no need|not need", q):
                return "no" if re.search(r"(will you|do you|would you).{0,40}(require|need)", q) else "yes"
            return "yes"
        if "sponsor" in q:
            return "no"
        if "pdf" in q and "resume" in q:
            return "yes"
        if "citizen" in q:
            return "yes"
        if "time zone" in q or "timezone" in q or "region" in q:
            return config.timeZone.lower()
        if "clearance" in q:
            return "no"
        if "background check" in q or "drug" in q:
            return "yes"
        if "relocat" in q:
            return config.willingToRelocate.lower()
        if "remote" in q and ("comfortable" in q or "willing" in q or "able" in q):
            return "yes"
        return None

    def pickSingle(self, question: str, options: List[str]) -> Optional[int]:
        lowered = [o.lower() for o in options]
        if self.isEeo(question, options):
            return self.eeoPick(question, options, single=True)
        answer = self.ruleAnswer(question) or self.matchAnswer("radio", question) or self.matchAnswer("dropdown", question)
        if answer:
            answer = answer.lower()
            for i, o in enumerate(lowered):
                if o == answer:
                    return i
            for i, o in enumerate(lowered):
                if answer in o or (o and o in answer):
                    return i
        if "yes" in lowered and "no" in lowered:
            if self.hasSkill(question):
                return lowered.index("yes")
            if re.search(r"experience|familiar|worked with|proficien|knowledge|skilled|expert", question.lower()):
                return lowered.index("no")
        matches = [i for i, o in enumerate(options) if self.optionMatchesSkills(o)]
        if matches:
            return max(matches, key=lambda i: len(self.optionTokens(options[i])))
        if config.defaultRadioOption and len(options) >= config.defaultRadioOption:
            return config.defaultRadioOption - 1
        return None

    def pickMultiple(self, question: str, options: List[str]) -> List[int]:
        lowered = [o.lower() for o in options]
        if self.isEeo(question, options):
            pick = self.eeoPick(question, options, single=False)
            return [] if pick is None else [pick]
        answer = self.matchAnswer("checkbox", question) or self.matchAnswer("radio", question)
        if answer:
            wanted = [a.strip().lower() for a in answer.split(",") if a.strip()]
            picked = [i for i, o in enumerate(lowered) if any(w == o or w in o for w in wanted)]
            if picked:
                return picked
        matches = [i for i, o in enumerate(options) if self.optionMatchesSkills(o)]
        if len(matches) > 1:
            longest = max(len(self.optionTokens(options[i])) for i in matches)
            matches = [i for i in matches if not ("only" in lowered[i] and len(self.optionTokens(options[i])) < longest)]
        if matches:
            return matches
        for fallback in ["other", "none", "neither", "n/a"]:
            for i, o in enumerate(lowered):
                if o.startswith(fallback):
                    return [i]
        return [0] if options else []

    def fillChoiceGroups(self) -> None:
        try:
            groups = self.driver.execute_script(self.CHOICE_GROUPS_JS) or []
        except Exception:
            return
        for question, inputs, options, isMulti in groups:
            try:
                if any(i.is_selected() for i in inputs):
                    continue
                picks = self.pickMultiple(question, options) if isMulti else [self.pickSingle(question, options)]
                picks = [i for i in picks if i is not None and i < len(inputs)]
                for index in picks:
                    self.click(inputs[index])
                    time.sleep(0.3)
                self.answeredQuestions.append(f"{question[:90]} -> {', '.join(options[i] for i in picks)}")
            except Exception:
                continue

    def fillRadios(self) -> None:
        groups: Dict[str, List[WebElement]] = {}
        for radio in self.driver.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
            name = radio.get_attribute("name") or radio.get_attribute("id") or ""
            groups.setdefault(name, []).append(radio)
        for name, radios in groups.items():
            try:
                if any(r.is_selected() for r in radios):
                    continue
                question = self.questionText(radios[0])
                answer = self.matchAnswer("radio", question)
                target = None
                if answer:
                    for r in radios:
                        labelText = self.questionText(r)
                        valueText = (r.get_attribute("value") or "").lower()
                        if answer.lower() == valueText or answer.lower() in labelText.split():
                            target = r
                            break
                if target is None and config.defaultRadioOption and len(radios) >= config.defaultRadioOption:
                    target = radios[config.defaultRadioOption - 1]
                if target is not None:
                    self.click(target)
            except Exception:
                continue

    submitTexts = ["submit application", "submit", "send application"]
    nextTexts = ["next", "continue", "next step", "review", "review application", "save and continue", "save & continue"]
    successTexts = ["application submitted", "application was submitted", "successfully applied", "application sent", "thanks for applying", "thank you for applying", "you've applied", "you have applied"]

    UNANSWERED_JS = r"""
const root = arguments[0] || document;
const out = [];
const seen = new Set();
function visible(el) { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }
function clean(t) { return (t || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim(); }
function labelOf(el) {
  if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l && clean(l.innerText)) return clean(l.innerText); }
  const ids = (el.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  let t = ''; for (const id of ids) { const n = document.getElementById(id); if (n) t += ' ' + n.innerText; }
  if (clean(t)) return clean(t);
  const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
  let p = el.parentElement;
  for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
    const head = p.querySelector('label, legend, h3, h4, p, span');
    if (head && head !== el && clean(head.innerText) && clean(head.innerText).length < 300) return clean(head.innerText);
  }
  return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
}
function add(q, why) {
  q = clean(q); if (!q || seen.has(q)) return; seen.add(q); out.push(q + (why ? ' [' + why + ']' : ''));
}
for (const el of root.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=radio]):not([type=checkbox]):not([type=search]), textarea, select, button[role=combobox]')) {
  if (!visible(el)) continue;
  if (el.closest('header, nav')) continue;
  let empty;
  if (el.tagName === 'SELECT') empty = el.selectedIndex < 0 || /^(select|choose|please|--|)$/i.test(clean(el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''));
  else if (el.tagName === 'BUTTON') empty = !clean(el.innerText) || /^(select|choose|please)/i.test(clean(el.innerText));
  else empty = !clean(el.value);
  const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelOf(el) + ' ') ;
  const invalid = el.getAttribute('aria-invalid') === 'true' || (el.validity && !el.validity.valid);
  if (invalid) add(labelOf(el), empty ? 'empty' : 'invalid: ' + clean(el.value || el.innerText).slice(0, 40));
  else if (empty && required) add(labelOf(el), 'empty');
}
for (const g of root.querySelectorAll('[role=radiogroup], [role=group], fieldset')) {
  const inputs = [...g.querySelectorAll('input[type=radio], input[type=checkbox]')];
  if (!inputs.length || inputs.some(i => i.checked)) continue;
  let q = '';
  const ids = (g.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  for (const id of ids) { const n = document.getElementById(id); if (n) q += ' ' + n.innerText; }
  if (!clean(q)) { const l = g.querySelector('legend'); q = l ? l.innerText : (g.innerText || '').split('\n')[0]; }
  const required = inputs.some(i => i.required || i.getAttribute('aria-required') === 'true') || g.getAttribute('aria-required') === 'true' || /\*/.test(q);
  if (required) add(q, 'not selected');
}
const fields = [...root.querySelectorAll('input:not([type=hidden]):not([type=submit]), textarea, select, button[role=combobox], [role=radiogroup], [role=group]')];
for (const e of root.querySelectorAll('[role=alert], [class*=error], [class*=Error], [id*=error]')) {
  if (!visible(e)) continue;
  const msg = clean(e.innerText);
  if (!msg || msg.length > 200) continue;
  let field = null;
  for (const f of fields) { if (f.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING && !f.contains(e)) field = f; }
  let q = '';
  if (field) {
    if (field.matches('[role=radiogroup], [role=group]')) {
      const ids = (field.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
      for (const id of ids) { const n = document.getElementById(id); if (n) q += ' ' + n.innerText; }
      q = clean(q);
    } else q = labelOf(field);
  }
  q = q || 'Form error';
  const idx = out.findIndex(x => x.startsWith(q + ' [') || x === q);
  if (idx >= 0) { if (!out[idx].includes(msg)) out[idx] = out[idx].replace(/\]$/, '; ' + msg + ']'); }
  else add(q, msg);
}
return out.slice(0, 15);
"""

    def collectUnanswered(self) -> List[str]:
        try:
            scope = self.dialogScope()
            return [q for q in (self.driver.execute_script(self.UNANSWERED_JS, scope) or []) if q]
        except Exception:
            return []

    def completeApplication(self, maxSteps: int = 10) -> str:
        self.unansweredQuestions = []
        result = self.runApplicationSteps(maxSteps)
        if result == "failed":
            self.unansweredQuestions = self.collectUnanswered()
        return result

    def runApplicationSteps(self, maxSteps: int = 10) -> str:
        for _ in range(maxSteps):
            self.pause(2, 4)
            if any(t in self.pageText() for t in self.successTexts):
                return "applied"
            self.fillForm()
            scope = self.dialogScope()
            submit = self.findClickable(self.submitTexts, exact=True, scope=scope) or self.findClickable(["submit"], scope=scope)
            if submit is not None:
                if config.dryRun:
                    return "dry"
                self.click(submit)
                self.pause(3, 5)
                text = self.pageText()
                if any(t in text for t in self.successTexts) or self.findClickable(self.submitTexts, exact=True, scope=self.dialogScope()) is None:
                    return "applied"
                continue
            nextButton = self.findClickable(self.nextTexts, exact=True, scope=scope)
            if nextButton is None:
                return "failed"
            self.click(nextButton)
        return "failed"

    def isBlacklisted(self, title: str, company: str) -> str:
        reasons = []
        titles = [b for b in config.blackListTitles if b.lower() in title.lower()]
        companies = [b for b in config.blacklistCompanies if b.lower() in company.lower()]
        if titles:
            reasons.append("blacklisted title: " + " ".join(titles))
        if companies:
            reasons.append("blacklisted company: " + " ".join(companies))
        return ", ".join(reasons)

    failedHeader = ["Date", "Time", "Site", "Job Title", "Company", "Location", "Reason", "Link", "Questions Not Answered"]

    def record(self, properties: str, result: str, url: str) -> None:
        result = " ".join(result.split())
        self.displayWriteResults(f"{properties} | {result}: {url}")
        failed = "🥵" in result or "⚠️" in result
        questions = self.unansweredQuestions if failed and "manual answers" in result else []
        for q in questions:
            self.displayWriteResults(f"      ❌ Not answered: {q}")
        if failed:
            self.logFailed(result, url, questions)
        self.unansweredQuestions = []

    def logFailed(self, result: str, url: str, questions: Optional[List[str]] = None) -> None:
        questions = questions or []
        reason = re.sub(r"^[\s*🥵⚠️]+", "", result).strip()
        row = [time.strftime("%Y-%m-%d"), time.strftime("%H:%M:%S"), self.siteName,
               self.currentTitle, self.currentCompany, self.currentLocation, reason, url, " ; ".join(questions)]
        try:
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", "Failed Jobs.csv")
            rows = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    rows = [r for r in csv.reader(f)][1:]
            rows = [r + [""] * (len(self.failedHeader) - len(r)) for r in rows if len(r) < 8 or r[7] != url]
            rows.append(row)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.failedHeader)
                writer.writerows(rows)
            day = os.path.join("data", f"Failed Jobs - {time.strftime('%Y%m%d')}.txt")
            with open(day, "a", encoding="utf-8") as f:
                f.write(f"{row[0]} {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} | {row[6]} | {row[7]}\n")
                for q in questions:
                    f.write(f"      ❌ Not answered: {q}\n")
        except Exception as e:
            utils.prRed("❌ Could not write failed job log: " + str(e)[:80])

    def applied(self) -> None:
        self.countApplied += 1

    def reachedCap(self) -> bool:
        return bool(config.maxApplicationsPerRun) and self.countApplied >= config.maxApplicationsPerRun

    def displayWriteResults(self, line: str) -> None:
        try:
            print(line)
            os.makedirs("data", exist_ok=True)
            utils.writeResults(line)
        except Exception as e:
            utils.prRed("❌ Error writing results: " + str(e))

    def run(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        startTime = time.time()
        utils.printInfoMes(self.siteName)
        try:
            if self.ensureLoggedIn():
                self.run()
        except KeyboardInterrupt:
            utils.prYellow("🛑 Stopped by user.")
        finally:
            if self.reachedCap():
                utils.prYellow(f"🛑 Reached max applications per run limit ({config.maxApplicationsPerRun}).")
            utils.prYellow(f"---- {self.siteName} ----")
            utils.printSessionSummary(self.countJobs, self.countApplied, self.countBlacklisted,
                                      self.countAlreadyApplied, self.countCannotApply, time.time() - startTime)
            try:
                self.driver.quit()
            except Exception:
                pass
