import json
import re
import sys
from typing import Dict, List, Optional, Tuple

I = re.I

TECH_CONTEXT = r"python|\bjava\b|rust|c\+\+|\bnode|typescript|javascript|kubernetes|docker|microservice|back-?end|programming|language|developer|engineer|software|\bapi|grpc|\baws\b|cloud|framework|stack|\bcod(e|ing)\b|scripting"

TERMS: List[Tuple[str, List[Tuple[str, int, Optional[str]]]]] = [
    ("Go", [(r"\bgolang\b", I, None),
            (r"(?<![\w-])Go(?![\w-])(?!\s+(to|live|ahead|beyond|above|over|back|through|on|out|for|with|into|get|the|a|an)\b)", 0, TECH_CONTEXT)]),
    ("Ruby/Rails", [(r"\bruby\s+on\s+rails\b|\bror\b", I, None),
                    (r"\bRails\b", 0, r"ruby|programming|developer|engineer|back-?end|framework|\bapi|stack|activerecord"),
                    (r"\bruby\b", I, r"rails|python|php|perl|programming|language|developer|engineer|back-?end|\bgems?\b|rspec|stack|scripting")]),
    ("Scala", [(r"\bscala\b", I, None)]),
    ("Kotlin", [(r"\bkotlin\b", I, None)]),
    ("Swift", [(r"\bSwiftUI\b", I, None),
               (r"\bSwift\b", 0, r"\bios\b|xcode|swiftui|objective-?c|apple|mobile|cocoa|programming|language|developer|engineer|kotlin|uikit")]),
    ("iOS", [(r"\bios\b", I, None)]),
    ("Android", [(r"\bandroid\b", I, None)]),
    ("Salesforce", [(r"\bsalesforce\b", I, None), (r"\bsfdc\b", I, None),
                    (r"\bApex\b", 0, r"salesforce|sfdc|trigger|lightning|visualforce|soql|\bclass(es)?\b|code|developer")]),
    ("SAP/ABAP", [(r"\bSAP\b", 0, None), (r"\babap\b", I, None), (r"\bs/4\s?hana\b", I, None)]),
    ("Mainframe", [(r"\bmainframes?\b", I, None), (r"\bz/os\b|\bjcl\b|\bcics\b", I, None)]),
    ("COBOL", [(r"\bcobol\b", I, None)]),
    ("Perl", [(r"\bperl\b", I, None)]),
]

OPTIONAL_CUE = r"nice[\s-]to[\s-]have|\bpreferred\b|\bpreferably\b|\ba plus\b|\bplus\b|\bbonus\b|\boptional\b|\bdesir(ed|able)\b|\bfamiliarity\b|\bexposure\b|\bideally\b|not required|\bhelpful\b|\bbeneficial\b|\bwelcome\b|\bany of\b|\bsuch as\b|\be\.g\."
REQUIRED_CUE = r"\brequired\b|\brequirements?\b|\bmust\b|\bmandatory\b|\bessential\b|\bstrong\b|\bsolid\b|\bproficien|\bexpert|\bhands[\s-]on\b|\bdeep\b|\bextensive\b|\d+\+?\s*(years|yrs)|\bminimum\b|\bprimary\b|\bcore\b|\bproven\b|\bexperience (with|in|developing|building)\b"
OPTIONAL_HEADING = r"nice[\s-]to[\s-]have|preferred|bonus|pluses|desired|optional|good to have"
REQUIRED_HEADING = r"requirements|required|qualifications|must[\s-]have|what you('ll)? (bring|need)|who you are|skills|experience|responsibilities|what we('re| are) looking for"


def findTerm(patterns: List[Tuple[str, int, Optional[str]]], text: str) -> List[re.Match]:
    found = []
    for pattern, flags, context in patterns:
        for m in re.finditer(pattern, text, flags):
            if context:
                window = text[max(0, m.start() - 80):m.end() + 80]
                if not re.search(context, window, I):
                    continue
            found.append(m)
    return found


def splitLines(text: str) -> List[str]:
    lines = []
    for line in re.split(r"[\r\n]+|(?<=[.;!?])\s+|\s*[•·▪●]\s*", text or ""):
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return lines


def lineLevel(line: str, section: str) -> str:
    if re.search(OPTIONAL_CUE, line, I):
        return "MENTIONED"
    if re.search(REQUIRED_CUE, line, I):
        return "REQUIRED"
    return "REQUIRED" if section == "required" else "MENTIONED"


def check(title: str = "", description: str = "") -> Dict:
    matches: Dict[str, Dict[str, str]] = {}
    for term, patterns in TERMS:
        if title and findTerm(patterns, title):
            matches[term] = {"term": term, "level": "REQUIRED", "evidence": f"job title: {title.strip()[:160]}"}
    section = ""
    for line in splitLines(description):
        if len(line) <= 60 and re.search(OPTIONAL_HEADING, line, I):
            section = "optional"
        elif len(line) <= 60 and re.search(REQUIRED_HEADING, line, I):
            section = "required"
        for term, patterns in TERMS:
            if matches.get(term, {}).get("level") == "REQUIRED" or not findTerm(patterns, line):
                continue
            level = lineLevel(line, section)
            if term not in matches or level == "REQUIRED":
                matches[term] = {"term": term, "level": level, "evidence": line[:160]}
    found = list(matches.values())
    required = [m["term"] for m in found if m["level"] == "REQUIRED"]
    mentioned = [m["term"] for m in found if m["level"] == "MENTIONED"]
    if required:
        decision, reason = "REJECT", "required blacklisted tech: " + ", ".join(required)
    elif mentioned:
        decision, reason = "FLAG", "mentioned blacklisted tech: " + ", ".join(mentioned)
    else:
        decision, reason = "PASS", "no blacklisted tech found"
    return {"decision": decision, "matches": found, "reason": reason}


if __name__ == "__main__":
    print(json.dumps(check("", sys.stdin.read()), ensure_ascii=False))
