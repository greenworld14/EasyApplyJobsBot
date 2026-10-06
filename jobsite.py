import csv
import hashlib
import json
import os
import pickle
import random
import re
import shutil
import sys
import time
from datetime import date, timedelta
from typing import Dict, List, Optional

import config
import constants
import techblacklist
import utils
from llm import AnswerAssistant, LLMClient

sys.stdout.reconfigure(encoding='utf-8')

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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
        self.answersMtime = self.answersFileMtime()
        self.answers = self.loadAnswers()
        self.driver = self.createDriver()
        self.cookiesPath = os.path.join(os.getcwd(), "cookies", f"{self.siteName.lower()}_{self.getHash(email or 'profile')}.pkl")

    def clearStaleDriverLock(self) -> None:
        lockPath = os.path.join(os.path.expanduser("~"), ".wdm", ".wdm-lock-chromedriver-win64")
        try:
            if os.path.exists(lockPath) and time.time() - os.path.getmtime(lockPath) > 120:
                os.remove(lockPath)
        except OSError:
            pass

    def createDriver(self) -> webdriver.Chrome:
        options = utils.chromeBrowserOptions()
        options.page_load_strategy = "eager"
        self.clearStaleDriverLock()
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

    def answersFileMtime(self) -> float:
        try:
            return os.path.getmtime("additionalQuestions.yaml")
        except OSError:
            return 0.0

    def loadAnswers(self, keepOnError: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Dict[str, str]]:
        empty = {"inputField": {}, "radio": {}, "dropdown": {}, "checkbox": {}}
        if yaml is None or not os.path.exists("additionalQuestions.yaml"):
            return keepOnError or empty
        try:
            with open("additionalQuestions.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            manual = {key: set() for key in empty}
            for key in empty:
                for k, v in (data.get(key) or {}).items():
                    if isinstance(v, dict):
                        if v.get("manual") or v.get("answer") is None or not str(v.get("answer")).strip():
                            manual[key].add(str(k))
                            continue
                        v = v.get("answer")
                    if v is None or not str(v).strip():
                        manual[key].add(str(k))
                        continue
                    empty[key][str(k)] = str(v)
            self.manualKeys = manual
        except Exception as e:
            utils.prRed("❌ additionalQuestions.yaml has a format error, saved answers are NOT being used: " + " ".join(str(e).split())[0:160])
            return keepOnError or empty
        return empty

    def refreshAnswers(self) -> None:
        mtime = self.answersFileMtime()
        if mtime == self.answersMtime:
            return
        self.answersMtime = mtime
        self.answers = self.loadAnswers(keepOnError=self.answers)
        utils.prYellow("🔄 additionalQuestions.yaml changed, reloaded saved answers.")

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
            return any(t in title for t in ["just a moment", "attention required", "security check", "additional verification required", "verification required", "verify you are human", "are you a robot"])
        except Exception:
            return False

    def pageLoadProblem(self) -> str:
        if self.onChallenge():
            return "security check was not solved"
        try:
            title = (self.driver.title or "").lower().replace("’", "'")
        except Exception:
            return "browser window is not responding"
        if re.search(r"can't be reached|is not available|no internet|err_[a-z_]+|took too long to respond|page isn't working", title):
            return f"page did not load ({title[:60]})"
        return ""

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

    def normalizeText(self, text: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9#+]+", " ", (text or "").lower()).split())

    def keyMatches(self, key: str, question: str, normalized: str) -> bool:
        k = key.lower().strip()
        if k and k in question and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?:e?s)?(?![a-z0-9])", question):
            return True
        nk = self.normalizeText(k)
        return len(nk) >= 12 and f" {nk} " in f" {normalized} "

    def yamlLookup(self, sections: List[str], question: str):
        question = (question or "").lower()
        normalized = self.normalizeText(question)
        best = None
        for section in sections:
            for key, value in self.answers.get(section, {}).items():
                if (best is None or len(key) > best[0]) and self.keyMatches(key, question, normalized):
                    best = (len(key), key, value, False)
            for key in getattr(self, "manualKeys", {}).get(section, ()):
                if (best is None or len(key) > best[0]) and self.keyMatches(key, question, normalized):
                    best = (len(key), key, None, True)
        return best

    def matchAnswer(self, section: str, question: str) -> Optional[str]:
        best = self.yamlLookup([section], question)
        return best[2] if best and not best[3] else None

    def manualEntry(self, question: str, sections: Optional[List[str]] = None) -> Optional[str]:
        best = self.yamlLookup(sections or ["inputField", "radio", "dropdown", "checkbox"], question)
        return best[1] if best and best[3] else None

    def cleanQuestion(self, question: str) -> str:
        return re.sub(r"\s*(\*|\(required\)|required)\s*$", "", " ".join((question or "").split()), flags=re.I).strip()

    def flagManual(self, question: str, key: str) -> None:
        self.lastAiFlagged = True
        question = self.cleanQuestion(question)
        if not hasattr(self, "flaggedQuestions"):
            self.flaggedQuestions = []
        if question.lower() in self.flaggedQuestionKeys():
            return
        reason = "manual entry in additionalQuestions.yaml"
        self.flaggedQuestions.append((question, reason))
        self.displayWriteResults(f"      🚩 Needs manual answer ({reason}: {key[:60]}): {question[:120]}")

    def phoneNumber(self) -> str:
        if getattr(config, "Phone", "").strip():
            return config.Phone.strip()
        return self.answers["inputField"].get("Phone Number", "").strip()

    def fillForm(self) -> None:
        self.refreshAnswers()
        self.fillSearchComboboxes()
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
        if re.search(r"(current|most recent|present|latest) (employer|company)|where do you (currently )?work|employer name", q) and len(q) < 70 and not re.search(r"\bwhy\b|driv|leav|reason|interest|excit|motivat", q):
            return config.currentEmployer
        if re.search(r"(current|most recent|present|latest) (job )?(title|position|role)", q) and len(q) < 70 and not re.search(r"\bwhy\b|driv|leav|reason|interest|excit|motivat", q):
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
        if re.search(r"salary|compensation|\bpay\b|hourly rate|pay rate|\brate\b|wage", q) and not re.search(r"familiar|experience (with|in)|knowledge of", q):
            if re.search(r"\balign|within (the|our|your|this) (range|budget)|comfortable with (the|our|this)|ok(ay)? with (the|our|this)|fall within", q):
                return config.salaryAlignAnswer
            return self.payNumber(q)
        if "time zone" in q or "timezone" in q:
            return config.timeZoneAnswer
        if "fun fact" in q or "hobb" in q or "outside of work" in q:
            return config.funFactAnswer
        if re.search(r"\bwhy\b", q) and re.search(r"passionate|interest|join|excite|want to work|want this|this job|apply|this role|this position|us\b", q):
            return config.whyCompanyAnswer.format(**fmt)
        if re.search(r"cover letter|tell us about yourself|introduce yourself|about you\b|summary|describe your experience|relevant experience", q):
            return config.aboutMeAnswer.format(**fmt)
        if re.search(r"start date|when (can|could|would|will) you (be able to )?(start|begin|join)|able to (start|begin|join)|notice period|availability|available to (start|begin|join)", q):
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

    FACT_PATTERN = r"address|\bzip\b|postal|\bcity\b|\btown\b|\bstate\b|province|\bcountry\b|first name|last name|full name|legal name|your name|preferred (first )?name|given name|surname|family name|^\s*name\b|linkedin|github|portfolio|website|personal site|(current|most recent|present|latest) (employer|company|job title|title|position|role)|employer name|school|college|university|graduat|highest (level of )?(education|degree)|education level|salary|compensation|\bpay\b|hourly rate|pay rate|desired rate|\bwage|time zone|timezone|how did you (hear|find|learn)|where did you (hear|find|see)|start date|when can you start|available to start|notice period|earliest start"

    def factAnswer(self, question: str) -> Optional[str]:
        q = question.lower()
        if not re.search(self.FACT_PATTERN, q):
            return None
        return self.openAnswer(q)

    def comboboxAnswer(self, question: str) -> str:
        q = question.lower()
        if re.search(self.SOURCE_PATTERN, q):
            return self.siteName
        candidates = [self.matchAnswer("dropdown", q), self.matchAnswer("radio", q), self.ruleAnswer(q), self.factAnswer(q) if self.aiEnabled() else self.openAnswer(q), self.matchAnswer("inputField", q)]
        for value in candidates:
            if value and len(str(value)) <= 60:
                return str(value)
        return ""

    def visibleOptions(self) -> List[WebElement]:
        try:
            return [o for o in self.driver.find_elements(By.CSS_SELECTOR, "[role='option'], [role='listbox'] li, li[id*='option']") if o.is_displayed() and (o.text or "").strip()]
        except Exception:
            return []

    def fillSearchComboboxes(self) -> None:
        fields = self.driver.find_elements(By.CSS_SELECTOR, "input[role='combobox'], input[aria-autocomplete='list'], input[aria-autocomplete='both']")
        for field in fields:
            try:
                if not field.is_displayed() or not field.is_enabled() or (field.get_attribute("value") or "").strip():
                    continue
                label = self.labelFor(field)
                question = label if label and not re.search(r"^(search|select|type)\b", label) else (self.previousQuestion(field) or label)
                answer = self.comboboxAnswer(question)
                self.click(field)
                time.sleep(0.8)
                if answer:
                    field.send_keys(answer[:20])
                    time.sleep(1.5)
                options = self.visibleOptions()
                if not options and answer:
                    field.send_keys(Keys.CONTROL, "a")
                    field.send_keys(Keys.DELETE)
                    time.sleep(1)
                    options = self.visibleOptions()
                if not options:
                    field.send_keys(Keys.ARROW_DOWN)
                    time.sleep(1)
                    options = self.visibleOptions()
                labels = [(o.text or "").strip() for o in options]
                if not labels:
                    continue
                index = self.pickSingle(question, labels)
                if index is None and answer:
                    index = next((i for i, l in enumerate(labels) if answer.lower() in l.lower() or l.lower() in answer.lower()), None)
                if index is None and (self.aiEnabled() or self.lastAiFlagged):
                    field.send_keys(Keys.ESCAPE)
                    continue
                if index is None:
                    index = 0
                self.click(options[index])
                time.sleep(0.6)
                self.answeredQuestions.append(f"{question[:90]} -> {labels[index]} [{getattr(self, 'lastPickSource', 'default')}]")
            except Exception:
                continue

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
                if index is None and (self.aiEnabled() or self.lastAiFlagged):
                    self.click(box)
                    continue
                if index is None:
                    index = 0
                self.click(pairs[index][0])
                time.sleep(0.5)
                self.answeredQuestions.append(f"{question[:90]} -> {pairs[index][1]} [{getattr(self, 'lastPickSource', 'default')}]")
            except Exception:
                continue

    def fillInputs(self) -> None:
        fields = self.driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='tel'], input[type='email'], input[type='number'], input[type='date'], input:not([type]), textarea")
        for field in fields:
            try:
                if not field.is_displayed() or not field.is_enabled() or (field.get_attribute("value") or "").strip():
                    continue
                label = self.labelFor(field)
                question = label or self.questionText(field)
                inputType = (field.get_attribute("type") or "").lower()
                if (field.get_attribute("role") or "").lower() == "combobox" or (field.get_attribute("aria-autocomplete") or "").lower() in ("list", "both"):
                    continue
                if self.isDateField(field, question):
                    self.fillDate(field, question)
                    continue
                value = None
                if inputType == "tel" or "phone" in question or "mobile" in question:
                    value = self.phoneNumber()
                elif inputType == "email" or "email" in question:
                    value = self.email or config.Email
                if not value and re.search(r"if other|please (specify|provide|explain|describe)|other \(please|if yes, (please )?(specify|explain)", question) and len(question) < 80:
                    previous = self.previousQuestion(field)
                    value = (self.openAnswer(previous) if previous else None) or ("No" if previous and re.search(r"refer|family|relative", previous) else None)
                isTextarea = field.tag_name.lower() == "textarea"
                source = "profile" if value else ""
                if not value:
                    value = self.matchAnswer("inputField", question)
                    countQuestion = re.search(r"how many|number of", question)
                    if value and re.fullmatch(r"[\d.,]+", value.strip()) and not countQuestion and (isTextarea or not re.search(r"year|how much|\brate\b|scale|gpa|hours|days|weeks?\b", question)):
                        value = None
                    source = "yaml" if value else ""
                if not value and re.search(r"address line 2|address 2\b|\bapt\b|apartment|suite|unit number|\bsuffix\b", question):
                    continue
                if not value:
                    manualKey = self.manualEntry(label or question)
                    if manualKey:
                        self.flagManual(label or question, manualKey)
                        continue
                if not value:
                    value = self.factAnswer(question) if self.aiEnabled() else self.openAnswer(question)
                    source = "profile" if value else ""
                if not value and "year" in question and re.search(r"\b(manag\w*|lead\w*|supervis\w*)\b", question):
                    value = str(config.yearsLeadingTeams)
                    source = "profile"
                if not value and self.aiEnabled():
                    source = "ai"
                    numeric = inputType == "number" or self.isNumericField(field) or re.search(r"how many|number of|\byears?\b", question)
                    value = self.aiAnswer(label or question, "number" if numeric else ("text" if isTextarea else "short"))
                    if value is None and self.lastAiFlagged:
                        continue
                if not value and (not isTextarea or re.search(r"how many years", question)) and (inputType == "number" or self.isNumericField(field) or re.search(r"\byears?\b|how many", question)):
                    value = str(config.yearsOfExperience)
                if not value and isTextarea:
                    value = config.genericAnswer.format(company=self.currentCompany or "your company", title=self.currentTitle or "this role")
                if value and self.isNumericField(field) and not re.fullmatch(r"[\d.,]+", str(value).strip()):
                    if "gpa" in question:
                        continue
                    value = self.payNumber(question) if re.search(r"salary|pay|rate|compensation|wage", question) else str(config.yearsOfExperience)
                if value:
                    self.typeInto(field, str(value))
                    if not re.fullmatch(r"[\d.,]+", str(value).strip()) and self.fieldInvalid(field):
                        value = self.payNumber(question) if re.search(r"salary|pay|rate|compensation|wage", question) else str(config.yearsOfExperience)
                        self.typeInto(field, value)
                    self.answeredQuestions.append(f"{question[:90]} -> {str(value)[:60]} [{source or 'default'}]")
            except Exception:
                continue

    DATE_PICK_JS = r"""
const [y, m, d] = arguments;
const months = ['january','february','march','april','may','june','july','august','september','october','november','december'];
function visible(el) { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
function cls(el) { const c = el.className; return ((c && c.baseVal !== undefined) ? c.baseVal : (c || '')).toLowerCase(); }
function disabled(el) { return el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled|outside|other-month|othermonth|prev-month-day|next-month-day|prevmonthday|nextmonthday|unavailable|blocked/.test(cls(el)); }
const pickers = [...document.querySelectorAll('[role=dialog], [role=grid], [role=application], [class*=calendar], [class*=Calendar], [class*=datepicker], [class*=DatePicker], [class*=date-picker], [class*=picker], .flatpickr-calendar, .ui-datepicker, .pika-single')].filter(visible);
if (!pickers.length) return null;
const iso = y + '-' + String(m).padStart(2, '0') + '-' + String(d).padStart(2, '0');
const name = months[m - 1];
const short = name.slice(0, 3);
const dayRe = new RegExp('\\b(' + name + '|' + short + ')\\s+' + d + '(st|nd|rd|th)?,?\\s+' + y + '\\b|\\b' + d + '(st|nd|rd|th)?\\s+(' + name + '|' + short + '),?\\s+' + y + '\\b|\\b0?' + m + '/0?' + d + '/' + y + '\\b', 'i');
for (const p of pickers) {
  for (const el of p.querySelectorAll('[data-date], [data-day], [data-value], [data-iso], [aria-label], [title]')) {
    if (!visible(el) || disabled(el)) continue;
    const attrs = ['data-date', 'data-day', 'data-value', 'data-iso', 'aria-label', 'title'].map(a => el.getAttribute(a) || '').join(' ');
    if (attrs.includes(iso) || dayRe.test(attrs)) return ['day', el];
  }
  for (const td of p.querySelectorAll('td[data-month][data-year]')) {
    if (+td.getAttribute('data-year') === y && +td.getAttribute('data-month') === m - 1 && (td.innerText || '').trim() === String(d) && visible(td)) return ['day', td.querySelector('a, button') || td];
  }
}
for (const p of pickers) {
  const text = (p.innerText || '').toLowerCase();
  if (!(text.includes(String(y)) && (text.includes(name) || new RegExp('\\b' + short + '\\b').test(text)))) continue;
  const cells = [...p.querySelectorAll('[role=gridcell], td, button, [class*=day]')].filter(el => visible(el) && !disabled(el) && (el.innerText || '').trim() === String(d) && !el.querySelector('[role=gridcell], td, button'));
  if (cells.length) return ['day', cells[0]];
}
for (const p of pickers) {
  for (const el of p.querySelectorAll('button, a, span, div[role=button], [class*=next], [class*=Next]')) {
    if (!visible(el) || disabled(el)) continue;
    const t = [el.getAttribute('aria-label') || '', el.getAttribute('title') || '', cls(el), (el.innerText || '').trim()].join(' ').toLowerCase();
    if (/year/.test(t)) continue;
    if (/next( month)?|navigation--next|arrow-right|chevron-right|(^|\s)(›|»|>|→)\s*$/.test(t)) return ['next', el];
  }
}
return ['none', null];
"""

    def isDateField(self, field: WebElement, question: str) -> bool:
        try:
            kind = (field.get_attribute("type") or "").lower()
            if kind in ("date", "datetime-local"):
                return True
            if field.tag_name.lower() == "textarea":
                return False
            attrs = " ".join((field.get_attribute(a) or "") for a in ["placeholder", "name", "id", "class", "autocomplete", "pattern", "data-testid"]).lower()
            if re.search(r"mm\s*[/.-]\s*dd|dd\s*[/.-]\s*mm|yyyy|(?<![a-z])date(?![a-z])|datepicker|date-picker|date_picker|calendar", attrs):
                return True
            return bool(re.search(r"\bdate\b", question)) and not re.search(r"birth|\bdob\b", question)
        except Exception:
            return False

    def dateFor(self, question: str) -> Optional[date]:
        q = question.lower()
        if re.search(r"birth|\bdob\b|graduat|expir|issue", q):
            return None
        if re.search(r"today|signature|\bsign", q):
            return date.today()
        return date.today() + timedelta(days=14)

    def dateText(self, field: WebElement, target: date) -> str:
        hint = " ".join((field.get_attribute(a) or "") for a in ["placeholder", "pattern", "data-date-format", "aria-label"]).lower()
        if (field.get_attribute("type") or "").lower() == "date" or re.search(r"yyyy\s*-\s*mm\s*-\s*dd", hint):
            return target.isoformat()
        order = re.search(r"dd\s*([/.-])\s*mm", hint)
        if order:
            sep = order.group(1)
            return target.strftime(f"%d{sep}%m{sep}%Y")
        return target.strftime("%m/%d/%Y")

    def pickFromCalendar(self, field: WebElement, target: date) -> bool:
        self.sawCalendar = False
        self.click(field)
        time.sleep(1)
        for _ in range(4):
            try:
                result = self.driver.execute_script(self.DATE_PICK_JS, target.year, target.month, target.day)
            except Exception:
                return False
            if result:
                self.sawCalendar = True
            if not result or result[0] == "none":
                return False
            self.click(result[1])
            time.sleep(0.8)
            if result[0] == "day":
                return bool((field.get_attribute("value") or "").strip())
        return False

    def setDateValue(self, field: WebElement, value: str) -> None:
        self.driver.execute_script("""
const el = arguments[0], v = arguments[1];
const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
desc.set.call(el, v);
for (const e of ['input', 'change', 'blur']) el.dispatchEvent(new Event(e, {bubbles: true}));
""", field, value)

    def fillDate(self, field: WebElement, question: str) -> None:
        target = self.dateFor(question)
        if target is None:
            return
        kind = (field.get_attribute("type") or "").lower()
        value = self.dateText(field, target)
        if kind == "date":
            self.setDateValue(field, value)
        elif kind == "datetime-local":
            self.setDateValue(field, value + "T09:00")
        elif not self.pickFromCalendar(field, target):
            if not (field.get_attribute("value") or "").strip():
                try:
                    self.typeInto(field, value)
                except Exception:
                    self.setDateValue(field, value)
            if getattr(self, "sawCalendar", False):
                try:
                    field.send_keys(Keys.ESCAPE)
                except Exception:
                    pass
        self.answeredQuestions.append(f"{question[:90]} -> {field.get_attribute('value') or value}")

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
                    if index is None and (self.aiEnabled() or self.lastAiFlagged):
                        continue
                    target = real[index] if index is not None and index < len(real) else (real[0] if real else select.options[1])
                    select.select_by_visible_text(target.text)
                    self.answeredQuestions.append(f"{question[:90]} -> {target.text.strip()} [{getattr(self, 'lastPickSource', 'default')}]")
            except Exception:
                continue

    CHOICE_GROUPS_JS = r"""
function clean(t) { return (t || '').replace(/\s+/g, ' ').trim(); }
function optionLabel(i) {
  let l = i.closest('label');
  if (!l && i.id) l = document.querySelector('label[for="' + CSS.escape(i.id) + '"]');
  if (l && clean(l.innerText)) return clean(l.innerText);
  const ids = (i.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  let t = ''; for (const id of ids) { const n = document.getElementById(id); if (n) t += ' ' + n.innerText; }
  if (clean(t)) return clean(t);
  if (i.getAttribute('aria-label')) return clean(i.getAttribute('aria-label'));
  let n = i.nextSibling;
  while (n && !clean(n.textContent)) n = n.nextSibling;
  if (n && clean(n.textContent).length < 120) return clean(n.textContent);
  const p = i.parentElement;
  if (p && p.querySelectorAll('input').length === 1 && clean(p.innerText) && clean(p.innerText).length < 120) return clean(p.innerText);
  return clean(i.value);
}
function precedingText(g) {
  let el = g;
  for (let depth = 0; depth < 4 && el; depth++, el = el.parentElement) {
    let prev = el.previousElementSibling;
    for (let k = 0; k < 3 && prev; k++, prev = prev.previousElementSibling) {
      if (prev.querySelector && prev.querySelector('input, select, textarea')) break;
      const t = clean(prev.innerText);
      if (t && t.length < 300) return t;
    }
  }
  return '';
}
const groups = [];
for (const g of document.querySelectorAll('[role=radiogroup], [role=group], fieldset')) {
  const inputs = [...g.querySelectorAll('input[type=radio], input[type=checkbox]')];
  if (!inputs.length) continue;
  const options = inputs.map(optionLabel);
  let question = '';
  const ids = (g.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  for (const id of ids) { const n = document.getElementById(id); if (n) question += ' ' + n.innerText; }
  if (!clean(question)) { const l = g.querySelector('legend'); if (l) question = l.innerText; }
  if (!clean(question) || options.includes(clean(question))) question = precedingText(g) || (g.getAttribute('aria-label') || '');
  groups.push([clean(question), inputs, options, inputs[0].type === 'checkbox']);
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

    DEMOGRAPHIC_OPTION_PATTERN = r"^(man|woman|male|female|non-?binary|genderqueer|agender|two-spirit|trans\w*|cisgender|asexual|bisexual|pansexual|gay|lesbian|queer|heterosexual|straight|black|white|asian|hispanic|latin\w*|native|american indian|alaska native|pacific islander|native hawaiian|middle eastern|north african|two or more|multiracial)\b"

    def isEeo(self, question: str, options: List[str]) -> bool:
        if re.search(self.EEO_PATTERN, (question + " " + " ".join(options)).lower()):
            return True
        return sum(1 for o in options if re.search(self.DEMOGRAPHIC_OPTION_PATTERN, o.lower().strip())) >= 2 or bool(re.search(self.DEMOGRAPHIC_OPTION_PATTERN, question.lower().strip()))

    def unreadableOptions(self, options: List[str]) -> bool:
        return not options or all(re.fullmatch(r"[\d\s_-]*", o or "") for o in options)

    def eeoPick(self, question: str, options: List[str], single: bool) -> Optional[int]:
        lowered = [o.lower().strip() for o in options]
        text = (question + " " + " ".join(options)).lower()
        own = self.matchAnswer("radio", question) or self.matchAnswer("dropdown", question) or self.matchAnswer("checkbox", question)
        if own:
            for i, o in enumerate(lowered):
                if o == own.lower():
                    return i
        for i, o in enumerate(lowered):
            if re.search(self.DECLINE_PATTERN, o):
                return i
        for key, value in config.eeoAnswers.items():
            if not value or not re.search(key, text):
                continue
            wanted = value.lower().strip()
            for i, o in enumerate(lowered):
                if o == wanted:
                    return i
            stems = [t.strip()[:5] for t in re.split(r"\s+or\s+|,|/", wanted) if len(t.strip()) >= 4]
            for i, o in enumerate(lowered):
                if stems and not re.match(r"\s*not\b", o) and any(re.search(r"\b" + re.escape(stem), o) for stem in stems):
                    return i
            if "yes" in lowered and "no" in lowered:
                q = question.lower()
                if wanted in q:
                    return lowered.index("no") if re.search(r"\bnot\s+" + re.escape(wanted), q) else lowered.index("yes")
        return None

    def ruleAnswer(self, question: str) -> Optional[str]:
        q = question.lower()
        if re.search(r"(ever|previously|formerly|currently|have you) (been )?(employed|worked) (by|for|at)|former employee|current employee|worked for us|employed by us|previously applied|applied (here|to us) before", q):
            return "no"
        if re.search(r"referr|referred", q):
            return "no"
        if re.search(r"know (anyone|someone|any one|any employees?)|anyone (that|who) (currently )?works|friends? or family (that|who) work", q):
            return "no"
        if re.search(r"relative|family member|related to (anyone|an employee)", q):
            return "no"
        if re.search(r"non-?compete|non-?solicit", q):
            return "no"
        if re.search(r"convicted|felony|criminal", q):
            return "no"
        if re.search(r"\bwaive\b|acknowledge|i agree|do you agree|consent|certify|attest|i understand|accurate and complete|true and complete|have you read .*(privacy|policy|notice|terms)|(read|reviewed) (and understood )?the .*(privacy|policy|notice|terms)", q):
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

    SOURCE_PATTERN = r"how did you (hear|find|learn|come across)|where did you (hear|find|see|learn)|source of (this )?application|referral source|how were you referred"

    def sourceIndex(self, question: str, options: List[str]) -> Optional[int]:
        if not re.search(self.SOURCE_PATTERN, question.lower()):
            return None
        lowered = [o.lower().strip() for o in options]
        for candidate in [self.siteName.lower(), "job board", "job site", "jobsite", "online job", "job posting", "job search", "online", "website", "internet", "other"]:
            for i, o in enumerate(lowered):
                if candidate and candidate in o:
                    return i
        return None

    def pickSingle(self, question: str, options: List[str]) -> Optional[int]:
        self.lastAiFlagged = False
        self.lastPickSource = "profile"
        lowered = [o.lower() for o in options]
        if self.unreadableOptions(options):
            return None
        if self.isEeo(question, options):
            return self.eeoPick(question, options, single=True)
        source = self.sourceIndex(question, options)
        if source is not None:
            return source
        yamlAnswer = self.matchAnswer("radio", question) or self.matchAnswer("dropdown", question)
        if not yamlAnswer:
            manualKey = self.manualEntry(question)
            if manualKey:
                self.flagManual(question, manualKey)
                return None
        if not yamlAnswer and not self.isEeo(question, options):
            consent = [i for i, o in enumerate(lowered) if re.match(r"\s*(yes,?\s*)?(i\s+)?(consent|agree|acknowledge|accept|understand|have read)\b", o)]
            if consent and len(consent) < len(options):
                return consent[0]
        for answer, origin in [(yamlAnswer, "yaml"), (self.ruleAnswer(question), "profile")]:
            if not answer:
                continue
            answer = answer.lower()
            self.lastPickSource = origin
            for i, o in enumerate(lowered):
                if o == answer:
                    return i
            for i, o in enumerate(lowered):
                if answer in o or (o and o in answer):
                    return i
        if self.aiEnabled():
            self.lastPickSource = "ai"
            picked = self.aiAnswer(question, "choice", options)
            if picked is not None:
                return options.index(picked)
            return None
        self.lastPickSource = "default"
        if "yes" in lowered and "no" in lowered:
            if self.hasSkill(question):
                return lowered.index("yes")
            if re.search(r"experience|familiar|worked with|proficien|knowledge|skilled|expert", question.lower()):
                return lowered.index("no")
        matches = [i for i, o in enumerate(options) if self.optionMatchesSkills(o)]
        if matches:
            return max(matches, key=lambda i: len(self.optionTokens(options[i])))
        if not ("yes" in lowered and "no" in lowered):
            for i, o in enumerate(lowered):
                if re.match(r"(none|neither|not applicable|n/a|no experience|i (do not|don't) have)\b", o.strip()):
                    return i
        if config.defaultRadioOption and len(options) >= config.defaultRadioOption:
            return config.defaultRadioOption - 1
        return None

    def pickMultiple(self, question: str, options: List[str]) -> List[int]:
        self.lastAiFlagged = False
        lowered = [o.lower() for o in options]
        if self.unreadableOptions(options):
            return []
        if self.isEeo(question, options):
            pick = self.eeoPick(question, options, single=False)
            return [] if pick is None else [pick]
        source = self.sourceIndex(question, options)
        if source is not None:
            return [source]
        self.lastPickSource = "profile"
        yamlAnswer = self.matchAnswer("checkbox", question) or self.matchAnswer("radio", question)
        if not yamlAnswer:
            manualKey = self.manualEntry(question)
            if manualKey:
                self.flagManual(question, manualKey)
                return []
        for answer, origin in [(yamlAnswer, "yaml"), (self.ruleAnswer(question), "profile")]:
            if not answer:
                continue
            wanted = [a.strip().lower() for a in answer.split(",") if a.strip()]
            picked = [i for i, o in enumerate(lowered) if any(w == o or w in o for w in wanted)]
            if picked:
                self.lastPickSource = origin
                return picked
        if self.aiEnabled():
            self.lastPickSource = "ai"
            picked = self.aiAnswer(question, "multi", options)
            return [options.index(p) for p in picked] if picked else []
        self.lastPickSource = "default"
        matches = [i for i, o in enumerate(options) if self.optionMatchesSkills(o)]
        if len(matches) > 1:
            longest = max(len(self.optionTokens(options[i])) for i in matches)
            matches = [i for i in matches if not ("only" in lowered[i] and len(self.optionTokens(options[i])) < longest)]
        if matches:
            return matches
        for fallback in ["none", "neither", "n/a"]:
            for i, o in enumerate(lowered):
                if o.startswith(fallback):
                    return [i]
        return []

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
                if picks:
                    self.answeredQuestions.append(f"{question[:90]} -> {', '.join(options[i] for i in picks)} [{getattr(self, 'lastPickSource', 'default')}]")
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
                if self.aiEnabled() and self.driver.execute_script("return !!arguments[0].closest('[role=radiogroup], [role=group], fieldset')", radios[0]):
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
                labels = [self.labelFor(r) or (r.get_attribute("value") or "") for r in radios]
                if target is None and (self.isEeo(question, labels) or self.unreadableOptions(labels)):
                    continue
                if target is None and (self.aiEnabled() or self.manualEntry(question)):
                    index = self.pickSingle(question, labels)
                    if index is None:
                        continue
                    target = radios[index]
                if target is None and config.defaultRadioOption and len(radios) >= config.defaultRadioOption:
                    target = radios[config.defaultRadioOption - 1]
                if target is not None:
                    self.click(target)
            except Exception:
                continue

    submitTexts = ["submit application", "submit", "send application"]
    nextTexts = ["next", "continue", "next step", "review", "review application", "save and continue", "save & continue"]
    skipTexts = ["skip for now", "skip", "no thanks", "no, thanks", "maybe later", "not now"]
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
            if not getattr(self, "flaggedQuestions", []) and getattr(config, "autoLearnAnswers", True) and self.learnAnswers(self.unansweredQuestions):
                result = self.runApplicationSteps(maxSteps)
                self.unansweredQuestions = self.collectUnanswered() if result == "failed" else []
        return result

    def learnedAnswer(self, question: str) -> Optional[str]:
        q = question.lower()
        fmt = {"company": self.currentCompany or "your company", "title": self.currentTitle or "this role"}
        if re.search(r"\b(who|which (person|people|teammate|employee|colleague|team member))\b.*\b(know|refer|works?|employee)|name of .*(referr|employee|contact)|know anyone", q):
            return "No one"
        if re.search(r"if (so|yes)\b|if applicable", q):
            return "N/A"
        if re.search(r"linkedin", q):
            return self.answers["inputField"].get("Linkedin")
        if re.search(r"\burl\b|link|portfolio|github|website", q):
            return self.answers["inputField"].get("Website")
        if re.search(r"salary|compensation|\bpay\b|rate", q):
            return self.payNumber(q)
        if re.search(r"how many|\byears?\b|number of", q):
            return str(config.yearsOfExperience)
        return config.genericAnswer.format(**fmt)

    def learnAnswers(self, questions: List[str]) -> bool:
        if yaml is None or not questions or self.aiEnabled():
            return False
        learned = {}
        for raw in questions:
            if re.search(r"\[(not selected|invalid)", raw):
                continue
            question = re.sub(r"\s*\[[^\]]*\]\s*$", "", raw).strip().lower()
            if len(question) < 12 or question == "form error" or re.match(r"(search|select|type|choose)\b.*\b(option|search|select)\b", question):
                continue
            if re.search(r"^(if other|please (specify|provide|explain))|gpa|grade point|birth|social security|\bssn\b|" + self.EEO_PATTERN, question):
                continue
            if self.matchAnswer("inputField", question):
                continue
            if question in self.flaggedQuestionKeys():
                continue
            answer = self.learnedAnswer(question)
            if answer:
                learned[question] = answer
        if not learned:
            return False
        try:
            if not self.appendYamlEntries("inputField", {q: {"answer": a, "needs_review": True} for q, a in learned.items()}):
                return False
            os.makedirs("data", exist_ok=True)
            with open(os.path.join("data", f"Learned Answers - {time.strftime('%Y%m%d')}.txt"), "a", encoding="utf-8") as f:
                for question, answer in learned.items():
                    f.write(f"{time.strftime('%H:%M:%S')} | {self.siteName} | {self.currentCompany} | {question} -> {answer}\n")
        except Exception as e:
            utils.prRed("❌ Could not save learned answers: " + str(e)[:80])
            return False
        for question, answer in learned.items():
            utils.prGreen(f"📝 Learned answer: {question[:80]} -> {answer[:60]}")
        self.refreshAnswers()
        return True

    def runApplicationSteps(self, maxSteps: int = 10) -> str:
        for _ in range(maxSteps):
            self.pause(2, 4)
            if any(t in self.pageText() for t in self.successTexts):
                return "applied"
            self.fillForm()
            if getattr(self, "flaggedQuestions", []):
                return "failed"
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
                skipButton = self.findClickable(self.skipTexts, exact=True, scope=scope)
                if skipButton is None:
                    return "failed"
                self.click(skipButton)
                self.pause(2, 3)
                continue
            before = self.pageText()
            self.click(nextButton)
            self.pause(2, 3)
            if self.pageText() == before:
                try:
                    self.driver.execute_script("arguments[0].click();", nextButton)
                except Exception:
                    pass
                self.pause(3, 4)
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

    DESCRIPTION_SELECTORS = ['[data-testid="jobDescriptionHtml"]', '#jobDescription', '#jobDescriptionText',
                             '.jobsearch-JobComponent-description', '.job_description', '[class*="jobDescription"]',
                             '[class*="job_description"]', '[class*="job-description"]', '[data-testid*="description"]']

    def jobDescription(self) -> str:
        try:
            text = self.driver.execute_script("""
for (const s of arguments[0]) {
  const e = document.querySelector(s);
  if (e && (e.innerText || '').trim().length > 100) return e.innerText;
}
return '';
""", self.DESCRIPTION_SELECTORS) or ""
        except Exception:
            text = ""
        return text or self.pageText()

    def techBlacklisted(self, title: str) -> str:
        if not getattr(config, "techBlacklist", True):
            return ""
        result = techblacklist.check(title, self.jobDescription())
        if result["decision"] == "REJECT":
            return result["reason"]
        if result["decision"] == "FLAG":
            self.displayWriteResults("      🚩 Tech flag: " + json.dumps(result, ensure_ascii=False))
        return ""

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
        flagged = [f"{q} [AI flagged: {reason}]" for q, reason in getattr(self, "flaggedQuestions", [])]
        listed = {self.cleanQuestion(re.sub(r"\s*\[[^\]]*\]\s*$", "", q)).lower() for q in questions}
        flagged = [f for f in flagged if self.cleanQuestion(re.sub(r"\s*\[[^\]]*\]\s*$", "", f)).lower() not in listed]
        if failed and "manual answers" in result:
            self.logQuestionList((questions + flagged) or [self.NO_QUESTION], url)
        elif flagged:
            self.logQuestionList(flagged, url)
        self.unansweredQuestions = []
        self.flaggedQuestions = []

    NO_QUESTION = "(no question detected - apply form did not open or has no Next/Submit button)"
    questionListHeader = ["Question", "Times", "Last Seen", "Site", "Company", "Job Title", "Link", "Answer In additionalQuestions.yaml", "Status"]

    def savedAnswerFor(self, question: str) -> str:
        if question == self.NO_QUESTION:
            return ""
        q = question.lower()
        for section in ("inputField", "radio", "dropdown", "checkbox"):
            answer = self.matchAnswer(section, q)
            if answer:
                return answer
        for key, value in config.eeoAnswers.items():
            if value and re.search(key, q):
                return f"{value} (config.py eeoAnswers)"
        return ""

    def logQuestionList(self, questions: List[str], url: str) -> None:
        try:
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", "Manual Answer Questions.csv")
            textPath = os.path.join("data", "Manual Answer Questions.txt")
            seen = set()
            counts: Dict[str, int] = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    for r in list(csv.reader(f))[1:]:
                        if r:
                            seen.add((r[0].strip().lower(), r[6].strip() if len(r) > 6 else ""))
                            if len(r) > 1 and r[1].isdigit():
                                counts[r[0]] = max(counts.get(r[0], 0), int(r[1]))
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self.refreshAnswers()
            rows = []
            for raw in questions:
                question = re.sub(r"\s*\[[^\]]*\]\s*$", "", raw).strip() or raw
                key = (question.lower(), url.strip())
                if key in seen:
                    continue
                seen.add(key)
                flag = re.search(r"\[(AI flagged: [^\]]*)\]\s*$", raw)
                status = flag.group(1) if flag else ""
                counts[question] = counts.get(question, 0) + 1
                answer = "" if status else self.savedAnswerFor(question)
                rows.append([question, str(counts[question]), now, self.siteName, self.currentCompany, self.currentTitle, url, answer, status])
            if not rows:
                return
            newFile = not os.path.exists(path)
            prefix = "" if newFile or self.endsWithNewline(path) else "\r\n"
            with open(path, "a", encoding="utf-8-sig" if newFile else "utf-8", newline="") as f:
                f.write(prefix)
                writer = csv.writer(f)
                if newFile:
                    writer.writerow(self.questionListHeader)
                writer.writerows(rows)
            newText = not os.path.exists(textPath)
            prefix = "" if newText or self.endsWithNewline(textPath) else "\n"
            with open(textPath, "a", encoding="utf-8") as f:
                f.write(prefix)
                if newText:
                    f.write(f"Updated {now}\n\nNEED AN ANSWER\n")
                for r in rows:
                    reason = f" ({r[8]})" if r[8] else ""
                    f.write(f"  [{r[1]}x] {r[0]}{reason}\n        last: {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]}\n")
                    if r[7]:
                        f.write(f"        -> {r[7][:150]}\n")
        except Exception as e:
            utils.prRed("❌ Could not write question list: " + str(e)[:80])

    def endsWithNewline(self, path: str) -> bool:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return True
            f.seek(-1, os.SEEK_END)
            return f.read(1) in (b"\n", b"\r")

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

    def setupAi(self) -> None:
        self.assistant = None
        client = LLMClient.fromEnv()
        if client is None:
            utils.prYellow("⚠️ AI answers disabled: set LLM_URL and LLM_MODEL in .env")
            return
        problem = client.check()
        if problem:
            utils.prRed(f"❌ AI answers disabled: {problem}")
            return
        self.assistant = AnswerAssistant(client)
        utils.prGreen(f"✅ AI answers enabled with {client.model} at {client.url}")

    def aiEnabled(self) -> bool:
        return getattr(self, "assistant", None) is not None

    def flaggedQuestionKeys(self) -> set:
        return {q.strip().lower() for q, _ in getattr(self, "flaggedQuestions", [])}

    def aiAnswer(self, question: str, kind: str, options: Optional[List[str]] = None):
        self.lastAiFlagged = False
        question = self.cleanQuestion(question)
        if not self.aiEnabled() or not question:
            return None
        value, reason, fresh = self.assistant.answer(question, kind, options, self.currentTitle, self.currentCompany)
        if value is None and "could not connect" in reason:
            problem = self.assistant.client.check()
            if problem:
                self.assistant = None
                utils.prRed(f"❌ AI answers disabled for the rest of this run: {problem}")
                self.displayWriteResults(f"      🚩 AI answers disabled: {problem}")
        if value is None:
            self.lastAiFlagged = True
            if not hasattr(self, "flaggedQuestions"):
                self.flaggedQuestions = []
            if question.lower() not in self.flaggedQuestionKeys():
                self.flaggedQuestions.append((question, reason))
            if fresh:
                self.displayWriteResults(f"      🚩 Needs manual answer (AI flagged: {reason}): {question[:120]}")
            return None
        if fresh:
            shown = ", ".join(value) if isinstance(value, list) else str(value)
            self.displayWriteResults(f"      🤖 AI answered: {question[:120]} -> {shown[:150]}")
            section = {"choice": "radio", "multi": "checkbox"}.get(kind, "inputField")
            key = re.sub(r"\s*(\*|\(required\)|required)\s*$", "", question.strip(), flags=re.I).strip().lower()[:200]
            if key and not self.yamlLookup([section], key) and self.appendYamlEntries(section, {key: {"answer": shown, "needs_review": True}}):
                self.displayWriteResults(f"      📝 Saved AI answer to additionalQuestions.yaml ({section}, needs_review): {key[:100]}")
        return value

    def appendYamlEntries(self, section: str, entries: Dict[str, Dict]) -> bool:
        path = "additionalQuestions.yaml"
        if not entries or not os.path.exists(path):
            return False
        try:
            if not os.path.exists(path + ".bak"):
                shutil.copy2(path, path + ".bak")
            with open(path, "r", encoding="utf-8") as f:
                original = f.read()
            lines = original.split("\n")
            block = []
            for key, fields in entries.items():
                block.append(f"  {json.dumps(key, ensure_ascii=False)}:")
                for name, value in fields.items():
                    block.append(f"    {name}: {json.dumps(value, ensure_ascii=False)}")
            start = next((i for i, line in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*$", line)), None)
            if start is None:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += [f"{section}:"] + block + [""]
            else:
                end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i])), len(lines))
                while end > start + 1 and not lines[end - 1].strip():
                    end -= 1
                lines[end:end] = block
            updated = "\n".join(lines)
            yaml.safe_load(updated)
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
            self.answersMtime = -1
            self.refreshAnswers()
            return True
        except Exception as e:
            utils.prRed("❌ Could not append to additionalQuestions.yaml: " + str(e)[:100])
            return False

    def start(self) -> None:
        startTime = time.time()
        utils.printInfoMes(self.siteName)
        self.setupAi()
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
